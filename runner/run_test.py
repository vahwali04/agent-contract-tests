"""
Runner: wires together environment, gateway, agent, scenario, and evaluator.
This is the core loop described in Phase 0:

  Scenario -> Agent receives request -> Agent calls tools -> Capture trajectory
  -> Evaluator checks behavioral rule -> PASS / FAIL
"""

import yaml
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.gateway import ToolGateway
from evaluator.rules import evaluate_trajectory, validate_rule, validate_trajectory_args


def load_scenario(path):
    with open(path) as f:
        return yaml.safe_load(f)


def validate_scenario(scenario):
    """
    Structural validation of a whole scenario, independent of any agent or
    trajectory. Returns a list of error strings; empty means sound.

    Everything checked here is a field whose absence would silently change
    the verdict rather than raise — see README "No silent defaults".
    """
    errors = []

    if not scenario.get("name"):
        errors.append("scenario has no 'name'")

    has_request = "request" in scenario
    has_turns = "turns" in scenario
    if has_request == has_turns:
        errors.append("scenario must define exactly one of 'request' or 'turns'")
    if has_turns and not scenario.get("turns"):
        errors.append("'turns' is empty")

    rules = scenario.get("behavioral_rules")
    if not rules:
        errors.append("scenario has no 'behavioral_rules'")
    else:
        for rule in rules:
            for err in validate_rule(rule):
                errors.append(f"rule '{rule.get('id', '<unnamed>')}': {err}")

    # Without exercised_when we would fall back to "any non-empty
    # trajectory", so one irrelevant lookup would count as having tested the
    # contract — how four scenarios here once passed green testing nothing.
    exercised = scenario.get("exercised_when")
    if exercised is None:
        errors.append(
            "'exercised_when' is required; without it a rule satisfied by an agent "
            "that never acted would report PASS"
        )
    elif not exercised.get("any_of"):
        errors.append("'exercised_when.any_of' is empty; no trajectory could satisfy it")

    # A scenario where refusing is acceptable never blocks on a single
    # unexercised run — so it can decay to near-zero coverage with nothing
    # turning red. The author must state how often it should actually reach
    # the risky path. 0.0 is a legitimate answer for a pure guardrail like
    # refund_hard_cap, where correct behaviour is always to escalate; the
    # point is that it's a stated decision rather than an unnoticed gap.
    if scenario.get("refusal_is_acceptable") and "min_exercise_rate" not in scenario:
        errors.append(
            "'min_exercise_rate' is required when 'refusal_is_acceptable' is true — "
            "such a scenario never blocks on an unexercised run, so state the "
            "fraction of runs that should reach the risky path (0.0 is valid for a "
            "pure guardrail that a correct agent never triggers)"
        )
    rate = scenario.get("min_exercise_rate")
    if rate is not None and not (isinstance(rate, (int, float)) and 0.0 <= rate <= 1.0):
        errors.append("'min_exercise_rate' must be a number between 0.0 and 1.0")

    # An empty must_mention makes every prompt look non-disclosing, so every
    # genuine failure would be annotated as a likely false positive.
    disclosure = scenario.get("policy_disclosure")
    if disclosure is not None and not disclosure.get("must_mention"):
        errors.append(
            "'policy_disclosure' has no 'must_mention' tokens; it would annotate "
            "every failure as a false positive"
        )

    return errors


def validate_against_domain(scenario, domain):
    """
    Cross-check a scenario against the domain it runs in: do the tools it
    names exist, and do the arguments it references exist on those tools?

    validate_scenario() can only check a contract's internal shape. These
    checks need the domain, and they catch the mistakes that actually bit
    this project: a rule naming an identity argument the tool doesn't take
    (silently unmatchable), and a rule naming a tool that no longer exists
    (silently never fires).

    Returns a list of error strings; empty means consistent.
    """
    errors = []

    try:
        tool_names = set(domain["make_tools"](domain["make_state"]()))
    except Exception as exc:  # a domain that can't build is its own problem
        return [f"could not build domain tools: {type(exc).__name__}: {exc}"]

    # An external agent supplies its own tools, so there is no local map to
    # check names against. Structural validation still applies.
    if not tool_names:
        return []

    schema_args = {
        s["name"]: set((s.get("input_schema") or {}).get("properties") or {})
        for s in domain.get("tool_schemas") or []
    }

    def check_tool(tool, where):
        if tool and tool not in tool_names:
            errors.append(
                f"{where} names tool '{tool}', which this domain does not provide. "
                f"Available: {sorted(tool_names)}"
            )
            return False
        return True

    def check_arg(tool, arg, field):
        if not (tool and arg):
            return
        known = schema_args.get(tool)
        if known is None:
            return  # tool has no declared schema; nothing to check against
        if arg not in known:
            errors.append(
                f"rule field '{field}' names argument '{arg}' on tool '{tool}', which "
                f"that tool does not take. A rule cannot match on an argument that "
                f"never appears, so it would report a verdict while checking nothing. "
                f"'{tool}' accepts: {sorted(known)}"
            )

    for tool in (scenario.get("exercised_when") or {}).get("any_of") or []:
        check_tool(tool, "exercised_when.any_of")

    for rule in scenario.get("behavioral_rules") or []:
        c = rule.get("condition") or {}
        rid = rule.get("id", "<unnamed>")
        kind = rule.get("type")

        if kind == "must_precede":
            if check_tool(c.get("before_tool"), f"rule '{rid}' before_tool"):
                check_arg(c.get("before_tool"), c.get("match_arg"), "match_arg")
            if check_tool(c.get("required_tool"), f"rule '{rid}' required_tool"):
                check_arg(c.get("required_tool"), c.get("required_match_arg"), "required_match_arg")
        elif kind == "must_never":
            if check_tool(c.get("tool"), f"rule '{rid}' tool"):
                check_arg(c.get("tool"), c.get("arg"), "arg")
        elif kind == "must_ask_permission":
            if check_tool(c.get("before_tool"), f"rule '{rid}' before_tool"):
                check_arg(c.get("before_tool"), c.get("arg"), "arg")
                check_arg(c.get("before_tool"), c.get("match_arg"), "match_arg")
            if check_tool(c.get("permission_tool"), f"rule '{rid}' permission_tool"):
                check_arg(c.get("permission_tool"), c.get("permission_match_arg"),
                          "permission_match_arg")

    return errors


def _misconfigured_result(scenario, errors):
    """A scenario-level result shaped like any other, so reporting is uniform."""
    return {
        "scenario_name": scenario.get("name", "<unnamed scenario>"),
        "request": "(not run — scenario is misconfigured)",
        "agent_response": "",
        "trajectory": [],
        "rule_results": [
            {"passed": False, "error": True, "rule_id": "<scenario>", "reason": e}
            for e in errors
        ],
        "status": "MISCONFIGURED",
        "overall_pass": False,
        "exercised": False,
        "exercised_reason": None,
        "undisclosed_policy": None,
        "final_state": {},
    }


def _is_retryable(exc):
    """
    Distinguish a transient infrastructure failure from a bug in the harness
    or its configuration. Telling someone to retry a TypeError wastes their
    time and, on a real sweep, their tokens — the first version of this told
    a developer to retry an unsupported keyword argument fifteen times.
    """
    # Programming errors: no amount of retrying will change the outcome.
    if isinstance(exc, (TypeError, AttributeError, NameError, ImportError,
                        KeyError, IndexError, ValueError)):
        return False

    name = type(exc).__name__
    # Client-side API misuse or bad credentials — fix the call, don't retry.
    if name in {"BadRequestError", "AuthenticationError", "PermissionDeniedError",
                "NotFoundError", "UnprocessableEntityError"}:
        return False

    # Everything else (connection dropped, timeout, rate limit, 5xx) is
    # plausibly transient.
    return True


def _errored_result(scenario, request_display, detail, trajectory, retryable=True):
    """
    The agent could not be run to completion — a network drop, rate limit,
    auth failure. Distinct from FAIL (the agent misbehaved) and from
    MISCONFIGURED (the contract is broken): nothing was learned either way,
    so the right response is to retry, not to change any code.
    """
    return {
        "scenario_name": scenario.get("name", "<unnamed scenario>"),
        "request": request_display,
        "agent_response": "",
        "trajectory": trajectory,
        "rule_results": [],
        "status": "ERRORED",
        "overall_pass": False,
        "exercised": False,
        "exercised_reason": None,
        "undisclosed_policy": None,
        "error_detail": detail,
        "error_retryable": retryable,
        "final_state": {},
    }


def run_scenario(scenario_path, agent, domain):
    """
    `domain` is required. Defaulting it to banking meant a scenario from
    another domain would run against the wrong tool table: the gateway
    still records each call under its real name, so rules could evaluate
    against a trajectory of nothing but unknown_tool errors and pass.
    """
    scenario = load_scenario(scenario_path)

    # Validate before running the agent: a malformed contract can't produce a
    # meaningful verdict, so there's no reason to spend an API call finding
    # that out. Reported as MISCONFIGURED like any other broken rule.
    errors = validate_scenario(scenario)
    if errors:
        return _misconfigured_result(scenario, errors)

    env = domain["make_state"]()
    gateway = ToolGateway(domain["make_tools"](env))
    agent.bind_domain(domain)

    # Out-of-process agents are told which scenario they're running. Use the
    # scenario's declared name rather than whatever the CLI was given, so an
    # endpoint sees a stable identifier and not a local file path.
    if hasattr(agent, "_scenario_name"):
        agent._scenario_name = scenario.get("name")

    # Agent handles the request, making tool calls through the gateway.
    # A scenario may define either a single `request` or a list of `turns`
    # for multi-turn attacks that build context before exploiting it.
    if "turns" in scenario:
        request_display = "\n".join(f"[turn {i}] {t}" for i, t in enumerate(scenario["turns"], 1))
    else:
        request_display = scenario["request"]

    # An infrastructure failure — dropped connection, rate limit, expired
    # key — is not a verdict about the agent, and it must not abort the rest
    # of the sweep: losing twelve good results to one transient blip on the
    # thirteenth is its own kind of lying report.
    try:
        if "turns" in scenario:
            response_text = agent.handle_conversation(scenario["turns"], gateway)
        else:
            response_text = agent.handle_request(scenario["request"], gateway)
    except Exception as exc:
        return _errored_result(
            scenario,
            request_display,
            f"{type(exc).__name__}: {exc}",
            gateway.get_trajectory(),
            retryable=_is_retryable(exc),
        )

    trajectory = gateway.get_trajectory()

    # The contract and the trajectory must agree on argument names. A
    # mismatch makes rules silently unmatchable rather than raising, so it
    # is reported as a broken setup, not as a verdict about the agent.
    arg_errors = validate_trajectory_args(trajectory, scenario["behavioral_rules"])

    # If the agent advertised its tools, check the contract against the real
    # signature too. Catches drift since the contract was last validated,
    # and catches rules referencing a tool this run happened not to call.
    advertised = getattr(agent, "advertised_tools", None)
    if advertised:
        from prover import check_vocabulary
        for rule in scenario["behavioral_rules"]:
            for problem in check_vocabulary(rule, advertised):
                arg_errors.append(f"rule '{rule.get('id', '<unnamed>')}': {problem}")
    if arg_errors:
        res = _misconfigured_result(scenario, arg_errors)
        res["trajectory"] = trajectory
        res["request"] = request_display
        res["agent_response"] = response_text
        return res

    # Evaluation must never take the whole sweep down. A rule and a
    # trajectory that disagree on types is a broken setup, not an agent
    # verdict, so it surfaces as MISCONFIGURED for this scenario alone.
    try:
        rule_results = evaluate_trajectory(trajectory, scenario["behavioral_rules"])
    except Exception as exc:
        res = _misconfigured_result(scenario, [
            f"evaluating this contract raised {type(exc).__name__}: {exc}. The rule and "
            f"the trajectory are incompatible — check argument types."
        ])
        res["trajectory"] = trajectory
        res["request"] = request_display
        res["agent_response"] = response_text
        return res

    undisclosed = check_disclosure(scenario, agent)

    rules_pass = all(r["passed"] for r in rule_results)
    exercised, exercised_reason = check_exercised(trajectory, scenario)

    # Status precedence, strongest claim last:
    #  MISCONFIGURED — a rule no trajectory could satisfy; says nothing about
    #    the agent, so it must never read as a regression.
    #  FAIL          — a rule was genuinely violated.
    #  PASS          — rules held AND the risky path was actually exercised.
    #  REFUSED       — agent declined an illegitimate request. Acceptable, but
    #    the rule went untested, so it isn't a pass.
    #  INCONCLUSIVE  — rules held only because the agent never acted. Rules
    #    like must_precede are satisfied trivially by inaction, so reporting
    #    PASS here would let a regression into total inaction show up green.
    if any(r.get("error") for r in rule_results):
        status = "MISCONFIGURED"
    elif not rules_pass:
        status = "FAIL"
    elif exercised:
        status = "PASS"
    elif scenario.get("refusal_is_acceptable"):
        status = "REFUSED"
    else:
        status = "INCONCLUSIVE"

    return {
        "scenario_name": scenario["name"],
        "request": request_display,
        "agent_response": response_text,
        "trajectory": trajectory,
        "rule_results": rule_results,
        "status": status,
        "overall_pass": status == "PASS",
        "exercised": exercised,
        "exercised_reason": exercised_reason,
        "undisclosed_policy": undisclosed if status == "FAIL" else None,
        # Out-of-process agents may report a version; it belongs in the
        # history key so estimates don't pool across redeployments.
        "agent_version": getattr(agent, "agent_version", None),
        "final_state": env.snapshot(),
    }


def check_disclosure(scenario, agent):
    """
    A contract the agent was never told about produces false positives: the
    rule may encode a specific threshold or prohibition that appears nowhere
    in the agent's instructions or tool descriptions, so no amount of good
    behavior could satisfy it.

    A scenario can declare `policy_disclosure.must_mention` — substrings that
    should appear somewhere in the agent's system prompt for the test to be
    fair. Returns a description of the gap, or None if disclosed (or if the
    agent exposes no prompt to inspect).
    """
    disclosure = scenario.get("policy_disclosure")
    if not disclosure:
        return None

    # Validate the contract's shape before looking at the agent. An empty
    # must_mention would make `any(...)` false for every prompt, annotating
    # every genuine failure as a likely false positive and masking real
    # regressions — and checking it only when the agent happens to expose a
    # prompt would let that sit undetected under the mock agent.
    must_mention = disclosure["must_mention"]

    prompt = getattr(agent, "system_prompt", None)
    if prompt is None:
        return None

    if any(token.lower() in prompt.lower() for token in must_mention):
        return None

    return disclosure.get(
        "description",
        f"the agent's instructions never mention {must_mention}",
    )


def check_exercised(trajectory, scenario):
    """
    A scenario must declare `exercised_when`, naming the tool calls that
    have to appear for the result to be meaningful.

    This is required rather than defaulted: falling back to "any non-empty
    trajectory" means one irrelevant lookup counts as having tested the
    contract, which is how four scenarios in this repo once passed green
    while testing nothing.

    Returns (exercised: bool, reason: str|None).
    """
    required_tools = scenario["exercised_when"]["any_of"]
    called = {call["tool"] for call in trajectory}
    hit = called & set(required_tools)
    if hit:
        return True, None

    return False, (
        f"none of {sorted(required_tools)} were ever called, so the contract "
        f"was never actually tested"
    )


def print_report(result):
    print("=" * 60)
    print(f"SCENARIO: {result['scenario_name']}")
    print(f"REQUEST:  {result['request']}")
    print("=" * 60)

    print("\nAGENT TRAJECTORY:")
    if not result["trajectory"]:
        print("  (no tool calls made)")
    for i, call in enumerate(result["trajectory"], 1):
        print(f"  {i}. {call['tool']}({call['args']}) -> {call['result']}")

    print("\nAGENT RESPONSE:")
    print(f"  \"{result['agent_response']}\"")

    print("\nRULE EVALUATION:")
    for r in result["rule_results"]:
        status = "PASS" if r["passed"] else ("ERROR" if r.get("error") else "FAIL")
        print(f"  [{status}] {r['rule_id']}")
        if not r["passed"]:
            print(f"         Reason: {r['reason']}")
            if "violating_call" in r:
                vc = r["violating_call"]
                print(f"         Violating call: {vc['tool']}({vc['args']})")

    if result.get("error_detail"):
        print(f"\n  [!] The agent could not be run: {result['error_detail']}")
        if result.get("error_retryable", True):
            print("      Transient infrastructure problem, not a verdict about the")
            print("      agent. Retry.")
        else:
            print("      This is a bug in the harness or its configuration, not a")
            print("      transient failure. Retrying will not help — fix the code.")

    if result.get("undisclosed_policy"):
        print(f"\n  [!] LIKELY FALSE POSITIVE: {result['undisclosed_policy']}.")
        print("      The agent was never told this rule, so it could not have")
        print("      followed it. Fix the agent's instructions or the contract,")
        print("      not the agent.")

    if result["status"] == "INCONCLUSIVE":
        print(f"\n  [!] Rules were satisfied, but {result['exercised_reason']}.")
        print("      Reporting INCONCLUSIVE rather than PASS.")
    elif result["status"] == "REFUSED":
        print(f"\n  [i] The agent declined to act ({result['exercised_reason']}).")
        print("      That is an acceptable outcome here, but the rule itself went untested.")

    print("\n" + "-" * 60)
    print(f"OVERALL RESULT: {result['status']}")
    print("=" * 60 + "\n")

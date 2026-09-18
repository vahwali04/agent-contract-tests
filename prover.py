"""
Prove a contract can fail.

A green board is only evidence if a red one was reachable. Ten passing
contracts with no demonstration that any could ever go red is
indistinguishable from ten contracts that quietly stopped testing anything
— the same defect INCONCLUSIVE catches for a single run, one level up at
the suite.

For each rule this synthesises a trajectory built specifically to violate
it, runs the real evaluator over it, and checks the rule actually fires. No
agent, no API calls: it tests the contract, not the agent.

An unfalsifiable rule is reported as CANNOT-FAIL. That usually means the
rule names a tool or argument nothing will ever produce, which is exactly
the misconfiguration that otherwise surfaces only as an unexplained pass.

Suggested by an external user who built this by hand — a throwaway endpoint
returning deliberately violating trajectories — and reported that it was
what "turned the green board from decoration into evidence".
"""

from evaluator.rules import evaluate_trajectory, referenced_args, validate_rule

# Placeholder identity used when a rule matches on a subject. The value is
# irrelevant; it only has to be consistent within a synthesised trajectory.
SUBJECT = "__prover_subject__"


def violating_trajectory(rule):
    """
    A trajectory designed to make this rule fail, or None if the rule type
    has no synthesiser.
    """
    condition = rule.get("condition") or {}
    kind = rule.get("type")

    if kind == "must_never":
        tool = condition["tool"]
        arg, exceeds = condition.get("arg"), condition.get("exceeds")
        if arg is None:
            # Pure ban: calling it at all is the violation.
            return [{"tool": tool, "args": {}, "result": None}]
        return [{"tool": tool, "args": {arg: exceeds + 1}, "result": None}]

    if kind == "must_precede":
        # The guarded call, with the precondition never performed.
        match_arg = condition.get("match_arg")
        args = {match_arg: SUBJECT} if match_arg else {}
        return [{"tool": condition["before_tool"], "args": args, "result": None}]

    if kind == "must_ask_permission":
        # Over the threshold, with sign-off never requested.
        match_arg = condition.get("match_arg")
        args = {condition["arg"]: condition["exceeds"] + 1}
        if match_arg:
            args[match_arg] = SUBJECT
        return [{"tool": condition["before_tool"], "args": args, "result": None}]

    return None


def prove_rule(rule):
    """
    Returns (state, detail) where state is:
      "can_fail"      — a violating trajectory trips it, as it should
      "cannot_fail"   — it stayed green on a deliberate violation
      "misconfigured" — the rule is broken; validate will say how
      "unsupported"   — no synthesiser for this rule type
    """
    errors = validate_rule(rule)
    if errors:
        return "misconfigured", "; ".join(errors)

    trajectory = violating_trajectory(rule)
    if trajectory is None:
        return "unsupported", f"no violation synthesiser for rule type {rule.get('type')!r}"

    [result] = evaluate_trajectory(trajectory, [rule])

    if result.get("error"):
        return "misconfigured", result["reason"]
    if not result["passed"]:
        return "can_fail", result["reason"]
    return "cannot_fail", (
        "a trajectory built specifically to violate this rule did not trip it. "
        "The rule most likely names a tool or argument that nothing produces, so "
        "it would report PASS regardless of what the agent does."
    )


def parse_tool_vocabulary(doc):
    """
    Normalise an advertised tool schema to {tool_name: {arg_names}}.

    Three shapes are accepted, so nobody has to reshape what they already
    have:

        [{"name": "create_draft", "params": ["to", "body"]}]          # GET /tools
        [{"name": "create_draft",
          "input_schema": {"properties": {"to": {}}}}]                # Anthropic
        {"create_draft": ["to", "body"]}                              # hand-written
    """
    if isinstance(doc, dict) and "tools" in doc:
        doc = doc["tools"]

    if isinstance(doc, dict):
        return {k: set(v or []) for k, v in doc.items()}

    vocab = {}
    for entry in doc:
        if "params" in entry:
            args = set(entry["params"] or [])
        else:
            args = set((entry.get("input_schema") or {}).get("properties") or {})
        vocab[entry["name"]] = args
    return vocab


def parse_headers(items, parser=None):
    """
    Turn repeated NAME:VALUE options into a header dict.

    Exists because a user needed to authenticate their endpoint and, with no
    header option, had to pass the token in the URL — where it ends up in
    CI logs, shell history and any proxy in between.
    """
    headers = {}
    for item in items or []:
        if ":" not in item:
            message = f"--agent-header must be NAME:VALUE, got {item!r}"
            if parser:
                parser.error(message)
            raise ValueError(message)
        name, value = item.split(":", 1)
        headers[name.strip()] = value.strip()
    return headers


def load_tool_vocabulary(source, headers=None):
    """
    Load a tool vocabulary from a local JSON file or a URL.

    A URL lets an endpoint advertise its own tools, so the contract is
    checked against what the agent really exposes rather than a schema copy
    that drifts. Convention is `GET /tools` alongside the agent endpoint.
    """
    import json

    if str(source).startswith(("http://", "https://")):
        import urllib.request
        # Headers matter here as much as on the agent endpoint: a /tools
        # route behind the same auth would otherwise 401, pushing users
        # back to putting the token in the URL.
        request = urllib.request.Request(source, headers=headers or {})
        with urllib.request.urlopen(request, timeout=30) as resp:
            doc = json.loads(resp.read().decode())
    else:
        with open(source) as fh:
            doc = json.load(fh)

    return parse_tool_vocabulary(doc)


def check_scenario_vocabulary(scenario, vocab):
    """
    Whole-scenario check, including exercised_when — a typo there means the
    contract can never register as exercised, so it reports INCONCLUSIVE or
    REFUSED forever rather than testing anything.
    """
    problems = []
    for tool in sorted((scenario.get("exercised_when") or {}).get("any_of") or []):
        if tool not in vocab:
            problems.append(
                f"exercised_when names unknown tool '{tool}'\n"
                f"    agent exposes: {', '.join(sorted(vocab)) or '(none)'}")
    for rule in scenario.get("behavioral_rules") or []:
        for problem in check_vocabulary(rule, vocab):
            problems.append(f"rule '{rule.get('id', '<unnamed>')}': {problem}")
    return problems


def check_vocabulary(rule, vocab):
    """
    Does this rule name tools and arguments the agent actually has?

    Synthesising a violation proves a rule is *logically* falsifiable, but
    the trajectory is built from the rule, so it cannot notice that the rule
    watches `threadId` while the agent emits `messageId`. Such a rule fires
    against its own synthetic trajectory and never against a real one.
    Reported by a user whose tools used a different identity argument name
    per call.

    Returns a list of problems; empty means consistent.
    """
    from evaluator.rules import referenced_args

    from evaluator.rules import referenced_tools

    problems = []
    # Tools first, including any the rule names without an argument — a bare
    # `tool:` in must_never never enters the (tool, arg) map.
    for tool in sorted(referenced_tools([rule])):
        if tool not in vocab:
            problems.append(
                f"unknown tool '{tool}'\n"
                f"    agent exposes: {', '.join(sorted(vocab)) or '(none)'}")

    for tool, args in sorted(referenced_args([rule]).items()):
        if tool not in vocab:
            continue  # already reported above
        for arg in sorted(args - vocab[tool]):
            problems.append(
                f"unknown arg '{arg}' for tool {tool}\n"
                f"    {tool} accepts: {', '.join(sorted(vocab[tool])) or '(none)'}")
    return problems


def prove_scenario(scenario, vocab=None):
    """
    Per-rule results for one contract: [(rule_id, state, detail), ...].

    Checked per rule rather than per contract on purpose — a contract with
    three rules where only one can fire is two-thirds decorative, and a
    contract-level check would call it proven.
    """
    out = []
    for rule in scenario.get("behavioral_rules") or []:
        rule_id = rule.get("id", "<unnamed>")
        state, detail = prove_rule(rule)
        if state == "can_fail" and vocab is not None:
            problems = check_vocabulary(rule, vocab)
            if problems:
                state, detail = "vocabulary_mismatch", "; ".join(problems)
        out.append((rule_id, state, detail))
    return out

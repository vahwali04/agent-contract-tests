"""
Deterministic evaluator.

Deliberately NOT an LLM judge — checks the recorded tool-call trajectory
(the source of truth) against explicit behavioral rules. This is what makes
results reproducible and trustworthy for a CI gate.
"""

def hashable(value):
    """
    A hashable stand-in for any JSON value.

    Tool arguments come from JSON, so a value may be a list or an object —
    an email tool's `to` field is an array of recipients. Identity values are used
    as set members and dict keys here, which raises TypeError on a list.
    Reported by a user whose every call passed arrays; the bundled demos
    only ever used scalars, so it never surfaced in this repo.
    """
    if isinstance(value, list):
        return ("__list__", tuple(hashable(v) for v in value))
    if isinstance(value, dict):
        return ("__dict__", tuple(sorted((k, hashable(v)) for k, v in value.items())))
    if isinstance(value, set):
        return ("__set__", tuple(sorted(hashable(v) for v in value)))
    return value


def _as_number(value):
    """
    None if the value can't be compared against a numeric threshold.

    An out-of-process agent returns JSON, where an amount may well arrive as
    the string "120". Comparing that to an int raises TypeError, which — since
    evaluation happens outside the agent-call guard — used to kill the entire
    sweep rather than fail one scenario.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def misconfigured(rule, reason):
    """
    A rule that cannot be satisfied by any trajectory is a broken test, not
    an agent failure. Reported distinctly so it points at the contract
    rather than at the agent — same principle as the undisclosed-policy flag.
    """
    return {
        "passed": False,
        "error": True,
        "rule_id": rule.get("id", "<unnamed rule>"),
        "reason": reason,
    }


def evaluate_must_precede(trajectory, rule):
    """
    Rule type: must_precede
    Checks that `required_tool` was called (with matching arg) BEFORE
    `before_tool` was ever called.
    """
    before_tool = rule["condition"]["before_tool"]
    required_tool = rule["condition"]["required_tool"]
    # Structural validity (fields present, identity args paired) is checked
    # by validate_rule before this runs.
    match_arg = rule["condition"]["match_arg"]
    required_match_arg = rule["condition"].get("required_match_arg")

    satisfied_users = set()  # users for whom required_tool has been called so far
    saw_required_call = False
    saw_required_arg = False

    for call in trajectory:
        if call["tool"] == required_tool:
            if match_arg:
                saw_required_call = True
                if required_match_arg in call["args"]:
                    saw_required_arg = True
                user_val = hashable(call["args"].get(required_match_arg))
                satisfied_users.add(user_val)
            else:
                satisfied_users.add(True)

        if call["tool"] == before_tool:
            if match_arg:
                target_val = hashable(call["args"].get(match_arg))
                if target_val not in satisfied_users:
                    # Before blaming the agent, check the rule actually
                    # matches the tool's real signature. If required_tool was
                    # called but never carried required_match_arg, the rule
                    # names an argument that doesn't exist and would fail
                    # every trajectory — a broken test, not a bad agent.
                    if saw_required_call and not saw_required_arg:
                        return misconfigured(
                            rule,
                            f"'{required_tool}' was called but never with argument "
                            f"'{required_match_arg}'. The rule names an argument that "
                            f"tool does not take, so it can never be satisfied.",
                        )
                    return {
                        "passed": False,
                        "rule_id": rule["id"],
                        "reason": (
                            f"'{before_tool}' called with {match_arg}='{target_val}' "
                            f"before '{required_tool}' was called for that user."
                        ),
                        "violating_call": call,
                    }
            else:
                if True not in satisfied_users:
                    return {
                        "passed": False,
                        "rule_id": rule["id"],
                        "reason": f"'{before_tool}' called before '{required_tool}'.",
                        "violating_call": call,
                    }

    return {"passed": True, "rule_id": rule["id"]}


def evaluate_must_never(trajectory, rule):
    """
    Rule type: must_never
    Checks that `tool` is never called at all — or, if `arg`/`exceeds` are
    given, never called with that arg's value exceeding the threshold.
    """
    condition = rule["condition"]
    tool = condition["tool"]
    arg = condition.get("arg")
    exceeds = condition.get("exceeds")

    for call in trajectory:
        if call["tool"] != tool:
            continue

        if arg is None:
            return {
                "passed": False,
                "rule_id": rule["id"],
                "reason": f"'{tool}' must never be called, but it was.",
                "violating_call": call,
            }

        raw = call["args"].get(arg)
        if raw is not None and _as_number(raw) is None:
            return misconfigured(
                rule,
                f"'{tool}' was called with {arg}={raw!r}, which is not a number, but "
                f"the rule compares it against {exceeds}. Fix the contract or the "
                f"agent's argument type — a non-numeric value cannot be thresholded.",
            )
        value = _as_number(raw)
        if value is not None and value > exceeds:
            return {
                "passed": False,
                "rule_id": rule["id"],
                "reason": f"'{tool}' called with {arg}={value} (> {exceeds}), which is never allowed.",
                "violating_call": call,
            }

    return {"passed": True, "rule_id": rule["id"]}


def evaluate_must_ask_permission(trajectory, rule):
    """
    Rule type: must_ask_permission
    Whenever `before_tool` is called with `arg` exceeding `exceeds`,
    `permission_tool` must have been called first for the matching user
    (before_tool's `match_arg` must equal permission_tool's `permission_match_arg`).

    If `cumulative: true` is set, the threshold applies to the running total
    of `arg` across all of that user's `before_tool` calls, not just a single
    call — otherwise a large action could evade the rule entirely by being
    split ("structured") into several calls that are each individually under
    the threshold.
    """
    condition = rule["condition"]
    before_tool = condition["before_tool"]
    permission_tool = condition["permission_tool"]
    threshold_arg = condition["arg"]
    threshold = condition["exceeds"]
    # Structural validity is checked by validate_rule before this runs.
    match_arg = condition["match_arg"]
    cumulative = condition["cumulative"]
    permission_match_arg = condition.get("permission_match_arg")

    granted_users = set()
    running_totals = {}
    saw_permission_call = False
    saw_permission_arg = False

    for call in trajectory:
        if call["tool"] == permission_tool:
            saw_permission_call = True
            if permission_match_arg in call["args"]:
                saw_permission_arg = True
            granted_users.add(hashable(call["args"].get(permission_match_arg)))

        if call["tool"] == before_tool:
            raw = call["args"].get(threshold_arg)
            if raw is not None and _as_number(raw) is None:
                return misconfigured(
                    rule,
                    f"'{before_tool}' was called with {threshold_arg}={raw!r}, which is "
                    f"not a number, but the rule compares it against {threshold}. Fix "
                    f"the contract or the agent's argument type.",
                )
            value = _as_number(raw)
            if value is None:
                continue
            user_val = hashable(call["args"].get(match_arg)) if match_arg else True

            if cumulative:
                running_totals[user_val] = running_totals.get(user_val, 0) + value
                effective_value = running_totals[user_val]
            else:
                effective_value = value

            if effective_value <= threshold:
                continue

            if user_val not in granted_users:
                if saw_permission_call and not saw_permission_arg:
                    return misconfigured(
                        rule,
                        f"'{permission_tool}' was called but never with argument "
                        f"'{permission_match_arg}'. The rule names an argument that "
                        f"tool does not take, so it can never be satisfied.",
                    )
                reason = (
                    f"'{before_tool}' calls for that user reached a cumulative "
                    f"{threshold_arg}={effective_value} (> {threshold}) without "
                    f"prior '{permission_tool}'."
                    if cumulative
                    else (
                        f"'{before_tool}' called with {threshold_arg}={value} "
                        f"(> {threshold}) without prior '{permission_tool}' for that user."
                    )
                )
                return {
                    "passed": False,
                    "rule_id": rule["id"],
                    "reason": reason,
                    "violating_call": call,
                }

    return {"passed": True, "rule_id": rule["id"]}


RULE_EVALUATORS = {
    "must_precede": evaluate_must_precede,
    "must_never": evaluate_must_never,
    "must_ask_permission": evaluate_must_ask_permission,
}


# Condition fields each rule type requires. A field listed here must be
# present as a key — its value may still be null where that is meaningful
# (e.g. `match_arg: null` for a rule with no identity dimension). These are
# exactly the fields where inferring a default would produce a verdict
# rather than an error; see README "No silent defaults".
REQUIRED_CONDITION_KEYS = {
    "must_precede": ["before_tool", "required_tool", "match_arg"],
    "must_never": ["tool"],
    "must_ask_permission": [
        "before_tool", "permission_tool", "arg", "exceeds", "match_arg", "cumulative",
    ],
}


def validate_rule(rule):
    """
    Structural validation of a single rule, independent of any trajectory.
    Single source of truth: called pre-flight so a malformed contract is
    caught before the agent runs, and again from evaluate_trajectory so a
    direct caller can't bypass it.

    Returns a list of error strings; empty means structurally sound.
    """
    errors = []

    if not rule.get("id"):
        errors.append("rule has no 'id'")

    rule_type = rule.get("type")
    if rule_type not in RULE_EVALUATORS:
        errors.append(
            f"unknown rule type '{rule_type}' (expected one of "
            f"{sorted(RULE_EVALUATORS)})"
        )
        return errors

    condition = rule.get("condition")
    if not isinstance(condition, dict):
        errors.append("rule has no 'condition' block")
        return errors

    for key in REQUIRED_CONDITION_KEYS[rule_type]:
        if key not in condition:
            errors.append(
                f"'{key}' must be set explicitly for {rule_type}; omitting it would "
                "silently change what the rule checks rather than raise"
            )

    # A placeholder left in from `extract`. The angle brackets are this
    # tool's own convention for "a human still has to answer this", so it
    # needs no schema to recognise — and a rule carrying one matches nothing
    # and passes forever. Without this, `validate` with no --tools-from gave
    # a green check to a proposal that had been pasted in unfinished.
    for key, value in condition.items():
        if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
            errors.append(
                f"'{key}' is still the placeholder {value} from `extract` — fill it "
                f"in. A rule holding a placeholder matches nothing, so it reports "
                f"PASS no matter what the agent does"
            )

    # Identity matching: naming the arg on one side but not the other would
    # compare against a value that is never present, failing every call.
    if condition.get("match_arg"):
        if rule_type == "must_precede" and not condition.get("required_match_arg"):
            errors.append(
                "'required_match_arg' is required when 'match_arg' is set — name the "
                f"identity argument on '{condition.get('required_tool')}' explicitly"
            )
        if rule_type == "must_ask_permission" and not condition.get("permission_match_arg"):
            errors.append(
                "'permission_match_arg' is required when 'match_arg' is set — name the "
                f"identity argument on '{condition.get('permission_tool')}' explicitly"
            )

    # Threshold mode is arg+exceeds together, or neither.
    if rule_type == "must_never":
        if (condition.get("arg") is None) != (condition.get("exceeds") is None):
            errors.append(
                "'arg' and 'exceeds' must be given together (threshold mode) or both "
                "omitted (ban the tool outright)"
            )

    return errors


def referenced_tools(rules):
    """
    Every tool name the rules mention, whether or not an argument is
    attached to it.

    Distinct from referenced_args, which keys on (tool, arg) pairs and so
    silently omits a bare `tool:` in must_never, or a required_tool whose
    match_arg is null. A typo'd tool name in those positions can never fire
    and would otherwise validate clean.
    """
    tools = set()
    for rule in rules:
        c = rule.get("condition") or {}
        for key in ("tool", "before_tool", "required_tool", "permission_tool"):
            if c.get(key):
                tools.add(c[key])
    return tools


def referenced_args(rules):
    """
    {tool_name: {arg_names}} that the rules actually depend on.
    """
    refs = {}

    def add(tool, arg):
        if tool and arg:
            refs.setdefault(tool, set()).add(arg)

    for rule in rules:
        c = rule.get("condition") or {}
        kind = rule.get("type")
        if kind == "must_precede":
            add(c.get("before_tool"), c.get("match_arg"))
            add(c.get("required_tool"), c.get("required_match_arg"))
        elif kind == "must_never":
            add(c.get("tool"), c.get("arg"))
        elif kind == "must_ask_permission":
            add(c.get("before_tool"), c.get("arg"))
            add(c.get("before_tool"), c.get("match_arg"))
            add(c.get("permission_tool"), c.get("permission_match_arg"))
    return refs


def validate_trajectory_args(trajectory, rules):
    """
    Check that every argument a rule depends on actually appears on the tool
    it names, whenever that tool is called.

    A mismatch is silent and dangerous rather than loud: must_ask_permission
    skips its threshold check entirely when the arg is absent, must_never's
    threshold mode can never fire, and must_precede fails every call. So a
    rule watching `customer_id` against instrumentation that emits
    `customerId` reports a confident PASS while checking nothing.

    This matters most for out-of-process agents, where the trajectory is
    hand-written by whoever instrumented them, but the same hole exists for
    any agent whose tool signature drifts from its contract.

    Returns a list of error strings; empty means consistent.
    """
    refs = referenced_args(rules)
    if not refs:
        return []

    seen = {}
    for call in trajectory:
        tool = call.get("tool")
        if tool in refs:
            seen.setdefault(tool, set()).update((call.get("args") or {}).keys())

    errors = []
    for tool, needed in sorted(refs.items()):
        if tool not in seen:
            continue  # never called — that's the rules' job to judge, not this
        missing = sorted(needed - seen[tool])
        if missing:
            errors.append(
                f"'{tool}' was called, but never with {missing} — the rules depend on "
                f"{sorted(needed)}. Either the contract names the wrong argument or the "
                f"agent's tool signature has drifted; a rule cannot match on an "
                f"argument that never appears, so it would report a verdict while "
                f"checking nothing. Observed arguments: {sorted(seen[tool])}"
            )
    return errors


def evaluate_trajectory(trajectory, behavioral_rules):
    """
    Runs every rule against the trajectory. Returns a list of results,
    one per rule, each with passed: True/False and details if failed.
    """
    results = []
    for rule in behavioral_rules:
        # Re-checked here as well as pre-flight, so a caller reaching the
        # evaluator directly can't skip validation and get a verdict from a
        # malformed rule.
        errors = validate_rule(rule)
        if errors:
            results.append(misconfigured(rule, "; ".join(errors)))
            continue
        result = RULE_EVALUATORS[rule["type"]](trajectory, rule)
        # Origin travels with the verdict so the ledger can answer whether
        # generated rules behave like hand-written ones. Annotated here
        # rather than in each evaluator, so a new rule type cannot forget.
        result["generated"] = bool(rule.get("generated"))
        results.append(result)
    return results

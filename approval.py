"""
The gate between a proposed rule and a rule in a suite.

`extract` prints candidates. Until now, moving one into a contract meant
reading the list and retyping it, which is fine for eleven proposals and
silently terrible for a hundred — the failure mode of any generator is a
suite that looks comprehensive and tests nothing, and nobody reads a
hundred YAML files closely.

So this is deliberately one proposal at a time, with no bulk accept and no
confidence-based auto-accept. High confidence from an AST-extracted guard
is still a rule nobody read; the evidence ranking orders a human's
attention, it does not substitute for it.

Four things hold here, and each is the reason a specific failure cannot
reach a file:

  1. Falsifiability is checked BEFORE a proposal is shown. A rule that
     cannot be made to fail never reaches a human at all — it goes to a
     separate list with the reason. A generated contract that passes
     forever because it names a tool nothing produces is exactly what this
     project exists to catch, and it would be perverse to generate one.

  2. A placeholder blocks acceptance. Extraction declines to guess an
     identity argument when a tool offers several; this makes that refusal
     visible at the moment of decision rather than letting `<IDENTITY_ARG>`
     reach a file and fail validation later.

  3. Everything accepted is marked `generated: true` with its source. That
     is the instrument for the question "is the generator producing real
     tests or plausible ones" — exercise rate and fire rate split by
     origin. Every layer of this project built to catch a defect class has
     turned out to contain one; assume this one does too, and build the
     thing that would show it.

  4. Rejections are logged with a reason. Ten people rejecting the same
     proposal shape is the signal that a heuristic is wrong, and it is the
     only feedback the extractor gets. Without it a rejected candidate just
     disappears.
"""

import json
import os
from datetime import datetime, timezone

import yaml

import prover
from evaluator.rules import validate_rule

REJECTIONS_FILE = os.environ.get(
    "AGENTTEST_REJECTIONS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agenttest_rejections.jsonl"))


def _unfilled(value):
    return isinstance(value, str) and value.startswith("<") and value.endswith(">")


def blocking_placeholders(rule):
    """
    Fields a human must answer before this rule can be written.

    validate_rule already rejects these, but at write time. Surfacing them
    at the decision point is the difference between "your file is invalid"
    and "pick one of these two arguments".
    """
    condition = (rule.get("condition") or {})
    return sorted(k for k, v in condition.items() if _unfilled(v))


def choices_for(field, rule, vocab):
    """
    The concrete options for a placeholder, read off the schema.

    An identity argument belongs to whichever tool the field refers to, so
    the human is offered that tool's actual parameters rather than a free
    text box. Returns [] when the schema cannot narrow it.
    """
    import extractor

    condition = rule.get("condition") or {}
    owner = {"match_arg": ("before_tool", "tool"),
             "required_match_arg": ("required_tool",),
             "permission_match_arg": ("permission_tool",)}.get(field)
    if owner:
        tool = next((condition.get(o) for o in owner if condition.get(o)), None)
        if tool in (vocab or {}):
            return extractor.identity_args(sorted(vocab[tool]))
    if field == "permission_tool":
        return extractor.approval_tools(vocab or {})
    return []


def screen(candidates, vocab=None):
    """
    Split proposals into those worth a human's time and those that are not.

    Returns (reviewable, unverifiable). A candidate is unverifiable when it
    is structurally broken, or when a trajectory built specifically to
    violate it does not trip it — in which case accepting it would add a
    contract that reports PASS regardless of what the agent does.

    Placeholders are NOT a reason to withhold a proposal. An unanswered
    field is the human's job; the point is to ask, not to hide the rule.
    """
    reviewable, unverifiable = [], []
    for c in candidates:
        rule = dict(c["rule"])
        rule.setdefault("id", "candidate")

        # A placeholder makes the rule invalid, so falsifiability has to be
        # judged on a filled-in stand-in. The stand-in is never written
        # anywhere; it exists only to answer "could this ever go red".
        probe = _with_placeholders_resolved(rule, vocab)
        state, detail = prover.prove_rule(probe)

        if state == "can_fail":
            problems = prover.check_vocabulary(probe, vocab) if vocab else []
            if problems:
                unverifiable.append((c, "names something the agent does not have: "
                                        + "; ".join(problems)))
            else:
                reviewable.append(c)
        elif state == "unsupported":
            # No synthesiser for this rule type. Not evidence against it.
            reviewable.append(c)
        else:
            unverifiable.append((c, detail))
    return reviewable, unverifiable


# Condition fields the evaluator does arithmetic on. A string stand-in here
# crashes the prover rather than failing it, which would have been reported
# as "this rule cannot be shown to fail" — a withheld proposal for a reason
# that was never about the rule.
NUMERIC_FIELDS = ("exceeds",)


def _with_placeholders_resolved(rule, vocab):
    """A copy with placeholders swapped for plausible real values, for proving."""
    probe = {**rule, "condition": dict(rule.get("condition") or {})}
    for field in blocking_placeholders(probe):
        if field in NUMERIC_FIELDS:
            probe["condition"][field] = 1
            continue
        options = choices_for(field, probe, vocab)
        probe["condition"][field] = options[0] if options else "__probe__"
    return probe


def conflicts_with(rule, existing):
    """
    How this proposal sits against rules already in the target scenario.

    Duplicates and contradictions are reported for a human to resolve, never
    merged silently — the same principle as reporting disagreeing extraction
    sources rather than picking one.
    """
    condition = rule.get("condition") or {}
    kind = rule.get("type")
    subject = condition.get("tool") or condition.get("before_tool")
    out = []

    for other in existing or []:
        other_cond = other.get("condition") or {}
        other_subject = other_cond.get("tool") or other_cond.get("before_tool")
        if other_subject != subject:
            continue

        # Across types, a bare ban and an ordering rule on the same tool
        # contradict each other: must_precede and must_ask_permission both
        # presuppose the tool may legitimately be called, and a ban says it
        # may not. Checking only same-type comparisons let a `never call
        # transfer_money` rule land in a scenario whose request is a
        # transfer and whose other rule says to authenticate first — leaving
        # a contract no behaviour could satisfy.
        if other.get("type") != kind:
            banned, ordered = None, None
            if is_bare_ban(rule) and other.get("type") in (
                    "must_precede", "must_ask_permission"):
                banned, ordered = "this proposal", f"'{other.get('id', '?')}'"
            elif is_bare_ban(other) and kind in (
                    "must_precede", "must_ask_permission"):
                banned, ordered = f"'{other.get('id', '?')}'", "this proposal"
            if banned:
                out.append(
                    f"contradicts {banned if ordered == 'this proposal' else ordered}"
                    f": one forbids calling '{subject}' at all while the other "
                    f"governs how it may be called. Both cannot hold — no "
                    f"behaviour would satisfy the pair.")
            continue

        if other_cond == condition:
            out.append(f"identical to existing rule '{other.get('id', '?')}'")
            continue

        differing = sorted(
            k for k in set(condition) | set(other_cond)
            if condition.get(k) != other_cond.get(k))
        out.append(
            f"overlaps existing rule '{other.get('id', '?')}' on the same tool, "
            f"differing in {', '.join(differing)} — decide which one is right "
            f"rather than keeping both")
    return out


def is_bare_ban(rule):
    """A must_never with no threshold: the tool may not be called at all."""
    condition = rule.get("condition") or {}
    return rule.get("type") == "must_never" and condition.get("arg") is None


def exercise_gap(rule, scenario):
    """
    Whether the target scenario can actually reach this rule.

    The correct relationship between a rule's tool and `exercised_when`
    depends on the rule, and getting it backwards is not symmetric:

      must_precede, must_ask_permission, thresholded must_never
        The tool is expected to be called. If exercised_when cannot reach
        it, the rule reports INCONCLUSIVE forever — present, and testing
        nothing.

      bare must_never
        The tool must NOT be called, so a scenario is exercised by some
        other tool — `no_account_deletion` is exercised by get_user, and
        `never_sends` by update_message_labels. Its absence is the design.
        A ban on the very tool that marks the scenario exercised can only
        register as exercised by being violated, so it can never pass.

    An earlier version applied the first rule to every type and warned that
    a ban's tool was missing from exercised_when — advice which, followed,
    would have broken a working contract.
    """
    condition = rule.get("condition") or {}
    subject = condition.get("tool") or condition.get("before_tool")
    reached = (scenario.get("exercised_when") or {}).get("any_of") or []
    if not subject or not reached:
        return None

    if is_bare_ban(rule):
        if subject in reached:
            return (f"'{subject}' is what marks this scenario exercised, and this "
                    f"rule forbids calling it. The scenario could only register "
                    f"as exercised by violating the rule, so it can never pass.")
        return None

    if subject not in reached:
        return (f"'{subject}' is not in this scenario's exercised_when "
                f"({', '.join(reached)}), so the rule would report "
                f"INCONCLUSIVE rather than testing anything. Add it there, or "
                f"pick a scenario that exercises {subject}.")
    return None


def rule_id_for(rule, existing):
    """A stable, readable id that does not collide with what is already there."""
    condition = rule.get("condition") or {}
    subject = condition.get("tool") or condition.get("before_tool") or "rule"
    other = (condition.get("required_tool") or condition.get("permission_tool") or "")
    base = {
        "must_precede": f"{other}_before_{subject}",
        "must_ask_permission": f"{subject}_requires_{other}",
        "must_never": f"never_{subject}",
    }.get(rule.get("type"), subject) or subject

    taken = {r.get("id") for r in existing or []}
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def accept(rule, candidate, scenario_path):
    """
    Append one approved rule to a scenario file.

    Marked `generated: true` with its source, so a later question about
    whether generated rules behave like hand-written ones has an answer in
    the data rather than an opinion.

    Returns the id written. Raises ValueError if the rule would not
    validate — nothing unverified reaches a file.
    """
    with open(scenario_path) as fh:
        scenario = yaml.safe_load(fh) or {}

    existing = scenario.get("behavioral_rules") or []
    written = {
        "id": rule_id_for(rule, existing),
        "type": rule.get("type"),
        "condition": dict(rule.get("condition") or {}),
        "generated": True,
        "generated_from": candidate.get("source"),
        "generated_evidence": candidate.get("evidence"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    errors = validate_rule(written)
    if errors:
        raise ValueError("; ".join(errors))

    scenario["behavioral_rules"] = existing + [written]
    with open(scenario_path, "w") as fh:
        yaml.safe_dump(scenario, fh, sort_keys=False, width=88)
    return written["id"]


def log_rejection(candidate, reason, path=None):
    """
    Record why a proposal was turned down.

    The only feedback loop the extractor has. A heuristic that keeps
    producing the same rejected shape is wrong, and that is invisible if a
    rejected candidate simply disappears.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rule_type": candidate.get("rule_type"),
        "source": candidate.get("source"),
        "confidence": candidate.get("confidence"),
        "evidence": candidate.get("evidence"),
        "condition": (candidate.get("rule") or {}).get("condition"),
        "reason": reason,
    }
    try:
        with open(path or REJECTIONS_FILE, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def rejection_patterns(path=None):
    """
    Rejected proposal shapes, most frequent first.

    Aggregated by (rule_type, source) because that is the grain a heuristic
    lives at: five rejections of schema-derived must_never rules says the
    destructive-verb list is too eager, which no single rejection shows.
    """
    entries = []
    try:
        with open(path or REJECTIONS_FILE) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return []

    counts = {}
    for e in entries:
        key = (e.get("rule_type"), e.get("source"))
        counts.setdefault(key, {"count": 0, "reasons": []})
        counts[key]["count"] += 1
        if e.get("reason"):
            counts[key]["reasons"].append(e["reason"])
    return sorted(
        ({"rule_type": k[0], "source": k[1], **v} for k, v in counts.items()),
        key=lambda r: -r["count"])

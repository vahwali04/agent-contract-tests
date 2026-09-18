"""
Run history — the tool's memory across invocations.

Every status this harness reports is an estimate from a sample, but until
now each invocation started blind: a scenario flaky at ~15% reports STABLE
on most 10-run batches, and reports it again tomorrow, and the rare branch
stays invisible. That is the same defect as the empty-trajectory false
passes and the confidently-wrong stability summary — a confident label on
evidence the tool doesn't have.

History accumulates as a side effect of normal use, so a scenario can be
reclassified from "no variance seen" to FLAKY by later evidence without
anyone deliberately re-testing it.

Two properties matter:

  - Estimates are reported as bounds, not verdicts, while N is small.
    "10 runs, no variance" and "200 runs, no variance" are wildly different
    claims; rendering both as STABLE hides the difference.

  - Runs are keyed by configuration. A model swap or prompt edit makes
    earlier runs incomparable — pooling them is exactly the confound that
    produced this project's first bad non-determinism claim. Runs under
    other configurations are counted and shown, never folded into the
    estimate.
"""

import json
import os
import hashlib
from datetime import datetime, timezone

HISTORY_FILE = os.environ.get(
    "AGENTTEST_HISTORY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agenttest_history.jsonl"),
)

# Fields that make two runs comparable. Anything here differing means the
# runs measure different things and must not share a variance estimate.
CONFIG_KEYS = ("agent", "model", "prompt_variant", "behavior", "agent_version",
               "concurrency")


def config_of(args, agent_version=None, concurrency=None):
    """
    Extract the comparability key from parsed CLI args.

    `agent_version` is only meaningful for HTTP agents: the same endpoint
    can serve a redeployed agent with no URL change, so without a reported
    version the ledger would pool estimates across agent versions — the
    one confound endpoint-keying can't catch on its own.
    """
    return {
        "agent_version": agent_version,
        # Parallelism belongs in the comparability key. A user reported
        # 17-45% "flakiness" that was three concurrent CLI processes
        # contending on one machine: their concurrency-3 and concurrency-1
        # runs pooled into one bucket, and a harness artefact read as agent
        # behaviour. Note this bounds contention the harness causes, not
        # contention on the host.
        "concurrency": concurrency,
        "agent": args.agent,
        "model": (args.model if args.agent == "anthropic"
                  else getattr(args, "agent_url", None) if args.agent == "http"
                  else None),
        "prompt_variant": args.prompt_variant,
        "behavior": args.behavior if args.agent == "mock" else None,
    }


def trajectory_hash(trajectory):
    """
    Stable, hashable identity for a trajectory's shape.

    Serialises through JSON rather than building tuples, so list- and
    object-valued arguments work. Callers that need to count distinct
    trajectories must use this rather than rolling their own tuple key —
    doing that crashed on any array-valued argument.
    """
    shape = [(c["tool"], sorted(c["args"].items())) for c in trajectory]
    return hashlib.sha256(
        json.dumps(shape, default=str, sort_keys=True).encode()
    ).hexdigest()[:12]


def record(scenario, domain, config, result):
    """Append one run. Never raises — history is diagnostics, not the test."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scenario": scenario,
        "domain": domain,
        "status": result["status"],
        "exercised": bool(result.get("exercised")),
        "trajectory": trajectory_hash(result.get("trajectory", [])),
        # Per-rule outcomes, so "do generated rules ever fire" is a question
        # the ledger can answer. A rule that never goes red across many runs
        # is decorative whatever its origin; the point is to see whether one
        # origin produces more of them than the other.
        "rules": [{"id": r.get("rule_id"),
                   "passed": bool(r.get("passed")),
                   "error": bool(r.get("error")),
                   "generated": bool(r.get("generated"))}
                  for r in result.get("rule_results") or []],
        **{**config, "agent_version": result.get("agent_version", config.get("agent_version"))},
    }
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def load(scenario=None):
    """All recorded runs, optionally filtered to one scenario."""
    if not os.path.exists(HISTORY_FILE):
        return []
    out = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a truncated line shouldn't poison the ledger
                if scenario is None or e.get("scenario") == scenario:
                    out.append(e)
    except OSError:
        return []
    return out


def _matches(entry, config):
    return all(entry.get(k) == config.get(k) for k in CONFIG_KEYS)


def summarize(scenario, config):
    """
    Accumulated evidence for one scenario under one configuration.

    Returns a dict with the variance estimate, the exercise rate, and a
    count of runs excluded for having used different settings.
    """
    entries = load(scenario)
    matching = [e for e in entries if _matches(e, config)]
    other = len(entries) - len(matching)

    # ERRORED and MISCONFIGURED mean nothing was learned about the agent —
    # an infrastructure failure or a broken contract, not a behaviour. They
    # are recorded (how often runs fail is worth knowing) but must never
    # enter a variance or coverage estimate: pooling them reported 5 PASS +
    # 5 ERRORED as "50% variance, FLAKY", and 10/10 ERRORED as "0% coverage"
    # when in fact the scenario had never run at all.
    NO_EVIDENCE = {"ERRORED", "MISCONFIGURED"}
    informative = [e for e in matching if e["status"] not in NO_EVIDENCE]
    no_evidence = len(matching) - len(informative)

    n = len(informative)
    if n == 0:
        return {"n": 0, "other_config_runs": other, "no_evidence_runs": no_evidence}

    statuses = {}
    for e in informative:
        statuses[e["status"]] = statuses.get(e["status"], 0) + 1

    # Only PASS and FAIL state whether the agent complied. REFUSED and
    # INCONCLUSIVE mean the contract was never exercised — informative about
    # coverage, silent about compliance. Counting them as disagreement
    # reports an agent that sometimes declined to act as an agent that
    # sometimes misbehaved, which is how a contention artefact was read as
    # 45% flakiness, and how this project's own "10% verdict variance"
    # finding overstated what it had measured.
    VERDICT = {"PASS", "FAIL"}
    verdicts = [e for e in informative if e["status"] in VERDICT]
    verdict_n = len(verdicts)
    if verdict_n:
        counts = {}
        for e in verdicts:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        minority = verdict_n - max(counts.values())
    else:
        minority = 0

    dominant = max(statuses.values())
    exercised = sum(1 for e in informative if e["exercised"])
    trajectories = len({e["trajectory"] for e in informative})

    # What a naive pool across every configuration would have reported. Not
    # used as the estimate — it is the thing to warn about. A user's
    # concurrency-3 contention batch pooled with their serial runs and read
    # as 45% agent flakiness; they had to spot the timestamps themselves.
    pooled = [e for e in entries if e["status"] not in NO_EVIDENCE]
    pooled_verdicts = [e for e in pooled if e["status"] in VERDICT]
    pooled_rate = 0.0
    if pooled_verdicts:
        pc = {}
        for e in pooled_verdicts:
            pc[e["status"]] = pc.get(e["status"], 0) + 1
        pooled_rate = (len(pooled_verdicts) - max(pc.values())) / len(pooled_verdicts)
    pooled_cov = (sum(1 for e in pooled if e["exercised"]) / len(pooled)) if pooled else 0.0

    return {
        "n": n,
        "statuses": statuses,
        "pooled_variance_rate": pooled_rate,
        "pooled_exercise_rate": pooled_cov,
        "minority": minority,
        "verdict_n": verdict_n,
        "variance_rate": (minority / verdict_n) if verdict_n else 0.0,
        "bound": 3.0 / verdict_n if verdict_n else 1.0,
        "exercise_rate": exercised / n,
        "distinct_trajectories": trajectories,
        "other_config_runs": other,
        "no_evidence_runs": no_evidence,
    }


# Below this many runs, an exercise rate is too noisy to act on.
MIN_RUNS_FOR_COVERAGE_JUDGEMENT = 10


def coverage_verdict(summary, min_rate):
    """
    Compare observed exercise rate against the scenario's declared floor.

    Returns (state, message) where state is "ok", "low", or "unknown".
    Deliberately separate from the variance estimate: a suite quietly
    ceasing to test anything is harder to notice than a flaky verdict,
    because nothing turns red on its own.
    """
    if min_rate is None or summary.get("n", 0) == 0:
        return "unknown", None
    if summary["n"] < MIN_RUNS_FOR_COVERAGE_JUDGEMENT:
        return "unknown", (
            f"only {summary['n']} runs — too few to judge coverage "
            f"(need {MIN_RUNS_FOR_COVERAGE_JUDGEMENT})"
        )
    rate = summary["exercise_rate"]
    if rate + 1e-9 < min_rate:
        return "low", (
            f"reached the risky path in {rate:.0%} of {summary['n']} runs, below the "
            f"declared floor of {min_rate:.0%} — this contract is barely testing anything"
        )
    return "ok", None


def format_summary(scenario, config, summary, batch_statuses=None, min_rate=None):
    """Human-readable block. Reports bounds while N is small, never a bare verdict."""
    lines = []
    cfg = " | ".join(
        str(v) for v in (config.get("agent"), config.get("model") or config.get("behavior"),
                         config.get("prompt_variant")) if v
    )
    lines.append(f"  {scenario}  [{cfg}]")

    if batch_statuses:
        spread = ", ".join(f"{s}x{batch_statuses.count(s)}" for s in sorted(set(batch_statuses)))
        lines.append(f"    This batch: {len(batch_statuses)} runs — {spread}")

    if summary["n"] == 0:
        ne = summary.get("no_evidence_runs", 0)
        if ne:
            lines.append(
                f"    All time:   {ne} run(s) recorded, all ERRORED or MISCONFIGURED — "
                "nothing was learned about the agent, so there is nothing to estimate"
            )
        else:
            lines.append("    All time:   no prior runs recorded")
        return "\n".join(lines)

    spread = ", ".join(f"{k}x{v}" for k, v in sorted(summary["statuses"].items(),
                                                     key=lambda kv: -kv[1]))
    lines.append(f"    All time:   {summary['n']} runs — {spread}")

    verdict_n = summary.get("verdict_n", 0)
    if summary["minority"] > 0:
        lines.append(
            f"    Compliance: {summary['variance_rate']:.0%} "
            f"({summary['minority']}/{verdict_n} runs that reached a verdict "
            f"disagreed)  → FLAKY"
        )
        lines.append(
            "                A single run of this scenario is not trustworthy alone."
        )
    elif verdict_n == 0:
        lines.append(
            f"    Compliance: no run reached a verdict — every one was refused or "
            f"inconclusive, so nothing is known about whether the agent complies"
        )
    else:
        lines.append(
            f"    Compliance: no disagreement in {verdict_n} run(s) that reached a "
            f"verdict — rules out variance above ~{summary['bound']:.0%} (95% conf.)"
        )
        if summary["bound"] > 0.10:
            lines.append(
                f"                Not a stability claim: a branch taken "
                f"{summary['bound'] / 2:.0%} of the time would likely still be unseen."
            )

    # Coverage is listed first of the two metrics and stated as a verdict,
    # not a footnote: a flaky verdict at least turns something red, whereas
    # a suite decaying toward testing nothing is silent by construction.
    rate = summary["exercise_rate"]
    state, message = coverage_verdict(summary, min_rate)
    floor = f" (floor {min_rate:.0%})" if min_rate is not None else ""
    label = {"low": "  → COVERAGE LOW", "ok": "", "unknown": ""}[state]
    lines.insert(
        1 if not batch_statuses else 2,
        f"    Coverage:   {rate:.0%} of {summary['n']} runs reached the risky path{floor}{label}",
    )
    if state == "low":
        lines.append(f"                {message}")
        lines.append(
            "                Nothing turns red for this on its own — that is the point."
        )
    elif state == "unknown" and message:
        lines.append(f"                {message}")

    if summary["distinct_trajectories"] > 1 and summary["minority"] == 0:
        lines.append(
            f"    Robustness: same verdict via {summary['distinct_trajectories']} distinct "
            "trajectories — the rule held across multiple agent strategies"
        )

    if summary.get("no_evidence_runs"):
        lines.append(
            f"    (excluded:  {summary['no_evidence_runs']} ERRORED/MISCONFIGURED run(s) "
            "— no evidence about the agent, so not pooled)"
        )

    if summary["other_config_runs"]:
        lines.append(
            f"    (excluded:  {summary['other_config_runs']} runs under different "
            "settings — not comparable, so not pooled)"
        )
        # Say what pooling would have claimed, but only when it differs
        # enough to have changed the reading. Silence otherwise.
        own_v, own_c = summary["variance_rate"], summary["exercise_rate"]
        pool_v, pool_c = summary["pooled_variance_rate"], summary["pooled_exercise_rate"]
        if abs(pool_v - own_v) >= 0.10 or abs(pool_c - own_c) >= 0.10:
            lines.append(
                f"    ⚠ MIXED POOL: pooling those would have reported "
                f"{pool_v:.0%} compliance variance and {pool_c:.0%} coverage, against "
                f"{own_v:.0%} and {own_c:.0%} here. Those runs measure something "
                f"else — a different model, prompt, agent version, or concurrency."
            )

    return "\n".join(lines)


def by_origin(scenario=None, config=None):
    """
    Do generated rules behave like hand-written ones?

    Every layer of this project built to catch a defect class has turned out
    to contain one, so assume the extractor does too and build the thing
    that would show it. The signal is not pass rate — a rule that always
    passes may be sound or may be decorative, and the two are
    indistinguishable from one run. It is whether a rule has *ever* gone
    red, across everything recorded.

    A rule that never fires is not necessarily wrong. It is unproven by use,
    which is exactly what `prove` cannot tell you: prove shows a rule CAN go
    red against a synthetic trajectory; this shows whether it ever DOES
    against a real agent.

    Returns {"generated": {...}, "hand_written": {...}} or None when there
    is nothing recorded — an empty ledger is not a finding of parity.
    """
    entries = load(scenario)
    if config:
        entries = [e for e in entries if _matches(e, config)]

    buckets = {"generated": {}, "hand_written": {}}
    for entry in entries:
        for rule in entry.get("rules") or []:
            if not rule.get("id"):
                continue
            side = "generated" if rule.get("generated") else "hand_written"
            seen = buckets[side].setdefault(
                rule["id"], {"evaluated": 0, "fired": 0, "errored": 0})
            seen["evaluated"] += 1
            seen["fired"] += int(not rule.get("passed") and not rule.get("error"))
            seen["errored"] += int(bool(rule.get("error")))

    if not any(buckets.values()):
        return None

    out = {}
    for side, rules in buckets.items():
        never = sorted(rid for rid, s in rules.items() if not s["fired"])
        out[side] = {
            "rules": len(rules),
            "evaluations": sum(s["evaluated"] for s in rules.values()),
            "ever_fired": sum(1 for s in rules.values() if s["fired"]),
            "never_fired": never,
            "errored": sorted(rid for rid, s in rules.items() if s["errored"]),
        }
    return out


def render_by_origin(report):
    """A comparison, or an honest statement that there is nothing to compare."""
    if not report:
        return ("No per-rule outcomes recorded yet. Rules accepted through "
                "`approve` are marked generated: true, but the comparison needs "
                "runs — sweep a suite containing both kinds under one config "
                "before reading anything here.")

    lines = ["  origin        rules  evals  ever fired  never fired"]
    for side in ("hand_written", "generated"):
        s = report[side]
        lines.append(f"  {side:12s}  {s['rules']:5d}  {s['evaluations']:5d}  "
                     f"{s['ever_fired']:10d}  {len(s['never_fired']):11d}")

    thin = [side for side in ("hand_written", "generated")
            if report[side]["rules"] and report[side]["evaluations"] < 5]
    if thin:
        lines.append(f"\n  Too few runs to compare ({', '.join(thin)} under 5 "
                     f"evaluations). The table is a tally, not a finding.")

    for side in ("hand_written", "generated"):
        never = report[side]["never_fired"]
        if never:
            lines.append(f"\n  {side} rules that have never gone red: "
                         f"{', '.join(never)}")
            lines.append("  Not proof they are wrong — proof they are unproven "
                         "by use. `prove` shows a rule CAN fail; only a run "
                         "shows it ever does.")
    return "\n".join(lines)

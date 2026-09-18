"""
CI entrypoint (used by action.yml).

Runs every registered scenario against one agent, prints a full trajectory
report per scenario, writes a markdown summary to $GITHUB_STEP_SUMMARY if
running in a GitHub Action, and exits non-zero if any scenario fails —
so a workflow can turn that into a required, merge-blocking check.
"""

import argparse
import os
import sys

import yaml
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.base_agent import MockAgent, AnthropicAgent
from agent.http_agent import HTTPAgent
from prover import parse_headers
from domains import DOMAINS, EXTERNAL_DOMAIN, PROMPT_VARIANTS, scenario_index
import history
from runner.run_test import run_scenario, print_report, load_scenario
from main import positive_int


def reproduce_command(args):
    """The exact command that produced this report."""
    cmd = f"python3 scripts/run_all.py --domain {args.domain} --agent {args.agent}"
    if args.agent == "anthropic":
        cmd += f" --model {args.model}"
    else:
        cmd += f" --behavior {args.behavior}"
    cmd += f" --prompt-variant {args.prompt_variant}"
    if args.repeat > 1:
        cmd += f" --repeat {args.repeat}"
    return cmd


def variance_bound(n):
    """
    Upper bound on an unobserved outcome's true rate, given n runs with no
    variance seen. Rule of three: the 95% upper bound is ~3/n. Reporting
    this stops "no variance in 5 runs" from being read as "deterministic".
    """
    return 3.0 / n if n else 1.0


def main():
    parser = argparse.ArgumentParser(description="Run all behavioral scenarios (CI entrypoint)")
    parser.add_argument("--agent", choices=["mock", "anthropic", "http"], default="anthropic")
    parser.add_argument(
        "--agent-url",
        help="Endpoint for --agent http. Receives {scenario, turns}, returns {response, trajectory}.",
    )
    parser.add_argument(
        "--agent-header", action="append", metavar="NAME:VALUE", default=[],
        help="Header to send with each request, repeatable. Use this for an auth "
             "token rather than putting it in the URL, where it lands in logs.",
    )
    parser.add_argument(
        "--behavior",
        choices=["correct", "buggy"],
        default="correct",
        help="Only used with --agent mock (self-test of the harness itself)",
    )
    parser.add_argument("--model", default="claude-sonnet-4-5-20250929")
    parser.add_argument("--prompt-variant", choices=PROMPT_VARIANTS, default="default")
    parser.add_argument(
        "--domain",
        choices=list(DOMAINS.keys()) + ["all"],
        default="all",
        help="Which domain's scenarios to run (default: all).",
    )
    parser.add_argument(
        "--enforce-coverage",
        action="store_true",
        help="Block when a scenario's accumulated exercise rate falls below its declared min_exercise_rate. Off by default because history is local: a fresh CI checkout has none.",
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=None,
        metavar="N",
        help="Parallel agent runs. Defaults to 6, or 1 for --agent http, whose "
             "endpoint often spawns a process per scenario — contention there "
             "reads as agent flakiness. Raise it if your endpoint is a real "
             "concurrent service.",
    )
    parser.add_argument(
        "--abort-after", type=int, default=3, metavar="N",
        help="Stop the sweep after N consecutive transport failures (default 3; "
             "0 disables). A dead endpoint fails every remaining request the same "
             "way, and the time is better spent on a retry.",
    )
    parser.add_argument(
        "--repeat",
        type=positive_int,
        default=1,
        metavar="N",
        help="Run every scenario N times under identical settings to measure flakiness. Reports which scenarios vary — concentrated flakiness is actionable, uniform flakiness undermines the merge gate.",
    )
    parser.add_argument(
        "--scenarios",
        metavar="PATH",
        help="Directory or glob of your own scenario files to run instead of the "
             "bundled ones. Use with --agent http, whose agent supplies its own "
             "environment, or with --domain to run them against a bundled world.",
    )
    args = parser.parse_args()

    if args.scenarios:
        import glob as _glob
        pattern = args.scenarios
        if os.path.isdir(pattern):
            pattern = os.path.join(pattern, "*.y*ml")
        paths = sorted(p for p in _glob.glob(pattern) if not p.endswith("TEMPLATE.yaml"))
        if not paths:
            parser.error(f"no scenario files matched {args.scenarios!r}")
        domain_name = "external" if args.agent == "http" else args.domain
        if domain_name in (None, "all"):
            parser.error("--scenarios needs --agent http, or --domain to say which "
                         "world to run them in")
        index = {os.path.splitext(os.path.basename(p))[0]: (domain_name, p) for p in paths}
    else:
        index = scenario_index(None if args.domain == "all" else args.domain)
    results = []
    repeats = {}  # scenario -> list of statuses across runs

    def build_agent(name):
        if args.agent == "anthropic":
            return AnthropicAgent(model=args.model, prompt_variant=args.prompt_variant)
        if args.agent == "http":
            if not args.agent_url:
                parser.error("--agent http requires --agent-url")
            a = HTTPAgent(args.agent_url, headers=parse_headers(args.agent_header, parser))
            a._scenario_name = name
            return a
        return MockAgent(behavior=args.behavior, scenario=name)

    # Every run is an agentic loop of several API round-trips, so a serial
    # sweep with --repeat is dominated by network wait. Run them in a thread
    # pool: each run builds its own agent, state, and gateway, so nothing is
    # shared across threads.

    tasks = [
        (name, domain_name, scenario_path, i)
        for name, (domain_name, scenario_path) in index.items()
        for i in range(args.repeat)
    ]
    total = len(tasks)
    by_scenario = {name: [] for name in index}
    done = 0

    # A dead transport fails every remaining request identically. A team lost
    # the rest of a CI window to eight scenarios hitting a tunnel's error page
    # after it dropped mid-sweep — no false verdicts, but no data and no time
    # left to retry. Stop early so the window goes to a retry instead.
    abort = {"tripped": False, "streak": 0}

    def execute(task):
        name, domain_name, scenario_path, _i = task
        if abort["tripped"]:
            with open(scenario_path) as fh:
                doc = yaml.safe_load(fh)
            return name, {
                "scenario_name": doc.get("name", name), "domain": domain_name,
                "request": "(not run)", "agent_response": "", "trajectory": [],
                "rule_results": [], "status": "ERRORED", "overall_pass": False,
                "exercised": False, "exercised_reason": None,
                "undisclosed_policy": None, "agent_version": None,
                "error_detail": (f"skipped — aborted after {args.abort_after} "
                                 f"consecutive transport failures"),
                "error_retryable": True, "final_state": {},
            }
        dom = EXTERNAL_DOMAIN if domain_name == "external" else DOMAINS[domain_name]
        r = run_scenario(scenario_path, build_agent(name), domain=dom)
        r["domain"] = domain_name
        history.record(r["scenario_name"], domain_name, hist_config, r)
        return name, r

    # HTTP endpoints default to serial. A team running a subprocess-backed
    # endpoint measured 17-45% "flakiness" at concurrency 3 that was three
    # CLI processes contending on one laptop; the same scenarios showed zero
    # disagreement run serially. Correctness of the default beats speed of
    # the default: a contention artefact reads as an agent finding, which is
    # worse than a slow sweep. Anyone fronting a genuinely concurrent
    # service should raise it.
    if args.agent == "mock":
        workers = 1
    elif args.concurrency is None:
        workers = 1 if args.agent == "http" else 6
    else:
        workers = max(1, args.concurrency)

    # Recorded in the comparability key, so a sweep run under contention is
    # never pooled with a serial one.
    hist_config = history.config_of(args, concurrency=workers)
    if total > 1:
        print(f"Running {total} scenario-run(s) with concurrency {workers}...\n",
              file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, r in pool.map(execute, tasks):
            by_scenario[name].append(r)
            done += 1
            if r["status"] == "ERRORED" and r.get("error_retryable", True):
                abort["streak"] += 1
                if (args.abort_after and abort["streak"] >= args.abort_after
                        and not abort["tripped"]):
                    abort["tripped"] = True
                    print(f"\n  Aborting: {abort['streak']} consecutive transport "
                          f"failures. The endpoint looks unreachable — retry rather "
                          f"than spend the rest of this run on it.\n", file=sys.stderr)
            else:
                abort["streak"] = 0
            if total > 1:
                print(f"  [{done}/{total}] {name}: {r['status']}", file=sys.stderr)

    for name in index:
        runs = by_scenario[name]
        # Report the last run in full; the spread is summarised separately.
        result = runs[-1]
        repeats[name] = [r["status"] for r in runs]
        result["trajectory_shapes"] = len(
            {history.trajectory_hash(r["trajectory"]) for r in runs})
        if args.repeat == 1:
            print_report(result)
        results.append((name, result))

    passed = [n for n, r in results if r["status"] == "PASS"]
    failed = [n for n, r in results if r["status"] == "FAIL"]
    inconclusive = [n for n, r in results if r["status"] == "INCONCLUSIVE"]
    refused = [n for n, r in results if r["status"] == "REFUSED"]
    broken = [n for n, r in results if r["status"] == "MISCONFIGURED"]
    errored = [n for n, r in results if r["status"] == "ERRORED"]

    observed_version = next(
        (r.get("agent_version") for _n, r in results if r.get("agent_version")), None)

    headline = f"**{len(passed)}/{len(results)} scenarios passed**"
    extra = []
    if failed:
        extra.append(f"{len(failed)} failed")
    if broken:
        extra.append(f"{len(broken)} misconfigured")
    if errored:
        extra.append(f"{len(errored)} errored")
    if refused:
        extra.append(f"{len(refused)} refused")
    if inconclusive:
        extra.append(f"{len(inconclusive)} inconclusive")
    if extra:
        headline += " — " + ", ".join(extra)

    # Provenance. A red check a developer can't reproduce is an assertion,
    # not evidence — so the settings that produced this verdict travel with
    # it, including the sampling temperature.
    provenance = [
        f"`agent={args.agent}`",
        f"`model={args.model}`" if args.agent == "anthropic" else f"`behavior={args.behavior}`",
        f"`prompt-variant={args.prompt_variant}`",
        f"`domain={args.domain}`",
        # The docs recommend reporting a version; showing it here is what makes
        # "did my edited prompt actually load?" answerable without curling the
        # endpoint by hand.
        f"`agent-version={observed_version}`" if observed_version else None,
        f"`repeat={args.repeat}`" if args.repeat > 1 else None,
    ]
    provenance = " · ".join(p for p in provenance if p)

    summary_lines = [
        "# Agent Behavioral Test Report",
        "",
        headline,
        "",
        f"<sub>{provenance}</sub>",
        "",
        "**Reproduce:**",
        "",
        "```bash",
        reproduce_command(args),
        "```",
        "",
        "| Domain | Scenario | Result |",
        "|---|---|---|",
    ]
    for name, result in results:
        summary_lines.append(f"| {result.get('domain', '-')} | {name} | {result['status']} |")

    if failed:
        summary_lines += ["", "## Failures"]
        for name, result in results:
            if result["status"] != "FAIL":
                continue
            summary_lines.append(f"### {name}")
            for r in result["rule_results"]:
                if not r["passed"]:
                    summary_lines.append(f"- **{r['rule_id']}**: {r['reason']}")
            if result.get("undisclosed_policy"):
                summary_lines.append(
                    f"- ⚠️ **Likely false positive** — {result['undisclosed_policy']}. "
                    "The agent was never told this rule, so it could not have "
                    "followed it. Fix the agent's instructions or the contract."
                )

    if broken:
        summary_lines += [
            "",
            "## Misconfigured (broken contracts, not agent failures)",
            "",
            "These rules cannot be satisfied by any trajectory, so they say nothing "
            "about the agent. Fix the contract.",
            "",
        ]
        for name, result in results:
            if result["status"] != "MISCONFIGURED":
                continue
            summary_lines.append(f"### {name}")
            for r in result["rule_results"]:
                if r.get("error"):
                    summary_lines.append(f"- {r['reason']}")

    if errored:
        summary_lines += [
            "",
            "## Errored (could not run)",
            "",
            "The agent could not be run to completion for these. Nothing was "
            "learned about the agent either way, and these runs are excluded from "
            "any accumulated estimate.",
            "",
        ]
        for name, result in results:
            if result["status"] == "ERRORED":
                tag = ("retry — likely transient" if result.get("error_retryable", True)
                       else "**not retryable** — fix the code or configuration")
                summary_lines.append(f"- **{name}** ({tag}): {result['error_detail']}")

    undisclosed = [n for n, r in results if r.get("undisclosed_policy")]
    if undisclosed:
        summary_lines += [
            "",
            f"> **{len(undisclosed)} of {len(failed)} failures may be test-design "
            "problems rather than agent bugs.** A contract that references a rule "
            "absent from the agent's instructions cannot be satisfied by any "
            "behavior. Review these before treating them as regressions.",
        ]

    if refused:
        summary_lines += [
            "",
            "## Refused (acceptable, but untested)",
            "",
            "The agent declined these requests outright. That's a legitimate "
            "outcome, so it doesn't fail the build — but the rule never ran, "
            "so these don't count as coverage.",
            "",
        ]
        for name, result in results:
            if result["status"] == "REFUSED":
                summary_lines.append(f"- **{name}**: {result['exercised_reason']}")

    if inconclusive:
        summary_lines += [
            "",
            "## Inconclusive",
            "",
            "These scenarios' rules were satisfied, but the agent never exercised "
            "the behavior under test — so they prove nothing. Treat them as gaps "
            "in coverage, not as passes.",
            "",
        ]
        for name, result in results:
            if result["status"] != "INCONCLUSIVE":
                continue
            summary_lines.append(f"- **{name}**: {result['exercised_reason']}")

    # Accumulated evidence across every prior invocation under these same
    # settings — the batch alone can't see a low-frequency branch.
    hist_lines = []
    low_coverage = []
    summary_config = {**hist_config, "agent_version": observed_version}
    for name, _r in results:
        s = history.summarize(_r["scenario_name"], summary_config)
        if s["n"] <= 1:
            continue
        min_rate = load_scenario(index[name][1]).get("min_exercise_rate")
        state, _msg = history.coverage_verdict(s, min_rate)
        if state == "low":
            low_coverage.append(name)
        if state == "low" or s.get("minority", 0) > 0:
            hist_lines.append(history.format_summary(name, summary_config, s, min_rate=min_rate))

    if hist_lines:
        summary_lines += [
            "",
            "## Accumulated history",
            "",
            "Evidence pooled across every prior run under these same settings. A "
            "scenario flaky at a low rate reports clean in most single batches, so "
            "this is where it surfaces.",
            "",
            "```",
            *hist_lines,
            "```",
        ]

    if args.repeat > 1 and errored:
        # Every "run" may have failed before reaching the agent. Reporting
        # "no scenario varied its verdict" here would be true and useless —
        # 15 identical ERRORs are not evidence of stability.
        summary_lines += [
            "",
            "## Stability",
            "",
            f"**Not assessed** — {len(errored)} of {len(results)} scenarios errored, so "
            "repeated runs measured nothing. Fix the errors above and re-run.",
        ]
    elif args.repeat > 1:
        flaky = {n: s for n, s in repeats.items() if len(set(s)) > 1}
        # Robustness requires the contract to have actually been exercised.
        # A scenario that REFUSED ten times via ten different paths found ten
        # ways of testing nothing — reporting that as "the rule held" is the
        # same false-confidence bug this project keeps finding in itself.
        robust = [n for n, s in repeats.items()
                  if set(s) == {"PASS"}
                  and dict(results).get(n, {}).get("trajectory_shapes", 1) > 1]
        bound = variance_bound(args.repeat)

        summary_lines += [
            "",
            "## Stability",
            "",
            f"Each scenario run **{args.repeat}×** under identical settings.",
            "",
        ]
        if flaky:
            summary_lines += [
                f"**{len(flaky)} of {len(repeats)} scenarios are FLAKY** — the same "
                "scenario under identical settings produced different verdicts. A "
                "single run of these is not trustworthy on its own.",
                "",
            ]
            for n, s in flaky.items():
                spread = ", ".join(f"{k}×{v}" for k, v in Counter(s).most_common())
                summary_lines.append(f"- **{n}**: {spread}")
            summary_lines += [
                "",
                "Whether this is *concentrated* (one borderline scenario, likely "
                "calibrated near the model's decision boundary) or *uniform* "
                "(every scenario varies) decides whether it's a scenario to retune "
                "or a problem for the merge-gate claim.",
            ]
        else:
            summary_lines += [
                f"No scenario varied its verdict across {args.repeat} runs.",
                "",
                f"> That does **not** mean deterministic. With {args.repeat} runs and no "
                f"variance observed, this only rules out variance above roughly "
                f"**{bound:.0%}** (95% confidence, rule of three). A branch taken "
                f"{bound / 2:.0%} of the time would very likely have gone unseen. "
                f"Raise `--repeat` for a tighter bound.",
            ]

        if robust:
            summary_lines += [
                "",
                f"**{len(robust)} scenario(s) passed by more than one route** — the agent "
                "reached the risky path via different trajectories and the rule held "
                "every time. "
                "That is stronger evidence than a bit-identical repeat: the contract is "
                "robust across multiple agent strategies rather than only tested against "
                "one.",
                "",
            ]
            for n in robust:
                summary_lines.append(f"- **{n}**: {repeats[n][0]} via "
                                     f"{dict(results)[n]['trajectory_shapes']} distinct trajectories")

    if args.repeat == 1:
        # Discoverability where it matters: an external team shipped ten green
        # contracts without ever running --repeat, so "green" meant green once.
        summary_lines += [
            "",
            "<sub>Single run per scenario — green here means green once. Add "
            "`--repeat N` for a variance bound. Note that unexercised runs "
            "(REFUSED, INCONCLUSIVE) are coverage, not compliance disagreement.</sub>",
        ]

    summary = "\n".join(summary_lines)
    print("\n" + summary)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summary + "\n")

    # Also write the report to a plain file in the workspace. A composite
    # action's own GITHUB_STEP_SUMMARY writes don't reliably surface to a
    # later step in the calling workflow, so a later "post to PR" step
    # should read this file instead of relying on the step summary.
    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    report_path = os.path.join(workspace, "behavioral-test-report.md")
    with open(report_path, "w") as f:
        f.write(summary + "\n")

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"passed={len(passed)}\n")
            f.write(f"failed={len(failed)}\n")
            f.write(f"inconclusive={len(inconclusive)}\n")
            f.write(f"refused={len(refused)}\n")
            f.write(f"misconfigured={len(broken)}\n")
            f.write(f"errored={len(errored)}\n")
            f.write(f"total={len(results)}\n")

    # Inconclusive results block by default: a suite that silently stopped
    # testing anything is exactly the condition a CI gate should surface,
    # not hide behind a green check.
    if low_coverage:
        summary_lines += [
            "",
            f"> **{len(low_coverage)} scenario(s) below their declared coverage floor.** "
            "These still report PASS or REFUSED on any single run — a suite that has "
            "quietly stopped testing anything turns nothing red on its own. "
            + ("Blocking (`--enforce-coverage`)." if args.enforce_coverage
               else "Not blocking; pass `--enforce-coverage` to make it."),
        ]

    blocking = bool(failed or inconclusive or broken or errored
                    or (low_coverage and args.enforce_coverage))
    sys.exit(1 if blocking else 0)


if __name__ == "__main__":
    main()

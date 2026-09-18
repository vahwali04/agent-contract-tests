"""
CLI entrypoint.

Usage:
    python main.py run transfer_test --behavior correct
    python main.py run transfer_test --behavior buggy

This proves the Phase 0 core loop end to end using a mock agent.
To plug in a real LLM-backed agent later, implement BaseAgent in
agent/base_agent.py (e.g. wire up OpenAI/Anthropic tool calling) and
swap it in below.
"""

import argparse
import os
import sys

import yaml


def positive_int(text):
    """
    argparse type for counts that must be >= 1.

    --repeat 0 used to run nothing and exit 0 — a green check from zero
    evidence, on a known-broken agent. Exactly the failure this project
    exists to catch, in its own CLI.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer")
    if value < 1:
        raise argparse.ArgumentTypeError(
            f"must be at least 1, got {value} — running zero times would report "
            f"success without testing anything")
    return value

sys.path.insert(0, os.path.dirname(__file__))

from agent.base_agent import MockAgent, AnthropicAgent
from agent.http_agent import HTTPAgent
from domains import DOMAINS, EXTERNAL_DOMAIN, PROMPT_VARIANTS, scenario_index
import history
import prover
import extractor
import approval
from prover import parse_headers
from runner.run_test import (run_scenario, print_report, load_scenario,
                             validate_scenario, validate_against_domain)

SCENARIO_INDEX = scenario_index()
SCENARIOS = {name: path for name, (_domain, path) in SCENARIO_INDEX.items()}


def resolve_scenario(args, parser):
    """
    Accept either a registered scenario name or a path to a scenario file.

    A file is the path a third party takes: they write a contract and run
    it, without first inventing a Python domain whose state and tools an
    external agent never uses.

    Returns (domain_name, scenario_path, domain).
    """
    if args.scenario in SCENARIO_INDEX:
        name, path = SCENARIO_INDEX[args.scenario]
        return name, path, DOMAINS[name]

    path = os.path.abspath(args.scenario)
    if not os.path.isfile(path):
        parser.error(
            f"'{args.scenario}' is neither a registered scenario nor a file that "
            f"exists. Registered: {', '.join(sorted(SCENARIO_INDEX))}")

    if args.agent == "http":
        # The agent brings its own environment; nothing to simulate here.
        return "external", path, EXTERNAL_DOMAIN
    if getattr(args, "domain", None):
        return args.domain, path, DOMAINS[args.domain]
    parser.error(
        f"{args.scenario} is a scenario file, so the harness needs a world to run "
        f"it in. Use --agent http (your agent supplies its own), or --domain "
        f"({'/'.join(DOMAINS)}) to run it against a bundled one.")


def _duplicate_names():
    """
    scenario_index() builds a flat name -> path dict, so two domains using
    the same scenario name would silently overwrite: no error, one scenario
    simply never runs again.
    """
    seen, dupes = {}, []
    for domain_name, domain in DOMAINS.items():
        for name in domain["scenarios"]:
            if name in seen:
                dupes.append((name, seen[name], domain_name))
            seen[name] = domain_name
    return dupes


def gather_candidates(args):
    """
    Run every requested source. Returns (vocab, candidates, notes, remarks, error).

    Shared by `extract`, which prints them, and `approve`, which walks a
    human through them. `error` is a fatal problem with the schema itself,
    where neither command can continue.
    """
    # notes  = a requested source failed; the measurement is incomplete.
    # remarks = the source ran and had something to say. Conflating the two
    #          made a deliberate model skip read as a broken run.
    vocab, candidates, notes, remarks = {}, [], [], []

    if args.tools_from:
        try:
            vocab = prover.load_tool_vocabulary(
                args.tools_from, parse_headers(args.agent_header))
        except Exception as exc:
            return {}, [], [], [], (
                f"could not read {args.tools_from}: {type(exc).__name__}: {exc}")
        candidates += extractor.from_tool_schemas(vocab)

    for path in args.implementation:
        found, err = extractor.from_implementations(path, vocab or None)
        if err:
            notes.append(f"   {err}")
        candidates += found

    if args.prompt:
        found, err, skips = extractor.from_system_prompt(
            args.prompt, vocab, args.model)
        if err:
            notes.append(f"   prompt: {err}")
        remarks += skips
        candidates += found

    if args.allowlist:
        if not vocab:
            notes.append("   allowlist: ignored — needs --tools-from to know "
                         "which tools exist")
        else:
            found, unknown = extractor.from_allowlist(vocab, args.allowlist.split(","))
            candidates += found
            if unknown:
                notes.append(f"   allowlist names tools the schema does not have: "
                             f"{unknown}")

    if getattr(args, "merge", False):
        candidates = extractor.merge(candidates)

    # Applied last, so it fills only what no source could. A tool with one
    # identity-shaped argument offers no choice; a tool with several holds a
    # decision that belongs to a human.
    candidates = extractor.complete_from_schema(candidates, vocab)
    return vocab, candidates, notes, remarks, None


def run_extract(args):
    """Propose candidates from whatever sources were supplied. Returns an exit code."""
    vocab, candidates, notes, remarks, error = gather_candidates(args)
    if error:
        print(error)
        return 2

    if args.benchmark:
        import glob
        reference = []
        pattern = os.path.join(args.benchmark, "*.y*ml")
        for path in sorted(glob.glob(pattern)):
            if path.endswith("TEMPLATE.yaml"):
                continue
            with open(path) as fh:
                doc = yaml.safe_load(fh)
            for rule in doc.get("behavioral_rules") or []:
                cond = rule.get("condition") or {}
                reference.append((doc.get("name", os.path.basename(path)),
                                  rule.get("type"),
                                  cond.get("tool") or cond.get("before_tool"),
                                  cond))
        if not reference:
            print(f"no contracts found in {args.benchmark}")
            return 2
        result = extractor.benchmark(candidates, reference)
        print(f"\nBENCHMARK against {args.benchmark}\n")

        # A recall figure is only a measurement of the sources that actually
        # ran. A missing API key made --prompt fail silently while the
        # benchmark still printed "invented: 0" and a confident conclusion
        # about which rule types are reachable — a score for a source that
        # never executed, which is worse than no score.
        if notes:
            print("  NOT A COMPLETE MEASUREMENT — a requested source failed:\n")
            for note in notes:
                print(f"  {note.strip()}")
            print("\n  The numbers below cover only the sources that ran. Fix the\n"
                  "  above and re-run before treating them as a result.\n")
        for remark in remarks:
            print(f"  {remark.strip()}")
        if remarks:
            print()

        print(extractor.render_benchmark(result, len(reference)))
        # Non-zero on an incomplete measurement, so a benchmark wired into CI
        # cannot go green on a run where a source silently did not execute.
        return 3 if notes else 0

    print(extractor.render(candidates, notes + remarks))
    return 0


def _show(candidate, index, total, scenario, vocab):
    """One proposal, with everything a decision needs and nothing else."""
    rule = candidate["rule"]
    cond = rule.get("condition") or {}
    subject = cond.get("tool") or cond.get("before_tool")
    other = cond.get("required_tool") or cond.get("permission_tool")

    phrasing = {
        "must_precede": f"{other} must precede {subject}",
        "must_ask_permission": f"{subject} needs {other} above the threshold",
        "must_never": f"{subject} must never be called",
    }.get(rule.get("type"), subject)

    print(f"\nPROPOSED  {index} of {total}")
    print(f"  Type     {rule.get('type')}")
    print(f"  Rule     {phrasing}")
    print(f"  Source   {candidate['source']}")
    print(f"  Evidence {candidate['confidence']} — {candidate['evidence']}")
    print(f"  prove    ok falsifiable")

    gap = approval.exercise_gap(rule, scenario)
    if gap:
        print(f"  REACH    {gap}")

    for problem in approval.conflicts_with(rule, scenario.get("behavioral_rules")):
        print(f"  CONFLICT {problem}")

    blocking = approval.blocking_placeholders(rule)
    for field in sorted(cond):
        value = cond[field]
        if field in blocking:
            options = approval.choices_for(field, rule, vocab)
            hint = " or ".join(options) if options else "no candidates in the schema"
            print(f"  Fields   {field}: {value}  <- needs you: {hint}")
        else:
            print(f"  Fields   {field}: {value!r}")
    return blocking


def _fill(rule, blocking, vocab, read):
    """Ask for each unanswered field. Returns False if the human backs out."""
    cond = rule["condition"]
    for field in blocking:
        options = approval.choices_for(field, rule, vocab)
        prompt = f"    {field} = "
        if options:
            prompt = f"    {field} ({' / '.join(options)}) = "
        answer = (read(prompt) or "").strip()
        if not answer:
            print("    left unanswered — cannot accept a rule with a placeholder")
            return False
        if options and answer not in options:
            print(f"    {answer!r} is not one of {options}; not accepting a guess")
            return False
        cond[field] = answer
    return True


def run_approve(args):
    """Walk a human through proposals one at a time. Returns an exit code."""
    if not os.path.isfile(args.into):
        print(f"{args.into} does not exist. Approval appends to a scenario that "
              f"already has a request and an exercised_when, because a rule "
              f"nothing exercises tests nothing.")
        return 2

    vocab, candidates, notes, _remarks, error = gather_candidates(args)
    if error:
        print(error)
        return 2
    if notes:
        # Accepting rules from a partial extraction bakes a gap into a file.
        print("A requested source failed, so the candidate list is incomplete:")
        for note in notes:
            print(f"  {note.strip()}")
        print("\nFix that and re-run. Approving from a partial list writes a "
              "suite that looks considered and is missing whatever that source "
              "would have found.")
        return 3

    with open(args.into) as fh:
        scenario = yaml.safe_load(fh) or {}

    reviewable, unverifiable = approval.screen(candidates, vocab)

    if unverifiable:
        print(f"\n{len(unverifiable)} proposal(s) withheld — could not be shown to "
              f"fail, so accepting one would add a contract that passes whatever "
              f"the agent does:\n")
        for candidate, reason in unverifiable:
            cond = candidate["rule"].get("condition") or {}
            subject = cond.get("tool") or cond.get("before_tool")
            print(f"  {candidate['rule_type']:20s} {subject}")
            print(f"      {reason}")

    if not reviewable:
        print("\nNothing to review.")
        return 0

    # Highest-confidence first: the ranking orders attention, and never
    # decides. There is no bulk accept and no auto-accept by design.
    order = {"high": 0, "medium": 1, "low": 2}
    reviewable.sort(key=lambda c: order.get(c["confidence"], 3))

    read = args._read if hasattr(args, "_read") else input
    accepted, rejected, skipped = [], 0, 0

    for i, candidate in enumerate(reviewable, 1):
        rule = {"type": candidate["rule"]["type"],
                "condition": dict(candidate["rule"].get("condition") or {})}
        blocking = _show(candidate, i, len(reviewable), scenario, vocab)

        while True:
            choice = (read("\n  [a]ccept  [e]dit  [r]eject  [s]kip  [?]sources > ")
                      or "").strip().lower()

            if choice in ("?", "sources"):
                for caveat in candidate["caveats"]:
                    print(f"    ! {caveat}")
                if not candidate["caveats"]:
                    print("    (no caveats recorded)")
                continue

            if choice in ("s", "skip", ""):
                skipped += 1
                break

            if choice in ("r", "reject"):
                reason = (read("    why? ") or "").strip()
                approval.log_rejection(candidate, reason)
                rejected += 1
                break

            if choice in ("a", "accept", "e", "edit"):
                if blocking and not _fill(rule, blocking, vocab, read):
                    continue
                if choice in ("e", "edit"):
                    print("    (edit fills the open fields; change anything else "
                          "in the file afterwards)")
                try:
                    rule_id = approval.accept(rule, candidate, args.into)
                except ValueError as exc:
                    print(f"    not written — {exc}")
                    continue
                # Re-read so conflict and id checks see what is now there.
                with open(args.into) as fh:
                    scenario = yaml.safe_load(fh) or {}
                accepted.append(rule_id)
                print(f"    written to {args.into} as '{rule_id}' (generated: true)")
                break

            print("    unrecognised — a, e, r, s or ?")

    print(f"\n{len(accepted)} accepted, {rejected} rejected, {skipped} skipped, "
          f"{len(unverifiable)} withheld.")
    if accepted:
        print(f"Every accepted rule is marked generated: true. Run "
              f"`python3 main.py prove {args.into}` before trusting the file.")

    patterns = approval.rejection_patterns()
    repeated = [p for p in patterns if p["count"] >= 3]
    if repeated:
        print("\nRepeatedly rejected proposal shapes — the heuristic behind these "
              "is probably wrong:")
        for p in repeated:
            print(f"  {p['count']}x  {p['rule_type']} from {p['source']}")
    return 0


def run_prove(args):
    """Check every contract is falsifiable. Returns a process exit code."""
    if args.path:
        path = os.path.abspath(args.path)
        if not os.path.isfile(path):
            print(f"{args.path} is not a file")
            return 2
        targets = [(os.path.basename(path), path)]
    else:
        wanted = getattr(args, "domain", None)
        targets = [(name, p) for name, (d, p) in SCENARIO_INDEX.items()
                   if not wanted or d == wanted]

    vocab = None
    if args.tools_from:
        try:
            vocab = prover.load_tool_vocabulary(
                args.tools_from, parse_headers(args.agent_header))
        except Exception as exc:
            print(f"could not read {args.tools_from}: {type(exc).__name__}: {exc}")
            return 2

    unfalsifiable = 0
    proven = 0

    for name, path in targets:
        try:
            with open(path) as fh:
                scenario = yaml.safe_load(fh)
        except Exception as exc:
            print(f"FAIL  {name}: could not parse — {type(exc).__name__}: {exc}")
            unfalsifiable += 1
            continue

        for rule_id, state, detail in prover.prove_scenario(scenario, vocab):
            if state == "can_fail":
                proven += 1
                print(f"  can fail   {name} · {rule_id}")
            else:
                unfalsifiable += 1
                label = {"cannot_fail": "CANNOT FAIL",
                         "misconfigured": "MISCONFIGURED",
                         "vocabulary_mismatch": "WRONG NAMES",
                         "unsupported": "UNSUPPORTED"}[state]
                print(f"  {label}  {name} · {rule_id}")
                print(f"             {detail}")

    print()
    if unfalsifiable:
        print(f"{unfalsifiable} rule(s) could not be shown to fail; {proven} proven.")
        print("A rule that cannot go red is not testing anything, however green "
              "the board looks.")
        return 1
    print(f"{proven} rule(s) proven falsifiable — every contract can go red.")
    if vocab is None:
        print("Checked against synthetic trajectories only. Pass --tools-from with "
              "your agent's schema to also confirm these rules name tools and "
              "arguments it actually emits.")
    return 0


def run_validate(args):
    """Static checks over contracts. Returns a process exit code."""
    targets = []
    if args.path:
        path = os.path.abspath(args.path)
        match = next(((d, p) for _n, (d, p) in SCENARIO_INDEX.items()
                      if os.path.abspath(p) == path), None)
        if match:
            targets.append((os.path.basename(path), match[0], path))
        elif args.domain:
            targets.append((os.path.basename(path), args.domain, path))
        else:
            # No domain: still worth checking the contract's own shape. Tool
            # and argument cross-checks need a world, so they are skipped and
            # said to be skipped rather than silently omitted.
            targets.append((os.path.basename(path), None, path))
    else:
        # --domain filters here, as it does for `prove`. It used to be
        # accepted and ignored in this mode, so `validate --domain banking`
        # silently checked the support suite too and reported a count for a
        # scope nobody asked for.
        wanted = getattr(args, "domain", None)
        targets = [(name, d, p) for name, (d, p) in SCENARIO_INDEX.items()
                   if not wanted or d == wanted]

    val_vocab = None
    if getattr(args, "tools_from", None):
        try:
            val_vocab = prover.load_tool_vocabulary(
                args.tools_from, parse_headers(args.agent_header))
        except Exception as exc:
            print(f"could not read {args.tools_from}: {type(exc).__name__}: {exc}")
            return 2

    total_errors = 0

    for name, domain_name, path in targets:
        errors = []
        try:
            scenario = load_scenario(path)
        except Exception as exc:
            errors = [f"could not parse: {type(exc).__name__}: {exc}"]
            scenario = None

        if scenario is not None:
            errors = validate_scenario(scenario)
            if not errors and val_vocab is not None:
                errors.extend(prover.check_scenario_vocabulary(scenario, val_vocab))
            # Cross-domain checks only make sense on a structurally sound
            # contract; running them on a broken one just adds noise.
            if not errors and domain_name:
                errors = validate_against_domain(scenario, DOMAINS[domain_name])

        if domain_name:
            label = f"[{domain_name}]"
        elif val_vocab is not None:
            label = "[structure + tool and argument names]"
        else:
            label = ("[structure only — pass --tools-from your agent's schema, or "
                     "--domain, to also check tool and argument names]")
        if errors:
            total_errors += len(errors)
            print(f"FAIL  {name}  {label}")
            for e in errors:
                print(f"        - {e}")
        else:
            print(f"ok    {name}  {label}")

    for dup_name, first, second in _duplicate_names():
        total_errors += 1
        print(f"FAIL  duplicate scenario name '{dup_name}' in domains "
              f"'{first}' and '{second}' — names must be unique across domains, "
              f"or one silently shadows the other")

    print()
    if total_errors:
        print(f"{total_errors} problem(s) found in {len(targets)} scenario(s).")
        return 1
    print(f"{len(targets)} scenario(s) valid.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Agent behavioral test runner (Phase 0 prototype)")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run a named scenario")
    run_parser.add_argument(
        "scenario",
        metavar="SCENARIO",
        help="A registered scenario name, or the path to your own scenario "
             f"file. Registered: {', '.join(sorted(SCENARIOS))}")
    run_parser.add_argument(
        "--behavior",
        choices=["correct", "buggy"],
        default="correct",
        help="Which mock agent behavior to test (only used with --agent mock; default: correct)",
    )
    run_parser.add_argument(
        "--agent",
        choices=["mock", "anthropic", "http"],
        default="mock",
        help="Which agent to test: 'mock' (scripted, no API key), 'anthropic' (Claude tool-calling), or 'http' (your own agent behind an endpoint — see --agent-url)",
    )
    run_parser.add_argument(
        "--agent-url",
        help="Endpoint for --agent http. Receives {scenario, turns}, returns {response, trajectory}. See agent/http_agent.py.",
    )
    run_parser.add_argument(
        "--agent-header", action="append", metavar="NAME:VALUE", default=[],
        help="Header to send with each request, repeatable. Use this for an auth "
             "token rather than putting it in the URL, where it lands in logs.",
    )
    run_parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20250929",
        help="Model id for --agent anthropic. Swap this to check whether a result generalizes across models.",
    )
    run_parser.add_argument(
        "--prompt-variant",
        choices=PROMPT_VARIANTS,
        default="default",
        help="System prompt to run the agent under. 'strict' names the rules explicitly, 'weak' gives no safety guidance, 'eager' applies realistic low-friction product pressure. Isolates model robustness from prompt engineering.",
    )

    run_parser.add_argument(
        "--domain",
        choices=list(DOMAINS),
        help="World to run a scenario FILE against. Unnecessary for registered "
             "scenarios, or with --agent http.",
    )
    run_parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=None,
        metavar="N",
        help="Parallel runs when using --repeat (default 6). Lower it if you hit rate limits.",
    )
    run_parser.add_argument(
        "--repeat",
        type=positive_int,
        default=1,
        metavar="N",
        help="Run the scenario N times under identical settings and report the spread of outcomes. Measures how flaky a scenario actually is rather than assuming.",
    )

    ex_parser = subparsers.add_parser(
        "extract",
        help="Read an agent and propose candidate contracts. Writes nothing.")
    ex_parser.add_argument("--tools-from", metavar="SCHEMA|URL",
                           help="Tool schema: the most reliable source.")
    ex_parser.add_argument("--implementation", metavar="FILE.py", action="append",
                           default=[], help="Tool implementation to scan for guards "
                                            "already enforced in code. Repeatable.")
    ex_parser.add_argument("--prompt", metavar="FILE",
                           help="System prompt. Needs ANTHROPIC_API_KEY; every "
                                "extracted rule must cite the line it came from.")
    ex_parser.add_argument("--allowlist", metavar="a,b,c",
                           help="Tools the agent may call. Everything else becomes "
                                "a flagged, low-confidence candidate.")
    ex_parser.add_argument("--agent-header", action="append", metavar="NAME:VALUE",
                           default=[], help="Header for fetching --tools-from.")
    ex_parser.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ex_parser.add_argument("--benchmark", metavar="DIR",
                           help="Score proposals against contracts already written "
                                "in DIR: what it finds, misses, and invents.")
    ex_parser.add_argument("--merge", action="store_true",
                           help="Combine candidates describing the same rule across "
                                "sources. A schema knows which arguments exist; a "
                                "prompt knows the thresholds and which tool approves. "
                                "Neither fills a condition alone.")

    ap_parser = subparsers.add_parser(
        "approve",
        help="Review extracted proposals one at a time and append the accepted "
             "ones to a scenario. Falsifiability is checked before a proposal is "
             "shown; placeholders block acceptance; nothing is bulk-accepted.")
    ap_parser.add_argument("--into", required=True, metavar="SCENARIO.yaml",
                           help="Existing scenario to append to. A rule its "
                                "exercised_when cannot reach would report "
                                "INCONCLUSIVE forever, so the target matters.")
    ap_parser.add_argument("--tools-from", metavar="SCHEMA|URL")
    ap_parser.add_argument("--implementation", action="append", default=[],
                           metavar="FILE.py")
    ap_parser.add_argument("--prompt", metavar="FILE")
    ap_parser.add_argument("--allowlist", metavar="a,b,c")
    ap_parser.add_argument("--agent-header", action="append", default=[],
                           metavar="NAME:VALUE")
    ap_parser.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ap_parser.add_argument("--merge", action="store_true", default=True,
                           help="On by default here: a human should see one "
                                "proposal per rule, not the same rule from each "
                                "source.")

    prove_parser = subparsers.add_parser(
        "prove",
        help="Prove each contract CAN fail. Synthesises a violating trajectory "
             "per rule and checks it trips. No agent, no API calls.")
    prove_parser.add_argument(
        "--domain", choices=list(DOMAINS),
        help="Prove only this domain's contracts. Useful when an endpoint serves "
             "one domain — proving others against its schema reports names it was "
             "never expected to have.",
    )
    prove_parser.add_argument(
        "--tools-from", metavar="SCHEMA.json",
        help="Your agent's tool schema. Without it, prove shows only that a rule "
             "is logically falsifiable — not that it matches the tool and argument "
             "names your agent actually emits.")
    prove_parser.add_argument(
        "--agent-header", action="append", metavar="NAME:VALUE", default=[],
        help="Header to send when fetching --tools-from over HTTP, repeatable. "
             "Use this for an auth token rather than the URL, where it lands in logs.",
    )
    prove_parser.add_argument(
        "path", nargs="?",
        help="Scenario file to prove. Omit to prove every registered scenario.")

    val_parser = subparsers.add_parser(
        "validate",
        help="Check contracts without running an agent. No API calls, no cost.")
    val_parser.add_argument(
        "path", nargs="?",
        help="Scenario file to check. Omit to check every registered scenario.")
    val_parser.add_argument(
        "--agent-header", action="append", metavar="NAME:VALUE", default=[],
        help="Header to send when fetching --tools-from over HTTP, repeatable. "
             "Use this for an auth token rather than the URL, where it lands in logs.",
    )
    val_parser.add_argument(
        "--tools-from", metavar="SCHEMA.json",
        help="Your agent's tool schema, for checking tool and argument names in "
             "HTTP mode where the harness has no local tool map.")
    val_parser.add_argument(
        "--domain", choices=list(DOMAINS),
        help="Domain to check against, for a file not in the registry.")

    args = parser.parse_args()

    if args.command == "validate":
        sys.exit(run_validate(args))

    if args.command == "prove":
        sys.exit(run_prove(args))

    if args.command == "extract":
        sys.exit(run_extract(args))

    if args.command == "approve":
        sys.exit(run_approve(args))

    if args.command != "run":
        parser.print_help()
        return

    domain_name, scenario_path, domain = resolve_scenario(args, parser)

    def build_agent():
        if args.agent == "anthropic":
            return AnthropicAgent(model=args.model, prompt_variant=args.prompt_variant)
        if args.agent == "http":
            if not args.agent_url:
                parser.error("--agent http requires --agent-url")
            a = HTTPAgent(args.agent_url, headers=parse_headers(args.agent_header, parser))
            a._scenario_name = args.scenario
            return a
        return MockAgent(behavior=args.behavior, scenario=args.scenario)

    config = history.config_of(args, concurrency=1)

    if args.repeat == 1:
        result = run_scenario(scenario_path, build_agent(), domain=domain)
        history.record(result['scenario_name'], domain_name, config, result)
        print_report(result)
        if args.agent != "mock":
            print("  Single run — green here means green once. Use --repeat N "
                  "for a variance bound.\n")
        # Accumulated evidence, so a lone run still benefits from every
        # prior one rather than starting blind.
        summary = history.summarize(
            result["scenario_name"], history.config_of(args, result.get("agent_version"), concurrency=1))
        if summary["n"] > 1:
            print("HISTORY")
            print(history.format_summary(
                args.scenario, config, summary,
                min_rate=load_scenario(scenario_path).get("min_exercise_rate")))
            print()
        sys.exit(0 if result["overall_pass"] else 1)

    # Repeat mode: identical settings each time, so any variation in the
    # outcome is the agent's sampling, not the configuration. Runs go through
    # a thread pool — each builds its own agent, state and gateway, and the
    # wall time is otherwise dominated by network wait.
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor

    statuses, trajectories = [], set()
    observed_version = None
    scenario_key = args.scenario
    if args.agent == "mock":
        workers = 1
    elif args.concurrency is None:
        workers = 1 if args.agent == "http" else 6
    else:
        workers = max(1, args.concurrency)
    print(f"Running {args.repeat}x with concurrency {workers}...")

    def one_run(_i):
        return run_scenario(scenario_path, build_agent(), domain=domain)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, result in enumerate(pool.map(one_run, range(args.repeat)), 1):
            statuses.append(result["status"])
            observed_version = result.get("agent_version")
            scenario_key = result["scenario_name"]
            history.record(result['scenario_name'], domain_name, config, result)
            trajectories.add(history.trajectory_hash(result["trajectory"]))
            print(f"  run {i}/{args.repeat}: {result['status']} "
                  f"({len(result['trajectory'])} tool calls)")

    print(f"\n{'=' * 62}")
    print(f"REPEATED {args.repeat}x — {scenario_key}\n")
    summary = history.summarize(
        scenario_key, history.config_of(args, observed_version, concurrency=workers))
    min_rate = load_scenario(scenario_path).get("min_exercise_rate")
    print(history.format_summary(scenario_key, config, summary, statuses, min_rate))
    print("=" * 62)
    sys.exit(0 if all(s == "PASS" for s in statuses) else 1)


if __name__ == "__main__":
    main()

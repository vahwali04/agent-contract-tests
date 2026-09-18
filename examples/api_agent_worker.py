"""
One agent run, one process, trajectory on stdout.

Spawned per request by api_agent_server.py. Deliberately a separate process
rather than an in-process call: the contention being tested came from an
endpoint forking per request, and an in-process shortcut would test a
different thing.

Prints {"response", "trajectory", "version"} as JSON. `--fake` skips the
API entirely so the plumbing can be exercised without a key.
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domains import DOMAINS, scenario_index
from runner.run_test import load_scenario
from tools.gateway import ToolGateway

FAKE_TRAJECTORY = [
    {"tool": "get_ticket", "args": {"ticket_id": "T-1001"}},
    {"tool": "verify_identity", "args": {"customer_id": "dana"}},
    {"tool": "issue_refund",
     "args": {"ticket_id": "T-1001", "customer_id": "dana", "amount": 120}},
]


def version_for(model, prompt_variant):
    """Stable per-configuration, so history doesn't pool across settings."""
    digest = hashlib.sha256(f"{model}|{prompt_variant}".encode()).hexdigest()[:8]
    return f"{prompt_variant}-{digest}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="")
    ap.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ap.add_argument("--prompt-variant", default="strict")
    ap.add_argument("--fake", action="store_true")
    args = ap.parse_args()

    version = version_for(args.model, args.prompt_variant)

    if args.fake:
        # Brief sleep so concurrent requests genuinely overlap — without it
        # the processes finish too fast to contend and the test would say
        # nothing about the condition it exists to measure.
        time.sleep(0.4)
        print(json.dumps({"response": "(fake)", "trajectory": FAKE_TRAJECTORY,
                          "version": f"fake-{version}"}))
        return

    index = scenario_index()
    if args.scenario not in index:
        print(json.dumps({"response": "", "trajectory": [],
                          "error": f"unknown scenario {args.scenario!r}"}))
        return

    domain_name, path = index[args.scenario]
    domain = DOMAINS[domain_name]
    scenario = load_scenario(path)

    from agent.base_agent import AnthropicAgent

    state = domain["make_state"]()
    gateway = ToolGateway(domain["make_tools"](state))
    agent = AnthropicAgent(model=args.model, prompt_variant=args.prompt_variant)
    agent.bind_domain(domain)

    if "turns" in scenario:
        response = agent.handle_conversation(scenario["turns"], gateway)
    else:
        response = agent.handle_request(scenario["request"], gateway)

    print(json.dumps({
        "response": response,
        "trajectory": [{"tool": c["tool"], "args": c["args"]}
                       for c in gateway.get_trajectory()],
        "version": version,
    }))


if __name__ == "__main__":
    main()

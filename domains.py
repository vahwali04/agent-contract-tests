"""
Domain registry.

A domain supplies everything world-specific: how to build its simulated
state, the tool bindings the gateway dispatches through, the tool schemas a
real LLM agent sees, its system-prompt variants, its scenarios, and its
scripted mock trajectories.

The Tool Gateway, evaluator, and runner take all of this as input and know
nothing about any particular world.
"""

import os

from environment.state import EnvironmentState
from agent.base_agent import ANTHROPIC_TOOLS, SYSTEM_PROMPTS
from examples.support.state import SupportState
from examples.support.tools import make_tools as make_support_tools, SUPPORT_TOOLS, SUPPORT_SYSTEM_PROMPTS
from examples.support.mock_agent import MOCK_HANDLERS as SUPPORT_MOCK_HANDLERS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Prompt-variant keys every domain must define, so the prompt-sensitivity and
# single-clause regression experiments run identically across domains.
PROMPT_VARIANTS = [
    "strict",
    "default",
    "weak",
    "eager",
    "strict_no_cumulative",
    "strict_no_injection_guard",
]


def _make_banking_tools(env):
    return {
        "get_user": env.get_user,
        "authenticate": env.authenticate,
        "transfer_money": env.transfer_money,
        "delete_account": env.delete_account,
        "request_permission": env.request_permission,
        "read_support_notes": env.read_support_notes,
    }


DOMAINS = {
    "banking": {
        "make_state": EnvironmentState,
        "make_tools": _make_banking_tools,
        "tool_schemas": ANTHROPIC_TOOLS,
        "system_prompts": SYSTEM_PROMPTS,
        # Banking's scripted trajectories still live as MockAgent methods, so
        # no handler table here — MockAgent falls back to _handle_<scenario>.
        "mock_handlers": None,
        "scenarios": {
            "transfer_test": "scenarios/transfer_without_auth.yaml",
            "no_account_deletion": "scenarios/no_account_deletion.yaml",
            "large_transfer_permission": "scenarios/large_transfer_permission.yaml",
            "split_transfer_evades_permission": "scenarios/split_transfer_evades_permission.yaml",
            "social_engineering_skip_auth": "scenarios/social_engineering_skip_auth.yaml",
            "prompt_injection_via_tool_data": "scenarios/prompt_injection_via_tool_data.yaml",
            "subtle_injection_delayed": "scenarios/subtle_injection_delayed.yaml",
            "combined_social_engineering_structuring": "scenarios/combined_social_engineering_structuring.yaml",
        },
    },
    "support": {
        "make_state": SupportState,
        "make_tools": make_support_tools,
        "tool_schemas": SUPPORT_TOOLS,
        "system_prompts": SUPPORT_SYSTEM_PROMPTS,
        "mock_handlers": SUPPORT_MOCK_HANDLERS,
        "scenarios": {
            "refund_baseline": "examples/support/scenarios/refund_baseline.yaml",
            "no_data_deletion": "examples/support/scenarios/no_data_deletion.yaml",
            "refund_hard_cap": "examples/support/scenarios/refund_hard_cap.yaml",
            "large_refund_permission": "examples/support/scenarios/large_refund_permission.yaml",
            "split_refund_evades_permission": "examples/support/scenarios/split_refund_evades_permission.yaml",
            "social_engineering_skip_verification": "examples/support/scenarios/social_engineering_skip_verification.yaml",
            "prompt_injection_via_kb": "examples/support/scenarios/prompt_injection_via_kb.yaml",
        },
    },
}


class _ExternalState:
    """
    Placeholder world for agents that bring their own.

    With --agent http the agent has already executed against its own test
    environment and returns the trajectory, so the harness records rather
    than dispatches: there is nothing here for it to simulate. Exists so a
    scenario can run without inventing a Python domain whose state, tools
    and prompts would all go unused.
    """

    def snapshot(self):
        return {}


EXTERNAL_DOMAIN = {
    "make_state": _ExternalState,
    "make_tools": lambda _state: {},
    "tool_schemas": [],
    "system_prompts": {},
    "mock_handlers": None,
    "scenarios": {},
}


def scenario_index(domain_filter=None):
    """
    Flat {scenario_name: (domain_name, absolute_path)} across domains.
    Scenario names are unique across domains, so a bare name is enough for
    the CLI.
    """
    index = {}
    for domain_name, domain in DOMAINS.items():
        if domain_filter and domain_name != domain_filter:
            continue
        for scenario_name, rel_path in domain["scenarios"].items():
            index[scenario_name] = (domain_name, os.path.join(BASE_DIR, rel_path))
    return index

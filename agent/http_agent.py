"""
HTTPAgent — test an agent that isn't written in Python and isn't ours.

The built-in AnthropicAgent calls the Claude API directly with this repo's
tool schemas, which is fine for the bundled examples and useless for
anyone else. Most real agents are somebody's LangChain app, a TypeScript
service, a Go binary, or an OpenAI-based loop. This adapter is the general
way in: your agent exposes one HTTP endpoint, and the harness treats it as
a black box.

Contract
--------
The harness POSTs JSON:

    {
      "scenario": "refund_baseline",
      "turns": ["first user message", "second user message"]
    }

Your endpoint runs the agent against a *test* environment and replies:

    {
      "response": "final assistant text",
      "trajectory": [
        {"tool": "verify_identity", "args": {"customer_id": "dana"}},
        {"tool": "issue_refund",
         "args": {"ticket_id": "T-1001", "customer_id": "dana", "amount": 120},
         "result": {"status": "ok"}}
      ]
    }

`result` is optional and only shown in reports — rules are evaluated over
tool names and arguments, so omitting it costs nothing.

**Report failures explicitly.** If your agent could not run, return a
non-200, or include an `error` field:

    {"error": "agent process exited 1: <reason>", "trajectory": []}

Either becomes ERRORED, which is excluded from every estimate. Returning
200 with an empty trajectory instead is indistinguishable from an agent
that deliberately did nothing, so the harness reports INCONCLUSIVE and the
real cause never surfaces. A team lost most of a CI sweep this way, with
the failure invisible by construction.

Optionally advertise your tools, either at `GET /tools` or as a `tools`
field on this response:

    {"tools": [{"name": "create_draft",
                "params": ["to", "threadId", "replyToMessageId", "body"]}]}

`GET /tools` is what `main.py validate --tools-from <url>` reads, so a
contract watching `messageId` on a tool that takes `replyToMessageId` is
caught before anything runs rather than as a mysterious red. Returning the
same list on the response additionally catches drift after validation.

Optionally include a `version` string:

    {"response": "...", "trajectory": [...], "version": "prompt-v7+gpt-4o"}

It participates in the history key, so accumulated variance and coverage
estimates don't silently pool across agent versions. Without it, the same
URL serving a redeployed agent looks identical to the ledger — which is
the one thing endpoint-keying alone cannot detect. Anything stable per
deployment works: a git SHA, a prompt hash, a semver.

Two things to get right on your side:

  1. **Tool names and argument names must match your contracts exactly.**
     They are the only thing the evaluator sees. `verify_identity` and
     `verifyIdentity` are different tools as far as a rule is concerned.
  2. **Point it at a test environment, not production.** The harness tells
     an agent to move money and delete accounts. It is asking your agent
     to try; make sure the tools behind it are simulated.

Because your agent already executed against its own environment, the
harness records the returned calls rather than re-running them — the
report shows what your agent actually saw, not what our fixtures would
have returned. The domain's own state and tools go unused for HTTP agents;
only its scenarios and rules apply.
"""

import json
import urllib.error
import urllib.request

from agent.base_agent import BaseAgent


class HTTPAgentError(RuntimeError):
    """Raised for a malformed or unreachable endpoint. Surfaces as ERRORED."""


class HTTPAgent(BaseAgent):
    def __init__(self, url, timeout=60, headers=None):
        self.url = url
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self._scenario_name = None
        # Set from the last response, if the endpoint reports one.
        self.agent_version = None
        # Set if the endpoint advertises its tools on the response. Lets the
        # runner catch a contract that drifted from the agent's real
        # signature after it was last validated.
        self.advertised_tools = None

    def bind_domain(self, domain):
        # Nothing to bind: the agent owns its own tools and prompt. Stated
        # explicitly so it's clear this isn't an oversight.
        pass

    def handle_request(self, request, gateway):
        return self.handle_conversation([request], gateway)

    def handle_conversation(self, turns, gateway):
        payload = json.dumps({
            "scenario": self._scenario_name,
            "turns": list(turns),
        }).encode()

        req = urllib.request.Request(self.url, data=payload, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise HTTPAgentError(
                f"agent endpoint returned HTTP {e.code}: {e.read().decode()[:200]}"
            ) from e
        except urllib.error.URLError as e:
            raise HTTPAgentError(f"could not reach agent endpoint {self.url}: {e.reason}") from e
        except json.JSONDecodeError as e:
            raise HTTPAgentError(f"agent endpoint returned non-JSON: {e}") from e

        # An endpoint that reports a failure in the body must not be read as
        # an agent that chose not to act. A team's bridge always returned 200
        # and fell through to an empty trajectory when its subprocess died —
        # the harness called that INCONCLUSIVE, which is "nothing was tested"
        # rather than "the transport broke", and the real cause stayed
        # invisible for a whole sweep.
        if isinstance(body, dict) and body.get("error"):
            raise HTTPAgentError(
                f"agent endpoint reported an error: {str(body['error'])[:300]}")

        if not isinstance(body, dict) or "trajectory" not in body:
            raise HTTPAgentError(
                "agent endpoint must return an object with a 'trajectory' key; "
                f"got: {str(body)[:200]}"
            )

        trajectory = body["trajectory"]
        if not isinstance(trajectory, list):
            raise HTTPAgentError("'trajectory' must be a list of tool calls")

        for i, call in enumerate(trajectory):
            if not isinstance(call, dict) or "tool" not in call:
                raise HTTPAgentError(
                    f"trajectory[{i}] must be an object with a 'tool' key; got: {call!r}"
                )
            args = call.get("args", {})
            if not isinstance(args, dict):
                raise HTTPAgentError(f"trajectory[{i}]['args'] must be an object")
            gateway.record(call["tool"], args, call.get("result"))

        if body.get("tools"):
            from prover import parse_tool_vocabulary
            try:
                self.advertised_tools = parse_tool_vocabulary(body["tools"])
            except Exception:
                self.advertised_tools = None

        version = body.get("version")
        if version is not None:
            self.agent_version = str(version)

        return body.get("response", "")

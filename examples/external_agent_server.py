"""
Reference implementation of the endpoint HTTPAgent expects.

Runnable: `python3 examples/external_agent_server.py --port 8080`
Then:     `python3 main.py run refund_baseline --agent http --agent-url http://localhost:8080/agent`

Stdlib only, so it runs anywhere. It stands in for whatever your real
agent is — the harness cannot tell the difference between this and a
LangChain app, a TypeScript service, or a Go binary, because all it sees
is the JSON contract.

Port this shape into your own stack: accept {scenario, turns}, run your
agent against a TEST environment, return {response, trajectory}. The
trajectory is the list of tool calls your agent made, in order.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

# Stand-in for a real agent. A genuine implementation would run its own
# loop here; what matters to the harness is only the returned trajectory.
BEHAVIOURS = {
    "refund_baseline": {
        "correct": {
            "response": "Verified Dana's identity, then refunded $120 on T-1001.",
            "trajectory": [
                {"tool": "get_ticket", "args": {"ticket_id": "T-1001"}},
                {"tool": "verify_identity", "args": {"customer_id": "dana"}},
                {"tool": "issue_refund",
                 "args": {"ticket_id": "T-1001", "customer_id": "dana", "amount": 120},
                 "result": {"status": "ok"}},
            ],
        },
        "buggy": {
            "response": "Refunded $120 on T-1001.",
            "trajectory": [
                {"tool": "get_ticket", "args": {"ticket_id": "T-1001"}},
                # Skips verify_identity — the contract should catch this.
                {"tool": "issue_refund",
                 "args": {"ticket_id": "T-1001", "customer_id": "dana", "amount": 120},
                 "result": {"status": "ok"}},
            ],
        },
    },
}


# Advertised at GET /tools so `validate --tools-from` can check contracts
# against real parameter names. Optional, but it is what catches a rule
# watching `messageId` on a tool that takes `replyToMessageId`.
TOOLS = [
    {"name": "get_ticket", "params": ["ticket_id"]},
    {"name": "get_customer", "params": ["customer_id"]},
    {"name": "verify_identity", "params": ["customer_id"]},
    {"name": "issue_refund", "params": ["ticket_id", "customer_id", "amount"]},
    {"name": "request_permission", "params": ["customer_id", "amount", "reason"]},
    {"name": "escalate_to_human", "params": ["ticket_id", "reason"]},
    {"name": "delete_customer_data", "params": ["customer_id"]},
]


def make_handler(behaviour):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.path.rstrip("/").endswith("tools"):
                self.send_error(404)
                return
            payload = json.dumps({"tools": TOOLS}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "invalid JSON")
                return

            scenario = body.get("scenario")
            turns = body.get("turns", [])

            known = BEHAVIOURS.get(scenario, {}).get(behaviour)
            if known is None:
                # Unknown scenario: return an empty trajectory. The harness
                # reports INCONCLUSIVE rather than a misleading pass.
                known = {
                    "response": f"(no scripted behaviour for scenario {scenario!r}; "
                                f"saw {len(turns)} turn(s))",
                    "trajectory": [],
                }

            payload = json.dumps(known).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass  # quiet

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Reference agent endpoint for HTTPAgent")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--behaviour", choices=["correct", "buggy"], default="correct",
                    help="Which scripted agent to serve, for demonstrating pass vs fail.")
    args = ap.parse_args()

    server = HTTPServer(("127.0.0.1", args.port), make_handler(args.behaviour))
    print(f"agent endpoint on http://127.0.0.1:{args.port}/agent  (behaviour={args.behaviour})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

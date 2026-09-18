"""
Subprocess-backed agent endpoint — runs anywhere, no tunnel.

Exists to close one open claim: "a dedicated CI runner would never see the
contention we saw on a laptop". Three attempts to test that failed because
the agent under test needed an authenticated local session, so it had to be
tunnelled into Actions, and a free quick-tunnel died every time.

An API-backed agent removes the tunnel entirely. It needs only a key, so it
can run *on the runner*, and the harness talks to localhost.

The shape matters more than the agent. The reported contention came from an
endpoint spawning one process per request — three at once fighting on one
machine. So this spawns a real subprocess per request rather than answering
in-process, reproducing the condition under test rather than a convenient
approximation of it.

    python3 examples/api_agent_server.py --port 8080            # real API calls
    python3 examples/api_agent_server.py --port 8080 --fake     # no key needed

`--fake` returns a canned trajectory after a short sleep. It exercises the
whole path — HTTP, subprocess spawn, JSON contract — without spending
tokens, so the plumbing can be verified before anything is dispatched.
"""

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TOOLS = [
    {"name": "get_ticket", "params": ["ticket_id"]},
    {"name": "get_customer", "params": ["customer_id"]},
    {"name": "verify_identity", "params": ["customer_id"]},
    {"name": "issue_refund", "params": ["ticket_id", "customer_id", "amount"]},
    {"name": "request_permission", "params": ["customer_id", "amount", "reason"]},
    {"name": "escalate_to_human", "params": ["ticket_id", "reason"]},
    {"name": "delete_customer_data", "params": ["customer_id"]},
    {"name": "search_knowledge_base", "params": ["query"]},
]


def summarise_failure(stderr):
    """
    Lead with the exception, not the traceback tail.

    Returning stderr[-500:] put the middle of a traceback in the report and
    cut the exception line off entirely, so a live CI run failed with no
    indication of why. The last non-empty line of a Python traceback is the
    exception; that is the part worth surfacing.
    """
    lines = [line for line in (stderr or "").strip().splitlines() if line.strip()]
    if not lines:
        return "worker failed with no output"
    exception = lines[-1]
    context = " | ".join(lines[-4:-1])
    return f"{exception}" + (f"   [context: {context}]" if context else "")


def make_handler(fake, model, prompt_variant):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.path.rstrip("/").endswith("tools"):
                self.send_error(404)
                return
            self._json({"tools": TOOLS})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length).decode() or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "invalid JSON")
                return

            cmd = [sys.executable, os.path.join(REPO, "examples", "api_agent_worker.py"),
                   "--scenario", str(body.get("scenario") or ""),
                   "--model", model, "--prompt-variant", prompt_variant]
            if fake:
                cmd.append("--fake")

            # One process per request: the condition under test.
            proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO,
                                  timeout=300)
            if proc.returncode != 0:
                self._json({"response": "", "trajectory": [],
                            "error": summarise_failure(proc.stderr)}, status=500)
                return
            try:
                self._json(json.loads(proc.stdout))
            except json.JSONDecodeError:
                self._json({"response": "", "trajectory": [],
                            "error": f"worker returned non-JSON: {proc.stdout[:200]}"},
                           status=500)

        def _json(self, doc, status=200):
            payload = json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--fake", action="store_true",
                    help="Canned trajectories, no API key and no tokens spent.")
    ap.add_argument("--model", default="claude-sonnet-4-5-20250929")
    ap.add_argument("--prompt-variant", default="strict")
    args = ap.parse_args()

    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(args.fake, args.model, args.prompt_variant))
    mode = "fake" if args.fake else f"live ({args.model})"
    print(f"agent endpoint on http://127.0.0.1:{args.port}  [{mode}, "
          f"one subprocess per request]", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

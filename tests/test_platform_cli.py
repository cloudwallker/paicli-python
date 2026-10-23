from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import unittest

from test_platform_queries import RUN, frame


@contextmanager
def platform_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), self.headers.get("Last-Event-ID")))
            if self.headers.get("Authorization") != "Bearer example-token":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b"do-not-echo-response")
                return
            responses = {"/api/me": {"tenant_id": "demo", "role": "viewer"},
                         "/api/quota": {"used_bytes": 3, "limit_bytes": 100, "remaining_bytes": 97},
                         "/api/runs": {"items": [{key: value for key, value in RUN.items() if key in {"id", "name", "status", "created_at", "updated_at", "error"}}]},
                         "/api/runs/run1": RUN}
            if self.path == "/api/runs/run1/events?follow=false":
                after = int(self.headers.get("Last-Event-ID", "0"))
                body = (frame(1) if after < 1 else "").encode()
                kind = "text/event-stream"
            else:
                body = json.dumps(responses[self.path]).encode()
                kind = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class PlatformCLITests(unittest.TestCase):
    def call(self, base_url, *arguments, token="example-token"):
        environment = {**os.environ, "FLOWTRAIL_API_TOKEN": token}
        return subprocess.run([sys.executable, "-m", "paicli", "platform", *arguments,
                               "--base-url", base_url, "--json"], env=environment,
                              capture_output=True, text=True, encoding="utf-8", timeout=10)

    def test_all_platform_commands_emit_json_and_events_replay_cursor(self):
        with platform_server() as (url, requests):
            for command, expected_key in [("me", "tenant_id"), ("quota", "remaining_bytes"), ("runs", "items"), ("run", "nodes"), ("events", "events")]:
                arguments = [command] + (["run1"] if command in {"run", "events"} else [])
                with self.subTest(command=command):
                    result = self.call(url, *arguments)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected_key, json.loads(result.stdout))
                    self.assertEqual(result.stderr, "")
            result = self.call(url, "events", "run1", "--after", "1")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"events": [], "cursor": 1})
        self.assertEqual(requests[-1], ("/api/runs/run1/events?follow=false", "Bearer example-token", "1"))

    def test_unauthorized_and_invalid_cursor_are_structured_errors_without_credentials(self):
        with platform_server() as (url, requests):
            result = self.call(url, "me", token="private-token")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["type"], "PlatformError")
            self.assertNotIn("private-token", result.stderr)
            self.assertNotIn("do-not-echo-response", result.stderr)
            self.assertEqual(result.stdout, "")
            result = self.call(url, "events", "run1", "--after", "-1")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["type"], "PlatformError")
        self.assertEqual(len(requests), 1)

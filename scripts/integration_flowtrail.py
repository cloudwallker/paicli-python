"""Run PaiCLI against an actual local FlowTrail server and temporary database.

Install both repositories' dependencies into this interpreter, then run:
python scripts/integration_flowtrail.py --flowtrail-src ../flowtrail-python/src

This is a separate integration gate, not part of the standalone unit-test suite.
It never contacts a live model: the inference node explicitly uses mock mode.
Tokens are created for this temporary database, passed through child-process
environments, and never included in the demonstration's JSON output.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def demo(flowtrail_src: Path) -> dict:
    paicli_src = Path(__file__).resolve().parents[1] / "src"
    require((flowtrail_src / "flowtrail" / "__main__.py").is_file(), "--flowtrail-src must contain the flowtrail package.")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("FLOWTRAIL_") and key != "OPENAI_API_KEY"}
    environment["PYTHONPATH"] = os.pathsep.join([str(paicli_src), str(flowtrail_src)])
    with tempfile.TemporaryDirectory(prefix="paicli-flowtrail-integration-") as temporary:
        environment["FLOWTRAIL_DB"] = str(Path(temporary) / "flowtrail.sqlite3")
        environment["FLOWTRAIL_QUOTA_BYTES"] = "1024"

        def bootstrap(tenant, role):
            result = subprocess.run([sys.executable, "-m", "flowtrail", "bootstrap", "--tenant", tenant, "--role", role],
                                    env=environment, capture_output=True, text=True, encoding="utf-8", timeout=15)
            require(result.returncode == 0, "FlowTrail bootstrap failed.")
            token = result.stdout.strip()
            require(bool(token) and all(33 <= ord(character) <= 126 for character in token), "Bootstrap did not return one access token.")
            return token

        admin = bootstrap("integration", "admin")
        viewer = bootstrap("integration", "viewer")
        other = bootstrap("isolated", "viewer")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen([sys.executable, "-m", "flowtrail", "serve", "--host", "127.0.0.1", "--port", str(port)],
                                  env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=base_url, timeout=2, trust_env=False, follow_redirects=False) as http:
                deadline = time.monotonic() + 15
                while True:
                    require(server.poll() is None, "FlowTrail exited before becoming healthy.")
                    try:
                        if http.get("/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    require(time.monotonic() < deadline, "FlowTrail health check timed out.")
                    time.sleep(0.05)

                def query(operation, *arguments, token=viewer, status=0):
                    result = subprocess.run([sys.executable, "-m", "paicli", "platform", operation, *arguments,
                                             "--base-url", base_url, "--json"],
                                            env={**environment, "FLOWTRAIL_API_TOKEN": token},
                                            capture_output=True, text=True, encoding="utf-8", timeout=15)
                    require(result.returncode == status, "PaiCLI returned an unexpected exit code for " + operation + ".")
                    stream = result.stdout if status == 0 else result.stderr
                    require(all(token not in stream for token in (admin, viewer, other)), "A temporary credential appeared in command output.")
                    if status:
                        require(not result.stdout, "Failed query emitted a successful JSON result.")
                    else:
                        require(not result.stderr, "Successful query emitted an error.")
                    try:
                        return json.loads(stream)
                    except ValueError:
                        raise RuntimeError("PaiCLI did not return parseable JSON.") from None

                identity = query("me")
                require(identity["tenant_id"] == "integration" and identity["role"] == "viewer", "Tenant identity or viewer role mismatch.")
                quota_before = query("quota")
                require(quota_before == {"used_bytes": 0, "limit_bytes": 1024, "remaining_bytes": 1024}, "Initial quota mismatch.")
                require(query("runs")["items"] == [], "Temporary database contained unexpected runs.")

                definition = {"workflow": {"name": "PaiCLI and FlowTrail integration", "nodes": [
                    {"id": "greeting", "type": "text", "config": {"template": "hello ${inputs.name}"}},
                    {"id": "summary", "type": "inference", "depends_on": ["greeting"],
                     "config": {"mode": "mock", "prompt": "summarize ${greeting.output}"}},
                ]}, "inputs": {"name": "平台"}}
                headers = {"Authorization": "Bearer " + admin, "Idempotency-Key": "integration-submission"}
                submitted = http.post("/api/runs", json=definition, headers=headers)
                require(submitted.status_code == 202, "Workflow submission failed.")
                run_id = submitted.json()["id"]
                replayed = http.post("/api/runs", json=definition, headers=headers)
                require(replayed.status_code == 202 and replayed.json()["id"] == run_id, "Idempotent submission created another run.")
                deadline = time.monotonic() + 15
                while True:
                    detail = http.get("/api/runs/" + run_id, headers=headers).json()
                    if detail["status"] in {"succeeded", "failed", "interrupted"}:
                        break
                    require(time.monotonic() < deadline, "Workflow execution timed out.")
                    time.sleep(0.05)
                require(detail["status"] == "succeeded", "Workflow did not succeed.")

                uploaded = http.put("/api/objects/integration.txt", content=b"demo", headers=headers)
                require(uploaded.status_code == 201, "Object upload failed.")
                quota_after = query("quota")
                require(quota_after == {"used_bytes": 4, "limit_bytes": 1024, "remaining_bytes": 1020}, "Quota did not reflect the committed object.")
                listed = query("runs")
                require([item["id"] for item in listed["items"]] == [run_id], "CLI run listing mismatch.")
                viewed = query("run", run_id)
                require(viewed["status"] == "succeeded" and viewed["nodes"][0]["output"] == "hello 平台", "CLI run detail did not preserve node output.")
                require(viewed["nodes"][1]["output"] == "[mock] summarize hello 平台", "Explicit mock inference output mismatch.")

                history = query("events", run_id)
                require(len(history["events"]) == 7, "Workflow event history was incomplete.")
                require(history["events"][0]["type"] == "RUN_QUEUED" and history["events"][-1]["type"] == "RUN_SUCCEEDED", "Event history endpoints mismatch.")
                require(history["cursor"] == history["events"][-1]["seq"], "History cursor mismatch.")
                middle = history["events"][2]["seq"]
                tail = query("events", run_id, "--after", str(middle))
                require(tail["events"] == history["events"][3:], "Replay did not honor Last-Event-ID.")
                empty = query("events", run_id, "--after", str(history["cursor"]))
                require(empty == {"events": [], "cursor": history["cursor"]}, "Empty replay did not retain the cursor.")

                denied = http.post("/api/runs", json=definition, headers={"Authorization": "Bearer " + viewer})
                require(denied.status_code == 403, "Viewer unexpectedly submitted a workflow.")
                require("401" in query("me", token="invalid-example-token", status=2)["error"], "Invalid credentials were not rejected.")
                require("404" in query("run", "missing", status=2)["error"], "Missing run was not reported.")
                require("404" in query("run", run_id, token=other, status=2)["error"], "Another tenant read the run.")
                require("404" in query("events", run_id, token=other, status=2)["error"], "Another tenant read events.")
                return {"ok": True, "mode": "live-local-flowtrail", "inference": "explicit-mock",
                        "commands": ["me", "quota", "runs", "run", "events"], "event_count": len(history["events"]),
                        "checks": ["bootstrap", "identity", "quota-before-and-after-object", "idempotent-submit",
                                   "executed-node-outputs", "sse-history", "cursor-replay", "empty-replay",
                                   "viewer-write-denied", "invalid-token-401", "missing-run-404", "tenant-isolation-404"],
                        "temporary_database": True, "credentials_logged": False}
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flowtrail-src", type=Path, required=True, help="Directory containing the real FlowTrail Python package")
    args = parser.parse_args()
    try:
        print(json.dumps(demo(args.flowtrail_src.resolve()), ensure_ascii=True, indent=2))
        return 0
    except (RuntimeError, OSError, httpx.HTTPError, subprocess.SubprocessError, KeyError, ValueError):
        # Never dump captured bootstrap output, request headers, or credentials.
        print(json.dumps({"ok": False, "error": "FlowTrail integration failed; inspect the local assertions and dependency setup."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

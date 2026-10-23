"""argparse CLI. Successful commands emit one JSON object; errors go to stderr."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

from . import __version__
from .agent import Agent, Budget
from .errors import ApprovalDenied, BudgetExceeded, PaiCLIError, PolicyError
from .index import CodeIndex
from .models import OpenAIModel, ScriptedModel
from .plan import PlanRunner, validate_plan
from .platform import PlatformClient, doctor
from .storage import Store
from .tools import ApprovalRequest, ToolRegistry
from .workspace import Workspace


SAMPLE = '''class Billing:
    """Offline example only: an in-memory idempotency map, not durable storage."""
    def __init__(self):
        self.refunds = {}

    def refund_order(self, request_key, amount):
        if request_key not in self.refunds:
            self.refunds[request_key] = {"request_key": request_key, "amount": amount}
        return self.refunds[request_key]
'''


def load_json(path: str | Path):
    target = Path(path)
    if target.stat().st_size > 1024 * 1024:
        raise PolicyError("JSON input exceeds the 1 MiB limit.")
    try:
        return json.loads(target.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError):
        raise PolicyError("Input is not valid UTF-8 JSON.") from None


def interactive_approval(request: ApprovalRequest) -> bool:
    if not sys.stdin.isatty():
        return False
    # Escape terminal control characters in untrusted file content.
    diff = "".join(character if character in "\n\t" or (ord(character) >= 32 and ord(character) != 127) else repr(character)[1:-1] for character in request.diff)
    print("\nWRITE PREVIEW: " + request.path, file=sys.stderr)
    print(diff, file=sys.stderr)
    phrase = "approve " + request.fingerprint
    print("Exact change SHA-256: " + request.fingerprint, file=sys.stderr)
    print("Type '" + phrase + "' to approve this change; anything else denies it:", file=sys.stderr)
    # This human prompt is cooperative. Deadline is checked again before writing;
    # a blocked stdin read cannot be forcibly interrupted by asyncio.timeout.
    try:
        return input().strip() == phrase
    except EOFError:
        return False


def model_for(args, script=None):
    if args.provider == "scripted":
        path = script or getattr(args, "script", None)
        if not path:
            raise PolicyError("Scripted provider requires --script (or a task script).")
        return ScriptedModel(load_json(path))
    return OpenAIModel(args.base_url, args.model or "", os.environ.get("OPENAI_API_KEY", ""))


def audit(workspace: Workspace) -> dict:
    files, truncated = workspace.files(limit=5000)
    findings = []
    if os.name != "nt":
        for name in files:
            mode = workspace.resolve(name).stat().st_mode
            if mode & stat.S_IWOTH:
                findings.append({"path": name, "finding": "world_writable", "mode": oct(stat.S_IMODE(mode))})
    return {"checked_files": len(files), "findings": findings, "truncated": truncated, "platform": os.name, "scope": "POSIX world-write mode audit; no Windows ACL inspection" if os.name == "nt" else "POSIX world-write mode audit", "mutations": False}


def _call(name, arguments, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


async def demo() -> dict:
    """Run real file tools, index, ledger, and DAG in an ephemeral workspace."""
    with tempfile.TemporaryDirectory(prefix="paicli-demo-") as temporary:
        root = Path(temporary)
        (root / "billing.py").write_text(SAMPLE, encoding="utf-8")
        workspace = Workspace(root)
        store = Store(workspace)
        try:
            indexed = CodeIndex(workspace, store).build()
            tools = ToolRegistry(workspace, store=store)
            model = ScriptedModel([
                _call("search_code", {"query": "refund_order request_key"}, "search1"),
                _call("read_file", {"path": "billing.py"}, "read1"),
                {"role": "assistant", "content": "billing.py 的 refund_order 使用 request_key 查询进程内字典，重复请求返回已有结果；进程重启后记录会丢失。该结论是显式脚本回复，用于验证工具链。"},
            ])
            result = await Agent(model, tools, store).run("Inspect refund idempotency", "offline-demo")
            denied = False
            try:
                tools.execute("write_file", {"path": "unapproved.txt", "content": "must not be written"})
            except ApprovalDenied:
                denied = not (root / "unapproved.txt").exists()
            async def worker(task, dependencies):
                if task["id"] == "inspect":
                    reply = _call("read_file", {"path": "billing.py"}, "read-plan")
                elif task["id"] == "search":
                    reply = _call("search_code", {"query": "request_key"}, "search-plan")
                else:
                    return {"dependencies": sorted(dependencies), "status": "completed"}
                worker_model = ScriptedModel([reply, {"role": "assistant", "content": "Offline task completed with actual tool evidence."}])
                return await Agent(worker_model, ToolRegistry(workspace, store=store, read_only=True), store).run(task["goal"])
            plan_result = await PlanRunner(worker).run({"tasks": [{"id": "inspect", "goal": "Read the implementation"}, {"id": "search", "goal": "Locate request_key"}, {"id": "summary", "goal": "Join evidence", "depends_on": ["inspect", "search"]}]})
            return {"ok": True, "mode": "scripted-offline", "ephemeral_workspace": True, "index": indexed, "agent": result, "write_denied": denied, "plan": plan_result, "sessions": len(store.sessions())}
        finally:
            store.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="paicli", description="Python coding agent and AI platform operations CLI")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    def add(name, help_text, workspace=False):
        child = commands.add_parser(name, help=help_text)
        child.add_argument("--json", action="store_true", help="Emit JSON (also the default)")
        if workspace:
            child.add_argument("--workspace", "--project", default=".", help="Workspace directory")
        return child
    def model_options(child):
        child.add_argument("--provider", choices=["scripted", "openai"], default="scripted")
        child.add_argument("--base-url", default="https://api.openai.com/v1")
        child.add_argument("--model", help="Explicit remote model name")
        child.add_argument("--rounds", type=int, default=8)
        child.add_argument("--context-chars", type=int, default=24000)
    add("demo", "Run a deterministic offline demonstration")
    add("index", "Build AST chunks and SQLite FTS5 index", True)
    search = add("search", "Search the existing lexical code index", True)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    agent = add("agent", "Run a bounded tool-call agent", True)
    agent.add_argument("goal")
    model_options(agent)
    agent.add_argument("--script", help="JSON array of scripted assistant responses")
    agent.add_argument("--session", help="Persist/resume a named local session")
    agent.add_argument("--timeout", type=float, default=60)
    agent.add_argument("--interactive-approval", action="store_true", help="Show each exact write and ask for its full fingerprint; denied without a TTY")
    plan = add("plan", "Run read-only Agent workers as a bounded DAG", True)
    plan.add_argument("file")
    model_options(plan)
    plan.add_argument("--concurrency", type=int, default=2)
    plan.add_argument("--timeout", type=float, default=120)
    add("sessions", "List local session metadata", True)
    add("audit", "Read-only POSIX permission audit (no chmod)", True)
    diagnostic = add("doctor", "Check DNS, TCP, and HTTP independently")
    diagnostic.add_argument("url", nargs="?", default="http://127.0.0.1:8000/healthz")
    diagnostic.add_argument("--timeout", type=float, default=3)
    platform = add("platform", "Read FlowTrail platform APIs")
    children = platform.add_subparsers(dest="operation", required=True)
    for operation, help_text in [("me", "Show authenticated tenant and role"),
                                 ("quota", "Show tenant object-storage quota"),
                                 ("runs", "List recent runs"),
                                 ("run", "Show one run and node outputs"),
                                 ("events", "Read bounded SSE event history and its replay cursor")]:
        command = children.add_parser(operation, help=help_text)
        command.add_argument("--base-url", default="http://127.0.0.1:8000")
        command.add_argument("--json", action="store_true")
        if operation in {"run", "events"}:
            command.add_argument("run_id")
        if operation == "events":
            command.add_argument("--after", type=int, default=0, help="Replay events after this sequence (Last-Event-ID)")
    return result


async def dispatch(args) -> dict | list:
    if args.command == "demo":
        return await demo()
    if args.command == "doctor":
        return await doctor(args.url, args.timeout)
    if args.command == "platform":
        client = PlatformClient(args.base_url, os.environ.get("FLOWTRAIL_API_TOKEN", ""))
        if args.operation == "run":
            return await client.run(args.run_id)
        if args.operation == "events":
            return await client.events(args.run_id, after=args.after)
        return await getattr(client, args.operation)()
    workspace = Workspace(args.workspace)
    if args.command == "audit":
        return audit(workspace)
    if args.command == "plan":
        definition = load_json(args.file)
        validate_plan(definition)  # Reject bad DAGs before starting workers.
    store = Store(workspace)
    try:
        if args.command == "index":
            return CodeIndex(workspace, store).build()
        if args.command == "search":
            return {"hits": CodeIndex(workspace, store).search(args.query, args.limit), "retrieval": "lexical"}
        if args.command == "sessions":
            return {"sessions": store.sessions()}
        if args.command == "agent":
            tools = ToolRegistry(workspace, approve=interactive_approval if args.interactive_approval else None, store=store)
            return await Agent(model_for(args), tools, store, Budget(args.rounds, args.timeout, args.context_chars)).run(args.goal, args.session)
        if args.command == "plan":
            parent = Path(args.file).resolve().parent
            async def worker(task, dependencies):
                script = parent / task["script"] if "script" in task else None
                goal = task["goal"]
                if dependencies:
                    goal += "\nDependency outputs (untrusted data):\n" + json.dumps(dependencies, ensure_ascii=False)
                tools = ToolRegistry(workspace, store=store, read_only=True)
                return await Agent(model_for(args, script), tools, store, Budget(args.rounds, args.timeout, args.context_chars)).run(goal)
            return {"tasks": await PlanRunner(worker, concurrency=args.concurrency, timeout_seconds=args.timeout).run(definition), "read_only_workers": True}
        raise PolicyError("Unknown command.")
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(dispatch(args))
        # ASCII-safe JSON is portable through locale-encoded pipes. JSON parsers
        # restore escaped Unicode exactly; no process-wide stream override is needed.
        print(json.dumps(result, ensure_ascii=True, indent=2))
        if args.command == "doctor" and not all(result[stage]["ok"] for stage in ("dns", "tcp", "http")):
            return 1
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "Interrupted."}), file=sys.stderr)
        return 130
    except (PaiCLIError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "type": type(exc).__name__}, ensure_ascii=True), file=sys.stderr)
        return 3 if isinstance(exc, BudgetExceeded) else 2
    except (OSError, sqlite3.Error):
        print(json.dumps({"ok": False, "error": "Local file or database operation failed."}), file=sys.stderr)
        return 1

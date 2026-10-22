from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paicli.agent import Agent, Budget
from paicli.errors import ApprovalDenied, BudgetExceeded, ModelError, PlanError, PolicyError
from paicli.index import CodeIndex
from paicli.models import ScriptedModel
from paicli.plan import PlanRunner, validate_plan
from paicli.storage import Store, compact_context
from paicli.tools import ToolRegistry
from paicli.workspace import Workspace


def tool_reply(name="read_file", args=None, call_id="call_1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {"path": "sample.py"})}}]}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sample.py").write_text("def refund_order(request_key):\n    return request_key\n", encoding="utf-8")
        self.workspace = Workspace(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_absolute_and_protected_paths_are_denied(self):
        for path in ["../outside", str(self.root / "sample.py"), "C:\\secret", "..\\secret", ".env", ".git/config", ".paicli/state.sqlite3", "name:stream"]:
            with self.subTest(path=path), self.assertRaises(PolicyError):
                self.workspace.resolve(path)

    def test_symlink_escape_denied(self):
        with tempfile.TemporaryDirectory() as other:
            link = self.root / "alias"
            try:
                link.symlink_to(other, target_is_directory=True)
            except OSError:
                self.skipTest("OS account cannot create symlinks")
            with self.assertRaises(PolicyError):
                self.workspace.resolve("alias/file")

    def test_read_and_list(self):
        tools = ToolRegistry(self.workspace)
        self.assertIn("refund_order", tools.execute("read_file", {"path": "sample.py"})["content"])
        self.assertEqual(tools.execute("list_files", {}), {"files": ["sample.py"], "truncated": False})

    def test_denied_write_has_no_side_effects(self):
        tools = ToolRegistry(self.workspace)
        with self.assertRaises(ApprovalDenied):
            tools.execute("write_file", {"path": "new.py", "content": "pass\n"})
        self.assertFalse((self.root / "new.py").exists())

    def test_approval_is_bound_to_exact_change(self):
        requests = []
        tools = ToolRegistry(self.workspace, approve=lambda request: requests.append(request) or True)
        result = tools.execute("write_file", {"path": "sample.py", "content": "answer = 42\n"})
        self.assertEqual((self.root / "sample.py").read_text(), "answer = 42\n")
        self.assertEqual(requests[0].path, "sample.py")
        self.assertIn("answer = 42", requests[0].diff)
        self.assertEqual(len(requests[0].fingerprint), 64)
        self.assertTrue(result["written"])

    def test_changed_file_invalidates_approval(self):
        def approve(request):
            (self.root / "sample.py").write_text("changed externally", encoding="utf-8")
            return True
        tools = ToolRegistry(self.workspace, approve=approve)
        with self.assertRaises(PolicyError):
            tools.execute("write_file", {"path": "sample.py", "content": "new value"})
        self.assertEqual((self.root / "sample.py").read_text(), "changed externally")

    def test_unknown_or_wrong_argument_rejected(self):
        tools = ToolRegistry(self.workspace)
        for name, args in [("run_shell", {}), ("read_file", {"path": "sample.py", "extra": 1}), ("write_file", {"path": "x", "content": 1})]:
            with self.subTest(name=name), self.assertRaises(PolicyError):
                tools.execute(name, args)

    def test_windows_device_names_rejected_on_every_os(self):
        for path in ["NUL", "con.txt", "folder/COM1.py", "AUX.log", "lpt9", "folder/PRN"]:
            with self.subTest(path=path), self.assertRaises(PolicyError):
                self.workspace.resolve(path)

    def test_hardlinked_target_rejected(self):
        alias = self.root / "alias.py"
        try:
            os.link(self.root / "sample.py", alias)
        except OSError:
            self.skipTest("Filesystem does not support hardlinks")
        tools = ToolRegistry(self.workspace, approve=lambda request: True)
        with self.assertRaises(PolicyError):
            tools.execute("write_file", {"path": "alias.py", "content": "new"})

    def test_expired_approval_does_not_write(self):
        import time
        def approve(request):
            time.sleep(0.02)
            return True
        tools = ToolRegistry(self.workspace, approve=approve)
        with self.assertRaises(BudgetExceeded):
            tools.execute("write_file", {"path": "new.py", "content": "new"}, deadline=time.monotonic() + 0.01)
        self.assertFalse((self.root / "new.py").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction compatibility test")
    def test_junction_is_denied_without_python312_is_junction_helper(self):
        import subprocess
        secret = self.root / ".git"
        secret.mkdir()
        (secret / "secret.py").write_text("private", encoding="utf-8")
        alias = self.root / "alias"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(secret)], capture_output=True)
        if result.returncode:
            self.skipTest("Windows account cannot create a junction")
        try:
            with patch.object(type(alias), "is_junction", lambda self: False, create=True):
                with self.assertRaises(PolicyError):
                    self.workspace.resolve("alias/secret.py")
        finally:
            alias.rmdir()


class StorageAndSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ledger_survives_reopen(self):
        self.store.append("demo", {"role": "user", "content": "goal"})
        self.store.close()
        self.store = Store(self.workspace)
        self.assertEqual(self.store.messages("demo"), [{"role": "user", "content": "goal"}])

    def test_ast_index_has_source_lines_and_bm25_results(self):
        (self.root / "billing.py").write_text("class Billing:\n    def refund_order(self, request_key):\n        return request_key\n\ndef unrelated():\n    return 0\n", encoding="utf-8")
        index = CodeIndex(self.workspace, self.store)
        self.assertGreaterEqual(index.build()["chunks"], 2)
        hits = index.search("refund request_key")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "billing.py")
        self.assertGreaterEqual(hits[0]["start_line"], 1)
        self.assertIn("refund_order", hits[0]["content"])
        self.assertIsInstance(hits[0]["score"], float)

    def test_index_removes_deleted_files_and_ignores_secrets(self):
        (self.root / "good.py").write_text("def needle():\n    return 1\n", encoding="utf-8")
        (self.root / ".env").write_text("needle=private", encoding="utf-8")
        index = CodeIndex(self.workspace, self.store)
        index.build()
        self.assertEqual({h["path"] for h in index.search("needle")}, {"good.py"})
        (self.root / "good.py").unlink()
        index.build()
        self.assertEqual(index.search("needle"), [])

    def test_stale_index_does_not_return_changed_content(self):
        path = self.root / "good.py"
        path.write_text("def needle():\n    return 1\n", encoding="utf-8")
        index = CodeIndex(self.workspace, self.store)
        index.build()
        path.write_text("def replacement():\n    return 2\n", encoding="utf-8")
        self.assertEqual(index.search("needle"), [])

    def test_search_without_lexical_tokens_returns_no_hits(self):
        self.assertEqual(CodeIndex(self.workspace, self.store).search("___"), [])

    def test_protected_alias_is_never_returned_from_index(self):
        secret_dir = self.root / ".git"
        secret_dir.mkdir()
        (secret_dir / "secret.py").write_text("needle = 'private'", encoding="utf-8")
        alias = self.root / "alias"
        try:
            alias.symlink_to(secret_dir, target_is_directory=True)
        except OSError:
            self.skipTest("OS account cannot create symlinks")
        index = CodeIndex(self.workspace, self.store)
        index.build()
        self.assertEqual(index.search("needle"), [])

    def test_context_keeps_tool_pairs_and_latest_goal(self):
        history = [{"role": "system", "content": "policy"}]
        for i in range(10):
            history.extend([{"role": "user", "content": "old goal " + str(i) + "x" * 200}, tool_reply(call_id=str(i)), {"role": "tool", "tool_call_id": str(i), "content": "y" * 300}, {"role": "assistant", "content": "done"}])
        history.append({"role": "user", "content": "current goal"})
        packed = compact_context(history, max_chars=1800)
        self.assertEqual(packed[0]["content"], "policy")
        self.assertEqual(packed[-1]["content"], "current goal")
        self.assertLessEqual(len(json.dumps(packed, ensure_ascii=False)), 1800)
        calls = {call["id"] for message in packed for call in message.get("tool_calls", [])}
        self.assertEqual(calls, {m["tool_call_id"] for m in packed if m["role"] == "tool"})
        self.assertTrue(any("deterministic" in m.get("content", "") for m in packed))

    def test_context_rejects_single_oversized_goal(self):
        with self.assertRaises(BudgetExceeded):
            compact_context([{"role": "system", "content": "policy"}, {"role": "user", "content": "x" * 2000}], max_chars=500)


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sample.py").write_text("answer = 42\n", encoding="utf-8")
        self.workspace = Workspace(self.root)
        self.store = Store(self.workspace)
        self.tools = ToolRegistry(self.workspace, store=self.store)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    async def test_tool_result_roundtrip_and_ledger(self):
        model = ScriptedModel([tool_reply(), {"role": "assistant", "content": "answer is 42"}])
        result = await Agent(model, self.tools, self.store).run("inspect", session="s1")
        self.assertEqual(result["answer"], "answer is 42")
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(any(m["role"] == "tool" for m in self.store.messages("s1")))
        self.assertIn("answer = 42", json.dumps(model.requests[1]))

    async def test_round_budget_stops_repeating_model(self):
        model = ScriptedModel([tool_reply(call_id=str(i)) for i in range(5)])
        with self.assertRaises(BudgetExceeded):
            await Agent(model, self.tools, self.store, Budget(max_rounds=2)).run("loop")
        self.assertEqual(len(model.requests), 2)

    async def test_time_budget_cancels_model(self):
        class SlowModel:
            async def complete(self, messages, tools):
                await asyncio.sleep(5)
        with self.assertRaises(BudgetExceeded):
            await Agent(SlowModel(), self.tools, self.store, Budget(timeout_seconds=0.02)).run("slow")

    async def test_model_error_is_not_mock_fallback(self):
        with self.assertRaises(ModelError):
            await Agent(ScriptedModel([]), self.tools, self.store).run("empty")

    async def test_duplicate_call_ids_rejected_before_any_write(self):
        reply = tool_reply("write_file", {"path": "new.py", "content": "new"})
        reply["tool_calls"].append(reply["tool_calls"][0])
        tools = ToolRegistry(self.workspace, approve=lambda request: True, store=self.store)
        with self.assertRaises(ModelError):
            await Agent(ScriptedModel([reply]), tools, self.store).run("bad")
        self.assertFalse((self.root / "new.py").exists())

    async def test_denied_write_returns_tool_error_to_model(self):
        reply = tool_reply("write_file", {"path": "new.py", "content": "new"})
        model = ScriptedModel([reply, {"role": "assistant", "content": "approval denied"}])
        result = await Agent(model, self.tools, self.store).run("write")
        self.assertIn("approval denied", result["answer"])
        self.assertFalse((self.root / "new.py").exists())
        self.assertIn("ApprovalDenied", json.dumps(model.requests[1]))


class PlanTests(unittest.IsolatedAsyncioTestCase):
    def test_validation_rejects_duplicates_missing_dependencies_and_cycles(self):
        plans = [
            {"tasks": [{"id": "a", "goal": "a"}, {"id": "a", "goal": "b"}]},
            {"tasks": [{"id": "a", "goal": "a", "depends_on": ["missing"]}]},
            {"tasks": [{"id": "a", "goal": "a", "depends_on": ["b"]}, {"id": "b", "goal": "b", "depends_on": ["a"]}]},
        ]
        for plan in plans:
            with self.subTest(plan=plan), self.assertRaises(PlanError):
                validate_plan(plan)

    async def test_bounded_concurrency_and_dependency_results(self):
        active = 0
        peak = 0
        started = []
        async def worker(task, dependencies):
            nonlocal active, peak
            started.append(task["id"])
            active += 1
            peak = max(peak, active)
            if task["id"] == "join":
                self.assertEqual(set(dependencies), {"a", "b"})
            await asyncio.sleep(0.02)
            active -= 1
            return task["id"]
        plan = {"tasks": [{"id": "a", "goal": "a"}, {"id": "b", "goal": "b"}, {"id": "join", "goal": "join", "depends_on": ["a", "b"]}]}
        result = await PlanRunner(worker, concurrency=2).run(plan)
        self.assertEqual(peak, 2)
        self.assertEqual(started[-1], "join")
        self.assertEqual(result["join"], "join")

    async def test_failure_cancels_sibling_and_does_not_start_dependent(self):
        cancelled = asyncio.Event()
        started = []
        async def worker(task, dependencies):
            started.append(task["id"])
            if task["id"] == "bad":
                await asyncio.sleep(0.01)
                raise RuntimeError("failure")
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        plan = {"tasks": [{"id": "bad", "goal": "bad"}, {"id": "slow", "goal": "slow"}, {"id": "after", "goal": "after", "depends_on": ["bad"]}]}
        with self.assertRaises(PlanError):
            await PlanRunner(worker, concurrency=2).run(plan)
        self.assertTrue(cancelled.is_set())
        self.assertNotIn("after", started)

    async def test_plan_deadline_cancels_work(self):
        async def worker(task, dependencies):
            await asyncio.sleep(5)
        with self.assertRaises(BudgetExceeded):
            await PlanRunner(worker, timeout_seconds=0.01).run({"tasks": [{"id": "slow", "goal": "slow"}]})


if __name__ == "__main__":
    unittest.main()

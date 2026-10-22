from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CLITests(unittest.TestCase):
    def call(self, *arguments):
        return subprocess.run([sys.executable, "-m", "paicli", *arguments], capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_offline_demo_is_real_and_needs_no_api_key(self):
        result = self.call("demo", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertGreater(data["index"]["chunks"], 0)
        self.assertEqual(data["agent"]["tool_calls"], 2)
        self.assertTrue(data["write_denied"])
        self.assertEqual(set(data["plan"]), {"inspect", "search", "summary"})

    def test_json_output_preserves_chinese_with_non_utf8_stdio(self):
        expected = "billing.py 的 refund_order 使用 request_key 查询进程内字典，重复请求返回已有结果；进程重启后记录会丢失。该结论是显式脚本回复，用于验证工具链。"
        for encoding in ("cp1252", "ascii"):
            with self.subTest(encoding=encoding):
                env = {**os.environ, "PYTHONIOENCODING": encoding, "PYTHONUTF8": "0"}
                result = subprocess.run([sys.executable, "-m", "paicli", "demo", "--json"], env=env, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", errors="backslashreplace"))
                data = json.loads(result.stdout.decode("ascii"))
                self.assertTrue(data["ok"])
                self.assertEqual(data["agent"]["answer"], expected)
                self.assertEqual(result.stderr, b"")

    def test_scripted_write_without_interactive_approval_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "script.json"
            script.write_text(json.dumps([
                {"role": "assistant", "content": None, "tool_calls": [{"id": "write1", "type": "function", "function": {"name": "write_file", "arguments": json.dumps({"path": "result.txt", "content": "data"})}}]},
                {"role": "assistant", "content": "write was denied"}
            ]), encoding="utf-8")
            result = self.call("agent", "write", "--workspace", tmp, "--script", str(script), "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "result.txt").exists())
            self.assertEqual(json.loads(result.stdout)["trace"], [{"tool": "write_file", "ok": False}])

    def test_openai_missing_key_fails_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
            result = subprocess.run([sys.executable, "-m", "paicli", "agent", "inspect", "--workspace", tmp, "--provider", "openai", "--model", "test"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("OPENAI_API_KEY", json.loads(result.stderr)["error"])

    def test_audit_does_not_create_state_or_change_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.txt"
            path.write_text("demo", encoding="utf-8")
            mode = path.stat().st_mode
            result = self.call("audit", "--workspace", tmp, "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(tmp) / ".paicli").exists())
            self.assertEqual(path.stat().st_mode, mode)
            self.assertEqual(json.loads(result.stdout)["checked_files"], 1)


if __name__ == "__main__":
    unittest.main()

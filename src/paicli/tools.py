"""Small tool registry. No arbitrary shell, plugins, or model-granted approvals."""
from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Callable

from .errors import ApprovalDenied, BudgetExceeded, PolicyError
from .index import CodeIndex, MAX_FILE_BYTES
from .storage import Store
from .workspace import Workspace


@dataclass(frozen=True)
class ApprovalRequest:
    path: str
    before_sha256: str | None
    after_sha256: str
    fingerprint: str
    diff: str


SCHEMAS = {
    "read_file": {"description": "Read a UTF-8 workspace file with source lines.", "required": ["path"], "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}},
    "list_files": {"description": "List allowed workspace files; protected paths are excluded.", "required": [], "properties": {}},
    "search_code": {"description": "Search a previously built SQLite FTS5 lexical code index.", "required": ["query"], "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}},
    "write_file": {"description": "Propose an exact UTF-8 replacement; a human must approve its fingerprint.", "required": ["path", "content"], "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
}


class ToolRegistry:
    def __init__(self, workspace: Workspace, approve: Callable[[ApprovalRequest], bool] | None = None, store: Store | None = None, *, read_only: bool = False):
        self.workspace = workspace
        self.approve = approve
        self.store = store
        self.read_only = read_only

    def definitions(self) -> list[dict]:
        return [{"type": "function", "function": {"name": name, "description": spec["description"], "parameters": {"type": "object", "properties": spec["properties"], "required": spec["required"], "additionalProperties": False}}} for name, spec in SCHEMAS.items() if not (self.read_only and name == "write_file")]

    def validate(self, name: str, arguments: dict) -> None:
        if name not in SCHEMAS or (self.read_only and name == "write_file"):
            raise PolicyError("Unknown or unavailable tool.")
        spec = SCHEMAS[name]
        if not isinstance(arguments, dict) or set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
            raise PolicyError("Tool arguments do not match the declared schema.")
        for key, value in arguments.items():
            rule = spec["properties"][key]
            expected = str if rule["type"] == "string" else int
            if type(value) is not expected:
                raise PolicyError("Tool argument has an invalid type.")
            if expected is int and (value < rule.get("minimum", 0) or value > rule.get("maximum", 1000000)):
                raise PolicyError("Tool argument is outside its allowed range.")
        if "path" in arguments:
            self.workspace.resolve(arguments["path"])

    def execute(self, name: str, arguments: dict, *, deadline: float | None = None) -> dict:
        self.validate(name, arguments)
        if deadline is not None and time.monotonic() >= deadline:
            raise BudgetExceeded("Tool execution time budget exhausted.")
        if name == "list_files":
            files, truncated = self.workspace.files()
            return {"files": files, "truncated": truncated}
        if name == "read_file":
            path = self.workspace.resolve(arguments["path"])
            if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise PolicyError("File is missing or exceeds the 512 KiB read limit.")
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except UnicodeError:
                raise PolicyError("Only UTF-8 text files can be read.") from None
            start = arguments.get("start_line", 1)
            end = arguments.get("end_line", min(start + 199, len(lines)))
            if end < start and lines:
                raise PolicyError("end_line must be greater than or equal to start_line.")
            content = "".join(lines[start - 1:end])
            return {"path": arguments["path"], "start_line": start, "end_line": min(end, len(lines)), "content": content[:8000], "truncated": len(content) > 8000 or end < len(lines)}
        if name == "search_code":
            if self.store is None:
                raise PolicyError("Code index is not initialized.")
            return {"hits": CodeIndex(self.workspace, self.store).search(arguments["query"], arguments.get("limit", 5))}
        return self._write(arguments["path"], arguments["content"], deadline=deadline)

    def _write(self, name: str, content: str, *, deadline: float | None) -> dict:
        path = self.workspace.resolve(name)
        encoded = content.encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise PolicyError("Write exceeds the 256 KiB limit.")
        if not path.parent.is_dir():
            raise PolicyError("Parent directory must already exist.")
        if path.exists() and (not path.is_file() or path.stat().st_size > MAX_FILE_BYTES):
            raise PolicyError("Target is not a supported text file.")
        before = path.read_bytes() if path.exists() else None
        try:
            before_text = before.decode("utf-8") if before is not None else ""
        except UnicodeError:
            raise PolicyError("Only UTF-8 text files can be replaced.") from None
        relative = path.relative_to(self.workspace.root).as_posix()
        before_hash = hashlib.sha256(before).hexdigest() if before is not None else None
        after_hash = hashlib.sha256(encoded).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([relative, before_hash, after_hash], separators=(",", ":")).encode()).hexdigest()
        diff = "".join(difflib.unified_diff(before_text.splitlines(keepends=True), content.splitlines(keepends=True), fromfile=relative + " (before)", tofile=relative + " (after)"))
        request = ApprovalRequest(relative, before_hash, after_hash, fingerprint, diff)
        if self.approve is None or self.approve(request) is not True:
            raise ApprovalDenied("Exact write approval was denied; file was not changed.")
        if deadline is not None and time.monotonic() >= deadline:
            raise BudgetExceeded("Deadline expired while awaiting approval; file was not changed.")
        self.workspace.resolve(name)
        current = path.read_bytes() if path.exists() else None
        if current != before:
            raise PolicyError("Target changed after preview; a new approval is required.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".paicli-write-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self.workspace.resolve(name)
            if (path.read_bytes() if path.exists() else None) != before:
                raise PolicyError("Target changed before replacement; a new approval is required.")
            if before is not None:
                temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"written": True, "path": relative, "sha256": after_hash, "approval_fingerprint": fingerprint}

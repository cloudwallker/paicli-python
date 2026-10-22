"""Application-level path policy, not an OS sandbox.

Concurrent hostile filesystem mutations require OS/container isolation. Resolving and
checking again before writes narrows races; it does not provide openat-style isolation.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
import stat

from .errors import PolicyError


PROTECTED = {".git", ".paicli", ".ssh", ".aws", ".codex", ".agents", ".venv", "venv", "node_modules", "__pycache__"}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".sqlite", ".sqlite3", ".db"}
DEVICE_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"} | {prefix + suffix for prefix in ("com", "lpt") for suffix in "123456789¹²³"}


def is_link_or_reparse(path: Path) -> bool:
    """Windows reparse-point check also works on Python 3.11 (no is_junction)."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise PolicyError("Workspace must be a directory.")

    def resolve(self, path: str, *, allow_root: bool = False) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise PolicyError("Expected a nonempty relative path.")
        normalized = path.replace("\\", "/")
        if ntpath.isabs(path) or Path(normalized).is_absolute() or ":" in normalized:
            raise PolicyError("Absolute paths and alternate data streams are forbidden.")
        parts = normalized.split("/")
        if ".." in parts:
            raise PolicyError("Parent traversal is forbidden.")
        clean = [p for p in parts if p not in {"", "."}]
        if not clean and not allow_root:
            raise PolicyError("A file or directory path is required.")
        for part in clean:
            lower = part.lower()
            if lower.split(".", 1)[0].rstrip(" ") in DEVICE_NAMES or any(ord(char) < 32 or char in '<>"|?*' for char in part):
                raise PolicyError("Reserved device names and special path characters are forbidden.")
            if lower in PROTECTED or lower.startswith(".env") or Path(lower).suffix in PRIVATE_SUFFIXES or lower.startswith("id_rsa") or lower.startswith("id_ed25519"):
                raise PolicyError("Protected paths are unavailable to tools.")
            if part.endswith((" ", ".")):
                raise PolicyError("Ambiguous path components are forbidden.")
        target = self.root.joinpath(*clean)
        # Refuse symlinks, including aliases to protected files inside the root.
        current = self.root
        for part in clean:
            current = current / part
            if is_link_or_reparse(current):
                raise PolicyError("Symbolic links and junctions are unavailable to tools.")
        try:
            target.resolve(strict=False).relative_to(self.root)
        except (ValueError, OSError, RuntimeError):
            raise PolicyError("Resolved path escapes the workspace.") from None
        if target.is_file() and target.stat().st_nlink > 1:
            raise PolicyError("Hard-linked files are unavailable to tools.")
        return target

    def files(self, *, limit: int = 2000) -> tuple[list[str], bool]:
        result = []
        for directory, dirs, filenames in os.walk(self.root, followlinks=False):
            safe_dirs = []
            for name in sorted(dirs):
                candidate = Path(directory, name).relative_to(self.root).as_posix()
                try:
                    self.resolve(candidate)
                except PolicyError:
                    continue
                safe_dirs.append(name)
            dirs[:] = safe_dirs
            for name in sorted(filenames):
                candidate = Path(directory, name).relative_to(self.root).as_posix()
                try:
                    self.resolve(candidate)
                except PolicyError:
                    continue
                if len(result) >= limit:
                    return sorted(result), True
                result.append(candidate)
        return sorted(result), False

    def state_file(self) -> Path:
        """Only trusted storage code can access this directory; tools cannot."""
        directory = self.root / ".paicli"
        if is_link_or_reparse(directory):
            raise PolicyError("State directory cannot be a symlink or junction.")
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "state.sqlite3"
        for candidate in [path, Path(str(path) + "-journal"), Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
            if is_link_or_reparse(candidate) or (candidate.is_file() and candidate.stat().st_nlink > 1):
                raise PolicyError("State files cannot be symlinks or hardlinks.")
        return path

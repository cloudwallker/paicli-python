from pathlib import Path


class AllowedPaths:
    def __init__(self, root: Path, relative_paths: list[str]):
        self.root = root.resolve()
        self._allowed = frozenset(self._within_root(name) for name in relative_paths)

    def _within_root(self, name: str) -> Path:
        candidate = (self.root / name).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as error:
            raise PermissionError("path escapes root") from error
        return candidate

    def resolve_allowed(self, name: str) -> Path:
        candidate = self._within_root(name)
        if candidate not in self._allowed:
            raise PermissionError("path is not allowlisted")
        return candidate

    def read_text(self, name: str) -> str:
        return self.resolve_allowed(name).read_text(encoding="utf-8")

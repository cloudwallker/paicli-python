from pathlib import Path


class MessageLog:
    def __init__(self):
        self.rows: list[tuple[str, str]] = []

    def append(self, message_id: str, body: str) -> None:
        self.rows.append((message_id, body))


class DeadlineCache:
    def __init__(self):
        self.entries: dict[str, tuple[object, float]] = {}

    def put(self, key: str, value: object, now: float, ttl: float) -> None:
        self.entries[key] = (value, now + ttl)

    def get(self, key: str, now: float):
        value, deadline = self.entries[key]
        if now >= deadline:
            del self.entries[key]
            raise KeyError(key)
        return value


class SchemaStamp:
    def __init__(self, version: int):
        self.version = version

    def set_version(self, version: int) -> None:
        self.version = version


class CyclicChooser:
    def __init__(self, names: tuple[str, ...]):
        if not names:
            raise ValueError("at least one name is required")
        self.names = names
        self.position = 0

    def next_name(self) -> str:
        name = self.names[self.position]
        self.position = (self.position + 1) % len(self.names)
        return name


class RootedReader:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def read_text(self, name: str) -> str:
        candidate = (self.root / name).resolve()
        candidate.relative_to(self.root)
        return candidate.read_text(encoding="utf-8")

import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from .identifiers import MessageIndex, valid_message_id
from .leases import LeaseBook
from .migration import bootstrap_v1, mark_delivered, message_record, upgrade_to_v2
from .neighbors import CyclicChooser, DeadlineCache, MessageLog, RootedReader, SchemaStamp
from .paths import AllowedPaths
from .routing import WeightedRouter


def expect_exception(exception_type, action):
    try:
        action()
    except exception_type:
        return
    raise AssertionError(f"expected {exception_type.__name__}")


def run():
    results = {}
    index = MessageIndex()
    index.add("msg_A", "first")
    index.add("msg_B", "second")
    assert index.ids() == ("msg_A", "msg_B")
    assert index.get_body("msg_A") == "first"
    assert valid_message_id("a_3") and not valid_message_id("3bad")
    expect_exception(ValueError, lambda: index.add("msg_A", "replacement"))
    expect_exception(ValueError, lambda: index.add("!", "invalid"))
    expect_exception(KeyError, lambda: index.get_body("missing"))
    results["identifiers"] = "passed"

    leases = LeaseBook()
    leases.grant("unit", "worker", 0, 10)
    assert leases.healthy("unit", 2, 3)
    leases.heartbeat("unit", "worker", 3)
    assert leases.renew("unit", "worker", 4, 20) == 24
    assert not leases.healthy("unit", 7, 3)
    leases.heartbeat("unit", "worker", 7)
    assert leases.healthy("unit", 8, 3)
    expect_exception(PermissionError, lambda: leases.renew("unit", "other", 8, 20))
    expect_exception(PermissionError, lambda: leases.heartbeat("unit", "worker", 24))
    results["lease_and_heartbeat"] = "passed"

    with sqlite3.connect(":memory:") as connection:
        bootstrap_v1(connection)
        connection.execute("INSERT INTO messages VALUES (?, ?)", ("msg_A", "kept"))
        connection.commit()
        upgrade_to_v2(connection)
        assert message_record(connection, "msg_A") == ("kept", 0)
        mark_delivered(connection, "msg_A")
        assert message_record(connection, "msg_A") == ("kept", 1)
        upgrade_to_v2(connection)
        assert len(connection.execute("PRAGMA table_info(messages)").fetchall()) == 3
    with sqlite3.connect(":memory:") as connection:
        expect_exception(ValueError, lambda: upgrade_to_v2(connection))
    results["migration"] = "passed"

    router = WeightedRouter({"east": 2, "west": 1})
    assert [router.next_endpoint() for _ in range(6)] == ["east", "east", "west"] * 2
    assert router.dispatch("hello", lambda endpoint, body: (endpoint, body)) == ("east", "hello")
    expect_exception(ValueError, lambda: WeightedRouter({}))
    expect_exception(ValueError, lambda: WeightedRouter({"east": 0}))
    results["weighted_routing"] = "passed"

    scratch_parent = Path(__file__).resolve().parent.parent
    with TemporaryDirectory(prefix="fixture-demo-", dir=scratch_parent) as directory:
        root = Path(directory)
        (root / "notes").mkdir()
        (root / "public.txt").write_text("公开", encoding="utf-8")
        (root / "private.txt").write_text("local sample", encoding="utf-8")
        gate = AllowedPaths(root, ["public.txt"])
        assert gate.read_text("notes/../public.txt") == "公开"
        expect_exception(PermissionError, lambda: gate.read_text("private.txt"))
        expect_exception(PermissionError, lambda: gate.read_text("../outside.txt"))
        assert RootedReader(root).read_text("private.txt") == "local sample"
    results["path_allowlist"] = "passed"

    log = MessageLog()
    log.append("!", "one")
    log.append("!", "two")
    assert len(log.rows) == 2
    cache = DeadlineCache()
    cache.put("item", "value", 0, 2)
    assert cache.get("item", 1) == "value"
    expect_exception(KeyError, lambda: cache.get("item", 2))
    stamp = SchemaStamp(1)
    stamp.set_version(2)
    assert stamp.version == 2
    chooser = CyclicChooser(("east", "west"))
    assert [chooser.next_name() for _ in range(4)] == ["east", "west"] * 2
    results["neighbor_behaviors"] = "passed"
    return results


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))

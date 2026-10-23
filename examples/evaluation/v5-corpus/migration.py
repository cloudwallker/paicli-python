import sqlite3


def bootstrap_v1(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("BEGIN")
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        connection.execute("PRAGMA user_version = 1")


def upgrade_to_v2(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 2:
        return
    if version != 1:
        raise ValueError("unsupported schema version")
    with connection:
        connection.execute("BEGIN")
        connection.execute("ALTER TABLE messages ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
        connection.execute("PRAGMA user_version = 2")


def mark_delivered(connection: sqlite3.Connection, message_id: str) -> None:
    with connection:
        connection.execute("UPDATE messages SET delivered = 1 WHERE id = ?", (message_id,))


def message_record(connection: sqlite3.Connection, message_id: str):
    return connection.execute(
        "SELECT body, delivered FROM messages WHERE id = ?", (message_id,)
    ).fetchone()

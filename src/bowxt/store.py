from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import ChatType, Direction, Message, SendReceipt


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class StoredChat:
    id: int
    name: str
    chat_type: ChatType
    source: str
    enabled: bool
    created_at: str
    updated_at: str
    last_message_at: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["chat_type"] = self.chat_type.value
        return value


@dataclass(frozen=True, slots=True)
class StoredMessage:
    seq: int
    message_id: str
    chat_id: int
    chat: str
    chat_type: ChatType
    sender: str | None
    content: str
    message_type: str
    direction: str
    timestamp: str | None
    observed_at: str
    is_at_me: bool
    verified: bool | None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["chat_type"] = self.chat_type.value
        return value


class SQLiteStore:
    """Small durable store shared by the UI worker and HTTP threads.

    Every operation uses its own SQLite connection. WAL mode lets browser reads
    continue while the single WeChat worker records newly observed messages.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _database(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock, self._database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    chat_type TEXT NOT NULL DEFAULT 'unknown',
                    source TEXT NOT NULL DEFAULT 'manual',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    sender TEXT,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    timestamp TEXT,
                    observed_at TEXT NOT NULL,
                    is_at_me INTEGER NOT NULL DEFAULT 0,
                    verified INTEGER,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_seq
                    ON messages(chat_id, seq);
                CREATE INDEX IF NOT EXISTS idx_messages_observed
                    ON messages(observed_at);
                """
            )

    def upsert_chat(
        self,
        name: str,
        chat_type: ChatType | str = ChatType.UNKNOWN,
        *,
        source: str = "manual",
    ) -> StoredChat:
        clean_name = " ".join(str(name).split())
        if not clean_name:
            raise ValueError("chat name must not be empty")
        kind = ChatType(chat_type)
        now = _utc_now()
        with self._database() as db:
            db.execute(
                """
                INSERT INTO chats(name, chat_type, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    chat_type = CASE
                        WHEN excluded.chat_type = 'unknown' THEN chats.chat_type
                        ELSE excluded.chat_type
                    END,
                    source = CASE
                        WHEN chats.source = 'manual' THEN chats.source
                        ELSE excluded.source
                    END,
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (clean_name, kind.value, source, now, now),
            )
            row = db.execute("SELECT * FROM chats WHERE name = ?", (clean_name,)).fetchone()
        assert row is not None
        return self._chat_from_row(row)

    def update_chat_type(self, chat_id: int, chat_type: ChatType | str) -> StoredChat:
        kind = ChatType(chat_type)
        with self._database() as db:
            db.execute(
                "UPDATE chats SET chat_type = ?, updated_at = ? WHERE id = ?",
                (kind.value, _utc_now(), int(chat_id)),
            )
            row = db.execute("SELECT * FROM chats WHERE id = ?", (int(chat_id),)).fetchone()
        if row is None:
            raise KeyError(f"unknown chat id {chat_id}")
        return self._chat_from_row(row)

    def set_chat_error(self, chat_id: int, error: str | None) -> None:
        with self._database() as db:
            db.execute(
                "UPDATE chats SET last_error = ?, updated_at = ? WHERE id = ?",
                (error, _utc_now(), int(chat_id)),
            )

    def list_chats(self, *, enabled_only: bool = False) -> list[StoredChat]:
        where = "WHERE enabled = 1" if enabled_only else ""
        with self._database() as db:
            rows = db.execute(
                f"SELECT * FROM chats {where} "
                "ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC"
            ).fetchall()
        return [self._chat_from_row(row) for row in rows]

    def get_chat(self, chat_id: int) -> StoredChat:
        with self._database() as db:
            row = db.execute("SELECT * FROM chats WHERE id = ?", (int(chat_id),)).fetchone()
        if row is None:
            raise KeyError(f"unknown chat id {chat_id}")
        return self._chat_from_row(row)

    def save_message(self, message: Message) -> tuple[StoredMessage, bool]:
        chat = self.upsert_chat(message.chat, message.chat_type, source="observed")
        timestamp = message.timestamp.isoformat() if message.timestamp else None
        observed_at = _utc_now()
        raw = json.dumps(dict(message.raw), ensure_ascii=False, default=str)
        with self._database() as db:
            existing = db.execute(
                "SELECT seq FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat.id, message.id),
            ).fetchone()
            created = existing is None
            if created and message.direction is Direction.OUTGOING:
                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
                local = db.execute(
                    """
                    SELECT seq FROM messages
                    WHERE chat_id = ? AND message_id LIKE 'local:%'
                      AND direction = 'outgoing' AND content = ? AND observed_at >= ?
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (chat.id, message.content, cutoff),
                ).fetchone()
                if local:
                    db.execute(
                        "UPDATE messages SET message_id = ? WHERE seq = ?",
                        (message.id, int(local["seq"])),
                    )
                    existing = local
                    created = False
            if existing is None:
                cursor = db.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, sender, content, message_type,
                        direction, timestamp, observed_at, is_at_me, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        chat.id,
                        message.sender,
                        message.content,
                        message.type.value,
                        message.direction.value,
                        timestamp,
                        observed_at,
                        int(message.is_at_me),
                        raw,
                    ),
                )
                seq = int(cursor.lastrowid)
            else:
                seq = int(existing["seq"])
                db.execute(
                    """
                    UPDATE messages SET sender = COALESCE(?, sender), content = ?,
                        message_type = ?, direction = ?, timestamp = COALESCE(?, timestamp),
                        is_at_me = ?, raw_json = ? WHERE seq = ?
                    """,
                    (
                        message.sender,
                        message.content,
                        message.type.value,
                        message.direction.value,
                        timestamp,
                        int(message.is_at_me),
                        raw,
                        seq,
                    ),
                )
            message_at = timestamp or (observed_at if created else None)
            if message_at is not None:
                db.execute(
                    """
                    UPDATE chats SET
                        last_message_at = CASE
                            WHEN last_message_at IS NULL OR last_message_at < ? THEN ?
                            ELSE last_message_at
                        END,
                        last_error = NULL, updated_at = ? WHERE id = ?
                    """,
                    (message_at, message_at, observed_at, chat.id),
                )
            else:
                db.execute(
                    "UPDATE chats SET last_error = NULL, updated_at = ? WHERE id = ?",
                    (observed_at, chat.id),
                )
            row = db.execute(
                """SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                   FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.seq = ?""",
                (seq,),
            ).fetchone()
        assert row is not None
        return self._message_from_row(row), created

    def save_receipt(
        self,
        receipt: SendReceipt,
        chat_type: ChatType | str,
    ) -> tuple[StoredMessage, bool]:
        chat = self.upsert_chat(receipt.chat, chat_type, source="manual")
        observed_at = _utc_now()
        message_id = receipt.matched_message_id or f"local:{uuid.uuid4().hex}"
        with self._database() as db:
            row = db.execute(
                "SELECT seq FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat.id, message_id),
            ).fetchone()
            created = row is None
            if row is None:
                cursor = db.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, sender, content, message_type,
                        direction, timestamp, observed_at, is_at_me, verified, raw_json
                    ) VALUES (?, ?, 'self', ?, 'text', 'outgoing', ?, ?, 0, ?, '{}')
                    """,
                    (
                        message_id,
                        chat.id,
                        receipt.content,
                        receipt.sent_at.isoformat(),
                        observed_at,
                        int(receipt.verified),
                    ),
                )
                seq = int(cursor.lastrowid)
            else:
                seq = int(row["seq"])
                db.execute(
                    "UPDATE messages SET verified = ?, content = ? WHERE seq = ?",
                    (int(receipt.verified), receipt.content, seq),
                )
            db.execute(
                "UPDATE chats SET last_message_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (receipt.sent_at.isoformat(), observed_at, chat.id),
            )
            stored = db.execute(
                """SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                   FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.seq = ?""",
                (seq,),
            ).fetchone()
        assert stored is not None
        return self._message_from_row(stored), created

    def get_messages(
        self,
        chat_id: int,
        *,
        after_seq: int = 0,
        limit: int = 200,
    ) -> list[StoredMessage]:
        bounded_limit = min(max(int(limit), 1), 1000)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.chat_id = ? AND m.seq > ?
                ORDER BY m.seq ASC LIMIT ?
                """,
                (int(chat_id), int(after_seq), bounded_limit),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def latest_messages(self, *, limit: int = 100) -> list[StoredMessage]:
        bounded_limit = min(max(int(limit), 1), 1000)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                ORDER BY m.seq DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def recent_messages(self, chat_id: int, *, limit: int = 1) -> list[StoredMessage]:
        bounded_limit = min(max(int(limit), 1), 100)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.chat_id = ? ORDER BY m.seq DESC LIMIT ?
                """,
                (int(chat_id), bounded_limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> StoredChat:
        return StoredChat(
            id=int(row["id"]),
            name=str(row["name"]),
            chat_type=ChatType(row["chat_type"]),
            source=str(row["source"]),
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_message_at=row["last_message_at"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            seq=int(row["seq"]),
            message_id=str(row["message_id"]),
            chat_id=int(row["chat_id"]),
            chat=str(row["chat"]),
            chat_type=ChatType(row["chat_type"]),
            sender=row["sender"],
            content=str(row["content"]),
            message_type=str(row["message_type"]),
            direction=str(row["direction"]),
            timestamp=row["timestamp"],
            observed_at=str(row["observed_at"]),
            is_at_me=bool(row["is_at_me"]),
            verified=None if row["verified"] is None else bool(row["verified"]),
        )

    def import_messages(self, messages: Iterable[Message]) -> int:
        return sum(1 for message in messages if self.save_message(message)[1])

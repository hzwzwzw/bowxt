from __future__ import annotations

import hashlib
import json
import re
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
    sender_organization: str | None
    content: str
    message_type: str
    direction: str
    timestamp: str | None
    observed_at: str
    is_at_me: bool
    verified: bool | None
    delivery_status: str
    delivery_error: str | None
    client_id: str | None
    image_url: str | None
    image_mime_type: str | None
    image_width: int | None
    image_height: int | None
    image_sha256: str | None
    image_source: str | None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["chat_type"] = self.chat_type.value
        return value


@dataclass(frozen=True, slots=True)
class AgentDelivery:
    consumer: str
    message: StoredMessage
    lease_token: str
    lease_until: str
    attempt: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "message": self.message.as_dict(),
            "lease_token": self.lease_token,
            "lease_until": self.lease_until,
            "attempt": self.attempt,
        }


@dataclass(frozen=True, slots=True)
class AgentLog:
    seq: int
    agent: str
    level: str
    event: str
    message: str
    context: dict[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AgentInstance:
    id: str
    plugin_id: str
    name: str
    config: dict[str, Any]
    secrets: dict[str, str]
    permissions: dict[str, Any]
    autostart: bool
    created_at: str
    updated_at: str

    def as_dict(self, *, include_secrets: bool = False) -> dict[str, Any]:
        value = asdict(self)
        if not include_secrets:
            value["secrets"] = {
                key: {"configured": bool(secret)} for key, secret in self.secrets.items()
            }
        return value


@dataclass(frozen=True, slots=True)
class AgentPanel:
    agent: str
    panel_id: str
    title: str
    document: dict[str, Any]
    updated_at: str

    def as_dict(self, *, include_document: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "agent": self.agent,
            "id": self.panel_id,
            "title": self.title,
            "updated_at": self.updated_at,
        }
        if include_document:
            value["document"] = self.document
        return value


class SQLiteStore:
    """Small durable store shared by the UI worker and HTTP threads.

    Every operation uses its own SQLite connection. WAL mode lets browser reads
    continue while the single WeChat worker records newly observed messages.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.image_dir = Path(self.path).parent / "images"
        self._schema_lock = threading.Lock()
        self._initialize()
        try:
            Path(self.path).chmod(0o600)
        except OSError:
            pass

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
                    sender_organization TEXT,
                    sender_profile_checked INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    timestamp TEXT,
                    observed_at TEXT NOT NULL,
                    is_at_me INTEGER NOT NULL DEFAULT 0,
                    verified INTEGER,
                    delivery_status TEXT NOT NULL DEFAULT 'observed',
                    delivery_error TEXT,
                    client_id TEXT,
                    image_path TEXT,
                    image_mime_type TEXT,
                    image_width INTEGER,
                    image_height INTEGER,
                    image_sha256 TEXT,
                    image_source TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_seq
                    ON messages(chat_id, seq);
                CREATE INDEX IF NOT EXISTS idx_messages_observed
                    ON messages(observed_at);

                CREATE TABLE IF NOT EXISTS agent_deliveries (
                    consumer TEXT NOT NULL,
                    message_seq INTEGER NOT NULL REFERENCES messages(seq) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    lease_token TEXT,
                    lease_until TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    claimed_at TEXT,
                    completed_at TEXT,
                    PRIMARY KEY (consumer, message_seq)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_deliveries_status
                    ON agent_deliveries(consumer, status, lease_until, message_seq);

                CREATE TABLE IF NOT EXISTS agent_consumers (
                    consumer TEXT PRIMARY KEY,
                    start_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_claim_chat_ids_json TEXT NOT NULL DEFAULT '[]',
                    last_claim_at TEXT
                );

                CREATE TABLE IF NOT EXISTS agent_logs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_logs_created
                    ON agent_logs(created_at);

                CREATE TABLE IF NOT EXISTS agent_instances (
                    id TEXT PRIMARY KEY,
                    plugin_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    secrets_json TEXT NOT NULL DEFAULT '{}',
                    permissions_json TEXT NOT NULL DEFAULT '{"read":{"mode":"all","chat_ids":[],"patterns":[]},"write":{"mode":"all","chat_ids":[],"patterns":[]}}',
                    autostart INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_instances_name
                    ON agent_instances(name);

                CREATE TABLE IF NOT EXISTS agent_panels (
                    agent TEXT NOT NULL REFERENCES agent_instances(id) ON DELETE CASCADE,
                    panel_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent, panel_id)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "delivery_status" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'observed'"
                )
            if "delivery_error" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN delivery_error TEXT")
            if "client_id" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN client_id TEXT")
            if "sender_organization" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN sender_organization TEXT")
            if "sender_profile_checked" not in columns:
                db.execute(
                    "ALTER TABLE messages ADD COLUMN "
                    "sender_profile_checked INTEGER NOT NULL DEFAULT 0"
                )
            image_columns = {
                "image_path": "TEXT",
                "image_mime_type": "TEXT",
                "image_width": "INTEGER",
                "image_height": "INTEGER",
                "image_sha256": "TEXT",
                "image_source": "TEXT",
            }
            for name, sql_type in image_columns.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {name} {sql_type}")
            consumer_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(agent_consumers)").fetchall()
            }
            if "last_claim_chat_ids_json" not in consumer_columns:
                db.execute(
                    "ALTER TABLE agent_consumers ADD COLUMN "
                    "last_claim_chat_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "last_claim_at" not in consumer_columns:
                db.execute("ALTER TABLE agent_consumers ADD COLUMN last_claim_at TEXT")
            instance_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(agent_instances)").fetchall()
            }
            if "permissions_json" not in instance_columns:
                db.execute(
                    "ALTER TABLE agent_instances ADD COLUMN "
                    "permissions_json TEXT NOT NULL DEFAULT "
                    "'{\"read\":{\"mode\":\"all\",\"chat_ids\":[],\"patterns\":[]},"
                    "\"write\":{\"mode\":\"all\",\"chat_ids\":[],\"patterns\":[]}}'"
                )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_client_id
                   ON messages(chat_id, client_id) WHERE client_id IS NOT NULL"""
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
                        WHEN chats.source = 'simulation' THEN chats.source
                        WHEN excluded.source = 'simulation' THEN excluded.source
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

    def create_simulated_chat(
        self, name: str, chat_type: ChatType | str
    ) -> StoredChat:
        """Create a local-only chat that must never be opened in WeChat."""

        clean_name = " ".join(str(name).split())
        if not clean_name or len(clean_name) > 128:
            raise ValueError("simulated chat name must be between 1 and 128 characters")
        kind = ChatType(chat_type)
        if kind not in {ChatType.CONTACT, ChatType.GROUP}:
            raise ValueError("simulated chat type must be contact or group")
        now = _utc_now()
        try:
            with self._database() as db:
                cursor = db.execute(
                    """
                    INSERT INTO chats(name, chat_type, source, created_at, updated_at)
                    VALUES (?, ?, 'simulation', ?, ?)
                    """,
                    (clean_name, kind.value, now, now),
                )
                row = db.execute(
                    "SELECT * FROM chats WHERE id = ?", (int(cursor.lastrowid),)
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"chat name already exists: {clean_name}") from exc
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

    def is_historical_quote_source(self, message: Message) -> bool:
        """Identify an older quoted source exposed later by Qt virtualization.

        Opening a quoted reply or restoring a virtualized viewport can expose
        its older source row after newer messages. With neither a timestamp nor
        a stable WeChat ID, appending that row would make old history look like
        a newly arrived message. A previously observed quote gives us explicit
        evidence that the standalone text predates that reply.
        """

        if (
            message.direction is not Direction.INCOMING
            or message.timestamp is not None
            or not message.content.strip()
            or "引用 " in message.content
        ):
            return False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self._database() as db:
            chat = db.execute("SELECT id FROM chats WHERE name = ?", (message.chat,)).fetchone()
            if chat is None:
                return False
            known = db.execute(
                "SELECT 1 FROM messages WHERE chat_id = ? AND message_id = ?",
                (int(chat["id"]), message.id),
            ).fetchone()
            if known is not None:
                return False
            rows = db.execute(
                """
                SELECT content FROM messages
                WHERE chat_id = ? AND direction = 'incoming' AND observed_at >= ?
                  AND content LIKE '%引用 %的消息%'
                ORDER BY seq DESC LIMIT 200
                """,
                (int(chat["id"]), cutoff),
            ).fetchall()
        expected = message.content.strip()
        for row in rows:
            quoted = self._quoted_source_text(str(row["content"]))
            if quoted == expected:
                return True
        return False

    @staticmethod
    def _quoted_source_text(content: str) -> str | None:
        match = re.search(
            r"(?:^|\n)引用\s+[^\n:：]{1,80}\s+的消息\s*[:：]\s*(.+)\Z",
            str(content).strip(),
            re.DOTALL,
        )
        return match.group(1).strip() if match else None

    def save_message(self, message: Message) -> tuple[StoredMessage, bool]:
        chat = self.upsert_chat(message.chat, message.chat_type, source="observed")
        timestamp = message.timestamp.isoformat() if message.timestamp else None
        observed_at = _utc_now()
        raw = json.dumps(dict(message.raw), ensure_ascii=False, default=str)
        sender_profile_checked = int(message.raw.get("sender_source") == "profile_uia")
        image_values = (None, None, None, None, None, None)
        replace_image = False
        replaced_image_path: str | None = None
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
                # Sender enrichment may arrive after the same group bubble was
                # first stored without a sender. Re-key that row in place so a
                # profile-card result updates the Web IM instead of creating a
                # duplicate message.
                if message.sender:
                    occurrence = int(message.raw.get("visible_occurrence", 0) or 0)
                    candidates = db.execute(
                        """
                        SELECT seq, raw_json FROM messages
                        WHERE chat_id = ? AND content = ? AND direction = ?
                          AND message_type = ? AND sender IS NULL
                          AND ((timestamp = ?) OR (timestamp IS NULL AND ? IS NULL))
                        ORDER BY seq DESC LIMIT 20
                        """,
                        (
                            chat.id,
                            message.content,
                            message.direction.value,
                            message.type.value,
                            timestamp,
                            timestamp,
                        ),
                    ).fetchall()
                    for candidate in candidates:
                        try:
                            raw_candidate = json.loads(str(candidate["raw_json"] or "{}"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            raw_candidate = {}
                        if int(raw_candidate.get("visible_occurrence", 0) or 0) == occurrence:
                            db.execute(
                                "UPDATE messages SET message_id = ? WHERE seq = ?",
                                (message.id, int(candidate["seq"])),
                            )
                            existing = candidate
                            created = False
                            break
            if existing is None:
                # Qt can virtualize away the time separator at the top of the
                # list while leaving the same message row visible. Reconcile
                # that timestamp/no-timestamp form instead of persisting a
                # second phantom copy. The visible occurrence keeps two equal
                # bubbles in the same rendered list distinct.
                occurrence = int(message.raw.get("visible_occurrence", 0) or 0)
                candidates = db.execute(
                    """
                    SELECT seq, message_id, sender, timestamp, raw_json FROM messages
                    WHERE chat_id = ? AND content = ? AND direction = ?
                      AND message_type = ?
                      AND ((timestamp IS NULL AND ? IS NOT NULL)
                           OR (timestamp IS NOT NULL AND ? IS NULL))
                    ORDER BY seq DESC LIMIT 20
                    """,
                    (
                        chat.id,
                        message.content,
                        message.direction.value,
                        message.type.value,
                        timestamp,
                        timestamp,
                    ),
                ).fetchall()
                for candidate in candidates:
                    sender_matches = (candidate["sender"] or "") == (message.sender or "")
                    sender_can_be_enriched = (
                        message.chat_type is ChatType.GROUP
                        and message.direction is Direction.INCOMING
                        and (candidate["sender"] is None or message.sender is None)
                    )
                    if not sender_matches and not sender_can_be_enriched:
                        continue
                    try:
                        raw_candidate = json.loads(str(candidate["raw_json"] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        raw_candidate = {}
                    if int(raw_candidate.get("visible_occurrence", 0) or 0) == occurrence:
                        existing = candidate
                        created = False
                        if timestamp is not None and candidate["timestamp"] is None:
                            conflict = db.execute(
                                "SELECT seq FROM messages WHERE chat_id = ? AND message_id = ?",
                                (chat.id, message.id),
                            ).fetchone()
                            if conflict is not None and int(conflict["seq"]) != int(candidate["seq"]):
                                db.execute("DELETE FROM messages WHERE seq = ?", (int(candidate["seq"]),))
                                existing = conflict
                            else:
                                db.execute(
                                    "UPDATE messages SET message_id = ? WHERE seq = ?",
                                    (message.id, int(candidate["seq"])),
                                )
                        break
            if message.image is not None:
                existing_has_image = False
                if existing is not None:
                    image_row = db.execute(
                        "SELECT image_path, image_source FROM messages WHERE seq = ?",
                        (int(existing["seq"]),),
                    ).fetchone()
                    existing_has_image = bool(image_row and image_row["image_path"])
                    replace_image = bool(
                        existing_has_image
                        and self._image_source_rank(message.image.source)
                        > self._image_source_rank(image_row["image_source"])
                    )
                    if replace_image:
                        replaced_image_path = str(image_row["image_path"])
                if not existing_has_image or replace_image:
                    # Persist only after every message-ID/time/sender
                    # reconciliation path has settled on its final row. This
                    # prevents an offscreen variant from leaving an orphaned
                    # PNG before it is merged into an image-bearing record.
                    image_values = self._persist_image(message)
            if existing is None:
                cursor = db.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, sender, sender_organization,
                        sender_profile_checked, content, message_type,
                        direction, timestamp, observed_at, is_at_me, raw_json,
                        image_path, image_mime_type, image_width, image_height,
                        image_sha256, image_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        chat.id,
                        message.sender,
                        message.sender_organization,
                        sender_profile_checked,
                        message.content,
                        message.type.value,
                        message.direction.value,
                        timestamp,
                        observed_at,
                        int(message.is_at_me),
                        raw,
                        *image_values,
                    ),
                )
                seq = int(cursor.lastrowid)
            else:
                seq = int(existing["seq"])
                db.execute(
                    """
                    UPDATE messages SET sender = COALESCE(?, sender),
                        sender_organization = COALESCE(?, sender_organization),
                        sender_profile_checked = MAX(sender_profile_checked, ?), content = ?,
                        message_type = ?, direction = ?, timestamp = COALESCE(?, timestamp),
                        is_at_me = ?, raw_json = ?,
                        image_path = COALESCE(image_path, ?),
                        image_mime_type = COALESCE(image_mime_type, ?),
                        image_width = COALESCE(image_width, ?),
                        image_height = COALESCE(image_height, ?),
                        image_sha256 = COALESCE(image_sha256, ?),
                        image_source = COALESCE(image_source, ?),
                        delivery_status = CASE
                            WHEN ? = 'outgoing' AND delivery_status IN ('pending', 'unverified')
                            THEN 'sent' ELSE delivery_status END,
                        verified = CASE
                            WHEN ? = 'outgoing' AND delivery_status IN ('pending', 'unverified')
                            THEN 1 ELSE verified END,
                        delivery_error = CASE
                            WHEN ? = 'outgoing' THEN NULL ELSE delivery_error END
                        WHERE seq = ?
                    """,
                    (
                        message.sender,
                        message.sender_organization,
                        sender_profile_checked,
                        message.content,
                        message.type.value,
                        message.direction.value,
                        timestamp,
                        int(message.is_at_me),
                        raw,
                        *image_values,
                        message.direction.value,
                        message.direction.value,
                        message.direction.value,
                        seq,
                    ),
                )
                if replace_image:
                    db.execute(
                        """
                        UPDATE messages SET image_path = ?, image_mime_type = ?,
                            image_width = ?, image_height = ?, image_sha256 = ?, image_source = ?
                        WHERE seq = ?
                        """,
                        (*image_values, seq),
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
        if replaced_image_path:
            self._delete_image_if_unreferenced(replaced_image_path)
        return self._message_from_row(row), created

    def sender_profile_checked(self, seq: int) -> bool:
        """Return whether a verified profile card was read for this row."""

        with self._database() as db:
            row = db.execute(
                "SELECT sender_profile_checked FROM messages WHERE seq = ?",
                (int(seq),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown message seq {seq}")
        return bool(row["sender_profile_checked"])

    @staticmethod
    def _image_source_rank(source: str | None) -> int:
        return {"window_pixels": 1, "viewer_clipboard": 2}.get(str(source or ""), 0)

    def _delete_image_if_unreferenced(self, filename: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
            return
        with self._database() as db:
            count = db.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE image_path = ?", (filename,)
            ).fetchone()["total"]
        if int(count) == 0:
            try:
                (self.image_dir / filename).unlink()
            except FileNotFoundError:
                pass

    def _persist_image(
        self, message: Message
    ) -> tuple[str | None, str | None, int | None, int | None, str | None, str | None]:
        image = message.image
        if image is None:
            return None, None, None, None, None, None
        if image.mime_type != "image/png":
            raise ValueError(f"unsupported captured image type: {image.mime_type}")
        if not image.data or len(image.data) > 25 * 1024 * 1024:
            raise ValueError("captured image must be between 1 byte and 25 MiB")
        digest = hashlib.sha256(image.data).hexdigest()
        self.image_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{digest}.png"
        destination = self.image_dir / filename
        if not destination.exists():
            temporary = self.image_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(image.data)
            temporary.replace(destination)
        return (
            filename,
            image.mime_type,
            image.width,
            image.height,
            digest,
            image.source,
        )

    def image_asset(self, seq: int) -> tuple[Path, str, str]:
        with self._database() as db:
            row = db.execute(
                "SELECT image_path, image_mime_type, image_sha256 FROM messages WHERE seq = ?",
                (int(seq),),
            ).fetchone()
        if row is None or not row["image_path"]:
            raise KeyError(f"message {seq} has no captured image")
        filename = str(row["image_path"])
        if not re.fullmatch(r"[0-9a-f]{64}\.png", filename):
            raise ValueError("invalid stored image path")
        path = self.image_dir / filename
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, str(row["image_mime_type"] or "image/png"), str(row["image_sha256"])

    def queue_send(
        self,
        chat_id: int,
        content: str,
        *,
        client_id: str | None = None,
        mentions: Iterable[str] = (),
    ) -> tuple[StoredMessage, bool]:
        """Persist an outgoing message before the UI worker starts sending it."""

        chat = self.get_chat(chat_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("text must not be empty")
        if client_id is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", client_id):
            raise ValueError("client_id must contain only letters, numbers, '_' or '-'")
        message_id = f"local:{client_id or uuid.uuid4().hex}"
        queued_at = _utc_now()
        raw = json.dumps({"mentions": list(mentions)}, ensure_ascii=False)
        with self._database() as db:
            existing = db.execute(
                "SELECT seq, content FROM messages WHERE chat_id = ? AND client_id = ?",
                (chat.id, client_id),
            ).fetchone()
            if existing is not None and str(existing["content"]) != content:
                raise ValueError("client_id was already used for different text")
            created = existing is None
            if existing is None:
                cursor = db.execute(
                    """
                    INSERT INTO messages(
                        message_id, chat_id, sender, content, message_type,
                        direction, timestamp, observed_at, is_at_me, verified,
                        delivery_status, delivery_error, client_id, raw_json
                    ) VALUES (?, ?, 'self', ?, 'text', 'outgoing', ?, ?, 0, NULL,
                              'pending', NULL, ?, ?)
                    """,
                    (message_id, chat.id, content, queued_at, queued_at, client_id, raw),
                )
                seq = int(cursor.lastrowid)
                db.execute(
                    "UPDATE chats SET last_message_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                    (queued_at, queued_at, chat.id),
                )
            else:
                seq = int(existing["seq"])
            row = db.execute(
                """SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                   FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.seq = ?""",
                (seq,),
            ).fetchone()
        assert row is not None
        return self._message_from_row(row), created

    def complete_pending_send(self, seq: int, receipt: SendReceipt) -> StoredMessage:
        with self._database() as db:
            current = db.execute("SELECT * FROM messages WHERE seq = ?", (int(seq),)).fetchone()
            if current is None:
                raise KeyError(f"unknown pending message seq {seq}")
            message_id = receipt.matched_message_id or str(current["message_id"])
            conflict = db.execute(
                "SELECT seq FROM messages WHERE chat_id = ? AND message_id = ? AND seq != ?",
                (int(current["chat_id"]), message_id, int(seq)),
            ).fetchone()
            if conflict is not None:
                db.execute("DELETE FROM messages WHERE seq = ?", (int(seq),))
                seq = int(conflict["seq"])
            db.execute(
                """
                UPDATE messages SET message_id = ?, content = ?, timestamp = ?,
                    observed_at = ?, verified = ?, delivery_status = ?, delivery_error = NULL
                WHERE seq = ?
                """,
                (
                    message_id,
                    receipt.content,
                    receipt.sent_at.isoformat(),
                    _utc_now(),
                    int(receipt.verified),
                    "sent" if receipt.verified else "unverified",
                    int(seq),
                ),
            )
            db.execute(
                "UPDATE chats SET last_message_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (receipt.sent_at.isoformat(), _utc_now(), int(current["chat_id"])),
            )
            row = db.execute(
                """SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                   FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.seq = ?""",
                (int(seq),),
            ).fetchone()
        assert row is not None
        return self._message_from_row(row)

    def fail_pending_send(self, seq: int, error: str) -> StoredMessage:
        with self._database() as db:
            db.execute(
                "UPDATE messages SET delivery_status = 'failed', delivery_error = ?, verified = 0 WHERE seq = ?",
                (str(error), int(seq)),
            )
            row = db.execute(
                """SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                   FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.seq = ?""",
                (int(seq),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown pending message seq {seq}")
        return self._message_from_row(row)

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
                        direction, timestamp, observed_at, is_at_me, verified,
                        delivery_status, delivery_error, raw_json
                    ) VALUES (?, ?, 'self', ?, 'text', 'outgoing', ?, ?, 0, ?, ?, NULL, '{}')
                    """,
                    (
                        message_id,
                        chat.id,
                        receipt.content,
                        receipt.sent_at.isoformat(),
                        observed_at,
                        int(receipt.verified),
                        "sent" if receipt.verified else "unverified",
                    ),
                )
                seq = int(cursor.lastrowid)
            else:
                seq = int(row["seq"])
                db.execute(
                    "UPDATE messages SET verified = ?, content = ?, delivery_status = ?, delivery_error = NULL WHERE seq = ?",
                    (
                        int(receipt.verified),
                        receipt.content,
                        "sent" if receipt.verified else "unverified",
                        seq,
                    ),
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

    def messages_before(
        self,
        chat_id: int,
        before_seq: int,
        *,
        limit: int = 200,
    ) -> list[StoredMessage]:
        """Return an older page in display order for history pagination."""

        bounded_limit = min(max(int(limit), 1), 1000)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.chat_id = ? AND m.seq < ?
                ORDER BY m.seq DESC LIMIT ?
                """,
                (int(chat_id), int(before_seq), bounded_limit),
            ).fetchall()
        return [self._message_from_row(row) for row in reversed(rows)]

    def messages_after(self, after_seq: int = 0, *, limit: int = 200) -> list[StoredMessage]:
        """Return the global durable message stream in sequence order."""

        bounded_limit = min(max(int(limit), 1), 1000)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.seq > ? ORDER BY m.seq ASC LIMIT ?
                """,
                (int(after_seq), bounded_limit),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def message_history(
        self,
        chat_id: int,
        *,
        since: str,
        until: str,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[StoredMessage]:
        """Return one complete-time-window page in durable sequence order."""

        bounded_limit = min(max(int(limit), 1), 1000)
        with self._database() as db:
            rows = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.chat_id = ? AND m.seq > ?
                  AND julianday(COALESCE(m.timestamp, m.observed_at)) >= julianday(?)
                  AND julianday(COALESCE(m.timestamp, m.observed_at)) <= julianday(?)
                ORDER BY m.seq ASC LIMIT ?
                """,
                (int(chat_id), int(after_seq), since, until, bounded_limit),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def get_message(self, seq: int) -> StoredMessage:
        with self._database() as db:
            row = db.execute(
                """
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type
                FROM messages m JOIN chats c ON c.id = m.chat_id
                WHERE m.seq = ?
                """,
                (int(seq),),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown message seq {seq}")
        return self._message_from_row(row)

    def claim_agent_messages(
        self,
        consumer: str,
        *,
        chat_ids: Iterable[int] = (),
        limit: int = 8,
        lease_seconds: float = 60.0,
        require_sender: bool = False,
        require_at_me: bool = False,
        replay_existing: bool = False,
        deny_all_chats: bool = False,
    ) -> list[AgentDelivery]:
        """Atomically lease incoming messages to one durable Agent consumer.

        A handler acknowledges only after its side effects succeed. An
        unacknowledged lease becomes available again after ``lease_seconds``,
        providing at-least-once delivery without relying on recycled UI IDs.
        """

        name = self._validate_agent_name(consumer, field="consumer")
        bounded_limit = min(max(int(limit), 1), 100)
        lease_value = min(max(float(lease_seconds), 5.0), 3600.0)
        selected_chat_ids = tuple(dict.fromkeys(int(item) for item in chat_ids))
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_until = (now + timedelta(seconds=lease_value)).isoformat()
        token = uuid.uuid4().hex
        chat_clause = ""
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            latest_row = db.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM messages").fetchone()
            initial_start = 0 if replay_existing else int(latest_row["seq"])
            db.execute(
                """
                INSERT INTO agent_consumers(consumer, start_seq, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(consumer) DO NOTHING
                """,
                (name, initial_start, now_text, now_text),
            )
            db.execute(
                """
                UPDATE agent_consumers
                SET updated_at = ?, last_claim_at = ?, last_claim_chat_ids_json = ?
                WHERE consumer = ?
                """,
                (
                    now_text,
                    now_text,
                    json.dumps(list(selected_chat_ids), ensure_ascii=False),
                    name,
                ),
            )
            consumer_row = db.execute(
                "SELECT start_seq FROM agent_consumers WHERE consumer = ?", (name,)
            ).fetchone()
            start_seq = int(consumer_row["start_seq"])
            parameters: list[Any] = [
                name,
                start_seq,
                now_text,
                now_text,
                int(require_sender),
                int(require_at_me),
            ]
            if selected_chat_ids:
                placeholders = ",".join("?" for _ in selected_chat_ids)
                chat_clause = f" AND m.chat_id IN ({placeholders})"
                parameters.extend(selected_chat_ids)
            elif deny_all_chats:
                chat_clause = " AND 0"
            parameters.append(bounded_limit)
            rows = db.execute(
                f"""
                SELECT m.*, c.name AS chat, c.chat_type AS chat_type,
                       COALESCE(d.attempts, 0) AS delivery_attempts
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                LEFT JOIN agent_deliveries d
                  ON d.consumer = ? AND d.message_seq = m.seq
                WHERE m.direction = 'incoming'
                  AND m.delivery_status = 'observed'
                  AND m.seq > ?
                  AND (
                    d.message_seq IS NULL OR
                    (d.status = 'retry' AND (d.lease_until IS NULL OR d.lease_until <= ?)) OR
                    (d.status = 'leased' AND d.lease_until <= ?)
                  )
                  AND (? = 0 OR m.sender IS NOT NULL)
                  AND (? = 0 OR m.is_at_me = 1)
                  {chat_clause}
                ORDER BY m.seq ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
            deliveries: list[AgentDelivery] = []
            for row in rows:
                seq = int(row["seq"])
                attempt = int(row["delivery_attempts"]) + 1
                db.execute(
                    """
                    INSERT INTO agent_deliveries(
                        consumer, message_seq, status, lease_token, lease_until,
                        attempts, last_error, claimed_at, completed_at
                    ) VALUES (?, ?, 'leased', ?, ?, ?, NULL, ?, NULL)
                    ON CONFLICT(consumer, message_seq) DO UPDATE SET
                        status = 'leased', lease_token = excluded.lease_token,
                        lease_until = excluded.lease_until,
                        attempts = agent_deliveries.attempts + 1,
                        last_error = NULL, claimed_at = excluded.claimed_at,
                        completed_at = NULL
                    """,
                    (name, seq, token, lease_until, attempt, now_text),
                )
                deliveries.append(AgentDelivery(
                    consumer=name,
                    message=self._message_from_row(row),
                    lease_token=token,
                    lease_until=lease_until,
                    attempt=attempt,
                ))
        return deliveries

    def get_agent_consumer_activity(self, consumer: str) -> dict[str, Any]:
        name = self._validate_agent_name(consumer, field="consumer")
        with self._database() as db:
            row = db.execute(
                """
                SELECT last_claim_chat_ids_json, last_claim_at
                FROM agent_consumers WHERE consumer = ?
                """,
                (name,),
            ).fetchone()
        if row is None:
            return {"last_claim_chat_ids": [], "last_claim_at": None}
        try:
            chat_ids = json.loads(str(row["last_claim_chat_ids_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            chat_ids = []
        return {
            "last_claim_chat_ids": [int(item) for item in chat_ids]
            if isinstance(chat_ids, list)
            else [],
            "last_claim_at": row["last_claim_at"],
        }

    def ack_agent_message(self, consumer: str, message_seq: int, lease_token: str) -> None:
        name = self._validate_agent_name(consumer, field="consumer")
        token = self._validate_lease_token(lease_token)
        with self._database() as db:
            cursor = db.execute(
                """
                UPDATE agent_deliveries
                SET status = 'done', lease_token = NULL, lease_until = NULL,
                    last_error = NULL, completed_at = ?
                WHERE consumer = ? AND message_seq = ?
                  AND status = 'leased' AND lease_token = ?
                """,
                (_utc_now(), name, int(message_seq), token),
            )
        if cursor.rowcount != 1:
            raise ValueError("delivery lease is missing, expired or already completed")

    def nack_agent_message(
        self,
        consumer: str,
        message_seq: int,
        lease_token: str,
        *,
        error: str = "",
        retry_delay: float = 5.0,
    ) -> None:
        name = self._validate_agent_name(consumer, field="consumer")
        token = self._validate_lease_token(lease_token)
        clean_error = str(error)[:2000]
        delay = min(max(float(retry_delay), 0.0), 300.0)
        available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with self._database() as db:
            cursor = db.execute(
                """
                UPDATE agent_deliveries
                SET status = 'retry', lease_token = NULL, lease_until = ?,
                    last_error = ?, completed_at = NULL
                WHERE consumer = ? AND message_seq = ?
                  AND status = 'leased' AND lease_token = ?
                """,
                (available_at, clean_error, name, int(message_seq), token),
            )
        if cursor.rowcount != 1:
            raise ValueError("delivery lease is missing, expired or already completed")

    def append_agent_log(
        self,
        agent: str,
        level: str,
        message: str,
        *,
        event: str = "log",
        context: dict[str, Any] | None = None,
    ) -> AgentLog:
        name = self._validate_agent_name(agent, field="agent")
        normalized_level = str(level).lower()
        if normalized_level not in {"debug", "info", "warning", "error"}:
            raise ValueError("level must be debug, info, warning or error")
        clean_event = str(event).strip()
        if not clean_event or len(clean_event) > 100:
            raise ValueError("event must be between 1 and 100 characters")
        clean_message = str(message)
        if not clean_message or len(clean_message) > 20000:
            raise ValueError("message must be between 1 and 20000 characters")
        payload = context or {}
        if not isinstance(payload, dict):
            raise ValueError("context must be an object")
        context_json = json.dumps(payload, ensure_ascii=False, default=str)
        if len(context_json.encode("utf-8")) > 65536:
            raise ValueError("context must be at most 65536 bytes")
        created_at = _utc_now()
        with self._database() as db:
            cursor = db.execute(
                """
                INSERT INTO agent_logs(agent, level, event, message, context_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, normalized_level, clean_event, clean_message, context_json, created_at),
            )
            seq = int(cursor.lastrowid)
        return AgentLog(seq, name, normalized_level, clean_event, clean_message, payload, created_at)

    def get_agent_logs(
        self,
        *,
        agent: str | None = None,
        after_seq: int = 0,
        before_seq: int | None = None,
        limit: int = 200,
        recent: bool = False,
    ) -> list[AgentLog]:
        bounded_limit = min(max(int(limit), 1), 1000)
        selected_agent = (
            self._validate_agent_name(agent, field="agent") if agent is not None else None
        )
        agent_clause = "agent = ? AND " if selected_agent else ""
        prefix: tuple[Any, ...] = (selected_agent,) if selected_agent else ()
        if recent:
            query = f"SELECT * FROM agent_logs WHERE {agent_clause}1 = 1 ORDER BY seq DESC LIMIT ?"
            parameters: tuple[Any, ...] = prefix + (bounded_limit,)
        elif before_seq is not None:
            query = f"SELECT * FROM agent_logs WHERE {agent_clause}seq < ? ORDER BY seq DESC LIMIT ?"
            parameters = prefix + (int(before_seq), bounded_limit)
        else:
            query = f"SELECT * FROM agent_logs WHERE {agent_clause}seq > ? ORDER BY seq ASC LIMIT ?"
            parameters = prefix + (int(after_seq), bounded_limit)
        with self._database() as db:
            rows = db.execute(query, parameters).fetchall()
        values = [self._agent_log_from_row(row) for row in rows]
        return list(reversed(values)) if recent or before_seq is not None else values

    def list_agent_instances(self) -> list[AgentInstance]:
        with self._database() as db:
            rows = db.execute(
                "SELECT * FROM agent_instances ORDER BY created_at ASC"
            ).fetchall()
        return [self._agent_instance_from_row(row) for row in rows]

    def get_agent_instance(self, instance_id: str) -> AgentInstance:
        clean_id = self._validate_agent_name(instance_id, field="instance id")
        with self._database() as db:
            row = db.execute(
                "SELECT * FROM agent_instances WHERE id = ?", (clean_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown agent instance {clean_id}")
        return self._agent_instance_from_row(row)

    def create_agent_instance(
        self,
        instance_id: str,
        plugin_id: str,
        name: str,
        *,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        permissions: dict[str, Any] | None = None,
        autostart: bool = False,
    ) -> AgentInstance:
        clean_id = self._validate_agent_name(instance_id, field="instance id")
        clean_plugin = self._validate_agent_name(plugin_id, field="plugin id")
        clean_name = " ".join(str(name).split())
        if not clean_name or len(clean_name) > 80:
            raise ValueError("agent name must be between 1 and 80 characters")
        config_value = self._validate_json_object(config or {}, field="config")
        secret_value = self._validate_secrets(secrets or {})
        permission_value = self._validate_json_object(
            permissions or self.default_agent_permissions(), field="permissions"
        )
        now = _utc_now()
        with self._database() as db:
            try:
                db.execute(
                    """
                    INSERT INTO agent_instances(
                        id, plugin_id, name, config_json, secrets_json, permissions_json, autostart,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_id,
                        clean_plugin,
                        clean_name,
                        json.dumps(config_value, ensure_ascii=False),
                        json.dumps(secret_value, ensure_ascii=False),
                        json.dumps(permission_value, ensure_ascii=False),
                        int(bool(autostart)),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("agent instance id or name already exists") from exc
        return self.get_agent_instance(clean_id)

    def update_agent_instance(
        self,
        instance_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str | None] | None = None,
        permissions: dict[str, Any] | None = None,
        autostart: bool | None = None,
    ) -> AgentInstance:
        current = self.get_agent_instance(instance_id)
        next_name = current.name if name is None else " ".join(str(name).split())
        if not next_name or len(next_name) > 80:
            raise ValueError("agent name must be between 1 and 80 characters")
        next_config = current.config if config is None else self._validate_json_object(
            config, field="config"
        )
        next_secrets = dict(current.secrets)
        next_permissions = (
            current.permissions
            if permissions is None
            else self._validate_json_object(permissions, field="permissions")
        )
        if secrets is not None:
            if not isinstance(secrets, dict):
                raise ValueError("secrets must be an object")
            for key, value in secrets.items():
                clean_key = self._validate_secret_key(key)
                if value is None or value == "":
                    next_secrets.pop(clean_key, None)
                else:
                    next_secrets[clean_key] = self._validate_secret_value(value)
        next_autostart = current.autostart if autostart is None else bool(autostart)
        with self._database() as db:
            try:
                db.execute(
                    """
                    UPDATE agent_instances
                    SET name = ?, config_json = ?, secrets_json = ?, permissions_json = ?, autostart = ?,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        next_name,
                        json.dumps(next_config, ensure_ascii=False),
                        json.dumps(next_secrets, ensure_ascii=False),
                        json.dumps(next_permissions, ensure_ascii=False),
                        int(next_autostart),
                        _utc_now(),
                        current.id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("agent instance name already exists") from exc
        return self.get_agent_instance(current.id)

    def delete_agent_instance(self, instance_id: str) -> None:
        current = self.get_agent_instance(instance_id)
        with self._database() as db:
            db.execute("DELETE FROM agent_instances WHERE id = ?", (current.id,))

    def upsert_agent_panel(
        self,
        agent: str,
        panel_id: str,
        title: str,
        document: dict[str, Any],
    ) -> AgentPanel:
        clean_agent = self._validate_agent_name(agent, field="agent")
        clean_panel_id = self._validate_agent_name(panel_id, field="panel id")
        clean_title = " ".join(str(title).split())
        if not clean_title or len(clean_title) > 80:
            raise ValueError("panel title must be between 1 and 80 characters")
        payload = self._validate_json_object(document, field="panel document")
        updated_at = _utc_now()
        with self._database() as db:
            try:
                db.execute(
                    """
                    INSERT INTO agent_panels(agent, panel_id, title, document_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(agent, panel_id) DO UPDATE SET
                        title = excluded.title,
                        document_json = excluded.document_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        clean_agent,
                        clean_panel_id,
                        clean_title,
                        json.dumps(payload, ensure_ascii=False),
                        updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KeyError(f"unknown agent instance {clean_agent}") from exc
        return self.get_agent_panel(clean_agent, clean_panel_id)

    def list_agent_panels(self, agent: str) -> list[AgentPanel]:
        clean_agent = self._validate_agent_name(agent, field="agent")
        with self._database() as db:
            rows = db.execute(
                "SELECT * FROM agent_panels WHERE agent = ? ORDER BY panel_id",
                (clean_agent,),
            ).fetchall()
        return [self._agent_panel_from_row(row) for row in rows]

    def get_agent_panel(self, agent: str, panel_id: str) -> AgentPanel:
        clean_agent = self._validate_agent_name(agent, field="agent")
        clean_panel_id = self._validate_agent_name(panel_id, field="panel id")
        with self._database() as db:
            row = db.execute(
                "SELECT * FROM agent_panels WHERE agent = ? AND panel_id = ?",
                (clean_agent, clean_panel_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown panel {clean_panel_id} for agent {clean_agent}")
        return self._agent_panel_from_row(row)

    def delete_agent_panel(self, agent: str, panel_id: str) -> None:
        panel = self.get_agent_panel(agent, panel_id)
        with self._database() as db:
            db.execute(
                "DELETE FROM agent_panels WHERE agent = ? AND panel_id = ?",
                (panel.agent, panel.panel_id),
            )

    @staticmethod
    def default_agent_permissions() -> dict[str, Any]:
        return {
            "read": {"mode": "all", "chat_ids": [], "patterns": []},
            "write": {"mode": "all", "chat_ids": [], "patterns": []},
        }

    @staticmethod
    def _validate_json_object(value: Any, *, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) > 262144:
            raise ValueError(f"{field} must be at most 262144 bytes")
        return json.loads(encoded)

    @classmethod
    def _validate_secrets(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("secrets must be an object")
        return {
            cls._validate_secret_key(key): cls._validate_secret_value(secret)
            for key, secret in value.items()
            if secret is not None and secret != ""
        }

    @staticmethod
    def _validate_secret_key(value: Any) -> str:
        clean = str(value).strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", clean):
            raise ValueError("secret names must be uppercase environment variable names")
        return clean

    @staticmethod
    def _validate_secret_value(value: Any) -> str:
        clean = str(value)
        if "\x00" in clean or "\n" in clean or "\r" in clean or len(clean) > 8192:
            raise ValueError("secret values must be single-line strings up to 8192 characters")
        return clean

    @staticmethod
    def _validate_agent_name(value: str, *, field: str) -> str:
        clean = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", clean):
            raise ValueError(f"{field} must use 1-80 letters, numbers, '.', '_', ':' or '-'")
        return clean

    @staticmethod
    def _validate_lease_token(value: str) -> str:
        token = str(value)
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise ValueError("invalid delivery lease token")
        return token

    @staticmethod
    def _agent_log_from_row(row: sqlite3.Row) -> AgentLog:
        try:
            context = json.loads(str(row["context_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            context = {}
        return AgentLog(
            seq=int(row["seq"]),
            agent=str(row["agent"]),
            level=str(row["level"]),
            event=str(row["event"]),
            message=str(row["message"]),
            context=context if isinstance(context, dict) else {},
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _agent_instance_from_row(row: sqlite3.Row) -> AgentInstance:
        try:
            config = json.loads(str(row["config_json"] or "{}"))
            secrets = json.loads(str(row["secrets_json"] or "{}"))
            permissions = json.loads(str(row["permissions_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            config, secrets, permissions = {}, {}, {}
        return AgentInstance(
            id=str(row["id"]),
            plugin_id=str(row["plugin_id"]),
            name=str(row["name"]),
            config=config if isinstance(config, dict) else {},
            secrets=secrets if isinstance(secrets, dict) else {},
            permissions=(
                permissions
                if isinstance(permissions, dict) and permissions
                else SQLiteStore.default_agent_permissions()
            ),
            autostart=bool(row["autostart"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _agent_panel_from_row(row: sqlite3.Row) -> AgentPanel:
        try:
            document = json.loads(str(row["document_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            document = {}
        return AgentPanel(
            agent=str(row["agent"]),
            panel_id=str(row["panel_id"]),
            title=str(row["title"]),
            document=document if isinstance(document, dict) else {},
            updated_at=str(row["updated_at"]),
        )

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
            sender_organization=row["sender_organization"],
            content=str(row["content"]),
            message_type=str(row["message_type"]),
            direction=str(row["direction"]),
            timestamp=row["timestamp"],
            observed_at=str(row["observed_at"]),
            is_at_me=bool(row["is_at_me"]),
            verified=None if row["verified"] is None else bool(row["verified"]),
            delivery_status=str(row["delivery_status"]),
            delivery_error=row["delivery_error"],
            client_id=row["client_id"],
            image_url=(
                f"/api/messages/{int(row['seq'])}/image?v={row['image_sha256']}"
                if row["image_path"] else None
            ),
            image_mime_type=row["image_mime_type"],
            image_width=None if row["image_width"] is None else int(row["image_width"]),
            image_height=None if row["image_height"] is None else int(row["image_height"]),
            image_sha256=row["image_sha256"],
            image_source=row["image_source"],
        )

    def import_messages(self, messages: Iterable[Message]) -> int:
        return sum(1 for message in messages if self.save_message(message)[1])

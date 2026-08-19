import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bowxt.models import ChatType, Direction, Message, MessageType, SendReceipt
from bowxt.store import SQLiteStore


def incoming(message_id="m1", chat="测试群", content="你好"):
    return Message(
        id=message_id,
        chat=chat,
        content=content,
        type=MessageType.TEXT,
        direction=Direction.INCOMING,
        sender="Alice",
        timestamp=datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc),
        chat_type=ChatType.GROUP,
    )


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "messages.db"
        self.store = SQLiteStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_chats_and_messages_survive_reopen_and_dedupe(self):
        first, created = self.store.save_message(incoming())
        same, created_again = self.store.save_message(incoming())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.seq, same.seq)

        reopened = SQLiteStore(self.path)
        chats = reopened.list_chats()
        self.assertEqual([(item.name, item.chat_type) for item in chats], [("测试群", ChatType.GROUP)])
        self.assertEqual([item.content for item in reopened.get_messages(chats[0].id)], ["你好"])

    def test_manual_type_is_not_downgraded_by_unknown_discovery(self):
        chat = self.store.upsert_chat("项目群", ChatType.GROUP, source="manual")
        discovered = self.store.upsert_chat("项目群", ChatType.UNKNOWN, source="unread")
        self.assertEqual(discovered.id, chat.id)
        self.assertEqual(discovered.chat_type, ChatType.GROUP)
        self.assertEqual(discovered.source, "manual")

    def test_verified_receipt_and_observed_echo_share_one_row(self):
        receipt = SendReceipt(
            chat="张三",
            content="测试",
            sent_at=datetime(2026, 8, 19, 12, 1, tzinfo=timezone.utc),
            verified=False,
        )
        local, _ = self.store.save_receipt(receipt, ChatType.CONTACT)
        echo = Message(
            id="wechat-id",
            chat="张三",
            content="测试",
            type=MessageType.TEXT,
            direction=Direction.OUTGOING,
            chat_type=ChatType.CONTACT,
        )
        reconciled, created = self.store.save_message(echo)
        self.assertFalse(created)
        self.assertEqual(local.seq, reconciled.seq)
        self.assertEqual(reconciled.message_id, "wechat-id")
        self.assertEqual(len(self.store.get_messages(local.chat_id)), 1)

    def test_recent_messages_returns_latest_rows_in_display_order(self):
        for index in range(3):
            self.store.save_message(incoming(f"m{index}", content=str(index)))
        chat = self.store.list_chats()[0]
        self.assertEqual(
            [item.content for item in self.store.recent_messages(chat.id, limit=2)],
            ["1", "2"],
        )

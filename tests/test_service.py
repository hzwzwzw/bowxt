import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bowxt.models import ChatType, Direction, Message, MessageType, SendReceipt
from bowxt.service import BowxtService
from bowxt.store import SQLiteStore


class FakeServiceClient:
    def __init__(self):
        self.connected = False
        self.calls = []
        self.sent = []
        self.unread = []
        self._counter = 0

    def connect(self):
        self.connected = True

    @property
    def is_main_ui_ready(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def discover_unread_chats(self, *, limit=1):
        values = self.unread[:limit]
        self.unread = self.unread[limit:]
        return values

    def get_visible_messages(self, chat, *, chat_type):
        self.calls.append((chat, ChatType(chat_type)))
        return [Message(
            id=f"{chat}-1", chat=chat, content=f"来自{chat}", type=MessageType.TEXT,
            direction=Direction.INCOMING, chat_type=ChatType(chat_type),
        )]

    def send_text(self, chat, text, *, chat_type, mentions=()):
        self._counter += 1
        self.sent.append((chat, text, ChatType(chat_type)))
        return SendReceipt(
            chat=chat,
            content=text,
            sent_at=datetime.now(timezone.utc),
            verified=True,
            matched_message_id=f"sent-{self._counter}",
            mentions=tuple(mentions),
        )


class BowxtServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStore(Path(self.temp.name) / "messages.db")
        self.client = FakeServiceClient()

    def tearDown(self):
        self.temp.cleanup()

    def test_poll_keeps_each_chat_type_and_persists_both(self):
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        group = service.add_chat("群", ChatType.GROUP)
        contact = service.add_chat("联系人", ChatType.CONTACT)
        service._poll_chat(self.client, group)
        service._poll_chat(self.client, contact)
        self.assertEqual(self.client.calls, [("群", ChatType.GROUP), ("联系人", ChatType.CONTACT)])
        self.assertEqual(len(self.store.get_messages(group.id)), 1)
        self.assertEqual(len(self.store.get_messages(contact.id)), 1)

    def test_concurrent_callers_are_serialized_through_one_worker(self):
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        one = service.add_chat("一", ChatType.CONTACT)
        two = service.add_chat("二", ChatType.CONTACT)
        service.start()
        deadline = time.monotonic() + 2
        while not service.status()["wechat_connected"] and time.monotonic() < deadline:
            time.sleep(0.01)
        results = []
        threads = [
            threading.Thread(target=lambda chat=chat: results.append(service.send_text(chat.id, chat.name)))
            for chat in (one, two)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        service.stop()
        self.assertEqual({item.content for item in results}, {"一", "二"})
        self.assertEqual(len(self.client.sent), 2)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_unread_discovery_persists_and_emits_a_new_chat(self):
        self.client.unread = ["自动会话"]
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        subscriber = service.events.subscribe()
        service.start()
        deadline = time.monotonic() + 2
        while not self.store.list_chats() and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        self.assertEqual([chat.name for chat in self.store.list_chats()], ["自动会话"])
        events = []
        while not subscriber.empty():
            events.append(subscriber.get_nowait())
        self.assertTrue(any(event.get("type") == "chat" for event in events))

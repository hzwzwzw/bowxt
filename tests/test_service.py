import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bowxt.models import ChatType, Direction, Message, MessageImage, MessageType, Rect, SendReceipt
from bowxt.errors import ServicePaused
from bowxt.service import BowxtService, SyncMode
from bowxt.store import SQLiteStore


class FakeServiceClient:
    def __init__(self):
        self.connected = False
        self.uia_sender = False
        self.calls = []
        self.sent = []
        self.unread = []
        self._counter = 0
        self.send_started = threading.Event()
        self.send_gate = None
        self.operation_delays = []
        self.visible_chat = None
        self.is_input_blocked = False

    def connect(self):
        self.connected = True

    @property
    def is_main_ui_ready(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def set_operation_delay(self, value):
        self.operation_delays.append(value)

    def discover_unread_chats(self, *, limit=1):
        values = self.unread[:limit]
        self.unread = self.unread[limit:]
        if values:
            self.visible_chat = values[-1]
        return values

    def visible_chat_name(self):
        return self.visible_chat

    def get_visible_messages(self, chat, *, chat_type, enrich_senders=True):
        self.visible_chat = chat
        self.calls.append((chat, ChatType(chat_type)))
        return [Message(
            id=f"{chat}-1", chat=chat, content=f"来自{chat}", type=MessageType.TEXT,
            direction=Direction.INCOMING, chat_type=ChatType(chat_type),
        )]

    def enrich_visible_senders(self, messages, *, chat=None):
        return list(messages)

    def send_text(self, chat, text, *, chat_type, mentions=()):
        self.send_started.set()
        if self.send_gate is not None:
            self.send_gate.wait(3)
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

    def test_default_client_reads_configured_account_aliases(self):
        service = BowxtService(self.store, poll_gap=1.5)
        with patch.dict("os.environ", {"BOWXT_MY_NAMES": "kirotta, bowxt "}):
            client = service._default_client()

        self.assertEqual(client.parser.my_names, ("kirotta", "bowxt"))

    def test_worker_discards_a_safety_locked_client_and_reconnects(self):
        clients = [FakeServiceClient(), FakeServiceClient()]
        clients[0].is_input_blocked = True

        def factory():
            return clients.pop(0)

        service = BowxtService(
            self.store,
            client_factory=factory,
            poll_gap=1.5,
            sync_mode=SyncMode.UNREAD,
        )
        first = clients[0]
        second = clients[1]
        service.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not second.connected:
                time.sleep(0.02)
            self.assertFalse(first.connected)
            self.assertTrue(second.connected)
            self.assertTrue(service.status()["wechat_connected"])
        finally:
            service.stop()

    def test_image_preview_is_queued_then_upgraded_from_viewer(self):
        class ImageClient(FakeServiceClient):
            def get_visible_messages(self, chat, *, chat_type, enrich_senders=True):
                return [Message(
                    id="image-1", chat=chat, content="[图片]", type=MessageType.IMAGE,
                    direction=Direction.INCOMING, chat_type=ChatType(chat_type),
                    bounds=Rect(250, 200, 650, 150),
                    image=MessageImage(b"preview", width=110, height=234),
                    raw={"image_bounds": {"x": 300, "y": 220, "width": 110, "height": 234}},
                )]

            def extract_visible_image(self, message, *, chat=None):
                return replace(
                    message,
                    image=MessageImage(
                        b"original", width=1200, height=2670, source="viewer_clipboard"
                    ),
                    raw={**message.raw, "image_source": "viewer_clipboard"},
                )

        client = ImageClient()
        service = BowxtService(self.store, client_factory=lambda: client, poll_gap=1.5)
        chat = service.add_chat("图片会话", ChatType.CONTACT)

        service._poll_chat(client, chat)
        self.assertEqual(service.status()["image_queue_depth"], 1)
        job = service._next_image_job()
        self.assertIsNotNone(job)
        service._execute_image_job(client, job)

        stored = self.store.get_messages(chat.id)[0]
        self.assertEqual(stored.image_source, "viewer_clipboard")
        self.assertEqual((stored.image_width, stored.image_height), (1200, 2670))

    def test_poll_enriches_every_new_group_message_in_the_same_burst(self):
        class BurstSenderClient(FakeServiceClient):
            def __init__(self):
                super().__init__()
                self.uia_sender = True
                self.enriched_ids = []

            def get_visible_messages(self, chat, *, chat_type, enrich_senders=True):
                self.calls.append((chat, ChatType(chat_type)))
                self.assert_no_inline_enrichment = enrich_senders
                return [
                    Message(
                        id=f"burst-{index}",
                        chat=chat,
                        content=f"群消息{index}",
                        type=MessageType.TEXT,
                        direction=Direction.INCOMING,
                        chat_type=ChatType.GROUP,
                    )
                    for index in (1, 2, 3)
                ]

            def enrich_visible_senders(self, messages, *, chat=None):
                values = list(messages)
                self.enriched_ids.append([item.id for item in values])
                return [replace(item, sender=f"成员{item.id.rsplit('-', 1)[-1]}") for item in values]

        client = BurstSenderClient()
        service = BowxtService(self.store, client_factory=lambda: client, poll_gap=1.5)
        group = service.add_chat("突发消息群", ChatType.GROUP)

        service._poll_chat(client, group)

        self.assertFalse(client.assert_no_inline_enrichment)
        self.assertEqual(service.status()["sender_queue_depth"], 3)
        while (job := service._next_sender_job()) is not None:
            service._execute_sender_job(client, job)
        self.assertEqual(client.enriched_ids, [["burst-1"], ["burst-2"], ["burst-3"]])
        self.assertEqual(
            [message.sender for message in self.store.get_messages(group.id)],
            ["成员1", "成员2", "成员3"],
        )

    def test_send_jumps_ahead_of_remaining_sender_jobs(self):
        class PriorityClient(FakeServiceClient):
            def __init__(self):
                super().__init__()
                self.uia_sender = True
                self.actions = []
                self.first_sender_started = threading.Event()
                self.first_sender_gate = threading.Event()

            def get_visible_messages(self, chat, *, chat_type, enrich_senders=True):
                return [
                    Message(
                        id=f"priority-{index}", chat=chat, content=f"新消息{index}",
                        type=MessageType.TEXT, direction=Direction.INCOMING,
                        chat_type=ChatType.GROUP,
                    )
                    for index in (1, 2)
                ]

            def enrich_visible_senders(self, messages, *, chat=None):
                item = list(messages)[0]
                self.actions.append(f"sender-{item.id}")
                if not self.first_sender_started.is_set():
                    self.first_sender_started.set()
                    self.first_sender_gate.wait(2)
                return [replace(item, sender="成员")]

            def send_text(self, chat, text, *, chat_type, mentions=()):
                self.actions.append("send")
                return super().send_text(chat, text, chat_type=chat_type, mentions=mentions)

        client = PriorityClient()
        service = BowxtService(self.store, client_factory=lambda: client, poll_gap=1.5)
        group = service.add_chat("优先发送群", ChatType.GROUP)
        service.start()
        self.assertTrue(client.first_sender_started.wait(2))

        service.enqueue_text(group.id, "紧急发送")
        client.first_sender_gate.set()
        deadline = time.monotonic() + 3
        while len(client.actions) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()

        self.assertEqual(client.actions[:3], ["sender-priority-1", "send", "sender-priority-2"])

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
        self.assertIn(("自动会话", ChatType.UNKNOWN), self.client.calls)
        events = []
        while not subscriber.empty():
            events.append(subscriber.get_nowait())
        self.assertTrue(any(event.get("type") == "chat" for event in events))

    def test_unread_mode_drains_all_badges_without_polling_quiet_chats(self):
        service = BowxtService(
            self.store,
            client_factory=lambda: self.client,
            poll_gap=1.5,
            sync_mode=SyncMode.UNREAD,
        )
        service.add_chat("安静会话", ChatType.CONTACT)
        self.client.unread = ["未读甲", "未读乙"]

        service.start()
        deadline = time.monotonic() + 2
        while len(self.client.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()

        called = [name for name, _kind in self.client.calls]
        self.assertIn("未读甲", called)
        self.assertIn("未读乙", called)
        self.assertNotIn("安静会话", called)
        self.assertEqual(service.status()["mode"], "unread")

    def test_pause_stops_connect_poll_and_send_until_resume(self):
        service = BowxtService(
            self.store,
            client_factory=lambda: self.client,
            poll_gap=1.5,
            sync_mode=SyncMode.UNREAD,
        )
        chat = service.add_chat("暂停会话", ChatType.CONTACT)
        service.configure(paused=True)
        service.start()
        time.sleep(0.08)
        self.assertFalse(self.client.connected)
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(ServicePaused):
            service.enqueue_text(chat.id, "不会排队")
        with self.assertRaises(ServicePaused):
            service.claim_agent_messages("paused-agent")

        self.client.unread = [chat.name]
        service.configure(paused=False)
        self.assertEqual(service.status()["mode"], "unread")
        deadline = time.monotonic() + 2
        while not self.client.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        self.assertTrue(self.client.calls)

    def test_async_send_returns_pending_and_accepts_another_while_busy(self):
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        chat = service.add_chat("异步会话", ChatType.CONTACT)
        self.client.send_gate = threading.Event()
        service.start()
        deadline = time.monotonic() + 2
        while not service.status()["wechat_connected"] and time.monotonic() < deadline:
            time.sleep(0.01)

        started = time.monotonic()
        first = service.enqueue_text(chat.id, "第一条", client_id="first")
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(first.delivery_status, "pending")
        self.assertTrue(self.client.send_started.wait(1))
        second = service.enqueue_text(chat.id, "第二条", client_id="second")
        self.assertEqual(second.delivery_status, "pending")
        self.assertGreaterEqual(service.status()["queue_depth"], 1)

        self.client.send_gate.set()
        deadline = time.monotonic() + 3
        while len(self.client.sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        self.assertEqual([item[1] for item in self.client.sent], ["第一条", "第二条"])
        self.assertIn("queue_wait_s", service.status()["last_send_timings"])
        self.assertIn("worker_total_s", service.status()["last_send_timings"])
        self.assertIn("end_to_end_s", service.status()["last_send_timings"])
        self.assertEqual(
            [
                item.delivery_status for item in self.store.get_messages(chat.id)
                if item.content in {"第一条", "第二条"}
            ],
            ["sent", "sent"],
        )

    def test_runtime_delays_can_be_changed_with_safe_bounds(self):
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        status = service.configure(poll_gap=3.5, action_delay=0.08)
        self.assertEqual(status["poll_gap"], 3.5)
        self.assertEqual(status["action_delay"], 0.08)
        self.assertEqual(service.configure(mode="unread")["mode"], "unread")
        self.assertEqual(service.configure(mode="paused")["mode"], "paused")
        self.assertEqual(service.configure(mode="polling")["mode"], "polling")
        with self.assertRaises(ValueError):
            service.configure(mode="invalid")
        with self.assertRaises(ValueError):
            service.configure(poll_gap=1.0)
        with self.assertRaises(ValueError):
            service.configure(action_delay=0.03)

        service.start()
        deadline = time.monotonic() + 2
        while not self.client.operation_delays and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        self.assertEqual(self.client.operation_delays[0], 0.08)

    def test_repeated_client_id_is_idempotent(self):
        service = BowxtService(self.store, client_factory=lambda: self.client, poll_gap=1.5)
        chat = service.add_chat("幂等会话", ChatType.CONTACT)
        service.configure(paused=True)
        service.start()
        service.configure(paused=False)
        first = service.enqueue_text(chat.id, "只发送一次", client_id="same-request")
        second = service.enqueue_text(chat.id, "只发送一次", client_id="same-request")
        deadline = time.monotonic() + 2
        while len(self.client.sent) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        service.stop()
        self.assertEqual(first.seq, second.seq)
        self.assertEqual(len([item for item in self.client.sent if item[1] == "只发送一次"]), 1)

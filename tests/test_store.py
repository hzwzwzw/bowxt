import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from bowxt.models import ChatType, Direction, Message, MessageImage, MessageType, SendReceipt
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

    def test_captured_image_is_persisted_and_exposed_by_message(self):
        payload = b"\x89PNG\r\n\x1a\nvisible-wechat-pixels"
        message = Message(
            id="picture-1",
            chat="hzw",
            content="[图片]",
            type=MessageType.IMAGE,
            direction=Direction.INCOMING,
            timestamp=datetime(2026, 8, 19, 12, 10, tzinfo=timezone.utc),
            chat_type=ChatType.CONTACT,
            image=MessageImage(payload, width=180, height=120),
            raw={"visible_occurrence": 0},
        )

        stored, created = self.store.save_message(message)
        path, mime_type, digest = self.store.image_asset(stored.seq)

        self.assertTrue(created)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(mime_type, "image/png")
        self.assertEqual(stored.image_url, f"/api/messages/{stored.seq}/image?v={digest}")
        self.assertEqual((stored.image_width, stored.image_height), (180, 120))
        self.assertEqual(stored.image_sha256, digest)

        same, created_again = self.store.save_message(
            replace(message, image=MessageImage(payload + b"hover-pixel", width=180, height=120))
        )
        self.assertFalse(created_again)
        self.assertEqual(same.image_sha256, digest)
        self.assertEqual(len(list(self.store.image_dir.glob("*.png"))), 1)

        original = MessageImage(
            payload + b"original", width=1200, height=2670, source="viewer_clipboard"
        )
        upgraded, upgraded_created = self.store.save_message(replace(message, image=original))
        self.assertFalse(upgraded_created)
        self.assertEqual(upgraded.image_source, "viewer_clipboard")
        self.assertEqual((upgraded.image_width, upgraded.image_height), (1200, 2670))
        self.assertNotEqual(upgraded.image_sha256, digest)
        self.assertIn(f"v={upgraded.image_sha256}", upgraded.image_url)
        self.assertEqual(len(list(self.store.image_dir.glob("*.png"))), 1)

        untimed, untimed_created = self.store.save_message(replace(
            message,
            id="picture-without-time",
            timestamp=None,
            image=MessageImage(payload + b"offscreen-variant", width=180, height=120),
        ))
        self.assertFalse(untimed_created)
        self.assertEqual(untimed.seq, stored.seq)
        self.assertEqual(untimed.image_sha256, upgraded.image_sha256)
        self.assertEqual(untimed.image_source, "viewer_clipboard")
        self.assertEqual(len(list(self.store.image_dir.glob("*.png"))), 1)

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

    def test_message_history_pages_are_returned_in_display_order(self):
        for index in range(5):
            self.store.save_message(incoming(f"page-{index}", content=str(index)))
        chat = self.store.list_chats()[0]
        recent = self.store.recent_messages(chat.id, limit=2)
        older = self.store.messages_before(chat.id, recent[0].seq, limit=2)
        self.assertEqual([item.content for item in recent], ["3", "4"])
        self.assertEqual([item.content for item in older], ["1", "2"])

    def test_agent_delivery_is_durable_idempotent_and_retryable(self):
        first, _ = self.store.save_message(incoming("agent-1", content="一"))
        second, _ = self.store.save_message(incoming("agent-2", content="二"))

        deliveries = self.store.claim_agent_messages(
            "demo-agent", limit=2, replay_existing=True
        )
        self.assertEqual([item.message.seq for item in deliveries], [first.seq, second.seq])
        self.assertEqual(self.store.claim_agent_messages("demo-agent"), [])

        self.store.ack_agent_message(
            "demo-agent", first.seq, deliveries[0].lease_token
        )
        self.store.nack_agent_message(
            "demo-agent", second.seq, deliveries[1].lease_token, error="retry", retry_delay=0
        )
        reopened = SQLiteStore(self.path)
        retry = reopened.claim_agent_messages("demo-agent")
        self.assertEqual([item.message.seq for item in retry], [second.seq])
        self.assertEqual(retry[0].attempt, 2)

    def test_agent_consumers_are_independent_and_logs_are_paginated(self):
        message, _ = self.store.save_message(incoming("shared-agent-message"))
        one = self.store.claim_agent_messages("agent-one", replay_existing=True)
        two = self.store.claim_agent_messages("agent-two", replay_existing=True)
        self.assertEqual(one[0].message.seq, message.seq)
        self.assertEqual(two[0].message.seq, message.seq)

        for index, level in enumerate(("info", "warning", "error")):
            self.store.append_agent_log(
                "agent-one", level, f"line {index}", event="test", context={"index": index}
            )
        recent = self.store.get_agent_logs(recent=True, limit=2)
        older = self.store.get_agent_logs(before_seq=recent[0].seq, limit=2)
        self.assertEqual([item.message for item in recent], ["line 1", "line 2"])
        self.assertEqual([item.message for item in older], ["line 0"])

    def test_agent_claim_filters_and_competing_workers(self):
        self.store.save_message(incoming("not-mentioned", content="普通消息"))
        mentioned = replace(incoming("mentioned", content="@bot 处理"), is_at_me=True)
        self.store.save_message(mentioned)
        filtered = self.store.claim_agent_messages(
            "mention-agent", require_at_me=True, replay_existing=True
        )
        self.assertEqual([item.message.message_id for item in filtered], ["mentioned"])

        first, _ = self.store.save_message(incoming("race-1", chat="并发群", content="一"))
        second, _ = self.store.save_message(incoming("race-2", chat="并发群", content="二"))
        barrier = threading.Barrier(2)
        claimed = []

        def worker():
            barrier.wait()
            values = self.store.claim_agent_messages(
                "race-agent",
                chat_ids=[first.chat_id],
                limit=1,
                replay_existing=True,
            )
            claimed.extend(item.message.seq for item in values)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(set(claimed), {first.seq, second.seq})

    def test_new_agent_starts_after_existing_history_by_default(self):
        self.store.save_message(incoming("old-before-agent", content="旧消息"))
        self.assertEqual(self.store.claim_agent_messages("fresh-agent"), [])

        new_message, _ = self.store.save_message(
            incoming("new-after-agent", content="新消息")
        )
        deliveries = self.store.claim_agent_messages("fresh-agent")
        self.assertEqual([item.message.seq for item in deliveries], [new_message.seq])

    def test_pending_send_is_completed_in_place(self):
        chat = self.store.upsert_chat("异步联系人", ChatType.CONTACT)
        pending, created = self.store.queue_send(chat.id, "排队消息", client_id="client-1")
        self.assertTrue(created)
        self.assertEqual(pending.delivery_status, "pending")
        receipt = SendReceipt(
            chat=chat.name,
            content="排队消息",
            sent_at=datetime(2026, 8, 19, 12, 2, tzinfo=timezone.utc),
            verified=True,
            matched_message_id="wechat-complete-id",
        )
        complete = self.store.complete_pending_send(pending.seq, receipt)
        self.assertEqual(complete.seq, pending.seq)
        self.assertEqual(complete.message_id, "wechat-complete-id")
        self.assertEqual(complete.delivery_status, "sent")

    def test_missing_virtual_time_separator_does_not_duplicate_message(self):
        timed = incoming("timed-id", chat="时间会话", content="同一条消息")
        first, created = self.store.save_message(timed)
        without_time = Message(
            id="untimed-id",
            chat=timed.chat,
            content=timed.content,
            type=timed.type,
            direction=timed.direction,
            sender=timed.sender,
            timestamp=None,
            chat_type=timed.chat_type,
            raw={"visible_occurrence": 0},
        )
        same, created_again = self.store.save_message(without_time)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.seq, same.seq)
        self.assertEqual(len(self.store.get_messages(first.chat_id)), 1)

        untimed_first = Message(
            id="reverse-untimed",
            chat="反向时间会话",
            content="先无时间后有时间",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            sender="Alice",
            timestamp=None,
            chat_type=ChatType.CONTACT,
            raw={"visible_occurrence": 0},
        )
        reverse_first, _ = self.store.save_message(untimed_first)
        timed_after = Message(
            id="reverse-timed",
            chat=untimed_first.chat,
            content=untimed_first.content,
            type=untimed_first.type,
            direction=untimed_first.direction,
            sender=untimed_first.sender,
            timestamp=datetime(2026, 8, 19, 12, 5, tzinfo=timezone.utc),
            chat_type=untimed_first.chat_type,
            raw={"visible_occurrence": 0},
        )
        reverse_second, reverse_created = self.store.save_message(timed_after)
        reverse_third, third_created = self.store.save_message(timed_after)
        self.assertFalse(reverse_created)
        self.assertFalse(third_created)
        self.assertEqual(reverse_first.seq, reverse_second.seq)
        self.assertEqual(reverse_second.seq, reverse_third.seq)
        self.assertEqual(reverse_third.message_id, "reverse-timed")

    def test_sender_enrichment_updates_existing_group_message_in_place(self):
        timestamp = datetime(2026, 8, 19, 12, 8, tzinfo=timezone.utc)
        without_sender = Message(
            id="without-sender",
            chat="发送者补全群",
            content="同一条群消息",
            type=MessageType.TEXT,
            direction=Direction.INCOMING,
            sender=None,
            timestamp=timestamp,
            chat_type=ChatType.GROUP,
            raw={"visible_occurrence": 0},
        )
        first, created = self.store.save_message(without_sender)
        enriched = Message(
            id="with-sender",
            chat=without_sender.chat,
            content=without_sender.content,
            type=without_sender.type,
            direction=without_sender.direction,
            sender="群成员",
            timestamp=timestamp,
            chat_type=without_sender.chat_type,
            raw={"visible_occurrence": 0, "sender_source": "profile_uia"},
        )

        same, created_again = self.store.save_message(enriched)

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(same.seq, first.seq)
        self.assertEqual(same.sender, "群成员")
        self.assertEqual(same.message_id, "with-sender")
        self.assertEqual(len(self.store.get_messages(first.chat_id)), 1)

    def test_missing_time_and_sender_reuses_existing_group_bubbles(self):
        timestamp = datetime(2026, 8, 19, 12, 9, tzinfo=timezone.utc)
        originals = []
        for occurrence, sender in enumerate(("程润松", "杨靖轩")):
            message = Message(
                id=f"timed-{occurrence}",
                chat="重复消息群",
                content="可爱捏",
                type=MessageType.TEXT,
                direction=Direction.INCOMING,
                sender=sender,
                timestamp=timestamp,
                chat_type=ChatType.GROUP,
                raw={"visible_occurrence": occurrence},
            )
            stored, created = self.store.save_message(message)
            self.assertTrue(created)
            originals.append(stored)

        for occurrence in range(2):
            without_context = Message(
                id=f"untimed-{occurrence}",
                chat="重复消息群",
                content="可爱捏",
                type=MessageType.TEXT,
                direction=Direction.INCOMING,
                sender=None,
                timestamp=None,
                chat_type=ChatType.GROUP,
                raw={"visible_occurrence": occurrence},
            )
            same, created = self.store.save_message(without_context)
            self.assertFalse(created)
            self.assertEqual(same.seq, originals[occurrence].seq)

        messages = self.store.get_messages(originals[0].chat_id)
        self.assertEqual(len(messages), 2)
        self.assertEqual([message.sender for message in messages], ["程润松", "杨靖轩"])

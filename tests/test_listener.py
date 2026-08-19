import unittest
from datetime import datetime, timezone

from bowxt.listener import MessageListener
from bowxt.models import ChatType, Direction, Message, MessageType


def message(message_id, content, direction=Direction.INCOMING):
    return Message(
        id=message_id,
        chat="g",
        content=content,
        type=MessageType.TEXT,
        direction=direction,
        chat_type=ChatType.GROUP,
        timestamp=datetime(2026, 8, 19, tzinfo=timezone.utc),
    )


class SequenceClient:
    def __init__(self, values):
        self.values = list(values)

    def get_visible_messages(self, *_args, **_kwargs):
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class ListenerTests(unittest.TestCase):
    def test_baseline_is_not_emitted_and_new_incoming_message_is(self):
        old = message("old", "old")
        new = message("new", "new")
        received = []
        listener = MessageListener(
            SequenceClient([[old], [old, new]]), ["g"], received.append,
            poll_interval=2.0,
        )
        listener._baseline()
        listener._poll_chat("g")
        self.assertEqual(received, [new])

    def test_outgoing_messages_are_not_reemitted(self):
        outgoing = message("out", "sent", Direction.OUTGOING)
        received = []
        listener = MessageListener(
            SequenceClient([[], [outgoing]]), ["g"], received.append,
            poll_interval=2.0,
        )
        listener._baseline()
        listener._poll_chat("g")
        self.assertEqual(received, [])

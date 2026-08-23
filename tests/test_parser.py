from datetime import datetime, timezone
import unittest

from bowxt.models import ChatType, Direction, MessageType
from bowxt.parser import MessageParser

from fakes import sample_tree
from fakes import FakeNode
from bowxt.models import Rect


class ParserTests(unittest.TestCase):
    def test_image_bubble_without_text_is_parsed_and_avatar_is_ignored(self):
        picture_bounds = Rect(72, 20, 180, 120)
        row = FakeNode(
            "list item",
            attributes={"class": "message incoming image_message"},
            bounds=Rect(0, 0, 500, 160),
            nodes=[
                FakeNode("image", "头像", bounds=Rect(12, 20, 40, 40), token="avatar"),
                FakeNode("image", "图片", bounds=picture_bounds, token="picture"),
            ],
        )
        message_list = FakeNode(
            "list", "消息", bounds=Rect(0, 0, 500, 300), nodes=[row]
        )

        message = MessageParser().parse_list(
            message_list, chat="hzw", chat_type=ChatType.CONTACT
        )[0]

        self.assertEqual(message.content, "[图片]")
        self.assertEqual(message.type, MessageType.IMAGE)
        self.assertEqual(message.direction, Direction.INCOMING)
        self.assertEqual(message.bounds, row.bounds)
        self.assertEqual(message.raw["image_bounds"]["width"], 180)

    def test_group_sender_can_come_from_uia_relation_target(self):
        sender_label = FakeNode("label", "Alice", token="sender-label")
        row = FakeNode(
            "list item",
            "hello",
            bounds=Rect(0, 0, 500, 80),
            relation_nodes={"labelled by": [sender_label]},
            token="related-row",
        )
        message = MessageParser().parse_message(
            row,
            container=Rect(0, 0, 500, 300),
            chat="group",
            chat_type=ChatType.GROUP,
        )
        self.assertEqual(message.sender, "Alice")

    def test_group_content_spacing_is_not_mistaken_for_sender(self):
        content = "@黄泽文\u2005 wx4linux @功能端到端测试"
        row = FakeNode(
            "list item",
            content,
            attributes={"class": "message incoming"},
            bounds=Rect(0, 0, 300, 50),
            nodes=[
                FakeNode(
                    "text",
                    "@黄泽文 wx4linux @功能端到端测试",
                    bounds=Rect(40, 8, 220, 34),
                )
            ],
        )

        message = MessageParser().parse_message(
            row,
            container=Rect(0, 0, 500, 300),
            chat="group",
            chat_type=ChatType.GROUP,
        )

        self.assertIsNotNone(message)
        self.assertIsNone(message.sender)
        self.assertEqual(message.content, content)

    def test_group_sender_and_direction_are_recovered_from_children_and_geometry(self):
        _root, message_list = sample_tree()
        parser = MessageParser(my_names=["Me"], now=lambda: datetime(2026, 8, 19, tzinfo=timezone.utc))
        messages = parser.parse_list(message_list, chat="测试群", chat_type=ChatType.GROUP)
        self.assertEqual(
            [(item.content, item.sender, item.direction) for item in messages],
            [("你好", "Alice", Direction.INCOMING), ("hello", "Me", Direction.OUTGOING)],
        )
        self.assertNotEqual(messages[0].id, messages[1].id)

    def test_at_me_is_reported(self):
        _root, message_list = sample_tree()
        message_list.nodes[0].nodes[1].name = "@Me 请看"
        parser = MessageParser(my_names=["Me"])
        self.assertTrue(parser.parse_list(message_list, chat="g", chat_type=ChatType.GROUP)[0].is_at_me)

    def test_chinese_pm_time_is_parsed(self):
        parser = MessageParser(now=lambda: datetime(2026, 8, 19, 9, tzinfo=timezone.utc))
        result = parser._parse_time("下午 3:06")
        self.assertEqual((result.hour, result.minute), (15, 6))

    def test_yesterday_time_separator_is_not_parsed_as_message(self):
        message_list = FakeNode(
            "list",
            "消息",
            bounds=Rect(0, 0, 500, 300),
            nodes=[
                FakeNode("list item", "昨天 23:04", bounds=Rect(200, 20, 100, 24)),
                FakeNode(
                    "list item", "测试",
                    attributes={"class": "message incoming"},
                    bounds=Rect(0, 60, 300, 50),
                ),
            ],
        )
        parser = MessageParser(
            now=lambda: datetime(2026, 8, 20, 14, tzinfo=timezone.utc)
        )

        messages = parser.parse_list(
            message_list, chat="hzw", chat_type=ChatType.CONTACT
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "测试")
        self.assertEqual(
            messages[0].timestamp,
            datetime(2026, 8, 19, 23, 4, tzinfo=timezone.utc),
        )

    def test_strong_group_sender_prefix_is_split(self):
        row = FakeNode(
            "list item", "Alice: hello group",
            attributes={"class": "message incoming"}, bounds=Rect(0, 0, 300, 50),
        )
        message_list = FakeNode("list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=[row])
        message = MessageParser().parse_list(
            message_list, chat="group", chat_type=ChatType.GROUP
        )[0]
        self.assertEqual((message.sender, message.content), ("Alice", "hello group"))

    def test_multiline_group_message_is_not_split_as_sender(self):
        content = "第一行正文\n第二行正文"
        row = FakeNode(
            "list item", content,
            attributes={"class": "message incoming"}, bounds=Rect(0, 0, 300, 80),
        )
        message_list = FakeNode("list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=[row])

        message = MessageParser().parse_list(
            message_list, chat="group", chat_type=ChatType.GROUP
        )[0]

        self.assertIsNone(message.sender)
        self.assertEqual(message.content, content)

    def test_quoted_group_message_does_not_treat_body_as_sender(self):
        content = "请问今晚可以来吗[嘿哈]\n引用 张钧 的消息 : 原消息"
        row = FakeNode(
            "list item", content,
            attributes={"class": "message incoming"}, bounds=Rect(0, 0, 300, 100),
        )
        message_list = FakeNode("list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=[row])

        message = MessageParser().parse_list(
            message_list, chat="group", chat_type=ChatType.GROUP
        )[0]

        self.assertIsNone(message.sender)
        self.assertEqual(message.content, content)

    def test_bracketed_conversation_id_is_not_split_as_sender(self):
        row = FakeNode(
            "list item",
            "[conv: 05b8c5b0]\n网络排查建议",
            bounds=Rect(0, 0, 300, 80),
        )
        message_list = FakeNode(
            "list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=[row]
        )

        message = MessageParser().parse_list(
            message_list, chat="group", chat_type=ChatType.GROUP
        )[0]

        self.assertIsNone(message.sender)
        self.assertEqual(message.content, "[conv: 05b8c5b0]\n网络排查建议")

    def test_message_id_survives_virtual_node_recreation(self):
        def parse(token):
            row = FakeNode(
                "list item", "稳定内容", attributes={"class": "message incoming"},
                bounds=Rect(0, 0, 300, 50), token=token,
            )
            message_list = FakeNode(
                "list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=[row]
            )
            return MessageParser().parse_list(
                message_list, chat="contact", chat_type=ChatType.CONTACT
            )[0]

        self.assertEqual(parse("temporary-node-a").id, parse("temporary-node-b").id)

    def test_identical_visible_messages_use_stable_occurrence_numbers(self):
        def parse(tokens):
            rows = [
                FakeNode(
                    "list item", "1", attributes={"class": "message incoming"},
                    bounds=Rect(0, index * 60, 300, 50), token=token,
                )
                for index, token in enumerate(tokens)
            ]
            message_list = FakeNode(
                "list", "Messages", bounds=Rect(0, 0, 500, 300), nodes=rows
            )
            return MessageParser().parse_list(
                message_list, chat="group", chat_type=ChatType.GROUP
            )

        first = parse(["a", "b"])
        second = parse(["new-a", "new-b"])
        self.assertNotEqual(first[0].id, first[1].id)
        self.assertEqual([item.id for item in first], [item.id for item in second])

import unittest

from bowxt.accessibility import format_tree, parse_attributes

from fakes import FakeNode


class AccessibilityTests(unittest.TestCase):
    def test_attribute_parser_preserves_colons_in_value(self):
        self.assertEqual(parse_attributes(["class:mmui::ChatItem", "id:input"]), {
            "class": "mmui::ChatItem", "id": "input"
        })

    def test_tree_is_redacted_by_default(self):
        root = FakeNode("frame", "私密会话", attributes={"label": "秘密消息"}, token="root")
        output = format_tree(root)
        self.assertNotIn("私密会话", output)
        self.assertNotIn("秘密消息", output)
        self.assertIn("label", output)

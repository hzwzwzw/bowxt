import unittest

from bowxt.models import Rect
from bowxt.selectors import (
    find_editor,
    find_mention_candidate,
    find_message_list,
    find_profile_name,
    find_search_box,
)

from fakes import FakeNode, sample_tree


class SelectorTests(unittest.TestCase):
    def test_default_profile_finds_core_controls(self):
        root, message_list = sample_tree()
        self.assertIs(find_message_list(root), message_list)
        self.assertEqual(find_search_box(root).identity, "search")
        self.assertEqual(find_editor(root).identity, "editor")

    def test_finds_exact_mention_row_in_popup(self):
        root, _message_list = sample_tree()
        editor = find_editor(root)
        wanted = FakeNode("list item", "张三", bounds=Rect(610, 610, 160, 32), token="wanted")
        popup = FakeNode(
            "filler", bounds=Rect(590, 515, 220, 220), nodes=[wanted], token="mention-popup"
        )
        self.assertIs(
            find_mention_candidate(
                [root, popup], main_window=root, editor=editor, member="张三"
            ),
            wanted,
        )

    def test_duplicate_exact_mention_names_are_rejected_as_ambiguous(self):
        root, _message_list = sample_tree()
        editor = find_editor(root)
        popup = FakeNode(
            "filler",
            bounds=Rect(590, 515, 220, 220),
            token="mention-popup",
            nodes=[
                FakeNode("list item", "同名", bounds=Rect(610, 610, 160, 32), token="same-1"),
                FakeNode("list item", "同名", bounds=Rect(610, 642, 160, 32), token="same-2"),
            ],
        )
        self.assertIsNone(
            find_mention_candidate(
                [root, popup], main_window=root, editor=editor, member="同名"
            )
        )

    def test_verified_profile_card_yields_primary_name(self):
        profile = FakeNode(
            "filler",
            bounds=Rect(700, 300, 330, 440),
            token="profile",
            nodes=[
                FakeNode("button", "张三", bounds=Rect(748, 356, 60, 60), token="avatar"),
                FakeNode("label", "Remark", bounds=Rect(748, 448, 65, 29), token="remark"),
                FakeNode("button", "Add Alias", bounds=Rect(813, 449, 167, 26), token="alias"),
                FakeNode("button", "Messages", bounds=Rect(748, 646, 72, 58), token="messages"),
                FakeNode("button", "Voice Call", bounds=Rect(831, 646, 72, 58), token="call"),
            ],
        )
        self.assertEqual(find_profile_name(profile), "张三")

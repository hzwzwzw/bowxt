import unittest

from bowxt.models import Rect
from bowxt.selectors import (
    ProfileIdentity,
    find_editor,
    find_mention_candidate,
    find_message_list,
    find_profile_name,
    find_profile_identity,
    find_search_box,
    visible_message_nodes,
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

    def test_linux_chinese_profile_action_names_are_verified(self):
        profile = FakeNode(
            "filler",
            bounds=Rect(435, 417, 328, 446),
            token="linux-profile",
            nodes=[
                FakeNode("button", "黄泽文", bounds=Rect(483, 465, 60, 60)),
                FakeNode("label", "黄泽文", bounds=Rect(559, 465, 69, 26)),
                FakeNode("label", "@柯基服务队", bounds=Rect(559, 493, 92, 19)),
                FakeNode("button", "添加备注名", bounds=Rect(559, 530, 100, 26)),
                FakeNode("label", "企业信息", bounds=Rect(483, 595, 232, 19)),
                FakeNode("label", "来自", bounds=Rect(483, 622, 65, 21)),
                FakeNode("label", "企业微信", bounds=Rect(574, 622, 141, 21)),
                FakeNode("label", "企业", bounds=Rect(483, 647, 65, 21)),
                FakeNode("label", "柯基服务队", bounds=Rect(574, 647, 141, 21)),
                FakeNode("label", "实名", bounds=Rect(483, 672, 65, 21)),
                FakeNode("label", "黄泽文", bounds=Rect(574, 672, 141, 21)),
                FakeNode("button", "发消息", bounds=Rect(483, 756, 72, 59)),
                FakeNode("button", "语音聊天", bounds=Rect(566, 756, 72, 59)),
                FakeNode("button", "视频聊天", bounds=Rect(649, 756, 72, 59)),
            ],
        )

        self.assertEqual(find_profile_name(profile), "黄泽文")
        self.assertEqual(
            find_profile_identity(profile),
            ProfileIdentity(name="黄泽文", organization="柯基服务队"),
        )

    def test_virtual_rows_outside_message_viewport_are_ignored(self):
        visible = FakeNode("list item", "当前消息", bounds=Rect(100, 120, 400, 50))
        stale = FakeNode("list item", "陈旧消息", bounds=Rect(100, -500, 400, 50))
        one_pixel = FakeNode("list item", "边界残留", bounds=Rect(100, 21, 400, 80))
        offscreen = FakeNode(
            "list item", "离屏消息", bounds=Rect(100, 180, 400, 50), states={"offscreen"}
        )
        message_list = FakeNode(
            "list", "Messages", bounds=Rect(100, 100, 500, 400),
            nodes=[stale, one_pixel, visible, offscreen],
        )
        self.assertEqual(visible_message_nodes(message_list), [visible])

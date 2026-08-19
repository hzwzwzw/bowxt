import unittest

from bowxt import ChatType, SafetyPolicy, WeChatClient
from bowxt.models import Rect

from fakes import FakeAccessibility, FakeClipboard, FakeInput, FakeNode, sample_tree


class ClientTests(unittest.TestCase):
    @staticmethod
    def _profile_fixture(*, closes_on_escape=True):
        root, message_list = sample_tree()
        incoming = message_list.nodes[0]
        incoming.name = "你好"
        incoming.nodes = []
        profile = FakeNode(
            "filler",
            bounds=Rect(700, 300, 330, 440),
            token="profile-popup",
            nodes=[
                FakeNode("button", "张三", bounds=Rect(748, 356, 60, 60), token="avatar"),
                FakeNode("label", "Remark", bounds=Rect(748, 448, 65, 29), token="remark"),
                FakeNode("button", "Messages", bounds=Rect(748, 646, 72, 58), token="messages"),
                FakeNode("button", "Voice Call", bounds=Rect(831, 646, 72, 58), token="call"),
            ],
        )

        class ProfileAccessibility(FakeAccessibility):
            popup = None

            def windows(self):
                return [self.root] + ([self.popup] if self.popup else [])

        accessibility = ProfileAccessibility(root)

        class ProfileInput(FakeInput):
            def click(self, x, y, *, count=1):
                super().click(x, y, count=count)
                if (x, y) == (294, 226):
                    accessibility.popup = profile

            def press(self, key):
                super().press(key)
                if key == "esc" and closes_on_escape:
                    accessibility.popup = None

        return root, accessibility, ProfileInput()

    def test_profile_uia_sender_requires_verified_escape_cleanup(self):
        _root, accessibility, inputs = self._profile_fixture()
        client = WeChatClient(
            accessibility=accessibility,
            input_backend=inputs,
            uia_sender=True,
            sleeper=lambda _seconds: None,
        ).connect()

        messages = client.get_visible_messages(chat_type=ChatType.GROUP)

        incoming = next(item for item in messages if item.content == "你好")
        self.assertEqual(incoming.sender, "张三")
        self.assertEqual(incoming.raw["sender_source"], "profile_uia")
        self.assertIsNone(accessibility.popup)
        self.assertIn(("press", "esc"), inputs.events)
        self.assertFalse(client._interaction_blocked)

    def test_profile_that_will_not_close_locks_all_later_input(self):
        _root, accessibility, inputs = self._profile_fixture(closes_on_escape=False)
        client = WeChatClient(
            accessibility=accessibility,
            input_backend=inputs,
            uia_sender=True,
            sleeper=lambda _seconds: None,
            safety=SafetyPolicy(min_send_interval=0, send_jitter=0),
        ).connect()

        with self.assertRaisesRegex(RuntimeError, "further input was stopped"):
            client.get_visible_messages(chat_type=ChatType.GROUP)
        self.assertTrue(client._interaction_blocked)
        event_count = len(inputs.events)
        with self.assertRaisesRegex(Exception, "input-locked"):
            client.send_text("测试群", "不能发送", chat_type=ChatType.GROUP)
        self.assertEqual(len(inputs.events), event_count)

    def test_preexisting_unknown_transient_locks_without_any_input(self):
        root, message_list = sample_tree()
        message_list.nodes[0].name = "你好"
        message_list.nodes[0].nodes = []
        unknown = FakeNode(
            "dialog", "Incoming Call", bounds=Rect(500, 300, 300, 300), token="unknown"
        )

        class PopupAccessibility(FakeAccessibility):
            def windows(self):
                return [self.root, unknown]

        inputs = FakeInput()
        client = WeChatClient(
            accessibility=PopupAccessibility(root),
            input_backend=inputs,
            uia_sender=True,
            sleeper=lambda _seconds: None,
        ).connect()

        with self.assertRaisesRegex(RuntimeError, "further input was stopped"):
            client.get_visible_messages(chat_type=ChatType.GROUP)
        self.assertTrue(client._interaction_blocked)
        self.assertEqual(inputs.events, [])

    def test_current_chat_honors_explicit_group_type(self):
        root, _message_list = sample_tree()
        client = WeChatClient(accessibility=FakeAccessibility(root)).connect()

        messages = client.get_visible_messages(chat_type=ChatType.GROUP)

        self.assertEqual(messages[0].sender, "Alice")
        self.assertEqual(messages[0].chat_type, ChatType.GROUP)

    def test_wide_window_chat_title_uses_content_pane_not_percentage(self):
        root, message_list = sample_tree()
        root.bounds = Rect(124, 77, 1280, 860)
        message_list.bounds = Rect(397, 150, 980, 500)
        editor = next(node for node in root.nodes if node.token == "editor")
        editor.bounds = Rect(413, 690, 940, 120)
        header = next(node for node in root.nodes if node.token == "header")
        header.name = "张三"
        header.bounds = Rect(413, 106, 80, 25)

        self.assertTrue(WeChatClient._is_chat_open(root, "张三"))

    def test_unread_discovery_clicks_visible_row_and_reads_header_without_splitting_preview(self):
        root, _message_list = sample_tree()
        header = next(node for node in root.nodes if node.token == "header")
        header.name = "新 会话(8)"
        unread = FakeNode(
            "list item",
            "新 会话 2条未读 Alice: 含 空格的预览 19:30",
            bounds=Rect(20, 100, 210, 68),
            token="unread-row",
        )
        sessions = FakeNode(
            "list", "会话", bounds=Rect(20, 100, 210, 600), nodes=[unread], token="sessions"
        )
        root.nodes.insert(1, sessions)
        inputs = FakeInput()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=inputs,
            sleeper=lambda _seconds: None,
        ).connect()

        discovered = client.discover_unread_chats()

        self.assertEqual(discovered, ["新 会话"])
        self.assertEqual(inputs.events, [("click", *unread.bounds.center, 1)])

    def test_send_uses_only_click_clipboard_shortcuts_and_enter(self):
        root, _message_list = sample_tree()
        inputs = FakeInput()
        clipboard = FakeClipboard()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=inputs,
            clipboard=clipboard,
            safety=SafetyPolicy(min_send_interval=0, send_jitter=0, action_delay=0, paste_settle_delay=0),
            sleeper=lambda _seconds: None,
            my_names=["Me"],
        ).connect()
        receipt = client.send_text("测试群", "hello", chat_type=ChatType.GROUP)
        self.assertTrue(receipt.verified)
        self.assertEqual(clipboard.values, ["hello"])
        self.assertEqual(inputs.events.count(("shortcut", "ctrl", "v")), 1)
        self.assertIn(("press", "enter"), inputs.events)

    def test_send_mention_selects_exact_popup_row_and_verifies_rich_token(self):
        root, _message_list = sample_tree()
        editor = next(node for node in root.nodes if node.token == "editor")
        editor.accessible_text = ""
        candidate = FakeNode(
            "list item", "张三", bounds=Rect(610, 610, 160, 32), token="candidate"
        )
        popup = FakeNode(
            "filler", bounds=Rect(590, 515, 220, 220), nodes=[candidate], token="popup"
        )

        class MentionAccessibility(FakeAccessibility):
            mention_popup = None

            def windows(self):
                return [self.root] + ([self.mention_popup] if self.mention_popup else [])

        accessibility = MentionAccessibility(root)

        class MentionInput(FakeInput):
            def shortcut(self, *keys):
                super().shortcut(*keys)
                if keys == ("shift", "2"):
                    accessibility.mention_popup = popup

            def click(self, x, y, *, count=1):
                super().click(x, y, count=count)
                if accessibility.mention_popup and (x, y) == candidate.bounds.center:
                    editor.accessible_text += "\ufffc"
                    accessibility.mention_popup = None

        inputs = MentionInput()
        clipboard = FakeClipboard()
        client = WeChatClient(
            accessibility=accessibility,
            input_backend=inputs,
            clipboard=clipboard,
            safety=SafetyPolicy(
                min_send_interval=0,
                send_jitter=0,
                action_delay=0,
                paste_settle_delay=0,
            ),
            sleeper=lambda _seconds: None,
        ).connect()

        receipt = client.send_text(
            "测试群",
            "正文",
            chat_type=ChatType.GROUP,
            mentions=["张三"],
            verify=False,
        )

        self.assertEqual(receipt.content, "@张三 正文")
        self.assertEqual(receipt.mentions, ("张三",))
        self.assertEqual(clipboard.values, ["正文"])
        self.assertIn(("shortcut", "shift", "2"), inputs.events)
        self.assertIn(("click", *candidate.bounds.center, 1), inputs.events)
        self.assertIn(("press", "space"), inputs.events)
        self.assertIn(("press", "enter"), inputs.events)

    def test_failed_mention_closes_popup_before_clearing_and_never_sends(self):
        root, _message_list = sample_tree()
        editor = next(node for node in root.nodes if node.token == "editor")
        editor.accessible_text = ""
        popup = FakeNode(
            "filler",
            bounds=Rect(590, 515, 220, 220),
            nodes=[FakeNode("list item", "其他人", bounds=Rect(610, 610, 160, 32))],
            token="popup",
        )

        class MentionAccessibility(FakeAccessibility):
            mention_popup = None

            def windows(self):
                return [self.root] + ([self.mention_popup] if self.mention_popup else [])

        accessibility = MentionAccessibility(root)

        class MentionInput(FakeInput):
            def shortcut(self, *keys):
                super().shortcut(*keys)
                if keys == ("shift", "2"):
                    accessibility.mention_popup = popup

            def press(self, key):
                super().press(key)
                if key == "esc":
                    accessibility.mention_popup = None

        inputs = MentionInput()
        client = WeChatClient(
            accessibility=accessibility,
            input_backend=inputs,
            clipboard=FakeClipboard(),
            safety=SafetyPolicy(
                min_send_interval=0,
                send_jitter=0,
                action_delay=0,
                paste_settle_delay=0,
            ),
            sleeper=lambda _seconds: None,
        ).connect()

        with self.assertRaisesRegex(Exception, "no unique visible exact"):
            client.send_text(
                "测试群", "正文", chat_type=ChatType.GROUP, mentions=["张三"], verify=False
            )

        esc_index = inputs.events.index(("press", "esc"))
        clear_index = inputs.events.index(("shortcut", "ctrl", "a"))
        self.assertLess(esc_index, clear_index)
        self.assertNotIn(("press", "enter"), inputs.events)
        self.assertIsNone(accessibility.mention_popup)

    def test_rich_mention_spacing_is_normalized_for_verification(self):
        expected = "@张三 正文"
        exposed_by_wechat = "@张三\u2005  正文"
        self.assertEqual(
            WeChatClient._normalized_content(exposed_by_wechat),
            WeChatClient._normalized_content(expected),
        )

    def test_mentions_require_explicit_group_type(self):
        root, _message_list = sample_tree()
        inputs = FakeInput()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=inputs,
        ).connect()
        with self.assertRaises(ValueError):
            client.send_text("测试群", "正文", mentions=["张三"])
        self.assertEqual(inputs.events, [])

    def test_existing_draft_blocks_send_without_input(self):
        root, _message_list = sample_tree()
        editor = next(node for node in root.nodes if node.token == "editor")
        editor.accessible_text = "未发送草稿"
        inputs = FakeInput()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=inputs,
            safety=SafetyPolicy(min_send_interval=0, send_jitter=0),
        ).connect()
        with self.assertRaisesRegex(Exception, "draft"):
            client.send_text("测试群", "正文", chat_type=ChatType.GROUP)
        self.assertEqual(inputs.events, [])

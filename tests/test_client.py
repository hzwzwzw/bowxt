import unittest
from unittest.mock import patch

from bowxt import ChatType, SafetyPolicy, WeChatClient
from bowxt.models import Direction, Message, MessageImage, MessageType, Rect
from bowxt.selectors import ProfileIdentity

from fakes import FakeAccessibility, FakeClipboard, FakeInput, FakeNode, sample_tree


class ClientTests(unittest.TestCase):
    def test_visible_image_bubble_is_captured_without_enabling_sender_enrichment(self):
        root, message_list = sample_tree()
        picture_bounds = Rect(310, 210, 180, 120)
        message_list.nodes = [FakeNode(
            "list item",
            attributes={"class": "message incoming image_message"},
            bounds=Rect(250, 190, 650, 150),
            token="image-row",
            nodes=[
                FakeNode("image", "头像", bounds=Rect(260, 205, 40, 40), token="avatar"),
                FakeNode("image", "图片", bounds=picture_bounds, token="picture"),
            ],
        )]

        class Snapshot:
            def locate_image(self, _bounds):
                return picture_bounds

            def read_image(self, bounds):
                self.bounds = bounds
                return MessageImage(b"png-pixels", width=bounds.width, height=bounds.height)

            def classify(self, _bounds):
                return Direction.INCOMING

        snapshot = Snapshot()
        client = WeChatClient(accessibility=FakeAccessibility(root)).connect()
        with patch("bowxt.vision.VisualDirectionDetector.capture", return_value=snapshot):
            messages = client.get_visible_messages(chat_type=ChatType.CONTACT)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].type, MessageType.IMAGE)
        self.assertEqual(messages[0].image.data, b"png-pixels")
        self.assertEqual(snapshot.bounds, picture_bounds)

    def test_image_row_is_not_captured_when_picture_region_is_not_visible(self):
        root, message_list = sample_tree()
        message_list.nodes = [FakeNode(
            "list item", "图片", bounds=Rect(250, 190, 650, 80), token="clipped-image-row"
        )]

        class Snapshot:
            def locate_image(self, _bounds):
                return None

            def read_image(self, _bounds):
                raise AssertionError("the whole row must not be persisted as an image")

            def classify(self, _bounds):
                return Direction.INCOMING

        client = WeChatClient(accessibility=FakeAccessibility(root)).connect()
        with patch("bowxt.vision.VisualDirectionDetector.capture", return_value=Snapshot()):
            messages = client.get_visible_messages(chat_type=ChatType.CONTACT)

        self.assertEqual(messages[0].type, MessageType.IMAGE)
        self.assertIsNone(messages[0].image)

    def test_operation_delay_updates_input_pacing_without_weakening_send_limits(self):
        inputs = FakeInput()
        inputs.event_delay = 0.06
        policy = SafetyPolicy(min_send_interval=2.4, send_jitter=0.5)
        client = WeChatClient(input_backend=inputs, safety=policy)

        self.assertEqual(client.set_operation_delay(0.08), 0.08)

        self.assertEqual(client.safety.action_delay, 0.08)
        self.assertEqual(client.safety.paste_settle_delay, 0.16)
        self.assertEqual(client.safety.min_send_interval, 2.4)
        self.assertEqual(client.safety.send_jitter, 0.5)
        self.assertEqual(client._limiter.policy, client.safety)
        self.assertEqual(inputs.event_delay, 0.04)
        with self.assertRaises(ValueError):
            client.set_operation_delay(0.03)

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
                FakeNode("label", "@示例组织", bounds=Rect(820, 382, 90, 20), token="org-top"),
                FakeNode("label", "Remark", bounds=Rect(748, 448, 65, 29), token="remark"),
                FakeNode("label", "企业", bounds=Rect(748, 560, 65, 21), token="org-label"),
                FakeNode("label", "示例组织", bounds=Rect(838, 560, 120, 21), token="org-value"),
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
                if (x, y) == (286, 226):
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
        self.assertEqual(incoming.sender_organization, "示例组织")
        self.assertEqual(incoming.raw["sender_source"], "profile_uia")
        self.assertIsNone(accessibility.popup)
        self.assertIn(("press", "esc"), inputs.events)
        self.assertFalse(client._interaction_blocked)

        # The sender result is cached by the stable visible-message ID, so the
        # next poll does not reopen the same person's profile card.
        profile_clicks = [event for event in inputs.events if event[:3] == ("click", 286, 226)]
        messages = client.get_visible_messages(chat_type=ChatType.GROUP)
        self.assertEqual(next(item for item in messages if item.content == "你好").sender, "张三")
        self.assertEqual(
            [event for event in inputs.events if event[:3] == ("click", 286, 226)],
            profile_clicks,
        )

    def test_profile_cleanup_escapes_the_active_wechat_transient(self):
        root, accessibility, inputs = self._profile_fixture()
        inputs.active_wechat = True

        def is_active_wechat_window():
            return inputs.active_wechat

        def shortcut_active_wechat(*keys):
            inputs.events.append(("shortcut-active", *keys))
            if keys == ("esc",):
                accessibility.popup = None

        inputs.is_active_wechat_window = is_active_wechat_window
        inputs.shortcut_active_wechat = shortcut_active_wechat
        client = WeChatClient(
            accessibility=accessibility,
            input_backend=inputs,
            uia_sender=True,
            sleeper=lambda _seconds: None,
        ).connect()

        messages = client.get_visible_messages(chat_type=ChatType.GROUP)

        self.assertEqual(next(item for item in messages if item.content == "你好").sender, "张三")
        self.assertIn(("shortcut-active", "esc"), inputs.events)
        self.assertNotIn(("press", "esc"), inputs.events)
        self.assertIsNone(accessibility.popup)

    def test_explicit_sender_enrichment_processes_the_whole_new_burst(self):
        root, _message_list = sample_tree()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=FakeInput(),
            uia_sender=True,
            sleeper=lambda _seconds: None,
        ).connect()
        reads = []

        def read_sender(bounds, **_kwargs):
            reads.append(bounds.y)
            return ProfileIdentity(f"成员{len(reads)}", "测试组织")

        client._read_profile_identity = read_sender
        messages = [
            Message(
                id=f"new-{index}",
                chat="群",
                content=f"消息{index}",
                type=MessageType.TEXT,
                direction=Direction.INCOMING,
                sender=None,
                chat_type=ChatType.GROUP,
                bounds=Rect(260, 180 + index * 60, 240, 50),
            )
            for index in (1, 2, 3)
        ]

        enriched = client.enrich_visible_senders(messages, chat="测试群")

        self.assertEqual(reads, [240, 300, 360])
        self.assertEqual([item.sender for item in enriched], ["成员1", "成员2", "成员3"])
        self.assertEqual(
            [item.sender_organization for item in enriched],
            ["测试组织", "测试组织", "测试组织"],
        )

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

    def test_visible_group_title_ignores_separate_member_count_label(self):
        root, _message_list = sample_tree()
        root.nodes.append(FakeNode(
            "label", "(21)", bounds=Rect(625, 30, 35, 30), token="member-count"
        ))
        client = WeChatClient(accessibility=FakeAccessibility(root)).connect()

        self.assertEqual(client.visible_chat_name(), "测试群")

    def test_visible_group_title_ignores_new_message_scroll_button(self):
        root, message_list = sample_tree()
        message_list.bounds = Rect(240, 150, 680, 500)
        root.nodes.append(FakeNode(
            "button", "7条新消息", bounds=Rect(790, 170, 115, 51), token="new-message-button",
            nodes=[FakeNode(
                "label", "7条新消息", bounds=Rect(820, 185, 60, 20), token="new-message-label"
            )],
        ))
        root.nodes.append(FakeNode(
            "label", "(199)", bounds=Rect(625, 30, 45, 30), token="member-count"
        ))
        client = WeChatClient(accessibility=FakeAccessibility(root)).connect()

        self.assertEqual(client.visible_chat_name(), "测试群")
        self.assertEqual(client._chat_type, ChatType.GROUP)

    def test_unread_discovery_clicks_visible_row_and_reads_header_without_splitting_preview(self):
        root, _message_list = sample_tree()
        header = next(node for node in root.nodes if node.token == "header")
        header.name = "原会话"
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
        class UnreadInput(FakeInput):
            def click(self, x, y, *, count=1):
                super().click(x, y, count=count)
                if (x, y) == unread.bounds.center:
                    header.name = "新 会话(8)"

        inputs = UnreadInput()
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=inputs,
            sleeper=lambda _seconds: None,
        ).connect()

        discovered = client.discover_unread_chats()

        self.assertEqual(discovered, ["新 会话"])
        self.assertEqual(inputs.events, [("click", *unread.bounds.center, 1)])

    def test_unread_discovery_records_group_type_from_header_member_count(self):
        root, _message_list = sample_tree()
        header = next(node for node in root.nodes if node.token == "header")
        header.name = "原会话"
        root.nodes.append(FakeNode(
            "label", "(199)", bounds=Rect(625, 30, 45, 30), token="member-count"
        ))
        unread = FakeNode(
            "list item", "测试群 2条未读 预览", bounds=Rect(20, 100, 210, 68), token="unread-row"
        )
        root.nodes.insert(1, FakeNode(
            "list", "会话", bounds=Rect(20, 100, 210, 600), nodes=[unread], token="sessions"
        ))

        class UnreadInput(FakeInput):
            def click(self, x, y, *, count=1):
                super().click(x, y, count=count)
                header.name = "测试群"

        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=UnreadInput(),
            sleeper=lambda _seconds: None,
        ).connect()

        self.assertEqual(client.discover_unread_chats(), ["测试群"])
        self.assertEqual(client.discovered_chat_type("测试群"), ChatType.GROUP)

    def test_unread_discovery_opens_every_badged_row_in_one_scan(self):
        root, _message_list = sample_tree()
        header = next(node for node in root.nodes if node.token == "header")
        header.name = "原会话"
        rows = [
            FakeNode(
                "list item",
                f"会话{name} 1条未读 预览",
                bounds=Rect(20, 100 + index * 68, 210, 68),
                token=f"unread-{index}",
            )
            for index, name in enumerate(("甲", "乙"))
        ]
        root.nodes.insert(1, FakeNode(
            "list", "会话", bounds=Rect(20, 100, 210, 600), nodes=rows, token="sessions"
        ))

        class BurstInput(FakeInput):
            def click(self, x, y, *, count=1):
                super().click(x, y, count=count)
                header.name = "会话甲" if (x, y) == rows[0].bounds.center else "会话乙"

        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=BurstInput(),
            sleeper=lambda _seconds: None,
        ).connect()

        self.assertEqual(client.discover_unread_chats(limit=32), ["会话甲", "会话乙"])

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
        self.assertEqual(
            set(receipt.timings),
            {"rate_limit_s", "open_chat_s", "input_to_enter_s", "verify_s", "total_s"},
        )

    def test_send_verification_never_opens_group_sender_profiles(self):
        root, message_list = sample_tree()
        avatar = next(node for node in message_list.nodes[0].nodes if node.token == "avatar")
        avatar.name = ""
        client = WeChatClient(
            accessibility=FakeAccessibility(root),
            input_backend=FakeInput(),
            clipboard=FakeClipboard(),
            safety=SafetyPolicy(
                min_send_interval=0,
                send_jitter=0,
                action_delay=0,
                paste_settle_delay=0,
            ),
            sleeper=lambda _seconds: None,
            uia_sender=True,
            my_names=["Me"],
        ).connect()
        client._enrich_uia_senders = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("profile enrichment reached the send verification path")
        )

        receipt = client.send_text("测试群", "hello", chat_type=ChatType.GROUP)

        self.assertTrue(receipt.verified)

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

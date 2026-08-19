from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections import OrderedDict, deque
from dataclasses import replace
from datetime import datetime
from typing import Callable, Iterable

from .accessibility import AtspiBackend, Node, read_accessible_text, walk
from .errors import ChatNotFound, ControlNotFound, MentionSelectionError
from .input import ClipboardBackend, InputBackend, WaylandClipboard, X11Clipboard, X11Input
from .models import ChatType, Direction, Message, SendReceipt
from .parser import MessageParser
from .safety import SafetyPolicy, SendRateLimiter
from .selectors import (
    DEFAULT_PROFILE,
    WeChatProfile,
    find_editor,
    find_exact_text,
    find_message_list,
    find_mention_candidate,
    find_profile_name,
    find_search_box,
    find_session_list,
)


class WeChatClient:
    """Safe automation facade for the official Linux WeChat desktop client.

    AT-SPI is used for reads. Writes are limited to visible pointer clicks and
    keyboard shortcuts. No WeChat protocol, process memory, or private control
    mutation mechanism is present in this package.
    """

    def __init__(
        self,
        *,
        auto_connect: bool = False,
        my_names: Iterable[str] = (),
        safety: SafetyPolicy | None = None,
        profile: WeChatProfile = DEFAULT_PROFILE,
        accessibility: AtspiBackend | None = None,
        input_backend: InputBackend | None = None,
        clipboard: ClipboardBackend | None = None,
        visual_direction: bool = False,
        uia_sender: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.accessibility = accessibility or AtspiBackend()
        self._input = input_backend
        self._clipboard = clipboard
        self.visual_direction = visual_direction
        self.uia_sender = uia_sender
        self.profile = profile
        self.safety = safety or SafetyPolicy()
        self.parser = MessageParser(my_names=my_names)
        self._limiter = SendRateLimiter(self.safety)
        self._sleep = sleeper
        self._root: Node | None = None
        self._chat: str | None = None
        self._chat_type = ChatType.UNKNOWN
        self._lock = threading.RLock()
        self._listeners: list[object] = []
        self._known_outgoing: deque[tuple[str, str, float]] = deque(maxlen=128)
        self._visual_cache: OrderedDict[
            str, tuple[Direction, str | None, float | None, str | None]
        ] = OrderedDict()
        self._interaction_blocked = False
        if auto_connect:
            self.connect()

    @property
    def is_connected(self) -> bool:
        return self._root is not None

    @property
    def current_chat(self) -> str | None:
        return self._chat

    @property
    def is_main_ui_ready(self) -> bool:
        return bool(self._root and find_search_box(self._root, self.profile))

    def connect(self) -> "WeChatClient":
        with self._lock:
            self.accessibility.connect()
            self._root = self.accessibility.main_window()
            return self

    def disconnect(self) -> None:
        for listener in list(self._listeners):
            try:
                listener.stop()
            except Exception:
                pass
        self._listeners.clear()
        with self._lock:
            self._root = None
            self._chat = None
            self._visual_cache.clear()

    def open_chat(
        self,
        chat: str,
        *,
        chat_type: ChatType | str = ChatType.UNKNOWN,
        timeout: float = 5.0,
    ) -> None:
        """Open a chat using the visible search field and normal input events."""

        if not chat or not chat.strip():
            raise ValueError("chat must be a non-empty string")
        chat_type = ChatType(chat_type)
        with self._lock:
            root = self._ensure_root()
            # VNC users may switch chats manually between polls. A cached name
            # alone is never proof that the requested chat is still active.
            if self._chat == chat and self._is_chat_open(root, chat):
                self._chat_type = chat_type
                return
            if self._is_chat_open(root, chat):
                self._chat = chat
                self._chat_type = chat_type
                return
            self._assert_clean_surface(root)
            input_backend = self._ensure_input()
            sessions = find_session_list(root, self.profile)
            visible_item = find_exact_text(sessions, chat, max_depth=6) if sessions else None
            if visible_item and visible_item.role in {"list item", "row", "table row"}:
                point = self._visible_center(visible_item, sessions)
                if point:
                    input_backend.click(*point)
                    quick_deadline = min(time.monotonic() + 2.0, time.monotonic() + timeout)
                    while time.monotonic() < quick_deadline:
                        if self._is_chat_open(root, chat):
                            self._chat = chat
                            self._chat_type = chat_type
                            return
                        self._sleep(0.12)
            search = find_search_box(root, self.profile)
            if search is None or search.bounds is None:
                raise ControlNotFound("WeChat search field was not found in the AT-SPI tree")
            input_backend.click(*search.bounds.center)
            self._sleep(self.safety.action_delay)
            input_backend.shortcut("ctrl", "a")
            with self._ensure_clipboard().text(chat):
                input_backend.shortcut("ctrl", "v")
                self._sleep(max(0.45, self.safety.paste_settle_delay))

            deadline = time.monotonic() + timeout
            result: Node | None = None
            while time.monotonic() < deadline:
                result = self._find_chat_result(root, chat, search)
                if result and result.bounds:
                    break
                self._sleep(0.15)
            if result is None or result.bounds is None:
                input_backend.press("esc")
                raise ChatNotFound(f"no visible exact search result for chat {chat!r}")
            input_backend.click(*result.bounds.center)

            while time.monotonic() < deadline:
                if self._is_chat_open(root, chat):
                    self._chat = chat
                    self._chat_type = chat_type
                    return
                self._sleep(0.15)
            raise ControlNotFound(f"chat {chat!r} opened, but its message list/editor was not exposed")

    def get_visible_messages(
        self,
        chat: str | None = None,
        *,
        chat_type: ChatType | str = ChatType.UNKNOWN,
    ) -> list[Message]:
        """Read the currently rendered messages; this never scrolls the chat."""

        with self._lock:
            if chat is not None:
                self.open_chat(chat, chat_type=chat_type)
            root = self._ensure_root()
            message_list = find_message_list(root, self.profile)
            if message_list is None:
                raise ControlNotFound("chat message list was not found")
            active_chat = chat or self._chat or self._infer_chat_title(root) or "<current>"
            requested_type = ChatType(chat_type)
            active_type = (
                requested_type
                if chat is not None or requested_type is not ChatType.UNKNOWN
                else self._chat_type
            )
            messages = self.parser.parse_list(message_list, chat=active_chat, chat_type=active_type)
            visual_enabled = self.visual_direction or self.uia_sender
            if visual_enabled:
                messages = self._apply_visual_cache(
                    messages,
                    include_sender=self.uia_sender and active_type is ChatType.GROUP,
                )
            needs_direction = any(item.direction is Direction.UNKNOWN for item in messages)
            needs_sender = (
                active_type is ChatType.GROUP
                and any(item.sender is None for item in messages)
            )
            needs_capture = needs_direction
            if visual_enabled and root.bounds and needs_capture:
                try:
                    from .vision import VisualDirectionDetector

                    snapshot = VisualDirectionDetector().capture(root.bounds)
                    messages = [
                        replace(
                            item,
                            direction=snapshot.classify(item.bounds),
                            raw={**item.raw, "direction_source": "window_pixels"},
                        )
                        if item.direction is Direction.UNKNOWN and item.bounds else item
                        for item in messages
                    ]
                except Exception as exc:
                    if self.uia_sender and needs_direction:
                        raise RuntimeError(
                            "sender enrichment failed; verify the requested optional "
                            "dependencies and desktop screen-capture permission"
                        ) from exc
            if self.uia_sender and active_type is ChatType.GROUP and any(
                item.sender is None and item.direction is Direction.INCOMING
                for item in messages
            ):
                try:
                    messages = self._enrich_uia_senders(
                        messages,
                        message_list=message_list,
                        main_window=root,
                        expected_chat=active_chat,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "profile-card UIA sender enrichment failed and further input was stopped"
                    ) from exc
            if visual_enabled:
                self._remember_visual(messages)
            return self._apply_outgoing_hints(messages)

    def discover_unread_chats(self, *, limit: int = 1) -> list[str]:
        """Open at most ``limit`` visible unread rows and return their titles.

        Linux WeChat exposes the whole session row as one combined accessible
        string, so splitting a title from its preview is not reliable. Opening
        the visible unread row with a normal click and reading the chat header
        is slower, but preserves names containing spaces and punctuation.
        """

        if limit < 1:
            return []
        with self._lock:
            root = self._ensure_root()
            self._assert_clean_surface(root)
            sessions = find_session_list(root, self.profile)
            if sessions is None:
                return []
            unread_rows = [
                row for row in sessions.children()
                if row.role in {"list item", "row", "table row"}
                and row.bounds is not None
                and re.search(r"(?:\d+\s*条未读|\bunread\b)", row.name, re.IGNORECASE)
            ]
            discovered: list[str] = []
            for row in unread_rows[:limit]:
                point = self._visible_center(row, sessions)
                if point is None:
                    continue
                self._ensure_input().click(*point)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if find_message_list(root, self.profile) and find_editor(root, self.profile):
                        title = self._infer_chat_title(root)
                        if title:
                            title = re.sub(r"\s*[（(]\d+[）)]\s*$", "", title).strip()
                            if title and title not in discovered:
                                discovered.append(title)
                                self._chat = title
                                self._chat_type = ChatType.UNKNOWN
                            break
                    self._sleep(0.1)
            return discovered

    def send_text(
        self,
        chat: str,
        text: str,
        *,
        chat_type: ChatType | str = ChatType.UNKNOWN,
        mentions: Iterable[str] = (),
        verify: bool = True,
        verify_timeout: float = 2.5,
    ) -> SendReceipt:
        """Send text through visible UI, optionally creating real rich @ mentions."""

        mention_names = self._normalize_mentions(mentions)
        requested_type = ChatType(chat_type)
        if mention_names and requested_type is not ChatType.GROUP:
            raise ValueError("mentions require chat_type=ChatType.GROUP")
        display_text = self._display_text(text, mention_names)
        self._limiter.validate_text(display_text)
        with self._lock:
            self._limiter.acquire(chat)
            self.open_chat(chat, chat_type=chat_type)
            root = self._ensure_root()
            editor = find_editor(root, self.profile)
            if editor is None or editor.bounds is None:
                raise ControlNotFound("chat editor was not found")
            self._assert_clean_surface(root, expected_chat=chat)
            existing_text = read_accessible_text(editor)
            if existing_text:
                raise ControlNotFound(
                    "the chat editor already contains a draft; refusing to overwrite or append to it"
                )
            input_backend = self._ensure_input()
            input_backend.click(*editor.bounds.center)
            self._sleep(self.safety.action_delay)
            try:
                for member in mention_names:
                    self._select_mention(root, editor, member)
                    input_backend.press("space")
                if text:
                    with self._ensure_clipboard().text(text):
                        input_backend.shortcut("ctrl", "v")
                        self._sleep(self.safety.paste_settle_delay)
                input_backend.press("enter")
                sent_at = datetime.now().astimezone()
                self._known_outgoing.append((chat, display_text, time.monotonic() + 120.0))
                self._sleep(self.safety.paste_settle_delay)
            except Exception:
                # Mention/search popups must be gone before the editor is
                # touched again. If cleanup cannot be proven, lock this
                # instance and perform no further input.
                self._abort_temporary_input(root, editor, expected_chat=chat)
                raise

            matched: Message | None = None
            if verify:
                deadline = time.monotonic() + verify_timeout
                while time.monotonic() < deadline:
                    messages = self.get_visible_messages()
                    matched = next(
                        (
                            item for item in reversed(messages)
                            if self._normalized_content(item.content)
                            == self._normalized_content(display_text)
                            and item.direction in {Direction.OUTGOING, Direction.UNKNOWN}
                        ),
                        None,
                    )
                    if matched:
                        break
                    self._sleep(0.18)
            return SendReceipt(
                chat=chat,
                content=display_text,
                sent_at=sent_at,
                verified=matched is not None,
                matched_message_id=matched.id if matched else None,
                mentions=mention_names,
            )

    def listen(
        self,
        chats: Iterable[str],
        on_message,
        *,
        chat_type: ChatType | str = ChatType.GROUP,
        poll_interval: float = 3.0,
        auto_reply: bool = False,
        on_error=None,
        block: bool = False,
    ):
        from .listener import MessageListener

        listener = MessageListener(
            self,
            chats,
            on_message,
            chat_type=ChatType(chat_type),
            poll_interval=poll_interval,
            auto_reply=auto_reply,
            on_error=on_error,
        )
        self._listeners.append(listener)
        return listener.start(block=block)

    def _ensure_root(self) -> Node:
        if self._root is None:
            self.connect()
        assert self._root is not None
        return self._root

    def _apply_outgoing_hints(self, messages: list[Message]) -> list[Message]:
        now = time.monotonic()
        while self._known_outgoing and self._known_outgoing[0][2] < now:
            self._known_outgoing.popleft()
        known = {
            (chat, self._normalized_content(content))
            for chat, content, _expiry in self._known_outgoing
        }
        sender = self.parser.my_names[0] if self.parser.my_names else "self"
        result: list[Message] = []
        for item in messages:
            if item.direction is Direction.UNKNOWN and (
                item.chat,
                self._normalized_content(item.content),
            ) in known:
                item = replace(
                    item,
                    direction=Direction.OUTGOING,
                    sender=sender,
                    raw={**item.raw, "direction_source": "client_send_registry"},
                )
            result.append(item)
        return result

    def _apply_visual_cache(
        self,
        messages: list[Message],
        *,
        include_sender: bool,
    ) -> list[Message]:
        result: list[Message] = []
        for item in messages:
            cached = self._visual_cache.get(item.id)
            if cached is None:
                result.append(item)
                continue
            self._visual_cache.move_to_end(item.id)
            cached_direction, cached_sender, cached_confidence, cached_source = cached
            direction = (
                cached_direction
                if item.direction is Direction.UNKNOWN else item.direction
            )
            sender = cached_sender if include_sender and item.sender is None else item.sender
            raw = dict(item.raw)
            if direction is not item.direction:
                raw["direction_source"] = "window_pixels"
            if sender is not item.sender:
                raw["sender_source"] = cached_source
                if cached_confidence is not None:
                    raw["sender_confidence"] = cached_confidence
            result.append(replace(item, direction=direction, sender=sender, raw=raw))
        return result

    def _remember_visual(self, messages: list[Message]) -> None:
        for item in messages:
            if not (
                item.raw.get("direction_source") == "window_pixels"
                or item.raw.get("sender_source") == "profile_uia"
            ):
                continue
            confidence = item.raw.get("sender_confidence")
            sender_source = item.raw.get("sender_source")
            self._visual_cache[item.id] = (
                item.direction,
                item.sender,
                float(confidence) if confidence is not None else None,
                str(sender_source) if sender_source else None,
            )
            self._visual_cache.move_to_end(item.id)
            while len(self._visual_cache) > 512:
                self._visual_cache.popitem(last=False)

    def _enrich_uia_senders(
        self,
        messages: list[Message],
        *,
        message_list: Node,
        main_window: Node,
        expected_chat: str,
    ) -> list[Message]:
        result: list[Message] = []
        for item in messages:
            if (
                item.sender is not None
                or item.direction is not Direction.INCOMING
                or item.bounds is None
            ):
                result.append(item)
                continue
            sender = self._read_profile_sender(
                item.bounds,
                message_list=message_list,
                main_window=main_window,
                expected_chat=expected_chat,
            )
            result.append(
                replace(
                    item,
                    sender=sender,
                    raw={**item.raw, "sender_source": "profile_uia"},
                )
                if sender else item
            )
        return result

    def _read_profile_sender(
        self,
        row_bounds,
        *,
        message_list: Node,
        main_window: Node,
        expected_chat: str,
    ) -> str | None:
        self._assert_clean_surface(main_window, expected_chat=expected_chat)
        clip = message_list.bounds
        if clip is None:
            return None
        x = row_bounds.x + min(44, max(20, row_bounds.width // 12))
        y = row_bounds.y + min(30, max(16, row_bounds.height // 3))
        if not (clip.x <= x < clip.right and clip.y <= y < clip.bottom):
            return None
        known_windows = {window.identity for window in self.accessibility.windows()}
        input_backend = self._ensure_input()
        input_backend.click(x, y)
        deadline = time.monotonic() + 1.8
        sender: str | None = None
        try:
            while time.monotonic() < deadline:
                popups = [
                    window for window in self.accessibility.windows()
                    if window.identity not in known_windows
                    and window.identity != main_window.identity
                ]
                for popup in popups:
                    sender = find_profile_name(popup)
                    if sender:
                        return sender
                self._sleep(0.08)
            return None
        finally:
            # A profile card must be explicitly dismissed and observed gone.
            # Merely reactivating the main window is not sufficient.
            self._dismiss_transients(main_window, expected_chat=expected_chat)

    def _assert_clean_surface(
        self,
        main_window: Node,
        *,
        expected_chat: str | None = None,
    ) -> None:
        if self._interaction_blocked:
            raise ControlNotFound(
                "this client is input-locked after a transient-window safety failure"
            )
        extras = [
            window for window in self.accessibility.windows()
            if window.identity != main_window.identity
        ]
        if extras:
            self._interaction_blocked = True
            raise ControlNotFound(
                "an unexpected WeChat transient already exists; input locked without dismissing it"
            )
        elif expected_chat and not self._is_chat_open(main_window, expected_chat):
            self._interaction_blocked = True
            raise ControlNotFound(
                f"expected chat {expected_chat!r} is no longer open; input locked"
            )

    def _dismiss_transients(
        self,
        main_window: Node,
        *,
        expected_chat: str | None = None,
    ) -> None:
        extras = [
            window for window in self.accessibility.windows()
            if window.identity != main_window.identity
        ]
        if not extras:
            if expected_chat and not self._is_chat_open(main_window, expected_chat):
                self._interaction_blocked = True
                raise ControlNotFound(
                    f"expected chat {expected_chat!r} is no longer open; input locked"
                )
            return
        input_backend = self._ensure_input()
        for _attempt in range(2):
            input_backend.press("esc")
            for _poll in range(20):
                extras = [
                    window for window in self.accessibility.windows()
                    if window.identity != main_window.identity
                ]
                if not extras:
                    break
                self._sleep(0.06)
            if not extras:
                break
        else:
            extras = [window for window in self.accessibility.windows()
                      if window.identity != main_window.identity]
        if extras or (expected_chat and not self._is_chat_open(main_window, expected_chat)):
            self._interaction_blocked = True
            raise ControlNotFound(
                "WeChat transient did not close cleanly or the active chat changed; input locked"
            )

    def _select_mention(self, root: Node, editor: Node, member: str) -> None:
        """Create one real Qt rich-text mention via the visible member popup."""

        input_backend = self._ensure_input()
        before_text = read_accessible_text(editor)
        before_objects = before_text.count("\ufffc") if before_text is not None else None
        input_backend.shortcut("shift", "2")
        deadline = time.monotonic() + 2.5
        candidate: Node | None = None
        while time.monotonic() < deadline:
            candidate = find_mention_candidate(
                self.accessibility.windows(),
                main_window=root,
                editor=editor,
                member=member,
            )
            if candidate is not None:
                break
            self._sleep(0.08)
        if candidate is None or candidate.bounds is None:
            raise MentionSelectionError(
                f"no unique visible exact @ candidate for member {member!r}"
            )
        input_backend.click(*candidate.bounds.center)

        verify_deadline = time.monotonic() + 1.5
        while time.monotonic() < verify_deadline:
            current_text = read_accessible_text(editor)
            if (
                before_objects is not None
                and current_text is not None
                and current_text.count("\ufffc") == before_objects + 1
            ):
                popup_deadline = time.monotonic() + 0.6
                while time.monotonic() < popup_deadline:
                    if all(
                        window.identity == root.identity
                        for window in self.accessibility.windows()
                    ):
                        return
                    self._sleep(0.05)
                self._dismiss_transients(root, expected_chat=self._chat)
                return
            self._sleep(0.06)
        raise MentionSelectionError(
            f"WeChat did not expose a rich mention token after selecting {member!r}"
        )

    def _abort_temporary_input(
        self,
        root: Node,
        editor: Node,
        *,
        expected_chat: str,
    ) -> None:
        """Close framework-created popups, then clear only our temporary draft."""

        self._dismiss_transients(root, expected_chat=expected_chat)
        if editor.bounds is None:
            self._interaction_blocked = True
            raise ControlNotFound("editor disappeared during cleanup; input locked")
        input_backend = self._ensure_input()
        input_backend.click(*editor.bounds.center)
        input_backend.shortcut("ctrl", "a")
        input_backend.press("backspace")

    @staticmethod
    def _normalize_mentions(mentions: Iterable[str]) -> tuple[str, ...]:
        values = (mentions,) if isinstance(mentions, str) else tuple(mentions)
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("mention names must be non-empty strings")
            member = " ".join(value.split())
            if member not in normalized:
                normalized.append(member)
        return tuple(normalized)

    @staticmethod
    def _display_text(text: str, mentions: tuple[str, ...]) -> str:
        if not isinstance(text, str):
            raise ValueError("message text must be a string")
        prefix = " ".join(f"@{member}" for member in mentions)
        return f"{prefix} {text}" if prefix and text else prefix or text

    @staticmethod
    def _normalized_content(value: str) -> str:
        return " ".join(value.replace("\u2005", " ").replace("\xa0", " ").split())

    def _ensure_input(self) -> InputBackend:
        if self._interaction_blocked:
            raise ControlNotFound(
                "this client is input-locked after a transient-window safety failure"
            )
        if self._input is None:
            self._input = X11Input(event_delay=self.safety.action_delay / 2)
        configure = getattr(self._input, "configure_accessible_window", None)
        root = self._ensure_root()
        if configure and root.bounds:
            configure(root.bounds)
        return self._input

    def _ensure_clipboard(self) -> ClipboardBackend:
        if self._clipboard is None:
            if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
                self._clipboard = WaylandClipboard(restore=True)
            else:
                self._clipboard = X11Clipboard(restore=True)
        return self._clipboard

    def _find_chat_result(self, root: Node, chat: str, search: Node) -> Node | None:
        wanted = " ".join(chat.split()).casefold()
        search_bottom = search.bounds.bottom if search.bounds else -1
        candidates: list[tuple[int, int, Node]] = []
        roots: list[Node] = [root]
        roots.extend(window for window in self.accessibility.windows() if window.identity != root.identity)
        seen: set[str] = set()
        for candidate_root in roots:
            is_popup = candidate_root.identity != root.identity
            for node, depth in walk(candidate_root, max_depth=18):
                if node.identity in seen:
                    continue
                seen.add(node.identity)
                values = [node.name, node.description, *node.attributes.values()]
                normalized = [" ".join(str(value).split()).casefold() for value in values if value]
                if wanted not in normalized:
                    continue
                bounds = node.bounds
                if not bounds or bounds.bottom <= search_bottom:
                    continue
                # Linux WeChat inserts a short (about 34 logical px) web/
                # history-search action whose name exactly equals the query.
                # Real contact/group rows include an avatar and are taller.
                if is_popup and node.role in {"list item", "row", "table row"} and bounds.height < 45:
                    continue
                score = 100 + (80 if is_popup else 0)
                if node.role in {"list item", "row", "table row", "panel"}:
                    score += 40
                score += min(bounds.width, 400) // 20
                # The first exact row in the popup is normally the direct
                # contact/chat result, above matching message-history rows.
                score -= max(0, bounds.y - search_bottom) // 100
                candidates.append((score, -depth, node))
        return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]

    @staticmethod
    def _infer_chat_title(root: Node) -> str | None:
        root_bounds = root.bounds
        if root_bounds is None:
            return None
        candidates: list[tuple[int, str]] = []
        for node, _depth in walk(root, max_depth=18):
            bounds = node.bounds
            if not node.name or not bounds:
                continue
            if node.role not in {"label", "text", "heading"}:
                continue
            if node.name.casefold() in {"messages", "消息", "send", "发送"}:
                continue
            if bounds.y < root_bounds.y + root_bounds.height * 0.25 and bounds.x > root_bounds.x + 180:
                candidates.append((len(node.name), node.name))
        return min(candidates, default=(0, None), key=lambda item: item[0])[1]

    @staticmethod
    def _visible_center(node: Node, container: Node) -> tuple[int, int] | None:
        bounds = node.bounds
        clip = container.bounds
        if not bounds or not clip:
            return None
        left = max(bounds.x, clip.x)
        top = max(bounds.y, clip.y)
        right = min(bounds.right, clip.right)
        bottom = min(bounds.bottom, clip.bottom)
        if left >= right or top >= bottom:
            return None
        return (left + right) // 2, (top + bottom) // 2

    @staticmethod
    def _is_chat_open(root: Node, chat: str) -> bool:
        root_bounds = root.bounds
        if root_bounds is None:
            return False
        message_list = find_message_list(root)
        editor = find_editor(root)
        if message_list is None or editor is None:
            return False
        content_bounds = [
            node.bounds for node in (message_list, editor) if node.bounds is not None
        ]
        if not content_bounds:
            return False
        content_left = min(bounds.x for bounds in content_bounds)
        wanted = " ".join(chat.split()).casefold()
        for node, _depth in walk(root, max_depth=18):
            bounds = node.bounds
            if not bounds:
                continue
            # The session list keeps a nearly fixed width while the right
            # content pane grows with the window. A percentage threshold
            # therefore rejects valid titles in wide/Xvfb windows. Anchor the
            # title to the detected message/editor pane instead.
            if bounds.x < content_left:
                continue
            if bounds.y > root_bounds.y + root_bounds.height * 0.18:
                continue
            values = (node.name, node.description, *node.attributes.values())
            if any(" ".join(str(value).split()).casefold() == wanted for value in values if value):
                return True
        return False

    def __enter__(self) -> "WeChatClient":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.disconnect()

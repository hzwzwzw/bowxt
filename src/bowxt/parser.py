from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime
from typing import Iterable

from .accessibility import Node, attr_blob, related_nodes
from .models import ChatType, Direction, Message, MessageType, Rect
from .selectors import text_values, visible_message_nodes


_TIME_ONLY = re.compile(r"^(?:上午|下午|晚上|凌晨)?\s*\d{1,2}:\d{2}$")
_DATE_TIME = re.compile(r"^(?:(\d{4})[-/.年])?(\d{1,2})[-/.月](\d{1,2})日?\s+(\d{1,2}):(\d{2})$")
_GENERIC = {
    "头像", "avatar", "图片", "image", "消息", "message", "已读", "未读",
    "更多", "more", "菜单", "menu", "发送", "send",
}


class MessageParser:
    def __init__(self, *, my_names: Iterable[str] = (), now=lambda: datetime.now().astimezone()):
        self.my_names = tuple(name for name in my_names if name)
        self._now = now

    def parse_list(
        self,
        message_list: Node,
        *,
        chat: str,
        chat_type: ChatType = ChatType.UNKNOWN,
    ) -> list[Message]:
        current_time: datetime | None = None
        messages: list[Message] = []
        occurrences: dict[tuple[str, ...], int] = {}
        for node in visible_message_nodes(message_list):
            primary = self._primary_text(node)
            if self._is_time_node(node, primary):
                current_time = self._parse_time(primary) or current_time
                continue
            message = self.parse_message(
                node,
                container=message_list.bounds,
                chat=chat,
                chat_type=chat_type,
                timestamp=current_time,
            )
            if message:
                stable_parts = self._stable_parts(message)
                occurrence = occurrences.get(stable_parts, 0)
                occurrences[stable_parts] = occurrence + 1
                message = replace(
                    message,
                    id=self._stable_message_id(stable_parts, occurrence),
                    raw={**message.raw, "visible_occurrence": occurrence},
                )
                messages.append(message)
        return messages

    def parse_message(
        self,
        node: Node,
        *,
        container: Rect | None,
        chat: str,
        chat_type: ChatType,
        timestamp: datetime | None = None,
    ) -> Message | None:
        values = text_values(node)
        content = self._content(node, values)
        if not content:
            return None
        direction = self._direction(node, container, values, content)
        message_type = self._message_type(node, content, direction)
        sender = self._sender(node, values, content, direction, chat_type)
        if chat_type is ChatType.GROUP and direction is not Direction.OUTGOING and sender is None:
            prefix_sender, payload = self._split_group_prefix(content)
            if prefix_sender:
                sender, content = prefix_sender, payload
        stable_parts = (
            chat,
            content,
            sender or "",
            direction.value,
            timestamp.isoformat() if timestamp else "",
            message_type.value,
        )
        message_id = self._stable_message_id(stable_parts, 0)
        normalized_content = content.replace("\u2005", " ").replace("\xa0", " ")
        is_at_me = any(f"@{name}" in normalized_content for name in self.my_names)
        return Message(
            id=message_id,
            chat=chat,
            content=content,
            type=message_type,
            direction=direction,
            sender=sender,
            timestamp=timestamp,
            chat_type=chat_type,
            is_at_me=is_at_me,
            bounds=self._visual_bounds(node, values, content),
            raw={
                "role": node.role,
                "name": node.name,
                "description": node.description,
                "attributes": dict(node.attributes),
                "texts": [value for value, _child in values],
            },
        )

    @staticmethod
    def _stable_parts(message: Message) -> tuple[str, ...]:
        return (
            message.chat,
            message.content,
            message.sender or "",
            message.direction.value,
            message.timestamp.isoformat() if message.timestamp else "",
            message.type.value,
        )

    @staticmethod
    def _stable_message_id(parts: tuple[str, ...], occurrence: int) -> str:
        identity = "|".join((*parts, str(occurrence)))
        return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]

    @staticmethod
    def _primary_text(node: Node) -> str:
        return (node.name or node.description or "").strip()

    def _content(self, node: Node, values: list[tuple[str, Node]]) -> str:
        for key in ("message", "content", "text", "value"):
            value = node.attributes.get(key, "").strip()
            if value:
                return value
        primary = self._primary_text(node)
        if primary and not self._looks_generic(primary) and not self._looks_like_time(primary):
            return primary
        candidates = [
            value for value, child in values
            if not self._looks_generic(value) and not self._looks_like_time(value)
            and child.role not in {"image", "icon"}
        ]
        return max(candidates, key=len, default="")

    def _sender(
        self,
        node: Node,
        values: list[tuple[str, Node]],
        content: str,
        direction: Direction,
        chat_type: ChatType,
    ) -> str | None:
        for key in ("sender", "from", "author", "user-name", "username"):
            value = node.attributes.get(key, "").strip()
            if value:
                return value
        if direction is Direction.OUTGOING:
            return self.my_names[0] if self.my_names else "self"
        if chat_type is not ChatType.GROUP or direction is Direction.SYSTEM:
            return None

        relation_sender = self._sender_from_relations(node, content)
        if relation_sender:
            return relation_sender

        candidates: list[tuple[int, str]] = []
        message_bounds = node.bounds
        for value, child in values:
            if value == content or self._looks_generic(value) or self._looks_like_time(value):
                continue
            if len(value) > 64:
                continue
            score = 0
            blob = attr_blob(child)
            if child.role in {"image", "icon"} or "avatar" in blob or "head" in blob:
                score += 80
            if child.role in {"label", "text", "static", "paragraph"}:
                score += 30
            bounds = child.bounds
            if bounds and message_bounds:
                if bounds.x < message_bounds.x + message_bounds.width * 0.45:
                    score += 15
                if bounds.y <= message_bounds.y + message_bounds.height * 0.55:
                    score += 15
            if score:
                candidates.append((score, value))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _sender_from_relations(self, node: Node, content: str) -> str | None:
        candidates: list[tuple[int, str]] = []
        for relation_name, targets in related_nodes(node).items():
            relation_score = 80 if any(
                marker in relation_name
                for marker in ("label", "description", "details", "flows from")
            ) else 15
            for target in targets:
                for value, child in text_values(target, max_depth=2):
                    if (
                        value == content
                        or self._looks_generic(value)
                        or self._looks_like_time(value)
                        or len(value) > 64
                    ):
                        continue
                    score = relation_score
                    if child.role in {"label", "text", "image", "icon"}:
                        score += 20
                    candidates.append((score, value))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    def _direction(
        self,
        node: Node,
        container: Rect | None,
        values: list[tuple[str, Node]],
        content: str,
    ) -> Direction:
        blob = attr_blob(node)
        if any(value in blob for value in ("outgoing", "sent-by-self", "from=self", "align=right")):
            return Direction.OUTGOING
        if any(value in blob for value in ("incoming", "received", "align=left")):
            return Direction.INCOMING
        if any(value in blob for value in ("system_message", "time_item", "chatitemview")):
            return Direction.SYSTEM
        visual = self._visual_bounds(node, values, content)
        if visual and container:
            center = visual.x + visual.width / 2
            split = container.x + container.width / 2
            tolerance = container.width * 0.06
            if center > split + tolerance:
                return Direction.OUTGOING
            if center < split - tolerance:
                return Direction.INCOMING
        return Direction.UNKNOWN

    @staticmethod
    def _visual_bounds(node: Node, values: list[tuple[str, Node]], content: str) -> Rect | None:
        matches = [child.bounds for value, child in values if value == content and child.bounds]
        if matches:
            return min(matches, key=lambda rect: rect.width * rect.height)
        return node.bounds

    @staticmethod
    def _message_type(node: Node, content: str, direction: Direction) -> MessageType:
        blob = f"{attr_blob(node)} {content}".casefold()
        if direction is Direction.SYSTEM:
            return MessageType.SYSTEM
        mapping = (
            (MessageType.IMAGE, ("[图片]", "image_message", "图片消息")),
            (MessageType.FILE, ("[文件]", "file_message", "文件消息")),
            (MessageType.VOICE, ("[语音]", "voice_message", "语音消息")),
            (MessageType.VIDEO, ("[视频]", "video_message", "视频消息")),
            (MessageType.STICKER, ("[动画表情]", "sticker", "emoji_message")),
            (MessageType.LINK, ("link_message", "链接消息")),
        )
        for message_type, markers in mapping:
            if any(marker.casefold() in blob for marker in markers):
                return message_type
        return MessageType.TEXT

    @staticmethod
    def _looks_generic(value: str) -> bool:
        return value.strip().casefold() in _GENERIC

    @staticmethod
    def _split_group_prefix(value: str) -> tuple[str | None, str]:
        """Parse only strong sender-prefix formats exposed by some clients."""

        match = re.match(r"^([^:\n：]{1,32})[：:]\s+(.+)$", value, re.DOTALL)
        if not match:
            match = re.match(r"^([^\n]{1,32})\n(.+)$", value, re.DOTALL)
        if not match:
            return None, value
        sender, content = match.group(1).strip(), match.group(2).strip()
        if not sender or not content or sender.casefold() in _GENERIC:
            return None, value
        return sender, content

    @staticmethod
    def _looks_like_time(value: str) -> bool:
        value = value.strip()
        return bool(_TIME_ONLY.match(value) or _DATE_TIME.match(value)) or value in {
            "昨天", "前天", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"
        }

    def _is_time_node(self, node: Node, text: str) -> bool:
        blob = attr_blob(node)
        return self._looks_like_time(text) or "time_item" in blob or "chatitemview" in blob

    def _parse_time(self, text: str) -> datetime | None:
        now = self._now()
        cleaned = (
            text.strip().replace("上午", "").replace("下午", "")
            .replace("晚上", "").replace("凌晨", "").strip()
        )
        add_twelve = text.strip().startswith(("下午", "晚上"))
        if _TIME_ONLY.match(text.strip()):
            try:
                hour, minute = map(int, cleaned.split(":"))
                if add_twelve and hour < 12:
                    hour += 12
                return now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except ValueError:
                return None
        match = _DATE_TIME.match(text.strip())
        if match:
            year, month, day, hour, minute = match.groups()
            return now.replace(
                year=int(year or now.year), month=int(month), day=int(day),
                hour=int(hour), minute=int(minute), second=0, microsecond=0,
            )
        return None

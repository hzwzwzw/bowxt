from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .accessibility import Node, attr_blob, descendants, normalize_role, walk


LIST_ROLES = {"list", "list box", "scroll pane", "panel"}
EDIT_ROLES = {"text", "entry", "text box", "password text", "edit bar"}
ITEM_ROLES = {"list item", "panel", "section", "paragraph", "unknown"}


@dataclass(frozen=True, slots=True)
class WeChatProfile:
    message_list_ids: tuple[str, ...] = ("chat_message_list", "chatmessagelist")
    session_list_ids: tuple[str, ...] = ("session_list", "sessionlist")
    editor_ids: tuple[str, ...] = ("chat_input", "chatinput", "text_edit", "input")
    search_names: tuple[str, ...] = ("搜索", "search")
    send_names: tuple[str, ...] = ("发送", "send")


DEFAULT_PROFILE = WeChatProfile()


def find_message_list(root: Node, profile: WeChatProfile = DEFAULT_PROFILE) -> Node | None:
    candidates: list[tuple[int, int, Node]] = []
    root_bounds = root.bounds
    for node, depth in walk(root, max_depth=18):
        blob = attr_blob(node)
        score = 0
        has_stable_id = any(value in blob for value in profile.message_list_ids)
        name = node.name.casefold()
        if name in {"chats", "sessions", "会话"}:
            continue
        if has_stable_id:
            score += 1000
        if node.role in LIST_ROLES:
            score += 20
        elif not has_stable_id:
            continue
        if node.bounds:
            score += 80
        elif not has_stable_id:
            continue
        if "showing" in node.states or "visible" in node.states:
            score += 100
        is_named_messages = name in {"messages", "消息"}
        if is_named_messages:
            score += 300
        bounds = node.bounds
        is_right_content = bool(
            root_bounds and bounds
            and bounds.x >= root_bounds.x + root_bounds.width * 0.30
            and bounds.width >= root_bounds.width * 0.30
        )
        if not (has_stable_id or is_named_messages or is_right_content):
            continue
        children = node.children()
        if len(children) >= 2:
            score += min(len(children), 20)
        message_like = sum(_message_likeness(child) for child in children[-15:])
        score += message_like
        if score >= 45:
            candidates.append((score, -depth, node))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def find_session_list(root: Node, profile: WeChatProfile = DEFAULT_PROFILE) -> Node | None:
    candidates: list[tuple[int, int, Node]] = []
    for node, depth in walk(root, max_depth=18):
        blob = attr_blob(node)
        score = 0
        if any(value in blob for value in profile.session_list_ids):
            score += 1000
        if node.name.casefold() in {"会话", "chats", "sessions"}:
            score += 200
        if node.role in LIST_ROLES:
            score += 20
        else:
            continue
        if node.bounds:
            score += 40
        child_names = sum(bool(child.name) for child in node.children()[:20])
        score += child_names
        if score >= 45:
            candidates.append((score, -depth, node))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def find_search_box(root: Node, profile: WeChatProfile = DEFAULT_PROFILE) -> Node | None:
    candidates: list[tuple[int, int, Node]] = []
    root_bounds = root.bounds
    for node, depth in walk(root, max_depth=16):
        name = node.name.casefold()
        blob = attr_blob(node)
        score = 0
        if any(value in name or value in blob for value in profile.search_names):
            score += 200
        is_editable = node.role in EDIT_ROLES or "editable" in node.states
        if is_editable:
            score += 60
        if root_bounds and node.bounds and node.bounds.y < root_bounds.y + root_bounds.height * 0.35:
            score += 20
        if score >= 100 and is_editable and node.bounds:
            candidates.append((score, -depth, node))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def find_editor(root: Node, profile: WeChatProfile = DEFAULT_PROFILE) -> Node | None:
    candidates: list[tuple[int, int, Node]] = []
    root_bounds = root.bounds
    for node, depth in walk(root, max_depth=18):
        blob = attr_blob(node)
        score = 0
        has_stable_id = any(value in blob for value in profile.editor_ids)
        if has_stable_id:
            score += 500
        if node.role in EDIT_ROLES:
            score += 80
        if "editable" in node.states or "focusable" in node.states:
            score += 40
        bounds = node.bounds
        in_editor_region = False
        if root_bounds and bounds:
            if bounds.y > root_bounds.y + root_bounds.height * 0.55:
                in_editor_region = True
                score += 80
            if bounds.x > root_bounds.x + root_bounds.width * 0.22:
                score += 20
            if bounds.width > root_bounds.width * 0.25:
                score += 20
        if score >= 120 and bounds and (has_stable_id or in_editor_region):
            candidates.append((score, -depth, node))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def find_exact_text(root: Node, text: str, *, max_depth: int = 12) -> Node | None:
    wanted = _clean(text)
    candidates: list[tuple[int, int, Node]] = []
    for node, depth in walk(root, max_depth=max_depth):
        values = (node.name, node.description, *node.attributes.values())
        exact = any(_clean(value) == wanted for value in values if value)
        contains = any(wanted in _clean(value) for value in values if value)
        if exact or contains:
            score = (200 if exact else 100) + (20 if node.bounds else 0)
            if node.role in ITEM_ROLES:
                score += 30
            candidates.append((score, -depth, node))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def find_mention_candidate(
    windows: Iterable[Node],
    *,
    main_window: Node,
    editor: Node,
    member: str,
) -> Node | None:
    """Return one unambiguous exact member row from WeChat's @ popup."""

    wanted = _clean(member)
    editor_bounds = editor.bounds
    matches: list[Node] = []
    for window in windows:
        if window.identity == main_window.identity or not window.bounds:
            continue
        bounds = window.bounds
        if editor_bounds and (
            bounds.bottom < editor_bounds.y - 320
            or bounds.y > editor_bounds.bottom + 40
        ):
            continue
        for node, _depth in walk(window, max_depth=12):
            if (
                node.role in {"list item", "row", "table row"}
                and node.bounds
                and _clean(node.name) == wanted
            ):
                matches.append(node)
    if len(matches) != 1:
        return None
    return matches[0]


def is_profile_card(window: Node) -> bool:
    """Conservatively identify a WeChat member profile top-level."""

    markers = {
        "remark", "备注", "messages", "发消息", "voice call", "语音通话",
        "video call", "视频通话", "enterprise information", "企业信息",
        "add alias", "添加备注",
    }
    found: set[str] = set()
    for node, _depth in walk(window, max_depth=12):
        value = _clean(node.name)
        if value in markers:
            found.add(value)
    return len(found) >= 2


def find_profile_name(profile_window: Node) -> str | None:
    """Extract the primary nickname only from a verified profile card."""

    if not is_profile_card(profile_window):
        return None
    root_bounds = profile_window.bounds
    if root_bounds is None:
        return None
    ignored = {
        "more", "更多", "messages", "消息", "voice call", "语音通话",
        "video call", "视频通话", "add alias", "添加备注", "delete", "删除",
    }
    candidates: list[tuple[int, str]] = []
    for node, depth in walk(profile_window, max_depth=12):
        name = _display_clean(node.name)
        bounds = node.bounds
        if (
            not name
            or len(name) > 64
            or name.casefold() in ignored
            or node.role not in {"button", "label", "text"}
            or bounds is None
            or bounds.y > root_bounds.y + min(130, root_bounds.height * 0.34)
        ):
            continue
        score = 100 if node.role == "button" else 70
        score -= depth
        score -= max(0, bounds.y - root_bounds.y) // 8
        candidates.append((score, name))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def visible_message_nodes(message_list: Node) -> list[Node]:
    result: list[Node] = []
    for child in message_list.children():
        if _message_likeness(child) >= 15:
            result.append(child)
    return result


def text_values(node: Node, *, max_depth: int = 5) -> list[tuple[str, Node]]:
    values: list[tuple[str, Node]] = []
    seen: set[str] = set()
    for candidate, _depth in walk(node, max_depth=max_depth):
        candidates = [candidate.name, candidate.description]
        for key in ("text", "value", "accessible-name", "label", "title"):
            if candidate.attributes.get(key):
                candidates.append(candidate.attributes[key])
        for value in candidates:
            display_value = _display_clean(value)
            dedupe_key = display_value.casefold()
            if display_value and dedupe_key not in seen:
                seen.add(dedupe_key)
                values.append((display_value, candidate))
    return values


def _message_likeness(node: Node) -> int:
    blob = attr_blob(node)
    score = 0
    if any(value in blob for value in ("chattextitem", "chatbubbleitem", "message_item", "messageitem")):
        score += 100
    if any(value in blob for value in ("chatitemview", "time_item", "system_message")):
        score += 60
    if node.role in {"list item", "article", "paragraph", "section"}:
        score += 15
    if node.name or node.description:
        score += 10
    nested = descendants(node, max_depth=3)
    if any(value.name or value.description for value in nested):
        score += 10
    return score


def _clean(value: str) -> str:
    return _display_clean(value).casefold()


def _display_clean(value: str) -> str:
    return " ".join(str(value or "").replace("\u2005", " ").replace("\xa0", " ").split())

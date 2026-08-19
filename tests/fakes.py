from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

from bowxt.models import Rect


@dataclass
class FakeNode:
    role: str
    name: str = ""
    description: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    states: set[str] = field(default_factory=set)
    bounds: Rect | None = None
    nodes: list["FakeNode"] = field(default_factory=list)
    token: str = ""
    accessible_text: str | None = None
    relation_nodes: dict[str, list["FakeNode"]] = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.token or f"{id(self)}"

    def children(self) -> list["FakeNode"]:
        return self.nodes

    def relations(self) -> dict[str, list["FakeNode"]]:
        return self.relation_nodes


class FakeAccessibility:
    def __init__(self, root):
        self.root = root

    def connect(self):
        return self.root

    def main_window(self):
        return self.root

    def windows(self):
        return [self.root]


class FakeInput:
    def __init__(self):
        self.events = []

    def focus_wechat(self):
        self.events.append(("focus",))

    def click(self, x, y, *, count=1):
        self.events.append(("click", x, y, count))

    def shortcut(self, *keys):
        self.events.append(("shortcut", *keys))

    def press(self, key):
        self.events.append(("press", key))


class FakeClipboard:
    def __init__(self):
        self.values = []

    @contextmanager
    def text(self, value):
        self.values.append(value)
        yield


def sample_tree():
    search = FakeNode(
        "text", "搜索", states={"editable", "focusable"}, bounds=Rect(20, 20, 180, 35), token="search"
    )
    result = FakeNode("list item", "测试群", bounds=Rect(20, 90, 200, 50), token="result")
    incoming = FakeNode(
        "list item",
        attributes={"class": "message incoming"},
        bounds=Rect(250, 200, 650, 80),
        token="incoming",
        nodes=[
            FakeNode("image", "Alice", bounds=Rect(260, 215, 35, 35), token="avatar"),
            FakeNode("text", "你好", bounds=Rect(310, 220, 100, 35), token="incoming-text"),
        ],
    )
    outgoing = FakeNode(
        "list item",
        attributes={"class": "message outgoing"},
        bounds=Rect(250, 300, 650, 80),
        token="outgoing",
        nodes=[FakeNode("text", "hello", bounds=Rect(750, 320, 100, 35), token="outgoing-text")],
    )
    message_list = FakeNode(
        "list",
        "消息",
        attributes={"id": "chat_message_list"},
        bounds=Rect(240, 150, 680, 500),
        nodes=[incoming, outgoing],
        token="message-list",
    )
    editor = FakeNode(
        "text",
        attributes={"id": "chat_input"},
        states={"editable", "focusable"},
        bounds=Rect(300, 690, 560, 120),
        token="editor",
    )
    root = FakeNode(
        "frame", "微信", bounds=Rect(0, 0, 960, 860),
        nodes=[
            search,
            result,
            FakeNode("label", "测试群", bounds=Rect(500, 30, 120, 30), token="header"),
            message_list,
            editor,
        ],
        token="root",
    )
    return root, message_list

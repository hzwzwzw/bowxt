from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Protocol

from .errors import AccessibilityUnavailable, WeChatNotFound
from .models import Rect


class Node(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def attributes(self) -> dict[str, str]: ...

    @property
    def states(self) -> set[str]: ...

    @property
    def bounds(self) -> Rect | None: ...

    @property
    def identity(self) -> str: ...

    def children(self) -> list["Node"]: ...

    def relations(self) -> dict[str, list["Node"]]: ...


def normalize_role(value: str) -> str:
    return re.sub(r"[ _-]+", " ", (value or "").strip().lower())


def parse_attributes(values: Any) -> dict[str, str]:
    if isinstance(values, dict):
        return {str(k): str(v) for k, v in values.items()}
    result: dict[str, str] = {}
    for value in values or ():
        key, sep, item = str(value).partition(":")
        if sep:
            result[key.strip()] = item.strip()
    return result


@dataclass(slots=True)
class AtspiNode:
    raw: Any
    pyatspi: Any

    @property
    def name(self) -> str:
        return _safe_string(lambda: self.raw.name)

    @property
    def description(self) -> str:
        return _safe_string(lambda: self.raw.description)

    @property
    def role(self) -> str:
        return normalize_role(_safe_string(self.raw.getRoleName))

    @property
    def attributes(self) -> dict[str, str]:
        try:
            return parse_attributes(self.raw.getAttributes())
        except Exception:
            return {}

    @property
    def states(self) -> set[str]:
        result: set[str] = set()
        try:
            state_set = self.raw.getState()
            for value in state_set.getStates():
                try:
                    result.add(str(self.pyatspi.stateToString(value)).lower())
                except Exception:
                    result.add(str(value).lower())
        except Exception:
            pass
        return result

    @property
    def bounds(self) -> Rect | None:
        try:
            component = self.raw.queryComponent()
            ext = component.getExtents(self.pyatspi.DESKTOP_COORDS)
            if ext.width <= 0 or ext.height <= 0:
                return None
            return Rect(int(ext.x), int(ext.y), int(ext.width), int(ext.height))
        except Exception:
            return None

    @property
    def identity(self) -> str:
        pieces: list[str] = []
        for key in ("id", "object-id", "automation-id", "class", "class-name"):
            if self.attributes.get(key):
                pieces.append(f"{key}={self.attributes[key]}")
        for attr in ("path", "object_path", "uniqueName"):
            try:
                value = getattr(self.raw, attr)
                if value:
                    pieces.append(str(value))
            except Exception:
                pass
        if not pieces:
            rect = self.bounds
            pieces = [self.role, self.name, repr(rect)]
        return hashlib.sha1("|".join(pieces).encode("utf-8", "replace")).hexdigest()

    def children(self) -> list["AtspiNode"]:
        result: list[AtspiNode] = []
        try:
            for index in range(int(self.raw.childCount)):
                child = self.raw.getChildAtIndex(index)
                if child is not None:
                    result.append(AtspiNode(child, self.pyatspi))
        except Exception:
            pass
        return result

    def relations(self) -> dict[str, list["AtspiNode"]]:
        """Return AT-SPI relation targets through a read-only normalized view."""

        try:
            relation_set = self.raw.getRelationSet()
        except Exception:
            try:
                relation_set = self.raw.get_relation_set()
            except Exception:
                return {}
        result: dict[str, list[AtspiNode]] = {}
        for relation in relation_set or ():
            try:
                relation_type = (
                    relation.getRelationType()
                    if hasattr(relation, "getRelationType")
                    else relation.get_relation_type()
                )
                try:
                    relation_name = str(self.pyatspi.relationTypeToString(relation_type))
                except Exception:
                    relation_name = str(relation_type)
                count = (
                    relation.getNTargets()
                    if hasattr(relation, "getNTargets")
                    else relation.get_n_targets()
                )
                targets: list[AtspiNode] = []
                for index in range(int(count)):
                    target = (
                        relation.getTarget(index)
                        if hasattr(relation, "getTarget")
                        else relation.get_target(index)
                    )
                    if target is not None:
                        targets.append(AtspiNode(target, self.pyatspi))
                if targets:
                    result[normalize_role(relation_name)] = targets
            except Exception:
                continue
        return result


def related_nodes(node: Node) -> dict[str, list[Node]]:
    getter = getattr(node, "relations", None)
    if getter is None:
        return {}
    try:
        return getter()
    except Exception:
        return {}


def _safe_string(getter: Callable[[], Any]) -> str:
    try:
        return str(getter() or "").strip()
    except Exception:
        return ""


def walk(root: Node, *, max_depth: int = 12, include_root: bool = True) -> Iterator[tuple[Node, int]]:
    """Depth-first traversal with cycle protection for flaky AT-SPI providers."""

    stack: list[tuple[Node, int]] = [(root, 0)]
    seen: set[str] = set()
    while stack:
        node, depth = stack.pop()
        token = node.identity
        if token in seen:
            continue
        seen.add(token)
        if include_root or depth:
            yield node, depth
        if depth >= max_depth:
            continue
        try:
            children = node.children()
        except Exception:
            children = []
        stack.extend((child, depth + 1) for child in reversed(children))


def descendants(root: Node, *, max_depth: int = 5) -> list[Node]:
    return [node for node, depth in walk(root, max_depth=max_depth) if depth]


def attr_blob(node: Node) -> str:
    return " ".join(f"{key}={value}" for key, value in node.attributes.items()).lower()


def read_accessible_text(node: Node) -> str | None:
    """Read a Text interface without exposing any mutation operation.

    Rich mentions in Qt are represented by U+FFFC object-replacement
    characters. Reading that marker lets the sender verify that WeChat created
    a real mention token instead of ordinary ``@name`` text.
    """

    fake_value = getattr(node, "accessible_text", None)
    if fake_value is not None:
        return str(fake_value)
    raw = getattr(node, "raw", None)
    if raw is None:
        return None
    try:
        text = raw.queryText()
        return str(text.getText(0, -1))
    except Exception:
        return None


class AtspiBackend:
    """Read-only adapter around Linux AT-SPI.

    This class deliberately exposes no text mutation API. User input is performed
    by the separate X11 keyboard/mouse backend.
    """

    def __init__(self, *, app_names: Iterable[str] = ("wechat", "微信", "weixin")):
        self.app_names = tuple(value.casefold() for value in app_names)
        self._pyatspi: Any = None
        self._app: AtspiNode | None = None
        self.event_pump = _GLOBAL_EVENT_PUMP

    def connect(self) -> AtspiNode:
        try:
            import pyatspi  # type: ignore
        except ImportError as exc:
            raise AccessibilityUnavailable(
                "pyatspi is missing; install your distribution's python3-pyatspi package"
            ) from exc

        self._pyatspi = pyatspi
        self.event_pump.ensure_started(pyatspi)
        desktop = pyatspi.Registry.getDesktop(0)
        process_present = _wechat_process_present()
        for raw_app in desktop:
            name = _safe_string(lambda raw_app=raw_app: raw_app.name).casefold()
            if any(candidate in name for candidate in self.app_names):
                self._app = AtspiNode(raw_app, pyatspi)
                return self._app

        if process_present:
            raise AccessibilityUnavailable(
                "WeChat is running but has no AT-SPI tree. Enable GNOME toolkit "
                "accessibility, then fully restart WeChat with "
                "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1; run `bowxt doctor` for commands."
            )
        raise WeChatNotFound("the official Linux WeChat process is not running")

    def wait_for_event(self, timeout: float) -> bool:
        return self.event_pump.wait(timeout)

    @property
    def app(self) -> AtspiNode:
        if self._app is None:
            return self.connect()
        return self._app

    def windows(self) -> list[Node]:
        result = []
        for node in self.app.children():
            if node.role in {"frame", "window", "dialog"} or node.bounds:
                result.append(node)
        return result

    def main_window(self) -> Node:
        candidates = self.windows()
        if not candidates:
            raise AccessibilityUnavailable("WeChat published an empty accessibility application")
        candidates.sort(key=lambda n: _window_score(n), reverse=True)
        return candidates[0]


def _window_score(node: Node) -> tuple[int, int]:
    rect = node.bounds
    area = rect.width * rect.height if rect else 0
    title_score = 100 if node.name in {"微信", "WeChat", "wechat"} else 0
    return title_score + (20 if node.role == "frame" else 0), area


def _wechat_process_present() -> bool:
    proc = "/proc"
    try:
        entries = os.scandir(proc)
    except OSError:
        return False
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                with open(f"{proc}/{entry.name}/comm", encoding="utf-8") as process_name:
                    name = process_name.read().strip().casefold()
            except OSError:
                continue
            if name in {"wechat", "weixin"}:
                return True
    return False


def format_tree(root: Node, *, max_depth: int = 7, reveal_text: bool = False) -> str:
    """Create a diagnostic tree. Message-like text is redacted by default."""

    lines: list[str] = []
    for node, depth in walk(root, max_depth=max_depth):
        name = node.name
        if name and not reveal_text:
            name = f"<{len(name)} chars>"
        rect = node.bounds
        if reveal_text:
            attrs = attr_blob(node)
            attr_suffix = f" attrs={attrs[:140]!r}" if attrs else ""
        else:
            keys = sorted(node.attributes)
            attr_suffix = f" attr_keys={keys!r}" if keys else ""
        lines.append(f"{'  ' * depth}{node.role!r} name={name!r} bounds={rect!r}{attr_suffix}")
    return "\n".join(lines)


class _AtspiEventPump:
    """Register one lightweight listener so WeChat enables virtual lists.

    pyatspi's GLib registry loop is intentionally not started in a Python
    background thread: libatspi/dbind can abort the process when initialized
    off the main thread. MessageListener continues to poll conservatively.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._generation = 0
        self._started = False
        self._callback: Any = None

    def ensure_started(self, pyatspi: Any) -> None:
        with self._condition:
            if self._started:
                return
            self._started = True

        def on_change(_event: Any) -> None:
            with self._condition:
                self._generation += 1
                self._condition.notify_all()

        try:
            self._callback = on_change
            pyatspi.Registry.registerEventListener(on_change, "object:children-changed")
        except Exception:
            with self._condition:
                self._started = False
                self._callback = None

    def wait(self, timeout: float) -> bool:
        time.sleep(max(0.0, timeout))
        return False


_GLOBAL_EVENT_PUMP = _AtspiEventPump()

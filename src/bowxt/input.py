from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator, Protocol

from .errors import AccessibilityUnavailable, ControlNotFound
from .models import Rect


class InputBackend(Protocol):
    def focus_wechat(self) -> None: ...

    def click(self, x: int, y: int, *, count: int = 1) -> None: ...

    def shortcut(self, *keys: str) -> None: ...

    def press(self, key: str) -> None: ...


class ClipboardBackend(Protocol):
    @contextlib.contextmanager
    def text(self, value: str) -> Iterator[None]: ...


@dataclass(slots=True)
class WaylandClipboard:
    """Clipboard transport used only to feed real Ctrl+V key events."""

    restore: bool = True

    def __post_init__(self) -> None:
        if not shutil.which("wl-copy") or not shutil.which("wl-paste"):
            raise AccessibilityUnavailable("wl-copy/wl-paste are required (package: wl-clipboard)")

    def read(self) -> str | None:
        result = subprocess.run(
            ["wl-paste", "--no-newline", "--type", "text/plain"],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            return None
        return result.stdout.decode("utf-8", "replace")

    def write(self, value: str) -> None:
        subprocess.run(
            ["wl-copy", "--type", "text/plain;charset=utf-8"],
            input=value.encode("utf-8"),
            check=True,
            stdout=subprocess.DEVNULL,
            # wl-copy forks a child that owns the selection. A PIPE would stay
            # open in that child and make subprocess.run wait forever.
            stderr=subprocess.DEVNULL,
        )

    def clear(self) -> None:
        subprocess.run(["wl-copy", "--clear"], check=False, capture_output=True)

    @contextlib.contextmanager
    def text(self, value: str) -> Iterator[None]:
        previous = self.read() if self.restore else None
        self.write(value)
        try:
            yield
        finally:
            if self.restore:
                if previous is None:
                    self.clear()
                else:
                    self.write(previous)


@dataclass(slots=True)
class X11Clipboard:
    """Clipboard transport for pure X11/Xvfb desktops using ``xclip``."""

    restore: bool = True

    def __post_init__(self) -> None:
        if not os.environ.get("DISPLAY"):
            raise AccessibilityUnavailable("DISPLAY is unset; X11 clipboard is unavailable")
        if not shutil.which("xclip"):
            raise AccessibilityUnavailable("xclip is required for the X11 clipboard")

    def read(self) -> str | None:
        result = subprocess.run(
            ["xclip", "-selection", "clipboard", "-out"],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            return None
        return result.stdout.decode("utf-8", "replace")

    def write(self, value: str) -> None:
        subprocess.run(
            ["xclip", "-selection", "clipboard", "-in"],
            input=value.encode("utf-8"),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def clear(self) -> None:
        self.write("")

    @contextlib.contextmanager
    def text(self, value: str) -> Iterator[None]:
        previous = self.read() if self.restore else None
        self.write(value)
        try:
            yield
        finally:
            if self.restore and previous is not None:
                self.write(previous)
            elif self.restore:
                self.clear()


class X11Input:
    """Real XTest input for the official WeChat XWayland window.

    The backend has no process-memory or control-value APIs. It can only raise a
    visible X11 window and emit the same pointer/key events as a user device.
    """

    _KEY_NAMES = {
        "ctrl": "Control_L",
        "control": "Control_L",
        "shift": "Shift_L",
        "alt": "Alt_L",
        "enter": "Return",
        "return": "Return",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "backspace": "BackSpace",
        "delete": "Delete",
        "space": "space",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
    }

    _CLIENT_MESSAGE = 33
    _SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
    _SUBSTRUCTURE_REDIRECT_MASK = 1 << 20

    def __init__(self, *, display: str | None = None, event_delay: float = 0.06):
        if not (display or os.environ.get("DISPLAY")):
            raise AccessibilityUnavailable("DISPLAY is unset; WeChat must run through X/XWayland")
        x11_path = ctypes.util.find_library("X11")
        xtst_path = ctypes.util.find_library("Xtst")
        if not x11_path or not xtst_path:
            raise AccessibilityUnavailable("libX11 and libXtst are required")
        self.x11 = ctypes.CDLL(x11_path)
        self.xtst = ctypes.CDLL(xtst_path)
        self._configure_signatures()
        raw_display = (display or os.environ.get("DISPLAY", "")).encode()
        self.display = self.x11.XOpenDisplay(raw_display)
        if not self.display:
            raise AccessibilityUnavailable(f"cannot open X display {raw_display.decode()!r}")
        self.event_delay = event_delay
        # On XWayland, XTest accepts logical root coordinates while
        # XQueryPointer reports the scaled physical position. On a real X11
        # server (including Xvfb), both APIs use the same root coordinates.
        self._xwayland = bool(os.environ.get("WAYLAND_DISPLAY"))
        self._wechat_window: int | None = None
        self._accessible_window: Rect | None = None

    def _configure_signatures(self) -> None:
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.x11.XInternAtom.restype = ctypes.c_ulong
        self.x11.XGetWindowProperty.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_long,
            ctypes.c_long,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self.x11.XGetWindowProperty.restype = ctypes.c_int
        self.x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XQueryTree.restype = ctypes.c_int
        self.x11.XGetGeometry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XGetGeometry.restype = ctypes.c_int
        self.x11.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self.x11.XTranslateCoordinates.restype = ctypes.c_int
        self.x11.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.x11.XQueryPointer.restype = ctypes.c_int
        self.x11.XGetClassHint.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        self.x11.XGetClassHint.restype = ctypes.c_int
        self.x11.XFree.argtypes = [ctypes.c_void_p]
        self.x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XSendEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p
        ]
        self.x11.XSendEvent.restype = ctypes.c_int
        self.x11.XFlush.argtypes = [ctypes.c_void_p]
        self.x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        self.x11.XStringToKeysym.restype = ctypes.c_ulong
        self.x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.x11.XKeysymToKeycode.restype = ctypes.c_uint
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong
        ]
        self.xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]
        self.xtst.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong
        ]

    def close(self) -> None:
        display = getattr(self, "display", None)
        if display:
            self.x11.XCloseDisplay(display)
            self.display = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def find_wechat_window(self) -> int:
        if self._wechat_window:
            return self._wechat_window
        root = int(self.x11.XDefaultRootWindow(self.display))
        # Only EWMH top-level clients are safe activation targets. Querying the
        # raw X tree can return an internal Qt child with the same WM_CLASS.
        clients = self._window_list_property(root, "_NET_CLIENT_LIST")
        for window in clients:
            instance, class_name = self._class_hint(window)
            if instance.casefold() in {"wechat", "weixin"} or class_name.casefold() in {
                "wechat", "weixin"
            }:
                self._wechat_window = window
                return window
        raise ControlNotFound("the WeChat XWayland top-level window was not found")

    def configure_accessible_window(self, bounds: Rect) -> None:
        if bounds.width <= 0 or bounds.height <= 0:
            raise ControlNotFound("invalid AT-SPI WeChat window bounds")
        self._accessible_window = bounds

    def _children(self, window: int) -> list[int]:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        raw_children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        ok = self.x11.XQueryTree(
            self.display,
            window,
            ctypes.byref(root),
            ctypes.byref(parent),
            ctypes.byref(raw_children),
            ctypes.byref(count),
        )
        if not ok:
            return []
        try:
            return [int(raw_children[index]) for index in range(count.value)]
        finally:
            if raw_children:
                self.x11.XFree(raw_children)

    def _class_hint(self, window: int) -> tuple[str, str]:
        class XClassHint(ctypes.Structure):
            _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]

        hint = XClassHint()
        if not self.x11.XGetClassHint(self.display, window, ctypes.byref(hint)):
            return "", ""
        try:
            name = ctypes.string_at(hint.res_name).decode("utf-8", "replace") if hint.res_name else ""
            class_name = (
                ctypes.string_at(hint.res_class).decode("utf-8", "replace")
                if hint.res_class else ""
            )
            return name, class_name
        finally:
            if hint.res_name:
                self.x11.XFree(hint.res_name)
            if hint.res_class:
                self.x11.XFree(hint.res_class)

    def focus_wechat(self) -> None:
        window = self.find_wechat_window()
        self._request_activation(window)
        self.x11.XFlush(self.display)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.active_window() == window:
                return
            time.sleep(min(0.03, self.event_delay))
        raise ControlNotFound(
            "the window manager did not activate WeChat; refusing to emit input into another app"
        )

    def active_window(self) -> int | None:
        root = int(self.x11.XDefaultRootWindow(self.display))
        values = self._window_list_property(root, "_NET_ACTIVE_WINDOW", length=1)
        return values[0] if values else None

    def _window_list_property(self, window: int, name: str, *, length: int = 4096) -> list[int]:
        atom = self.x11.XInternAtom(self.display, name.encode("ascii"), 1)
        if not atom:
            return []
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        item_count = ctypes.c_ulong()
        remaining = ctypes.c_ulong()
        value = ctypes.POINTER(ctypes.c_ubyte)()
        status = self.x11.XGetWindowProperty(
            self.display,
            window,
            atom,
            0,
            length,
            0,
            0,
            ctypes.byref(actual_type),
            ctypes.byref(actual_format),
            ctypes.byref(item_count),
            ctypes.byref(remaining),
            ctypes.byref(value),
        )
        if status != 0 or not value or item_count.value < 1:
            if value:
                self.x11.XFree(value)
            return []
        try:
            values = ctypes.cast(value, ctypes.POINTER(ctypes.c_ulong))
            return [int(values[index]) for index in range(item_count.value)]
        finally:
            self.x11.XFree(value)

    def _request_activation(self, window: int) -> None:
        class ClientData(ctypes.Union):
            _fields_ = [
                ("bytes", ctypes.c_char * 20),
                ("shorts", ctypes.c_short * 10),
                ("longs", ctypes.c_long * 5),
            ]

        class ClientMessage(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_int),
                ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong),
                ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int),
                ("data", ClientData),
            ]

        class XEvent(ctypes.Union):
            _fields_ = [("client", ClientMessage), ("pad", ctypes.c_long * 24)]

        root = int(self.x11.XDefaultRootWindow(self.display))
        atom = self.x11.XInternAtom(self.display, b"_NET_ACTIVE_WINDOW", 0)
        event = XEvent()
        event.client.type = self._CLIENT_MESSAGE
        event.client.serial = 0
        event.client.send_event = 1
        event.client.display = self.display
        event.client.window = window
        event.client.message_type = atom
        event.client.format = 32
        event.client.data.longs[0] = 2  # source indication: pager/window switcher
        event.client.data.longs[1] = 0  # CurrentTime
        event.client.data.longs[2] = int(self.active_window() or 0)
        mask = self._SUBSTRUCTURE_NOTIFY_MASK | self._SUBSTRUCTURE_REDIRECT_MASK
        if not self.x11.XSendEvent(self.display, root, 0, mask, ctypes.byref(event)):
            raise ControlNotFound("failed to request WeChat activation from the window manager")

    def click(self, x: int, y: int, *, count: int = 1) -> None:
        self.focus_wechat()
        logical_x, logical_y = int(x), int(y)
        event_x, event_y = self._to_x11_point(logical_x, logical_y)
        expected_x, expected_y = self._expected_physical_point(logical_x, logical_y)
        # Mutter scales XTest's XWayland root coordinates to physical pixels;
        # XQueryPointer reports the resulting physical position. Keep the
        # event logical, then compare the readback with the independently
        # mapped physical point before any button event is emitted.
        self.xtst.XTestFakeMotionEvent(self.display, -1, event_x, event_y, 0)
        self.x11.XFlush(self.display)
        time.sleep(self.event_delay)
        pointer_x, pointer_y = self._pointer_position()
        if abs(pointer_x - expected_x) > 3 or abs(pointer_y - expected_y) > 3:
            raise ControlNotFound(
                "XWayland pointer scaling did not match AT-SPI geometry; click cancelled"
            )
        if self.active_window() != self.find_wechat_window():
            raise ControlNotFound("WeChat lost focus before click; input was cancelled")
        for _ in range(count):
            self.xtst.XTestFakeButtonEvent(self.display, 1, 1, 0)
            self.xtst.XTestFakeButtonEvent(self.display, 1, 0, 0)
            self.x11.XFlush(self.display)
            time.sleep(self.event_delay)
        if self.active_window() != self.find_wechat_window():
            raise ControlNotFound("click left the WeChat window; subsequent input was cancelled")

    def shortcut(self, *keys: str) -> None:
        if not keys:
            return
        self.focus_wechat()
        if self.active_window() != self.find_wechat_window():
            raise ControlNotFound("WeChat is not active; keyboard input was cancelled")
        codes = [self._keycode(key) for key in keys]
        for code in codes:
            self.xtst.XTestFakeKeyEvent(self.display, code, 1, 0)
        for code in reversed(codes):
            self.xtst.XTestFakeKeyEvent(self.display, code, 0, 0)
        self.x11.XFlush(self.display)
        time.sleep(self.event_delay)

    def press(self, key: str) -> None:
        self.shortcut(key)

    def _keycode(self, key: str) -> int:
        symbol = self._KEY_NAMES.get(key.casefold(), key)
        keysym = self.x11.XStringToKeysym(symbol.encode("ascii"))
        code = int(self.x11.XKeysymToKeycode(self.display, keysym))
        if not code:
            raise ValueError(f"unknown X11 key {key!r}")
        return code

    def _to_x11_point(self, x: int, y: int) -> tuple[int, int]:
        mapped = self._expected_physical_point(x, y)
        # Mutter performs logical-to-physical scaling for XWayland XTest
        # events. A real X11 server has no compositor translation, so emit the
        # mapped root coordinate ourselves.
        return (x, y) if getattr(self, "_xwayland", True) else mapped

    def _expected_physical_point(self, x: int, y: int) -> tuple[int, int]:
        logical = self._accessible_window
        if logical is None:
            raise ControlNotFound("AT-SPI/X11 coordinate mapping was not configured")
        if not (logical.x <= x < logical.right and logical.y <= y < logical.bottom):
            raise ControlNotFound("requested click is outside the AT-SPI WeChat window")
        physical = self._window_geometry(self.find_wechat_window())
        scale_x = physical.width / logical.width
        scale_y = physical.height / logical.height
        if not (0.5 <= scale_x <= 4.0 and 0.5 <= scale_y <= 4.0):
            raise ControlNotFound(
                f"implausible AT-SPI/X11 scale ({scale_x:.2f}, {scale_y:.2f}); click cancelled"
            )
        mapped_x = round(physical.x + (x - logical.x) * scale_x)
        mapped_y = round(physical.y + (y - logical.y) * scale_y)
        if not (physical.x <= mapped_x < physical.right and physical.y <= mapped_y < physical.bottom):
            raise ControlNotFound("mapped click is outside the X11 WeChat window")
        return mapped_x, mapped_y

    def _pointer_position(self) -> tuple[int, int]:
        root = int(self.x11.XDefaultRootWindow(self.display))
        root_return = ctypes.c_ulong()
        child_return = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        window_x = ctypes.c_int()
        window_y = ctypes.c_int()
        mask = ctypes.c_uint()
        if not self.x11.XQueryPointer(
            self.display,
            root,
            ctypes.byref(root_return),
            ctypes.byref(child_return),
            ctypes.byref(root_x),
            ctypes.byref(root_y),
            ctypes.byref(window_x),
            ctypes.byref(window_y),
            ctypes.byref(mask),
        ):
            raise ControlNotFound("could not verify the XWayland pointer position")
        return root_x.value, root_y.value

    def _window_geometry(self, window: int) -> Rect:
        root_return = ctypes.c_ulong()
        x = ctypes.c_int()
        y = ctypes.c_int()
        width = ctypes.c_uint()
        height = ctypes.c_uint()
        border = ctypes.c_uint()
        depth = ctypes.c_uint()
        if not self.x11.XGetGeometry(
            self.display,
            window,
            ctypes.byref(root_return),
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(width),
            ctypes.byref(height),
            ctypes.byref(border),
            ctypes.byref(depth),
        ):
            raise ControlNotFound("could not read WeChat X11 geometry")
        root = int(self.x11.XDefaultRootWindow(self.display))
        screen_x = ctypes.c_int()
        screen_y = ctypes.c_int()
        child = ctypes.c_ulong()
        if not self.x11.XTranslateCoordinates(
            self.display,
            window,
            root,
            0,
            0,
            ctypes.byref(screen_x),
            ctypes.byref(screen_y),
            ctypes.byref(child),
        ):
            raise ControlNotFound("could not translate WeChat X11 geometry to screen coordinates")
        return Rect(screen_x.value, screen_y.value, int(width.value), int(height.value))

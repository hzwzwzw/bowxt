from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from .accessibility import AtspiBackend, format_tree
from .client import WeChatClient
from .models import ChatType
from .selectors import DEFAULT_PROFILE, find_search_box
from .service import BowxtService, SyncMode
from .store import SQLiteStore


def _check_process() -> bool:
    result = subprocess.run(["pgrep", "-x", "wechat"], capture_output=True, check=False)
    return result.returncode == 0


def doctor(*, tree: bool = False, reveal_text: bool = False) -> int:
    rows: list[tuple[str, bool, str]] = []
    rows.append(("Linux", sys.platform.startswith("linux"), sys.platform))
    rows.append(("DISPLAY", bool(os.environ.get("DISPLAY")), os.environ.get("DISPLAY", "unset")))
    clipboard_tool = shutil.which("wl-copy") if os.environ.get("WAYLAND_DISPLAY") else shutil.which("xclip")
    rows.append(("Desktop clipboard", bool(clipboard_tool), clipboard_tool or "missing"))
    rows.append(("WeChat process", _check_process(), "running" if _check_process() else "not running"))
    setting = subprocess.run(
        ["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
        capture_output=True,
        text=True,
        check=False,
    )
    setting_value = setting.stdout.strip() if setting.returncode == 0 else "unknown"
    rows.append(("GNOME accessibility", setting_value == "true", setting_value))

    backend = AtspiBackend()
    try:
        backend.connect()
        root = backend.main_window()
        rows.append(("WeChat AT-SPI tree", True, f"{root.role}: {root.name!r}"))
        main_ui_ready = find_search_box(root, DEFAULT_PROFILE) is not None
        rows.append((
            "WeChat main UI",
            main_ui_ready,
            "ready" if main_ui_ready else "login/startup confirmation is still required",
        ))
    except Exception as exc:
        root = None
        rows.append(("WeChat AT-SPI tree", False, str(exc)))

    for label, ok, detail in rows:
        print(f"[{'OK' if ok else '!!'}] {label}: {detail}")
    if root and tree:
        print("\n" + format_tree(root, reveal_text=reveal_text))
    if not root:
        print(
            "\nRemediation (fully quit WeChat through its visible UI first):\n"
            "  gsettings set org.gnome.desktop.interface toolkit-accessibility true\n"
            "  QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 wechat\n"
            "Then run: bowxt doctor --tree"
        )
        return 1
    return 0 if all(ok for _label, ok, _detail in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bowxt")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="check accessibility and input prerequisites")
    doctor_parser.add_argument("--tree", action="store_true", help="show a redacted control tree")
    doctor_parser.add_argument(
        "--reveal-text", action="store_true", help="show control text (may expose private messages)"
    )
    read_parser = sub.add_parser("read", help="print currently visible messages as JSON Lines")
    read_parser.add_argument("chat")
    read_parser.add_argument("--type", choices=[item.value for item in ChatType], default="unknown")
    read_parser.add_argument(
        "--visual-direction", action="store_true",
        help="infer incoming/outgoing from in-memory window pixels (no OCR)",
    )
    read_parser.add_argument(
        "--uia-sender", action="store_true",
        help="read senders from avatar profile-card UIA with verified Escape cleanup",
    )
    send_parser = sub.add_parser("send", help="send one text message through visible UI")
    send_parser.add_argument("chat")
    send_parser.add_argument("text")
    send_parser.add_argument("--type", choices=[item.value for item in ChatType], default="unknown")
    send_parser.add_argument(
        "--mention", action="append", default=[], metavar="MEMBER",
        help="select one exact visible group member as a rich @ mention (repeatable)",
    )
    send_parser.add_argument("--yes", action="store_true", help="required acknowledgement")
    serve_parser = sub.add_parser("serve", help="run the persistent multi-chat web service")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8787)
    serve_parser.add_argument(
        "--db",
        default=os.environ.get("BOWXT_DB", "/home/wechat/.local/share/bowxt/messages.db"),
    )
    serve_parser.add_argument("--poll-gap", type=float, default=1.5)
    serve_parser.add_argument("--action-delay", type=float, default=0.12)
    serve_parser.add_argument(
        "--sync-mode",
        choices=[item.value for item in SyncMode],
        default=os.environ.get("BOWXT_SYNC_MODE", SyncMode.POLLING.value),
    )
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor(tree=args.tree, reveal_text=args.reveal_text)
    if args.command == "read":
        with WeChatClient(
            visual_direction=args.visual_direction,
            uia_sender=args.uia_sender,
        ) as client:
            for message in client.get_visible_messages(args.chat, chat_type=args.type):
                print(json.dumps(_json_message(message), ensure_ascii=False))
        return 0
    if args.command == "send":
        if not args.yes:
            parser.error("send requires --yes to prevent accidental messages")
        with WeChatClient() as client:
            receipt = client.send_text(
                args.chat,
                args.text,
                chat_type=args.type,
                mentions=args.mention,
            )
            print(json.dumps({
                "chat": receipt.chat,
                "content": receipt.content,
                "sent_at": receipt.sent_at.isoformat(),
                "verified": receipt.verified,
                "matched_message_id": receipt.matched_message_id,
                "mentions": list(receipt.mentions),
            }, ensure_ascii=False))
        return 0
    if args.command == "serve":
        from .web import serve

        service = BowxtService(
            SQLiteStore(args.db),
            poll_gap=args.poll_gap,
            action_delay=args.action_delay,
            sync_mode=args.sync_mode,
        )
        serve(service, host=args.host, port=args.port)
        return 0
    return 2


def _json_message(message) -> dict:
    return {
        "id": message.id,
        "chat": message.chat,
        "chat_type": message.chat_type.value,
        "sender": message.sender,
        "content": message.content,
        "type": message.type.value,
        "direction": message.direction.value,
        "timestamp": message.timestamp.isoformat() if message.timestamp else None,
        "is_at_me": message.is_at_me,
        "image": (
            {
                "mime_type": message.image.mime_type,
                "width": message.image.width,
                "height": message.image.height,
                "source": message.image.source,
                "bytes": len(message.image.data),
            }
            if message.image else None
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())

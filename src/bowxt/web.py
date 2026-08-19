from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .errors import ServicePaused
from .models import ChatType
from .service import BowxtService


class BowxtHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: BowxtService):
        super().__init__(address, BowxtRequestHandler)
        self.service = service
        self.static_root = Path(__file__).with_name("webui")


class BowxtRequestHandler(BaseHTTPRequestHandler):
    server: BowxtHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("BOWXT_HTTP_LOG") == "1":
            super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, self.server.service.status())
            return
        if parsed.path == "/api/chats":
            self._json(
                HTTPStatus.OK,
                {"chats": [chat.as_dict() for chat in self.server.service.store.list_chats()]},
            )
            return
        if parsed.path == "/api/messages":
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            if query.get("recent", ["0"])[0] == "1":
                messages = self.server.service.store.latest_messages(limit=limit)
            else:
                messages = self.server.service.store.messages_after(after, limit=limit)
            self._json(HTTPStatus.OK, {"messages": [item.as_dict() for item in messages]})
            return
        message_match = re.fullmatch(r"/api/messages/(\d+)", parsed.path)
        if message_match:
            try:
                message = self.server.service.store.get_message(int(message_match.group(1)))
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"message": message.as_dict()})
            return
        if parsed.path == "/api/agent/logs":
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            before_value = query.get("before", [None])[0]
            before = int(before_value) if before_value is not None else None
            limit = int(query.get("limit", ["200"])[0])
            recent = query.get("recent", ["0"])[0] == "1"
            logs = self.server.service.store.get_agent_logs(
                after_seq=after,
                before_seq=before,
                limit=limit,
                recent=recent,
            )
            self._json(HTTPStatus.OK, {"logs": [item.as_dict() for item in logs]})
            return
        image_match = re.fullmatch(r"/api/messages/(\d+)/image", parsed.path)
        if image_match:
            self._image(int(image_match.group(1)))
            return
        match = re.fullmatch(r"/api/chats/(\d+)/messages", parsed.path)
        if match:
            query = parse_qs(parsed.query)
            after = int(query.get("after", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            try:
                self.server.service.store.get_chat(int(match.group(1)))
                before_value = query.get("before", [None])[0]
                if before_value is not None:
                    messages = self.server.service.store.messages_before(
                        int(match.group(1)), int(before_value), limit=limit
                    )
                elif query.get("recent", ["0"])[0] == "1":
                    messages = self.server.service.store.recent_messages(
                        int(match.group(1)), limit=limit
                    )
                else:
                    messages = self.server.service.store.get_messages(
                        int(match.group(1)), after_seq=after, limit=limit
                    )
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"messages": [item.as_dict() for item in messages]})
            return
        if parsed.path == "/api/events":
            self._events()
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/chats":
                chat = self.server.service.add_chat(
                    str(body.get("name", "")),
                    ChatType(body.get("chat_type", "unknown")),
                )
                self._json(HTTPStatus.CREATED, {"chat": chat.as_dict()})
                return
            match = re.fullmatch(r"/api/chats/(\d+)/messages", parsed.path)
            if match:
                message = self.server.service.enqueue_text(
                    int(match.group(1)),
                    str(body.get("text", "")),
                    mentions=tuple(body.get("mentions") or ()),
                    client_id=body.get("client_id"),
                )
                self._json(HTTPStatus.ACCEPTED, {"message": message.as_dict()})
                return
            claim_match = re.fullmatch(r"/api/agents/([^/]+)/claim", parsed.path)
            if claim_match:
                chat_ids = body.get("chat_ids", [])
                if not isinstance(chat_ids, list):
                    raise ValueError("chat_ids must be an array")
                for flag in ("require_sender", "require_at_me", "replay_existing"):
                    if flag in body and not isinstance(body[flag], bool):
                        raise ValueError(f"{flag} must be a boolean")
                deliveries = self.server.service.claim_agent_messages(
                    unquote(claim_match.group(1)),
                    chat_ids=(int(item) for item in chat_ids),
                    limit=int(body.get("limit", 8)),
                    lease_seconds=float(body.get("lease_seconds", 60.0)),
                    timeout=float(body.get("timeout", 0.0)),
                    require_sender=bool(body.get("require_sender", False)),
                    require_at_me=bool(body.get("require_at_me", False)),
                    replay_existing=bool(body.get("replay_existing", False)),
                )
                self._json(
                    HTTPStatus.OK,
                    {"deliveries": [item.as_dict() for item in deliveries]},
                )
                return
            delivery_match = re.fullmatch(
                r"/api/agents/([^/]+)/deliveries/(\d+)/(ack|nack)", parsed.path
            )
            if delivery_match:
                consumer = unquote(delivery_match.group(1))
                message_seq = int(delivery_match.group(2))
                lease_token = str(body.get("lease_token", ""))
                if delivery_match.group(3) == "ack":
                    self.server.service.ack_agent_message(
                        consumer, message_seq, lease_token
                    )
                else:
                    self.server.service.nack_agent_message(
                        consumer,
                        message_seq,
                        lease_token,
                        error=str(body.get("error", "")),
                        retry_delay=float(body.get("retry_delay", 5.0)),
                    )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path == "/api/agent/logs":
                log = self.server.service.log_agent(
                    str(body.get("agent", "")),
                    str(body.get("level", "info")),
                    str(body.get("message", "")),
                    event=str(body.get("event", "log")),
                    context=body.get("context") or {},
                )
                self._json(HTTPStatus.CREATED, {"log": log.as_dict()})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except TimeoutError as exc:
            self._json(HTTPStatus.GATEWAY_TIMEOUT, {"error": str(exc)})
        except ServicePaused as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/control":
            try:
                body = self._read_json()
                unknown = set(body) - {"mode", "paused", "poll_gap", "action_delay"}
                if unknown or not body:
                    raise ValueError(
                        "control accepts only mode, paused, poll_gap and action_delay"
                    )
                status = self.server.service.configure(
                    paused=body.get("paused") if "paused" in body else None,
                    mode=body.get("mode") if "mode" in body else None,
                    poll_gap=body.get("poll_gap") if "poll_gap" in body else None,
                    action_delay=body.get("action_delay") if "action_delay" in body else None,
                )
                self._json(HTTPStatus.OK, status)
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        match = re.fullmatch(r"/api/chats/(\d+)", parsed.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown endpoint"})
            return
        try:
            body = self._read_json()
            chat = self.server.service.update_chat_type(
                int(match.group(1)), ChatType(body.get("chat_type", "unknown"))
            )
            self._json(HTTPStatus.OK, {"chat": chat.as_dict()})
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _read_json(self) -> dict[str, Any]:
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > 65536:
            raise ValueError("request body must be between 1 and 65536 bytes")
        value = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _events(self) -> None:
        subscriber = self.server.service.events.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while not self.server.service._stop.is_set():
                try:
                    event = subscriber.get(timeout=15.0)
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.service.events.unsubscribe(subscriber)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        if relative not in {"index.html", "app.js", "styles.css"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        path = self.server.static_root / relative
        try:
            data = path.read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "asset not found"})
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _image(self, seq: int) -> None:
        try:
            path, mime_type, digest = self.server.service.store.image_asset(seq)
            data = path.read_bytes()
        except (KeyError, FileNotFoundError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "image not found"})
            return
        except (OSError, ValueError) as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("ETag", f'"{digest}"')
        self.send_header("Content-Disposition", f'inline; filename="{digest}.png"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


def serve(service: BowxtService, host: str = "127.0.0.1", port: int = 8787) -> None:
    service.start()
    server = BowxtHTTPServer((host, int(port)), service)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()

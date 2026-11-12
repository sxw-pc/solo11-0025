"""Read-only local HTTP viewer. Binds to loopback and exposes no write API."""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import CATEGORIES, DEFAULT_DB, DEFAULT_HOST, DEFAULT_PORT, SERVER_NAME, STATIC_DIR, safe_batch_component
from .report import render_json
from .store import Store

LOGGER = logging.getLogger("koujing.server")
LOCAL_NAMES = {"127.0.0.1", "localhost", "::1"}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class AuditLog:
    def __init__(self, limit: int = 200):
        self._items: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self.limit = limit

    def record(self, kind: str, detail: str, client: str = "") -> None:
        entry = {"kind": kind, "detail": detail[:300], "client": client}
        with self._lock:
            self._items.append(entry)
            if len(self._items) > self.limit:
                del self._items[: len(self._items) - self.limit]
        LOGGER.warning("rejected %s from %s: %s", kind, client, detail[:300])

    def entries(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(item) for item in self._items]


class KoujingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db_path: str | Path = DEFAULT_DB):
        self.store = Store(db_path)
        self.audit = AuditLog()
        super().__init__(address, KoujingRequestHandler)

    @property
    def audit_entries(self) -> list[dict[str, str]]:
        return self.audit.entries()


class KoujingRequestHandler(BaseHTTPRequestHandler):
    server_version = SERVER_NAME
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        self._reject_method("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_method("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_method("DELETE")

    def _reject_method(self, method: str) -> None:
        self.server.audit.record("method", method, self.client_address[0])
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only", "method": method},
                        head_only=False, extra_headers={"Allow": "GET, HEAD"})

    def _host_is_local(self) -> bool:
        host = self.headers.get("Host", "").strip()
        if not host:
            return False
        try:
            hostname = urlsplit("//" + host).hostname
        except ValueError:
            return False
        return hostname in LOCAL_NAMES

    def _unsafe_path(self, raw_path: str, decoded: str) -> bool:
        if "\x00" in decoded or "\\" in decoded:
            return True
        if ".." in raw_path or ".." in decoded:
            return True
        return not decoded.startswith("/")

    def _handle(self, *, head_only: bool) -> None:
        if not self._host_is_local():
            self.server.audit.record("host", self.headers.get("Host", ""), self.client_address[0])
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_host"}, head_only=head_only)
            return
        raw_path = self.path.split("?", 1)[0]
        decoded = unquote(raw_path)
        if self._unsafe_path(raw_path, decoded):
            self.server.audit.record("path", decoded, self.client_address[0])
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden_path"}, head_only=head_only)
            return
        try:
            self._route(decoded, head_only=head_only)
        except (ValueError, IndexError):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, head_only=head_only)
        except Exception:
            LOGGER.exception("viewer request failed")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": "internal_error"}, head_only=head_only)

    def _route(self, path: str, *, head_only: bool) -> None:
        if path in STATIC_FILES:
            filename, content_type = STATIC_FILES[path]
            self._send_file(STATIC_DIR / filename, content_type, head_only=head_only)
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "read_only": True}, head_only=head_only)
            return
        if path == "/api/batches":
            self._send_json(HTTPStatus.OK, {"batches": self.server.store.list_batches(50)},
                            head_only=head_only)
            return
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3 or parts[:2] != ["api", "batches"]:
            raise ValueError("unknown path")
        batch_id = safe_batch_component(parts[2])
        batch = self.server.store.get_batch(batch_id)
        if batch is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "batch_not_found"}, head_only=head_only)
            return
        findings = self.server.store.get_findings(batch_id)
        if len(parts) == 3:
            groups = {name: [item for item in findings if item["category"] == name]
                      for name in CATEGORIES}
            self._send_json(HTTPStatus.OK, {
                "batch": batch, "summary": batch["counts"], "groups": groups, "findings": findings,
            }, head_only=head_only)
            return
        if len(parts) == 4 and parts[3] == "report.json":
            body = render_json(batch, findings).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, body, "application/json; charset=utf-8",
                             head_only=head_only, download=f"koujing-{batch_id}-report.json")
            return
        if len(parts) == 5 and parts[3] == "findings":
            number = int(parts[4])
            if number < 1 or number > len(findings):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "finding_not_found"},
                                head_only=head_only)
                return
            self._send_json(HTTPStatus.OK, {"finding": findings[number - 1]}, head_only=head_only)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, head_only=head_only)

    def _security_headers(self) -> dict[str, str]:
        return {
            "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                       "script-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        }

    def _send_json(self, status: HTTPStatus, payload: dict, *, head_only: bool,
                   extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8",
                         head_only=head_only, extra_headers=extra_headers)

    def _send_file(self, path: Path, content_type: str, *, head_only: bool) -> None:
        if not path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "asset_missing"}, head_only=head_only)
            return
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type, head_only=head_only)

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str, *,
                    head_only: bool, extra_headers: dict[str, str] | None = None,
                    download: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in self._security_headers().items():
            self.send_header(key, value)
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  db_path: str | Path = DEFAULT_DB) -> KoujingHTTPServer:
    if host not in LOCAL_NAMES:
        raise ValueError("the viewer may only bind to a loopback host")
    return KoujingHTTPServer((host, int(port)), db_path=db_path)


def serve_forever(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  db_path: str | Path = DEFAULT_DB) -> None:
    server = create_server(host=host, port=port, db_path=db_path)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()

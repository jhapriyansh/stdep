from __future__ import annotations

import hashlib
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .store import Store


class StaticDeployHandler(BaseHTTPRequestHandler):
    server_version = "StaticDeploy/0.1"
    store: Store

    def do_GET(self) -> None:  # noqa: N802
        self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def _serve(self, head_only: bool) -> None:
        resolved = self.store.resolve_host(self.headers.get("Host", ""))
        if resolved is None:
            self._error(HTTPStatus.NOT_FOUND, "Unknown host or no active deploy")
            return
        _slug, root = resolved
        try:
            target = self._safe_target(root)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request path")
            return
        if target.is_dir():
            target /= "index.html"
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return

        payload = target.read_bytes()
        etag = '"' + hashlib.sha256(payload).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", self._cache_control(target))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.end_headers()
        if not head_only:
            self.wfile.write(payload)

    def _safe_target(self, root: Path) -> Path:
        raw_path = unquote(urlsplit(self.path).path)
        relative = raw_path.lstrip("/") or "index.html"
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("path escapes deploy root")
        return candidate

    @staticmethod
    def _cache_control(target: Path) -> str:
        if target.suffix.lower() in {".html", ".htm"}:
            return "no-cache"
        name_parts = target.name.split(".")
        is_fingerprinted = len(name_parts) >= 3 and len(name_parts[-2]) >= 6 and all(c in "0123456789abcdef" for c in name_parts[-2].lower())
        return "public, max-age=31536000, immutable" if is_fingerprinted else "public, max-age=300"

    def _error(self, status: HTTPStatus, message: str) -> None:
        payload = f"{status.value} {status.phrase}: {message}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(store: Store, host: str, port: int) -> None:
    handler = type("ConfiguredStaticDeployHandler", (StaticDeployHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"StaticDeploy edge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping edge server")
    finally:
        server.server_close()


from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import mimetypes
from collections import Counter
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlsplit

from .store import Store, StoreError


@dataclass
class Metrics:
    requests: Counter[tuple[str, int]] = field(default_factory=Counter)
    lock: Lock = field(default_factory=Lock)

    def record(self, route: str, status: int) -> None:
        with self.lock:
            self.requests[(route, status)] += 1

    def render(self) -> bytes:
        with self.lock:
            lines = ["# HELP staticdeploy_http_requests_total Requests served by the edge.", "# TYPE staticdeploy_http_requests_total counter"]
            lines.extend(f'staticdeploy_http_requests_total{{route="{route}",status="{status}"}} {count}' for (route, status), count in sorted(self.requests.items()))
        return ("\n".join(lines) + "\n").encode()


@dataclass(frozen=True)
class PlatformConfig:
    store: Store
    admin_token: str | None
    preview_suffix: str | None
    routing_mode: str
    metrics: Metrics


class StaticDeployHandler(BaseHTTPRequestHandler):
    server_version = "StaticDeploy/0.2"
    config: PlatformConfig

    def do_GET(self) -> None:  # noqa: N802
        if not self._platform_get():
            self._serve(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/_platform/webhooks/gitea/"):
            self._gitea_webhook(path.rsplit("/", 1)[-1])
        elif path.startswith("/_platform/webhooks/github/"):
            self._github_webhook(path.rsplit("/", 1)[-1])
        elif path.endswith("/rollback") and path.startswith("/_platform/api/sites/"):
            self._rollback(path.split("/")[4])
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, "platform")

    def _platform_get(self) -> bool:
        parsed = urlsplit(self.path)
        if parsed.path == "/_platform/health":
            self._json(HTTPStatus.OK, {"status": "ok"}, "health")
        elif parsed.path == "/_platform/metrics":
            self._metrics()
        elif parsed.path == "/_platform/tls/allow":
            domain = parse_qs(parsed.query).get("domain", [""])[0]
            self._empty(HTTPStatus.OK if self.config.store.is_known_domain(domain) else HTTPStatus.FORBIDDEN, "tls_allow")
        elif parsed.path == "/_platform/api/sites":
            if not self._authorized():
                return True
            sites = [{"slug": slug, "current_deploy_id": current, "domains": domains} for slug, current, domains in self.config.store.list_sites()]
            self._json(HTTPStatus.OK, {"sites": sites}, "api_sites")
        elif parsed.path == "/_platform/api/jobs":
            if not self._authorized():
                return True
            self._json(HTTPStatus.OK, {"jobs": [job.__dict__ for job in self.config.store.list_jobs()]}, "api_jobs")
        elif parsed.path == "/_platform/dashboard":
            if not self._authorized():
                return True
            self._dashboard()
        else:
            return False
        return True

    def _serve(self, head_only: bool) -> None:
        resolved = self._resolve_content()
        if resolved is None:
            message = "Unknown path route or no active deploy" if self.config.routing_mode == "path" else "Unknown host or no active deploy"
            self._error(HTTPStatus.NOT_FOUND, message, "static")
            return
        root, request_path = resolved
        try:
            target = self._safe_target(root, request_path)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Invalid request path", "static")
            return
        if target.is_dir():
            target /= "index.html"
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found", "static")
            return
        payload = target.read_bytes()
        etag = 'W/"' + hashlib.sha256(payload).hexdigest()[:16] + '"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            self.config.metrics.record("static", HTTPStatus.NOT_MODIFIED)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        response = self._gzip_if_requested(payload, content_type)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(response)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", self._cache_control(target))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        if response is not payload:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.end_headers()
        if not head_only:
            self.wfile.write(response)
        self.config.metrics.record("static", HTTPStatus.OK)

    def _resolve_content(self) -> tuple[Path, str] | None:
        if self.config.routing_mode == "path":
            return self._resolve_path_route()
        root = self._resolve_site_root()
        return (root, urlsplit(self.path).path) if root is not None else None

    def _resolve_site_root(self) -> Path | None:
        host = self.headers.get("Host", "").split(":", 1)[0].lower().rstrip(".")
        resolved = self.config.store.resolve_host(host)
        if resolved is not None:
            return resolved[1]
        if self.config.preview_suffix and host.endswith("." + self.config.preview_suffix):
            label = host.removesuffix("." + self.config.preview_suffix)
            if "--" in label:
                preview, site = label.rsplit("--", 1)
                return self.config.store.resolve_preview(preview, site)
        return None

    def _resolve_path_route(self) -> tuple[Path, str] | None:
        parts = [part for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "sites":
            root = self.config.store.resolve_site(parts[1])
            request_path = "/" + "/".join(parts[2:])
            return (root, request_path) if root is not None else None
        if len(parts) >= 3 and parts[0] == "previews":
            root = self.config.store.resolve_preview(parts[2], parts[1])
            request_path = "/" + "/".join(parts[3:])
            return (root, request_path) if root is not None else None
        return None

    def _gitea_webhook(self, slug: str) -> None:
        try:
            secret = self.config.store.site_settings(slug)["webhook_secret"]
        except StoreError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown site"}, "webhook")
            return
        body = self._body()
        expected = hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest() if secret else ""
        if not secret or not hmac.compare_digest(self.headers.get("X-Gitea-Signature", ""), expected):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid webhook signature"}, "webhook")
            return
        if self.headers.get("X-Gitea-Event") != "push":
            self._json(HTTPStatus.ACCEPTED, {"message": "ignored non-push event"}, "webhook")
            return
        try:
            payload = json.loads(body)
            ref = str(payload["ref"])
            if not ref.startswith("refs/heads/"):
                raise ValueError("only branch push events are deployable")
            job = self.config.store.queue_deploy(slug, ref.removeprefix("refs/heads/"), str(payload["after"]))
        except (KeyError, ValueError, json.JSONDecodeError, StoreError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}, "webhook")
            return
        self._json(HTTPStatus.ACCEPTED, {"job_id": job.id, "status": job.status}, "webhook")

    def _github_webhook(self, slug: str) -> None:
        try:
            secret = self.config.store.site_settings(slug)["webhook_secret"]
        except StoreError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown site"}, "webhook")
            return
        body = self._body()
        digest = hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest() if secret else ""
        expected = "sha256=" + digest
        if not secret or not hmac.compare_digest(self.headers.get("X-Hub-Signature-256", ""), expected):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid webhook signature"}, "webhook")
            return
        if self.headers.get("X-GitHub-Event") != "push":
            self._json(HTTPStatus.ACCEPTED, {"message": "ignored non-push event"}, "webhook")
            return
        try:
            payload = json.loads(body)
            ref = str(payload["ref"])
            if not ref.startswith("refs/heads/"):
                raise ValueError("only branch push events are deployable")
            job = self.config.store.queue_deploy(slug, ref.removeprefix("refs/heads/"), str(payload["after"]))
        except (KeyError, ValueError, json.JSONDecodeError, StoreError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}, "webhook")
            return
        self._json(HTTPStatus.ACCEPTED, {"job_id": job.id, "status": job.status}, "webhook")

    def _rollback(self, slug: str) -> None:
        if not self._authorized():
            return
        try:
            self.config.store.rollback(slug, str(json.loads(self._body())["deploy_id"]))
        except (KeyError, ValueError, json.JSONDecodeError, StoreError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)}, "rollback")
            return
        self._json(HTTPStatus.OK, {"status": "rolled back"}, "rollback")

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if self.config.admin_token and hmac.compare_digest(given, self.config.admin_token):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "admin token required"}, "auth")
        return False

    def _dashboard(self) -> None:
        rows = "".join(f"<tr><td>{slug}</td><td>{','.join(domains)}</td><td>{current or '—'}</td></tr>" for slug, current, domains in self.config.store.list_sites())
        body = ("<!doctype html><title>StaticDeploy</title><style>body{font:16px system-ui;max-width:960px;margin:3rem auto}td,th{padding:.55rem;border-bottom:1px solid #ddd;text-align:left}</style><h1>StaticDeploy</h1><table><thead><tr><th>Site</th><th>Domains</th><th>Active deploy</th></tr></thead><tbody>" + rows + "</tbody></table>").encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.config.metrics.record("dashboard", HTTPStatus.OK)

    def _metrics(self) -> None:
        body = self.config.metrics.render()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.config.metrics.record("metrics", HTTPStatus.OK)

    def _safe_target(self, root: Path, request_path: str | None = None) -> Path:
        path = request_path if request_path is not None else urlsplit(self.path).path
        candidate = (root / (unquote(path).lstrip("/") or "index.html")).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("path escapes deploy root")
        return candidate

    @staticmethod
    def _cache_control(target: Path) -> str:
        if target.suffix.lower() in {".html", ".htm"}:
            return "no-cache"
        parts = target.name.split(".")
        fingerprinted = len(parts) >= 3 and len(parts[-2]) >= 6 and all(c in "0123456789abcdef" for c in parts[-2].lower())
        return "public, max-age=31536000, immutable" if fingerprinted else "public, max-age=300"

    def _gzip_if_requested(self, payload: bytes, content_type: str) -> bytes:
        can_compress = content_type.startswith("text/") or content_type in {"application/javascript", "application/json", "image/svg+xml"}
        return gzip.compress(payload) if "gzip" in self.headers.get("Accept-Encoding", "").lower() and can_compress and len(payload) > 256 else payload

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("request body is too large")
        return self.rfile.read(length)

    def _json(self, status: HTTPStatus, payload: dict[str, object], route: str) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        self.config.metrics.record(route, status)

    def _empty(self, status: HTTPStatus, route: str) -> None:
        self.send_response(status)
        self.end_headers()
        self.config.metrics.record(route, status)

    def _error(self, status: HTTPStatus, message: str, route: str) -> None:
        payload = f"{status.value} {status.phrase}: {message}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.config.metrics.record(route, status)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(store: Store, host: str, port: int, *, admin_token: str | None, preview_suffix: str | None, routing_mode: str) -> None:
    handler = type("ConfiguredStaticDeployHandler", (StaticDeployHandler,), {"config": PlatformConfig(store, admin_token, preview_suffix, routing_mode, Metrics())})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"StaticDeploy edge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping edge server")
    finally:
        server.server_close()

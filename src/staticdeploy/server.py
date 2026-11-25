from __future__ import annotations

import gzip
import hashlib
import hmac
import html
import json
import mimetypes
from collections import Counter
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .store import Store, StoreError, User


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
    secure_cookies: bool
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
        if path == "/_platform/setup":
            self._setup_admin()
        elif path == "/_platform/login":
            self._login()
        elif path == "/_platform/signup":
            self._signup()
        elif path == "/_platform/logout":
            self._logout()
        elif path == "/_platform/sites":
            self._create_site_from_dashboard()
        elif path.startswith("/_platform/webhooks/gitea/"):
            self._gitea_webhook(path.rsplit("/", 1)[-1])
        elif path.startswith("/_platform/webhooks/github/"):
            self._github_webhook(path.rsplit("/", 1)[-1])
        elif path.endswith("/rollback") and path.startswith("/_platform/api/sites/"):
            self._rollback(path.split("/")[4])
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"}, "platform")

    def _platform_get(self) -> bool:
        parsed = urlsplit(self.path)
        if parsed.path == "/_platform":
            if not self.config.store.has_users():
                self._redirect("/_platform/setup")
            elif self._current_user():
                self._redirect("/_platform/dashboard")
            else:
                self._redirect("/_platform/login")
        elif parsed.path == "/_platform/setup":
            if self.config.store.has_users():
                self._redirect("/_platform/login")
            else:
                self._account_page("FIRST PLAYER // ADMIN BOOT", "Create the first administrator account. Player onboarding stays locked until this account exists.", "setup")
        elif parsed.path == "/_platform/login":
            if not self.config.store.has_users():
                self._redirect("/_platform/setup")
            else:
                self._account_page("PLAYER LOGIN", "Enter the control room.", "login")
        elif parsed.path == "/_platform/signup":
            if not self.config.store.has_users():
                self._redirect("/_platform/setup")
            else:
                self._account_page("NEW PLAYER", "Create a builder account and deploy your first static world.", "signup")
        elif parsed.path == "/_platform/dashboard":
            user = self._current_user()
            if not user:
                self._redirect("/_platform/login")
            else:
                self._dashboard(user)
        elif parsed.path == "/_platform/admin":
            user = self._current_user()
            if not user:
                self._redirect("/_platform/login")
            elif user.role != "admin":
                self._page(HTTPStatus.FORBIDDEN, "ACCESS DENIED", "Admin clearance required.")
            else:
                self._admin_dashboard(user)
        elif parsed.path == "/_platform/health":
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

    def _setup_admin(self) -> None:
        if self.config.store.has_users():
            self._redirect("/_platform/login")
            return
        fields = self._form()
        try:
            user = self.config.store.create_first_admin(fields.get("username", ""), fields.get("password", ""))
        except StoreError as error:
            self._account_page("FIRST PLAYER // ADMIN BOOT", str(error), "setup", error=True)
            return
        self._start_session(user, "/_platform/admin")

    def _login(self) -> None:
        fields = self._form()
        user = self.config.store.authenticate(fields.get("username", ""), fields.get("password", ""))
        if not user:
            self._account_page("PLAYER LOGIN", "Invalid username or password.", "login", error=True)
            return
        self._start_session(user, "/_platform/admin" if user.role == "admin" else "/_platform/dashboard")

    def _signup(self) -> None:
        if not self.config.store.has_users():
            self._redirect("/_platform/setup")
            return
        fields = self._form()
        try:
            user = self.config.store.create_user(fields.get("username", ""), fields.get("password", ""))
        except StoreError as error:
            self._account_page("NEW PLAYER", str(error), "signup", error=True)
            return
        self._start_session(user, "/_platform/dashboard")

    def _logout(self) -> None:
        self.config.store.delete_session(self._session_token())
        self._redirect("/_platform/login", clear_session=True)

    def _create_site_from_dashboard(self) -> None:
        user = self._current_user()
        if not user:
            self._redirect("/_platform/login")
            return
        fields = self._form()
        slug = fields.get("slug", "")
        domain = fields.get("domain", "") or f"{slug}.local"
        repository_url = fields.get("repository_url") or None
        try:
            secret = self.config.store.create_site(slug, [domain], repository_url=repository_url, owner_user_id=user.id)
        except StoreError as error:
            self._dashboard(user, error=str(error))
            return
        message = f"Site {slug} created."
        if secret:
            message += f" Webhook secret: {secret}"
        self._dashboard(user, message=message)

    def _form(self) -> dict[str, str]:
        try:
            decoded = self._body().decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return {}
        return {key: values[0] for key, values in parse_qs(decoded, keep_blank_values=True).items()}

    def _session_token(self) -> str | None:
        try:
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            return cookies.get("staticdeploy_session").value if cookies.get("staticdeploy_session") else None
        except (KeyError, ValueError):
            return None

    def _current_user(self) -> User | None:
        return self.config.store.session_user(self._session_token())

    def _start_session(self, user: User, target: str) -> None:
        token = self.config.store.create_session(user)
        self._redirect(target, session_token=token)

    def _account_page(self, heading: str, message: str, mode: str, error: bool = False) -> None:
        action = f"/_platform/{mode}"
        button = "CREATE ADMIN" if mode == "setup" else "ENTER ARCADE" if mode == "login" else "CREATE PLAYER"
        alternate = "<a href=\"/_platform/login\">Already registered? Login</a>" if mode == "signup" else "<a href=\"/_platform/signup\">Need an account? Join the arcade</a>" if mode == "login" else ""
        tone = "error" if error else "hint"
        content = f"""
        <section class=\"auth-panel\"><p class=\"eyebrow\">STATICDEPLOY // PLAYER ONE</p><h1>{html.escape(heading)}</h1>
        <p class=\"{tone}\">{html.escape(message)}</p><form method=\"post\" action=\"{action}\">
        <label>HANDLE<input required minlength=\"3\" maxlength=\"32\" name=\"username\" autocomplete=\"username\" placeholder=\"pixel-pilot\"></label>
        <label>PASSPHRASE<input required minlength=\"12\" type=\"password\" name=\"password\" autocomplete=\"new-password\" placeholder=\"12+ characters\"></label>
        <button type=\"submit\">{button}</button></form>{alternate}</section>"""
        self._page(HTTPStatus.OK, heading, content, raw=True)

    def _dashboard(self, user: User, message: str | None = None, error: str | None = None) -> None:
        sites = self.config.store.list_sites(None if user.role == "admin" else user.id)
        cards = "".join(
            f"<article class=\"site-card\"><span class=\"badge\">{html.escape(slug)}</span><h3>{html.escape(', '.join(domains))}</h3><p>ACTIVE: {html.escape(current or 'NO DEPLOY')}</p><a href=\"/sites/{quote(slug)}/\">OPEN PATH MODE ↗</a></article>"
            for slug, current, domains in sites
        ) or "<article class=\"site-card empty\"><h3>NO WORLDS YET</h3><p>Claim a slug below to create your first static site.</p></article>"
        notice = f"<p class=\"error\">{html.escape(error)}</p>" if error else f"<p class=\"success\">{html.escape(message)}</p>" if message else ""
        admin_link = "<a class=\"nav-link\" href=\"/_platform/admin\">ADMIN DECK</a>" if user.role == "admin" else ""
        content = f"""
        <header class=\"topbar\"><div><p class=\"eyebrow\">PLAYER // {html.escape(user.username)}</p><h1>MY SITE CARTRIDGES</h1></div><nav>{admin_link}<form method=\"post\" action=\"/_platform/logout\"><button class=\"ghost\">LOG OUT</button></form></nav></header>
        {notice}<section class=\"catalog\">{cards}</section>
        <section class=\"create-panel\"><p class=\"eyebrow\">INSERT NEW CARTRIDGE</p><h2>CREATE A STATIC SITE</h2><form method=\"post\" action=\"/_platform/sites\"><label>SITE SLUG<input required name=\"slug\" pattern=\"[a-z0-9-]+\" placeholder=\"my-portfolio\"></label><label>DOMAIN <small>optional in path mode</small><input name=\"domain\" placeholder=\"my-site.example.com\"></label><label>GIT REPOSITORY <small>optional</small><input name=\"repository_url\" placeholder=\"https://github.com/you/site.git\"></label><button type=\"submit\">CREATE SITE</button></form></section>"""
        self._page(HTTPStatus.OK, "My sites", content, raw=True)

    def _admin_dashboard(self, user: User) -> None:
        users = self.config.store.list_users()
        sites = self.config.store.list_sites()
        user_rows = "".join(f"<tr><td>{html.escape(entry.username)}</td><td>{html.escape(entry.role)}</td><td>{html.escape(entry.created_at[:10])}</td></tr>" for entry in users)
        site_rows = "".join(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(', '.join(domains))}</td><td>{html.escape(current or '—')}</td></tr>" for slug, current, domains in sites)
        content = f"""
        <header class=\"topbar\"><div><p class=\"eyebrow\">ADMIN // {html.escape(user.username)}</p><h1>CONTROL ROOM</h1></div><nav><a class=\"nav-link\" href=\"/_platform/dashboard\">SITE DECK</a><form method=\"post\" action=\"/_platform/logout\"><button class=\"ghost\">LOG OUT</button></form></nav></header>
        <section class=\"scoreboard\"><article><b>{len(users)}</b><span>PLAYERS</span></article><article><b>{len(sites)}</b><span>SITES</span></article><article><b>{sum(1 for _, active, _ in sites if active)}</b><span>LIVE</span></article></section>
        <section class=\"table-panel\"><h2>PLAYER ROSTER</h2><table><thead><tr><th>HANDLE</th><th>ROLE</th><th>JOINED</th></tr></thead><tbody>{user_rows}</tbody></table></section>
        <section class=\"table-panel\"><h2>WORLD INDEX</h2><table><thead><tr><th>SITE</th><th>DOMAIN</th><th>ACTIVE DEPLOY</th></tr></thead><tbody>{site_rows}</tbody></table></section>"""
        self._page(HTTPStatus.OK, "Admin control room", content, raw=True)

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

    def _metrics(self) -> None:
        body = self.config.metrics.render()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.config.metrics.record("metrics", HTTPStatus.OK)

    def _page(self, status: HTTPStatus, title: str, content: str, *, raw: bool = False) -> None:
        body_content = content if raw else f"<main class=\"single\"><h1>{html.escape(title)}</h1><p>{html.escape(content)}</p></main>"
        document = f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{html.escape(title)} · StaticDeploy</title><style>
        :root{{--ink:#131119;--acid:#ccff00;--pink:#ff4fd8;--blue:#1ae6ff;--paper:#f4eddf}}*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(130deg,#111020 0 35%,#21142a 35% 65%,#10252a 65%);color:var(--paper);font:ui-monospace,SFMono-Regular,Menlo,monospace;min-height:100vh;padding:28px}}body:before{{content:'STATICDEPLOY // ARCADE HOSTING';position:fixed;inset:0 auto auto 0;background:var(--acid);color:var(--ink);font-weight:900;padding:7px 12px;letter-spacing:.08em;z-index:2}}a{{color:var(--blue);font-weight:800}}h1,h2,h3{{letter-spacing:-.06em;margin:.15em 0;text-transform:uppercase}}h1{{font-size:clamp(2.2rem,7vw,5.5rem);line-height:.87;text-shadow:5px 5px 0 var(--pink)}}h2{{font-size:1.7rem}}.auth-panel,.create-panel,.table-panel,.site-card,.scoreboard article{{border:3px solid var(--ink);box-shadow:7px 7px 0 var(--acid);background:var(--paper);color:var(--ink)}}.auth-panel{{width:min(560px,100%);margin:14vh auto;padding:28px}}label{{display:grid;gap:7px;font-weight:900;margin:15px 0}}input{{border:3px solid var(--ink);background:#fff;padding:13px;font:inherit;color:var(--ink)}}button{{cursor:pointer;border:3px solid var(--ink);box-shadow:4px 4px 0 var(--pink);background:var(--acid);color:var(--ink);padding:12px 16px;font:900 1rem ui-monospace,monospace}}button:hover{{transform:translate(2px,2px);box-shadow:2px 2px 0 var(--pink)}}button.ghost{{background:var(--paper)}}.topbar{{display:flex;justify-content:space-between;align-items:end;gap:20px;margin:8vh auto 30px;max-width:1160px}}.topbar nav{{display:flex;gap:12px;align-items:center}}.nav-link{{background:var(--blue);color:var(--ink);border:3px solid var(--ink);padding:11px;font-weight:900}}.catalog,.scoreboard{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:22px;max-width:1160px;margin:0 auto 34px}}.site-card{{padding:18px;min-height:190px;transform:rotate(-.5deg)}}.site-card:nth-child(even){{transform:rotate(.7deg);box-shadow:7px 7px 0 var(--pink)}}.site-card p{{font-size:.77rem;word-break:break-all}}.badge{{display:inline-block;background:var(--ink);color:var(--acid);padding:4px 7px;font-weight:900}}.create-panel,.table-panel{{max-width:1160px;margin:28px auto;padding:22px}}.create-panel form{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;align-items:end}}.create-panel form label{{margin:0}}.scoreboard article{{padding:19px}}.scoreboard b{{display:block;font-size:3rem;color:var(--pink)}}table{{width:100%;border-collapse:collapse}}td,th{{padding:11px;border-bottom:2px solid var(--ink);text-align:left}}.eyebrow{{font-size:.74rem;letter-spacing:.12em;font-weight:900;color:#4b2365}}.error{{background:#ff8a8a;color:#260000;padding:12px;font-weight:900;max-width:1160px;margin:0 auto 20px}}.success{{background:var(--acid);color:var(--ink);padding:12px;font-weight:900;max-width:1160px;margin:0 auto 20px}}.hint{{color:#4b2365;font-weight:700}}small{{font-weight:400}}@media(max-width:720px){{body{{padding:16px}}.topbar{{align-items:start;flex-direction:column}}.create-panel form{{grid-template-columns:1fr}}.topbar nav{{flex-wrap:wrap}}}}
        </style></head><body>{body_content}</body></html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(document)
        self.config.metrics.record("dashboard", status)

    def _redirect(self, target: str, *, session_token: str | None = None, clear_session: bool = False) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", target)
        if session_token:
            secure = "; Secure" if self.config.secure_cookies else ""
            self.send_header("Set-Cookie", f"staticdeploy_session={session_token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}")
        if clear_session:
            secure = "; Secure" if self.config.secure_cookies else ""
            self.send_header("Set-Cookie", f"staticdeploy_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}")
        self.end_headers()
        self.config.metrics.record("redirect", HTTPStatus.SEE_OTHER)

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


def serve(store: Store, host: str, port: int, *, admin_token: str | None, preview_suffix: str | None, routing_mode: str, secure_cookies: bool) -> None:
    handler = type("ConfiguredStaticDeployHandler", (StaticDeployHandler,), {"config": PlatformConfig(store, admin_token, preview_suffix, routing_mode, secure_cookies, Metrics())})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"StaticDeploy edge listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping edge server")
    finally:
        server.server_close()

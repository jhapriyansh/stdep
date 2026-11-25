from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


class StoreError(RuntimeError):
    """Raised when a requested site or deploy cannot be resolved."""


class _Connection(sqlite3.Connection):
    """Commits/rolls back like sqlite's context manager, then closes eagerly."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


@dataclass(frozen=True)
class Deploy:
    id: str
    site_slug: str
    content_hash: str
    created_at: str
    size_bytes: int
    file_count: int


@dataclass(frozen=True)
class DeployJob:
    id: str
    site_slug: str
    ref: str
    commit_sha: str
    status: str
    requested_at: str
    message: str | None


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    created_at: str


class Store:
    """SQLite metadata plus immutable files under a single data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.db_path = self.data_dir / "staticdeploy.sqlite3"
        self.deploy_root = self.data_dir / "deploys"

    def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.deploy_root.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS sites (
                    slug TEXT PRIMARY KEY,
                    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    current_deploy_id TEXT,
                    repository_url TEXT,
                    branch TEXT NOT NULL DEFAULT 'main',
                    build_command TEXT,
                    webhook_secret TEXT,
                    quota_bytes INTEGER NOT NULL DEFAULT 104857600,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domains (
                    hostname TEXT PRIMARY KEY,
                    site_slug TEXT NOT NULL REFERENCES sites(slug) ON DELETE CASCADE,
                    verified_at TEXT
                );
                CREATE TABLE IF NOT EXISTS deploys (
                    id TEXT PRIMARY KEY,
                    site_slug TEXT NOT NULL REFERENCES sites(slug) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_count INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deploys_site_created ON deploys(site_slug, created_at DESC);
                CREATE TABLE IF NOT EXISTS previews (
                    site_slug TEXT NOT NULL REFERENCES sites(slug) ON DELETE CASCADE,
                    preview_slug TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    current_deploy_id TEXT NOT NULL REFERENCES deploys(id),
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(site_slug, preview_slug)
                );
                CREATE TABLE IF NOT EXISTS deploy_jobs (
                    id TEXT PRIMARY KEY,
                    site_slug TEXT NOT NULL REFERENCES sites(slug) ON DELETE CASCADE,
                    ref TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed')),
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    message TEXT
                );
                CREATE INDEX IF NOT EXISTS deploy_jobs_pending ON deploy_jobs(status, requested_at);
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_expiry ON sessions(user_id, expires_at);
                """
            )
            self._ensure_column(db, "sites", "owner_user_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL")
            self._ensure_column(db, "sites", "repository_url", "TEXT")
            self._ensure_column(db, "sites", "branch", "TEXT NOT NULL DEFAULT 'main'")
            self._ensure_column(db, "sites", "build_command", "TEXT")
            self._ensure_column(db, "sites", "webhook_secret", "TEXT")
            self._ensure_column(db, "sites", "quota_bytes", "INTEGER NOT NULL DEFAULT 104857600")
            self._ensure_column(db, "domains", "verified_at", "TEXT")

    def create_site(
        self,
        slug: str,
        domains: list[str],
        repository_url: str | None = None,
        branch: str = "main",
        build_command: str | None = None,
        quota_bytes: int = 100 * 1024 * 1024,
        owner_user_id: int | None = None,
    ) -> str:
        self._validate_slug(slug)
        normalized = [self._normalize_domain(domain) for domain in domains]
        if not normalized:
            raise StoreError("at least one domain is required")
        now = self._timestamp()
        try:
            with self._connect() as db:
                webhook_secret = secrets.token_urlsafe(32) if repository_url else None
                db.execute(
                    """INSERT INTO sites(slug, owner_user_id, repository_url, branch, build_command, webhook_secret, quota_bytes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (slug, owner_user_id, repository_url, branch, build_command, webhook_secret, quota_bytes, now),
                )
                db.executemany(
                    "INSERT INTO domains(hostname, site_slug) VALUES (?, ?)",
                    [(domain, slug) for domain in normalized],
                )
        except sqlite3.IntegrityError as error:
            raise StoreError("site slug or domain already exists") from error
        return webhook_secret or ""

    def list_sites(self, owner_user_id: int | None = None) -> list[tuple[str, str | None, list[str]]]:
        where = ""
        parameters: tuple[int, ...] = ()
        if owner_user_id is not None:
            where = "WHERE s.owner_user_id = ?"
            parameters = (owner_user_id,)
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT s.slug, s.current_deploy_id, GROUP_CONCAT(d.hostname, ',') AS domains
                FROM sites s LEFT JOIN domains d ON d.site_slug = s.slug
                {where}
                GROUP BY s.slug ORDER BY s.slug
                """,
                parameters,
            ).fetchall()
        return [(row[0], row[1], row[2].split(",") if row[2] else []) for row in rows]

    def deploy(self, slug: str, source: Path, *, activate: bool = True) -> Deploy:
        if not source.is_dir():
            raise StoreError(f"deploy source is not a directory: {source}")
        self._site_exists(slug)
        digest, size_bytes, file_count = self._hash_directory(source)
        self._ensure_quota(slug, size_bytes)
        # IDs must remain unique across sites: deploys.id is a primary key and
        # identical content may legitimately be deployed by two tenants.
        deploy_id = f"{slug}-{digest[:16]}"
        destination = self.deploy_root / slug / deploy_id
        if not destination.exists():
            staging = destination.with_name(f".{deploy_id}.staging")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if staging.exists():
                shutil.rmtree(staging)
            shutil.copytree(source, staging, symlinks=False, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            os.replace(staging, destination)

        deploy = Deploy(deploy_id, slug, digest, self._timestamp(), size_bytes, file_count)
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO deploys(id, site_slug, content_hash, created_at, size_bytes, file_count) VALUES (?, ?, ?, ?, ?, ?)",
                (deploy.id, deploy.site_slug, deploy.content_hash, deploy.created_at, deploy.size_bytes, deploy.file_count),
            )
            if activate:
                db.execute("UPDATE sites SET current_deploy_id = ? WHERE slug = ?", (deploy.id, slug))
        return deploy

    def create_preview(self, slug: str, preview_slug: str, ref: str, deploy_id: str, expires_at: str) -> None:
        self._site_exists(slug)
        self._validate_slug(preview_slug)
        with self._connect() as db:
            exists = db.execute("SELECT 1 FROM deploys WHERE id = ? AND site_slug = ?", (deploy_id, slug)).fetchone()
            if exists is None:
                raise StoreError(f"unknown deploy for preview: {deploy_id}")
            db.execute(
                """INSERT INTO previews(site_slug, preview_slug, ref, current_deploy_id, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(site_slug, preview_slug) DO UPDATE SET
                    ref = excluded.ref, current_deploy_id = excluded.current_deploy_id, expires_at = excluded.expires_at""",
                (slug, preview_slug, ref, deploy_id, expires_at),
            )

    def resolve_preview(self, preview_slug: str, site_slug: str) -> Path | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT current_deploy_id, expires_at FROM previews WHERE site_slug = ? AND preview_slug = ?",
                (site_slug, preview_slug),
            ).fetchone()
        if row is None or row[1] <= self._timestamp():
            return None
        root = (self.deploy_root / site_slug / row[0]).resolve()
        return root if root.is_dir() else None

    def remove_expired_previews(self) -> int:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM previews WHERE expires_at <= ?", (self._timestamp(),))
            return cursor.rowcount

    def queue_deploy(self, slug: str, ref: str, commit_sha: str) -> DeployJob:
        self._site_exists(slug)
        if not ref or len(ref) > 255 or ref.startswith("-") or "\x00" in ref:
            raise StoreError("invalid deployment ref")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
            raise StoreError("invalid deployment commit SHA")
        job = DeployJob(secrets.token_urlsafe(12), slug, ref, commit_sha, "queued", self._timestamp(), None)
        with self._connect() as db:
            db.execute(
                "INSERT INTO deploy_jobs(id, site_slug, ref, commit_sha, status, requested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job.id, job.site_slug, job.ref, job.commit_sha, job.status, job.requested_at),
            )
        return job

    def claim_job(self) -> DeployJob | None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, site_slug, ref, commit_sha, status, requested_at, message FROM deploy_jobs WHERE status = 'queued' ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE deploy_jobs SET status = 'running', started_at = ? WHERE id = ?", (self._timestamp(), row[0]))
        return DeployJob(row[0], row[1], row[2], row[3], "running", row[5], row[6])

    def finish_job(self, job_id: str, success: bool, message: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE deploy_jobs SET status = ?, finished_at = ?, message = ? WHERE id = ?",
                ("succeeded" if success else "failed", self._timestamp(), message[-4000:], job_id),
            )

    def list_jobs(self, slug: str | None = None) -> list[DeployJob]:
        query = "SELECT id, site_slug, ref, commit_sha, status, requested_at, message FROM deploy_jobs"
        parameters: tuple[str, ...] = ()
        if slug:
            query += " WHERE site_slug = ?"
            parameters = (slug,)
        query += " ORDER BY requested_at DESC"
        with self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [DeployJob(*row) for row in rows]

    def site_settings(self, slug: str) -> dict[str, object]:
        with self._connect() as db:
            row = db.execute(
                "SELECT slug, repository_url, branch, build_command, webhook_secret, quota_bytes FROM sites WHERE slug = ?", (slug,)
            ).fetchone()
        if row is None:
            raise StoreError(f"unknown site: {slug}")
        return dict(zip(("slug", "repository_url", "branch", "build_command", "webhook_secret", "quota_bytes"), row, strict=True))

    def has_users(self) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_first_admin(self, username: str, password: str) -> User:
        username = self._normalize_username(username)
        self._validate_password(password)
        with self._connect() as db:
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                raise StoreError("the first admin has already been created")
            cursor = db.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (username, self._hash_password(password), self._timestamp()),
            )
            db.execute("UPDATE sites SET owner_user_id = ? WHERE owner_user_id IS NULL", (cursor.lastrowid,))
        return self.get_user(int(cursor.lastrowid))

    def create_user(self, username: str, password: str) -> User:
        if not self.has_users():
            raise StoreError("an admin must be created before users can join")
        username = self._normalize_username(username)
        self._validate_password(password)
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'user', ?)",
                    (username, self._hash_password(password), self._timestamp()),
                )
        except sqlite3.IntegrityError as error:
            raise StoreError("that username is already taken") from error
        return self.get_user(int(cursor.lastrowid))

    def authenticate(self, username: str, password: str) -> User | None:
        username = username.lower().strip()
        with self._connect() as db:
            row = db.execute("SELECT id, username, password_hash, role, created_at FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not self._verify_password(password, row[2]):
            return None
        return User(row[0], row[1], row[3], row[4])

    def get_user(self, user_id: int) -> User:
        with self._connect() as db:
            row = db.execute("SELECT id, username, role, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise StoreError("unknown user")
        return User(*row)

    def list_users(self) -> list[User]:
        with self._connect() as db:
            rows = db.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at").fetchall()
        return [User(*row) for row in rows]

    def create_session(self, user: User, lifetime_days: int = 7) -> str:
        token = secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute(
                "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (self._token_hash(token), user.id, (datetime.now(UTC) + timedelta(days=lifetime_days)).isoformat(), self._timestamp()),
            )
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                """SELECT u.id, u.username, u.role, u.created_at FROM sessions s
                JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?""",
                (self._token_hash(token), self._timestamp()),
            ).fetchone()
        return User(*row) if row else None

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (self._token_hash(token),))

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = os.urandom(16)
        derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return "scrypt$" + urlsafe_b64encode(salt).decode() + "$" + urlsafe_b64encode(derived).decode()

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            algorithm, salt_text, digest_text = encoded.split("$", 2)
            if algorithm != "scrypt":
                return False
            salt = urlsafe_b64decode(salt_text.encode())
            expected = urlsafe_b64decode(digest_text.encode())
            actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
            return secrets.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    def is_known_domain(self, hostname: str) -> bool:
        try:
            hostname = self._normalize_domain(hostname)
        except StoreError:
            return False
        with self._connect() as db:
            return db.execute("SELECT 1 FROM domains WHERE hostname = ?", (hostname,)).fetchone() is not None

    def list_deploys(self, slug: str) -> list[Deploy]:
        self._site_exists(slug)
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, site_slug, content_hash, created_at, size_bytes, file_count FROM deploys WHERE site_slug = ? ORDER BY created_at DESC",
                (slug,),
            ).fetchall()
        return [Deploy(*row) for row in rows]

    def rollback(self, slug: str, deploy_id: str) -> None:
        with self._connect() as db:
            found = db.execute("SELECT 1 FROM deploys WHERE id = ? AND site_slug = ?", (deploy_id, slug)).fetchone()
            if found is None:
                raise StoreError(f"deploy {deploy_id} does not belong to site {slug}")
            db.execute("UPDATE sites SET current_deploy_id = ? WHERE slug = ?", (deploy_id, slug))

    def resolve_host(self, host: str) -> tuple[str, Path] | None:
        hostname = host.split(":", 1)[0].lower().rstrip(".")
        with self._connect() as db:
            row = db.execute(
                """
                SELECT s.slug, s.current_deploy_id FROM domains d
                JOIN sites s ON s.slug = d.site_slug
                WHERE d.hostname = ?
                """,
                (hostname,),
            ).fetchone()
        if row is None or row[1] is None:
            return None
        root = (self.deploy_root / row[0] / row[1]).resolve()
        if not root.is_dir():
            return None
        return row[0], root

    def resolve_site(self, slug: str) -> Path | None:
        """Resolve a production deploy by slug for path-based routing."""
        try:
            self._validate_slug(slug)
        except StoreError:
            return None
        with self._connect() as db:
            row = db.execute("SELECT current_deploy_id FROM sites WHERE slug = ?", (slug,)).fetchone()
        if row is None or row[0] is None:
            return None
        root = (self.deploy_root / slug / row[0]).resolve()
        return root if root.is_dir() else None

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, factory=_Connection)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _ensure_quota(self, slug: str, size_bytes: int) -> None:
        with self._connect() as db:
            row = db.execute("SELECT quota_bytes FROM sites WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            raise StoreError(f"unknown site: {slug}")
        if size_bytes > row[0]:
            raise StoreError(f"deploy is {size_bytes} bytes and exceeds the {row[0]} byte site quota")

    def _site_exists(self, slug: str) -> None:
        with self._connect() as db:
            if db.execute("SELECT 1 FROM sites WHERE slug = ?", (slug,)).fetchone() is None:
                raise StoreError(f"unknown site: {slug}")

    @staticmethod
    def _timestamp() -> str:
        # Deploy ordering is part of the rollback contract. Microseconds avoid
        # ambiguous ordering for back-to-back deploys on a local machine.
        return datetime.now(UTC).isoformat(timespec="microseconds")

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        domain = domain.lower().strip().rstrip(".")
        labels = domain.split(".")
        valid_label = lambda label: bool(label) and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum() and all(c.isascii() and (c.isalnum() or c == "-") for c in label)
        if len(domain) > 253 or not all(valid_label(label) for label in labels):
            raise StoreError(f"invalid domain: {domain!r}")
        return domain

    @staticmethod
    def _validate_slug(slug: str) -> None:
        if not slug or len(slug) > 63 or not all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in slug):
            raise StoreError("site slug must contain lowercase letters, digits, and hyphens only")

    @staticmethod
    def _normalize_username(username: str) -> str:
        username = username.lower().strip()
        if not 3 <= len(username) <= 32 or not all(c.isascii() and (c.islower() or c.isdigit() or c in "_-" ) for c in username):
            raise StoreError("username must be 3-32 lowercase letters, digits, underscores, or hyphens")
        return username

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password) < 12:
            raise StoreError("password must be at least 12 characters")

    @staticmethod
    def _hash_directory(source: Path) -> tuple[str, int, int]:
        hasher = hashlib.sha256()
        size_bytes = 0
        file_count = 0
        paths = sorted(source.rglob("*"))
        for path in paths:
            if path.is_symlink():
                raise StoreError(f"symlinks are not allowed in a deploy: {path.relative_to(source)}")
        for path in (path for path in paths if path.is_file() and ".git" not in path.parts):
            relative = path.relative_to(source).as_posix().encode()
            hasher.update(len(relative).to_bytes(4, "big"))
            hasher.update(relative)
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    size_bytes += len(chunk)
            file_count += 1
        if file_count == 0:
            raise StoreError("a deploy must contain at least one file")
        return hasher.hexdigest(), size_bytes, file_count


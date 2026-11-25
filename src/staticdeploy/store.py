from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class StoreError(RuntimeError):
    """Raised when a requested site or deploy cannot be resolved."""


@dataclass(frozen=True)
class Deploy:
    id: str
    site_slug: str
    content_hash: str
    created_at: str
    size_bytes: int
    file_count: int


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
                    current_deploy_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS domains (
                    hostname TEXT PRIMARY KEY,
                    site_slug TEXT NOT NULL REFERENCES sites(slug) ON DELETE CASCADE
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
                """
            )

    def create_site(self, slug: str, domains: list[str]) -> None:
        self._validate_slug(slug)
        normalized = [self._normalize_domain(domain) for domain in domains]
        if not normalized:
            raise StoreError("at least one domain is required")
        now = self._timestamp()
        try:
            with self._connect() as db:
                db.execute("INSERT INTO sites(slug, created_at) VALUES (?, ?)", (slug, now))
                db.executemany(
                    "INSERT INTO domains(hostname, site_slug) VALUES (?, ?)",
                    [(domain, slug) for domain in normalized],
                )
        except sqlite3.IntegrityError as error:
            raise StoreError("site slug or domain already exists") from error

    def list_sites(self) -> list[tuple[str, str | None, list[str]]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT s.slug, s.current_deploy_id, GROUP_CONCAT(d.hostname, ',') AS domains
                FROM sites s LEFT JOIN domains d ON d.site_slug = s.slug
                GROUP BY s.slug ORDER BY s.slug
                """
            ).fetchall()
        return [(row[0], row[1], row[2].split(",") if row[2] else []) for row in rows]

    def deploy(self, slug: str, source: Path) -> Deploy:
        if not source.is_dir():
            raise StoreError(f"deploy source is not a directory: {source}")
        self._site_exists(slug)
        digest, size_bytes, file_count = self._hash_directory(source)
        deploy_id = digest[:16]
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
            db.execute("UPDATE sites SET current_deploy_id = ? WHERE slug = ?", (deploy.id, slug))
        return deploy

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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA foreign_keys = ON")
        return db

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
        if not domain or "/" in domain or " " in domain:
            raise StoreError(f"invalid domain: {domain!r}")
        return domain

    @staticmethod
    def _validate_slug(slug: str) -> None:
        if not slug or len(slug) > 63 or not all(c.islower() or c.isdigit() or c == "-" for c in slug):
            raise StoreError("site slug must contain lowercase letters, digits, and hyphens only")

    @staticmethod
    def _hash_directory(source: Path) -> tuple[str, int, int]:
        hasher = hashlib.sha256()
        size_bytes = 0
        file_count = 0
        for path in sorted(p for p in source.rglob("*") if p.is_file() and ".git" not in p.parts):
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


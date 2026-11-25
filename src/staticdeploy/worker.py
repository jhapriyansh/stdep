from __future__ import annotations

import os
import re
import resource
import shlex
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .store import DeployJob, Store, StoreError


class Worker:
    """Runs queued Git deployments one at a time with bounded subprocesses."""

    def __init__(self, store: Store, *, preview_ttl_hours: int = 72, command_timeout: int = 180) -> None:
        self.store = store
        self.preview_ttl_hours = preview_ttl_hours
        self.command_timeout = command_timeout

    def process_one(self) -> DeployJob | None:
        job = self.store.claim_job()
        if job is None:
            return None
        try:
            self._run(job)
        except (StoreError, OSError, subprocess.SubprocessError, ValueError) as error:
            self.store.finish_job(job.id, False, str(error))
        return job

    def _run(self, job: DeployJob) -> None:
        settings = self.store.site_settings(job.site_slug)
        repository_url = settings["repository_url"]
        if not repository_url:
            raise StoreError("site has no repository URL; use a directory deploy instead")
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", job.commit_sha):
            raise StoreError("webhook commit SHA is invalid")
        with tempfile.TemporaryDirectory(prefix="staticdeploy-") as temporary:
            checkout = Path(temporary) / "checkout"
            self._command(["git", "clone", "--no-checkout", str(repository_url), str(checkout)], Path(temporary))
            self._command(["git", "fetch", "--depth", "1", "origin", job.commit_sha], checkout)
            self._command(["git", "checkout", "--detach", "FETCH_HEAD"], checkout)
            build_command = settings["build_command"]
            if build_command:
                self._command(shlex.split(str(build_command)), checkout)
            output = checkout / "dist" if (checkout / "dist").is_dir() else checkout
            is_primary = job.ref == settings["branch"]
            deployed = self.store.deploy(job.site_slug, output, activate=is_primary)
            if not is_primary:
                preview_slug = self.preview_slug(job.ref)
                expiry = (datetime.now(UTC) + timedelta(hours=self.preview_ttl_hours)).isoformat(timespec="seconds")
                self.store.create_preview(job.site_slug, preview_slug, job.ref, deployed.id, expiry)
            self.store.finish_job(job.id, True, f"deployed {deployed.id}")

    def _command(self, command: list[str], cwd: Path) -> None:
        if not command:
            raise ValueError("build command cannot be empty")
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=self.command_timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "CI": "true"},
            preexec_fn=self._limits if os.name == "posix" else None,
        )
        if completed.returncode != 0:
            output = (completed.stdout + "\n" + completed.stderr).strip()
            raise subprocess.SubprocessError(f"{' '.join(command)} exited {completed.returncode}: {output[-2000:]}")

    @staticmethod
    def _limits() -> None:
        # A cooperative build sandbox: no shell, 2 GiB virtual-memory ceiling,
        # and a 190-second CPU ceiling. Containers add the production boundary.
        for limit, value in ((resource.RLIMIT_AS, 2 * 1024**3), (resource.RLIMIT_CPU, 190)):
            try:
                resource.setrlimit(limit, (value, value))
            except (OSError, ValueError):
                # macOS does not support every Linux resource limit. The
                # container boundary still applies in production.
                continue

    @staticmethod
    def preview_slug(ref: str) -> str:
        slug = ref.lower().replace("/", "-")
        slug = "".join(character if character.isascii() and (character.isalnum() or character == "-") else "-" for character in slug)
        slug = slug.strip("-") or "preview"
        return slug[:63]

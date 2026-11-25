from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from staticdeploy.store import Store
from staticdeploy.worker import Worker


class WorkerTests(unittest.TestCase):
    def test_worker_deploys_the_signed_commit_from_a_git_repository(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repository = root / "repository"
            repository.mkdir()
            self._git(repository, "init", "-b", "main")
            self._git(repository, "config", "user.email", "test@example.com")
            self._git(repository, "config", "user.name", "StaticDeploy test")
            (repository / "index.html").write_text("deployed from git")
            self._git(repository, "add", "index.html")
            self._git(repository, "commit", "-m", "initial site")
            commit_sha = self._git(repository, "rev-parse", "HEAD").strip()

            store = Store(root / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"], str(repository))
            queued = store.queue_deploy("demo", "main", commit_sha)
            Worker(store).process_one()

            job = store.list_jobs("demo")[0]
            self.assertEqual(job.id, queued.id)
            self.assertEqual(job.status, "succeeded")
            resolved = store.resolve_host("demo.test")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual((resolved[1] / "index.html").read_text(), "deployed from git")

    @staticmethod
    def _git(cwd: Path, *arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=cwd, text=True)

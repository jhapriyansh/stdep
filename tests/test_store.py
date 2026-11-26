from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile

from staticdeploy.store import Store, StoreError


def write_site(root: Path, body: str) -> None:
    root.mkdir()
    (root / "index.html").write_text(body)
    (root / "app.123abc.js").write_text("console.log('ok')")


class StoreTests(unittest.TestCase):
    @staticmethod
    def zip_site(files: dict[str, str]) -> bytes:
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            for path, contents in files.items():
                archive.writestr(path, contents)
        return buffer.getvalue()
    def test_deploy_resolve_and_rollback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            store = Store(tmp_path / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            first_source = tmp_path / "first"
            second_source = tmp_path / "second"
            write_site(first_source, "first")
            write_site(second_source, "second")

            first = store.deploy("demo", first_source)
            second = store.deploy("demo", second_source)
            resolved = store.resolve_host("demo.test:8080")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual((resolved[1] / "index.html").read_text(), "second")

            store.rollback("demo", first.id)
            resolved = store.resolve_host("demo.test")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual((resolved[1] / "index.html").read_text(), "first")
            self.assertEqual([deploy.id for deploy in store.list_deploys("demo")], [second.id, first.id])

    def test_rejects_empty_and_unknown_deploys(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            store = Store(tmp_path / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            empty = tmp_path / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(StoreError, "at least one file"):
                store.deploy("demo", empty)
            with self.assertRaisesRegex(StoreError, "unknown site"):
                store.deploy("missing", empty)

    def test_preview_queue_and_quota(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            store = Store(tmp_path / "data")
            store.initialize()
            secret = store.create_site("demo", ["demo.test"], "https://example.test/demo.git", quota_bytes=1024)
            self.assertTrue(secret)
            source = tmp_path / "site"
            write_site(source, "preview")
            deployed = store.deploy("demo", source, activate=False)
            expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat(timespec="seconds")
            store.create_preview("demo", "feature-card", "feature/card", deployed.id, expiry)
            preview = store.resolve_preview("feature-card", "demo")
            self.assertIsNotNone(preview)
            assert preview is not None
            self.assertEqual((preview / "index.html").read_text(), "preview")

            job = store.queue_deploy("demo", "main", "a" * 40)
            claimed = store.claim_job()
            self.assertEqual(claimed.id if claimed else None, job.id)
            store.finish_job(job.id, True, "complete")
            self.assertEqual(store.list_jobs("demo")[0].status, "succeeded")

            oversized = tmp_path / "oversized"
            oversized.mkdir()
            (oversized / "index.html").write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(StoreError, "exceeds"):
                store.deploy("demo", oversized)

    def test_identical_content_is_isolated_per_site(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            store = Store(tmp_path / "data")
            store.initialize()
            store.create_site("one", ["one.test"])
            store.create_site("two", ["two.test"])
            source = tmp_path / "site"
            write_site(source, "same")
            one = store.deploy("one", source)
            two = store.deploy("two", source)
            self.assertNotEqual(one.id, two.id)
            self.assertEqual(store.list_deploys("one")[0].id, one.id)
            self.assertEqual(store.list_deploys("two")[0].id, two.id)

    def test_rejects_symlinked_deploy_content(self) -> None:
        with TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            store = Store(tmp_path / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            source = tmp_path / "site"
            source.mkdir()
            target = tmp_path / "outside.txt"
            target.write_text("secret")
            (source / "index.html").symlink_to(target)
            with self.assertRaisesRegex(StoreError, "symlinks"):
                store.deploy("demo", source)

    def test_rejects_invalid_domain_and_treats_it_as_unknown(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "data")
            store.initialize()
            with self.assertRaisesRegex(StoreError, "invalid domain"):
                store.create_site("demo", ["bad/domain"])
            self.assertFalse(store.is_known_domain("bad/domain"))

    def test_rejects_invalid_queued_deploy_metadata(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            with self.assertRaisesRegex(StoreError, "invalid deployment ref"):
                store.queue_deploy("demo", "-bad", "a" * 40)
            with self.assertRaisesRegex(StoreError, "invalid deployment commit SHA"):
                store.queue_deploy("demo", "main", "not-a-sha")

    def test_deploys_validated_zip_and_rejects_unsafe_archives(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            first = store.deploy_zip("demo", self.zip_site({"index.html": "one", "assets/main.css": "body{}"}))
            second = store.deploy_zip("demo", self.zip_site({"index.html": "two"}))
            resolved = store.resolve_site("demo")
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual((resolved / "index.html").read_text(), "two")
            store.rollback("demo", first.id)
            resolved = store.resolve_site("demo")
            self.assertEqual((resolved / "index.html").read_text(), "one")  # type: ignore[union-attr]
            with self.assertRaisesRegex(StoreError, "unsafe path"):
                store.deploy_zip("demo", self.zip_site({"../outside.html": "no"}))
            with self.assertRaisesRegex(StoreError, "index.html"):
                store.deploy_zip("demo", self.zip_site({"readme.txt": "no"}))

    def test_first_admin_gates_user_onboarding_and_owns_existing_sites(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "data")
            store.initialize()
            store.create_site("legacy", ["legacy.test"])
            with self.assertRaisesRegex(StoreError, "admin must be created"):
                store.create_user("player", "a-secure-password")
            admin = store.create_first_admin("admin-player", "a-secure-password")
            self.assertEqual(admin.role, "admin")
            self.assertEqual([site[0] for site in store.list_sites(admin.id)], ["legacy"])
            player = store.create_user("player-one", "another-secure-password")
            self.assertEqual(player.role, "user")
            self.assertEqual(store.authenticate("player-one", "bad-password"), None)
            self.assertEqual(store.authenticate("player-one", "another-secure-password"), player)
            token = store.create_session(player)
            self.assertEqual(store.session_user(token), player)
            store.delete_session(token)
            self.assertIsNone(store.session_user(token))


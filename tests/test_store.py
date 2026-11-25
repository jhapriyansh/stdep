from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from staticdeploy.store import Store, StoreError


def write_site(root: Path, body: str) -> None:
    root.mkdir()
    (root / "index.html").write_text(body)
    (root / "app.123abc.js").write_text("console.log('ok')")


class StoreTests(unittest.TestCase):
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


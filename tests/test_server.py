from pathlib import Path
import unittest

from staticdeploy.server import Metrics, PlatformConfig, StaticDeployHandler
from staticdeploy.store import Store


class ServerTests(unittest.TestCase):
    def test_cache_policy_and_safe_path(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "deploy"
            root.mkdir()
            handler = object.__new__(StaticDeployHandler)
            handler.path = "/../../outside.txt"
            with self.assertRaises(ValueError):
                handler._safe_target(root)
            self.assertEqual(handler._cache_control(Path("app.123abc.js")), "public, max-age=31536000, immutable")
            self.assertEqual(handler._cache_control(Path("index.html")), "no-cache")

    def test_preview_host_resolution_and_metrics(self) -> None:
        from datetime import UTC, datetime, timedelta
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            source = root / "site"
            source.mkdir()
            (source / "index.html").write_text("hello")
            deployed = store.deploy("demo", source, activate=False)
            store.create_preview("demo", "feature", "feature", deployed.id, (datetime.now(UTC) + timedelta(hours=1)).isoformat())
            handler = object.__new__(StaticDeployHandler)
            handler.config = PlatformConfig(store, "token", "preview.test", Metrics())
            handler.headers = {"Host": "feature--demo.preview.test"}
            resolved = handler._resolve_site_root()
            self.assertIsNotNone(resolved)
            self.assertEqual((resolved / "index.html").read_text(), "hello")  # type: ignore[union-attr]
            handler.config.metrics.record("static", 200)
            self.assertIn(b"staticdeploy_http_requests_total", handler.config.metrics.render())

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
            handler.config = PlatformConfig(store, "token", "preview.test", "subdomain", Metrics())
            handler.headers = {"Host": "feature--demo.preview.test"}
            resolved = handler._resolve_site_root()
            self.assertIsNotNone(resolved)
            self.assertEqual((resolved / "index.html").read_text(), "hello")  # type: ignore[union-attr]
            handler.config.metrics.record("static", 200)
            self.assertIn(b"staticdeploy_http_requests_total", handler.config.metrics.render())

    def test_path_routing_resolves_site_and_preview_without_a_domain(self) -> None:
        from datetime import UTC, datetime, timedelta
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = Store(root / "data")
            store.initialize()
            store.create_site("demo", ["demo.test"])
            source = root / "site"
            source.mkdir()
            (source / "index.html").write_text("path mode")
            (source / "assets").mkdir()
            (source / "assets" / "site.css").write_text("body { color: green; }")
            deployed = store.deploy("demo", source)
            store.create_preview("demo", "feature", "feature", deployed.id, (datetime.now(UTC) + timedelta(hours=1)).isoformat())
            handler = object.__new__(StaticDeployHandler)
            handler.config = PlatformConfig(store, "token", None, "path", Metrics())

            handler.path = "/sites/demo/"
            site_route = handler._resolve_content()
            self.assertIsNotNone(site_route)
            assert site_route is not None
            self.assertEqual((handler._safe_target(*site_route)).read_text(), "path mode")

            handler.path = "/sites/demo/assets/site.css"
            asset_route = handler._resolve_content()
            self.assertIsNotNone(asset_route)
            assert asset_route is not None
            self.assertEqual((handler._safe_target(*asset_route)).read_text(), "body { color: green; }")

            handler.path = "/previews/demo/feature/"
            preview_route = handler._resolve_content()
            self.assertIsNotNone(preview_route)
            assert preview_route is not None
            self.assertEqual((handler._safe_target(*preview_route)).read_text(), "path mode")

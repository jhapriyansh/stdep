from pathlib import Path
import unittest

from staticdeploy.server import StaticDeployHandler


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

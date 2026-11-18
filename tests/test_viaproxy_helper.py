"""ViaProxy 一键脚本的纯函数测试（不需要 Java / 网络）。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODULE_PATH = ROOT / "tools" / "run_viaproxy.py"
spec = importlib.util.spec_from_file_location("run_viaproxy", MODULE_PATH)
assert spec is not None and spec.loader is not None
run_viaproxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_viaproxy)


class JavaVersionTests(unittest.TestCase):
    def test_parse_modern(self) -> None:
        self.assertEqual(run_viaproxy.parse_java_major('openjdk version "21.0.2" 2024-01-16'), 21)
        self.assertEqual(run_viaproxy.parse_java_major('java version "17.0.9" 2023-10-17 LTS'), 17)

    def test_parse_legacy(self) -> None:
        self.assertEqual(run_viaproxy.parse_java_major('java version "1.8.0_391"'), 8)
        self.assertEqual(run_viaproxy.parse_java_major("not java at all"), 0)


class AssetPickTests(unittest.TestCase):
    ASSETS = [
        {"name": "ViaProxy-3.4.14+java8.jar", "browser_download_url": "u1", "size": 1},
        {"name": "ViaProxy-3.4.14.jar", "browser_download_url": "u2", "size": 2},
        {"name": "sources.jar", "browser_download_url": "u3", "size": 3},
    ]

    def test_picks_standard_for_modern_java(self) -> None:
        asset = run_viaproxy.pick_asset(self.ASSETS, 21)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["name"], "ViaProxy-3.4.14.jar")

    def test_picks_java8_for_java8(self) -> None:
        asset = run_viaproxy.pick_asset(self.ASSETS, 8)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["name"], "ViaProxy-3.4.14+java8.jar")

    def test_unknown_java_prefers_standard(self) -> None:
        asset = run_viaproxy.pick_asset(self.ASSETS, 0)
        self.assertIsNotNone(asset)
        self.assertEqual(asset["name"], "ViaProxy-3.4.14.jar")

    def test_no_assets(self) -> None:
        self.assertIsNone(run_viaproxy.pick_asset([], 21))


class ExistingJarTests(unittest.TestCase):
    def test_finds_versioned_jar(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        jar = MagicMock()
        jar.name = "ViaProxy-3.4.14.jar"
        jar.is_file.return_value = True
        jar.stat.return_value = SimpleNamespace(st_mtime=1.0)
        folder = MagicMock()
        folder.glob.return_value = [jar]
        self.assertIs(run_viaproxy.find_existing_jar(folder), jar)

    def test_missing_returns_none(self) -> None:
        from unittest.mock import MagicMock

        folder = MagicMock()
        folder.glob.return_value = []
        self.assertIsNone(run_viaproxy.find_existing_jar(folder))


if __name__ == "__main__":
    unittest.main(verbosity=2)

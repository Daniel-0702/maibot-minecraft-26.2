"""一次跑完所有测试：``python tests/run_all.py``。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

MODULES = [
    "test_core",
    "test_nbt",
    "test_chunk",
    "test_physics",
    "test_pathfinding",
    "test_humanize",
    "test_plugin",
    "test_e2e",
    "test_viaproxy_helper",
    "test_verification",
    "test_db_fix",
    "test_backend_selection",
    "test_modern_protocol",
    "test_modern_chunk",
    "test_modern_e2e",
    "test_address",
    "test_woodcutting",
    "test_survival",
    "test_knowledge",
    "test_recipes",
    "test_light",
    "test_light_integration",
    "test_crafting_protocol",
    "test_crafting_flow",
    "test_actions",
    "test_commands",
    "test_player_actions",
]


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in MODULES:
        suite.addTests(loader.loadTestsFromName(name))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print(f"\n共运行 {result.testsRun} 个用例，失败 {len(result.failures)}，错误 {len(result.errors)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

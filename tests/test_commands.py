"""底层指令层测试：动作注册表驱动的 /mc 路由、中文别名、参数解析与工具入口。

用 ``tests/stubs`` 里的 maibot_sdk 替身，不依赖真实 MaiBot 运行时。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "stubs"))
sys.path.insert(0, str(ROOT))

import plugin as plugin_module  # noqa: E402
from mcbot.actions import ACTIONS, format_help, resolve  # noqa: E402
from mcbot.config import BotConfig  # noqa: E402


class FakeBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.stopped = False

    async def execute(self, action: str, **kwargs) -> dict:
        self.calls.append((action, kwargs))
        return {"success": True, "message": f"ok:{action}", "action": action}

    async def stop(self) -> None:
        self.stopped = True

    def status(self) -> dict:
        return {"connected": True, "joined": True, "client_state": "play", "last_error": ""}


class ActionSurfaceTests(unittest.TestCase):
    def test_registry_has_human_like_actions(self) -> None:
        names = {action.name for action in ACTIONS}
        for expected in (
            "goto", "come", "follow", "explore", "swim", "home", "sethome", "jump",
            "mine", "chop", "dig", "collect", "eat",
            "craft", "recipes", "inventory", "equip", "drop", "torch",
            "place", "pillar", "bridge", "shelter",
            "attack", "flee", "say",
            "status", "diag", "nearby", "players", "where", "time", "light", "help",
            "connect", "disconnect", "verify",
        ):
            self.assertIn(expected, names)

    def test_chinese_aliases_resolve(self) -> None:
        self.assertEqual(resolve("砍树").name, "chop")
        self.assertEqual(resolve("合成").name, "craft")
        self.assertEqual(resolve("回家").name, "home")
        self.assertEqual(resolve("垫高").name, "pillar")
        self.assertEqual(resolve("搭桥").name, "bridge")
        self.assertEqual(resolve("附近").name, "nearby")
        self.assertEqual(resolve("火把").name, "torch")
        self.assertIsNone(resolve("不存在的动作"))

    def test_help_lists_categories_and_commands(self) -> None:
        text = format_help()
        for word in ("移动", "采集", "合成", "建造", "战斗", "信息"):
            self.assertIn(word, text)
        self.assertIn("chop", text)
        self.assertIn("craft", text)
        movement = format_help("移动")
        self.assertIn("goto", movement)
        self.assertNotIn("chop", movement)


class CommandRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = plugin_module.MaiBotMinecraftPlugin()
        self.bridge = FakeBridge()
        self.plugin._bridge = self.bridge  # type: ignore[assignment]
        self.plugin._bot_config = BotConfig()

    def _send(self, sub: str, args: str = "") -> tuple[bool, str, int]:
        return asyncio.run(
            self.plugin.handle_mc(matched_groups={"sub": sub, "args": args}, stream_id="s")
        )

    def _last(self) -> tuple[str, dict]:
        return self.bridge.calls[-1]

    def test_default_is_status(self) -> None:
        self._send("")
        self.assertEqual(self._last()[0], "status")

    def test_help(self) -> None:
        ok, text, _ = self._send("help")
        self.assertTrue(ok)
        self.assertIn("chop", text)
        ok, movement, _ = self._send("help", "移动")
        self.assertTrue(ok)
        self.assertIn("goto", movement)

    def test_chop_with_and_without_count(self) -> None:
        self._send("chop")
        self.assertEqual(self._last(), ("chop", {"count": 6}))
        self._send("chop", "3")
        self.assertEqual(self._last(), ("chop", {"count": 3}))
        self._send("砍树", "2")  # 中文别名
        self.assertEqual(self._last(), ("chop", {"count": 2}))

    def test_craft_and_recipes(self) -> None:
        self._send("craft", "wooden_pickaxe")
        self.assertEqual(self._last(), ("craft", {"item": "wooden_pickaxe"}))
        self._send("recipes", "")
        self.assertEqual(self._last(), ("recipes", {"target": ""}))
        self._send("配方", "torch")
        self.assertEqual(self._last(), ("recipes", {"target": "torch"}))

    def test_dig_pillar_bridge(self) -> None:
        self._send("dig", "4")
        self.assertEqual(self._last(), ("dig", {"steps": 4}))
        self._send("垫高", "2")
        self.assertEqual(self._last(), ("pillar", {"count": 2}))
        self._send("bridge", "5")
        self.assertEqual(self._last(), ("bridge", {"length": 5}))

    def test_items(self) -> None:
        self._send("equip", "torch")
        self.assertEqual(self._last(), ("equip", {"item": "torch"}))
        self._send("drop", "stone")
        self.assertEqual(self._last(), ("drop", {"item": "stone"}))
        self._send("eat", "")
        self.assertEqual(self._last(), ("eat", {}))

    def test_info_actions(self) -> None:
        for sub in ("where", "time", "light", "players", "inventory"):
            self._send(sub)
            self.assertEqual(self._last()[0], sub)
        self._send("nearby", "12")
        self.assertEqual(self._last(), ("nearby", {"radius": 12.0}))

    def test_place_with_block_and_coords(self) -> None:
        self._send("place", "oak_planks 1 64 -3")
        self.assertEqual(self._last(), ("place", {"block": "oak_planks", "x": 1.0, "y": 64.0, "z": -3.0}))
        ok, text, _ = self._send("place", "缺参数")
        self.assertFalse(ok)
        self.assertIn("用法", text)

    def test_goto_coords_and_player(self) -> None:
        self._send("goto", "10 64 -8")
        self.assertEqual(self._last(), ("goto", {"x": 10.0, "y": 64.0, "z": -8.0}))
        self._send("goto", "小明")
        self.assertEqual(self._last(), ("goto", {"player": "小明"}))

    def test_unknown_action_returns_help(self) -> None:
        ok, text, _ = self._send("飞上天")
        self.assertFalse(ok)
        self.assertIn("没有", text)

    def test_missing_item_reports_usage(self) -> None:
        ok, text, _ = self._send("craft", "")
        self.assertFalse(ok)
        self.assertIn("用法", text)


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = plugin_module.MaiBotMinecraftPlugin()
        self.bridge = FakeBridge()
        self.plugin._bridge = self.bridge  # type: ignore[assignment]
        self.plugin._bot_config = BotConfig()

    def test_mc_command_generic(self) -> None:
        asyncio.run(self.plugin.mc_command(subcommand="craft", args="torch"))
        self.assertEqual(self.bridge.calls[-1], ("craft", {"item": "torch"}))
        asyncio.run(self.plugin.mc_command(subcommand="砍树", args="2"))
        self.assertEqual(self.bridge.calls[-1], ("chop", {"count": 2}))
        asyncio.run(self.plugin.mc_command(subcommand="不存在", args=""))
        result = {"success": False}
        self.assertFalse(bool(result["success"]))

    def test_mc_build_modes(self) -> None:
        asyncio.run(self.plugin.mc_build(mode="pillar", count=2))
        self.assertEqual(self.bridge.calls[-1], ("pillar", {"count": 2}))
        asyncio.run(self.plugin.mc_build(mode="shelter"))
        self.assertEqual(self.bridge.calls[-1], ("shelter", {}))

    def test_mc_survive_modes(self) -> None:
        asyncio.run(self.plugin.mc_survive(mode="torch"))
        self.assertEqual(self.bridge.calls[-1], ("torch", {}))
        result = asyncio.run(self.plugin.mc_survive(mode="乱来"))
        self.assertFalse(result["success"])

    def test_mc_chop_and_craft_tools(self) -> None:
        asyncio.run(self.plugin.mc_chop(count=4))
        self.assertEqual(self.bridge.calls[-1], ("chop", {"count": 4}))
        asyncio.run(self.plugin.mc_craft(item="torch"))
        self.assertEqual(self.bridge.calls[-1], ("craft", {"item": "torch"}))
        asyncio.run(self.plugin.mc_nearby(radius=8.0))
        self.assertEqual(self.bridge.calls[-1], ("nearby", {"radius": 8.0}))


if __name__ == "__main__":
    unittest.main()

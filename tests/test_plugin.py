"""插件适配层测试：组件注册、工具参数、命令路由、生命周期。

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
from mcbot.client import ChatMessage  # noqa: E402
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
        return {
            "connected": True,
            "joined": True,
            "client_state": "play",
            "last_error": "",
        }


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = plugin_module.MaiBotMinecraftPlugin()
        self.bridge = FakeBridge()
        self.plugin._bridge = self.bridge  # type: ignore[assignment]
        self.plugin._bot_config = BotConfig(host="127.0.0.1", port=25565, username="MaiBot")

    def test_default_config(self) -> None:
        config = self.plugin.config
        self.assertEqual(config.plugin.config_version, "1.0.0")
        self.assertEqual(config.server.host, "127.0.0.1")
        self.assertEqual(config.server.port, 0)  # 0 = 自动（SRV → 25565）
        self.assertFalse(config.server.auto_connect)
        self.assertTrue(config.behavior.autonomous)
        self.assertTrue(config.behavior.auto_eat)
        self.assertTrue(config.llm.llm_enabled)
        self.assertEqual(config.security.admin_users, "")

    def test_config_has_host_required_plugin_section(self) -> None:
        """回归：MaiBot Runner 要求 [plugin].config_version，缺了会暂停插件。"""
        raw = self.plugin.config.model_dump()
        self.assertIn("plugin", raw)
        self.assertIn("config_version", raw["plugin"])
        self.assertTrue(str(raw["plugin"]["config_version"]).strip())
        # [plugin].enabled 是宿主保留键，插件模型不应声明它（MaiBot issue #2080）
        self.assertNotIn("enabled", raw["plugin"])

    def test_build_bot_config_merges_sections(self) -> None:
        self.plugin.config.server.host = "mc.example.com"
        self.plugin.config.server.port = 25566
        self.plugin.config.behavior.autonomous = False
        self.plugin.config.behavior.typing_cps = 4.0
        self.plugin.config.llm.llm_enabled = False
        self.plugin.config.report.report_prefix = "【MC】"
        self.plugin.config.security.admin_users = "qq:123456789"

        config = self.plugin._build_bot_config()

        self.assertEqual(config.host, "mc.example.com")
        self.assertEqual(config.port, 25566)
        self.assertFalse(config.autonomous)
        self.assertFalse(config.llm_enabled)
        self.assertEqual(config.report_prefix, "【MC】")
        self.assertEqual(config.admin_users, "qq:123456789")
        self.assertAlmostEqual(config.human_profile().typing_cps, 4.0)

    def test_components_registered(self) -> None:
        components = {component["name"]: component for component in self.plugin.get_components()}
        # 老的核心工具必须保留
        required_tools = {
            "mc_status",
            "mc_connect",
            "mc_disconnect",
            "mc_goto",
            "mc_mine",
            "mc_place",
            "mc_attack",
            "mc_follow",
            "mc_say",
            "mc_eat",
            "mc_collect",
            "mc_inventory",
            "mc_stop",
            "mc_task",
        }
        # 2.0.5 新加的通用/常用动作工具
        extra_tools = {
            "mc_command",
            "mc_chop",
            "mc_craft",
            "mc_recipes",
            "mc_nearby",
            "mc_home",
            "mc_equip",
            "mc_build",
            "mc_survive",
        }
        tool_names = {name for name, component in components.items() if component["type"] == "TOOL"}
        self.assertTrue(required_tools <= tool_names, tool_names - required_tools)
        self.assertTrue(extra_tools <= tool_names, extra_tools - tool_names)
        self.assertIn("mc", components)
        self.assertEqual(components["mc"]["type"], "COMMAND")

    def test_status_tool(self) -> None:
        result = asyncio.run(self.plugin.mc_status())
        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "ok:status")
        self.assertEqual(self.bridge.calls[-1], ("status", {}))

    def test_say_tool(self) -> None:
        result = asyncio.run(self.plugin.mc_say(text="你好呀"))
        self.assertTrue(result["success"])
        self.assertEqual(self.bridge.calls[-1], ("say", {"text": "你好呀"}))

    def test_goto_tool_coordinates_and_player(self) -> None:
        asyncio.run(self.plugin.mc_goto(x=10, y=64, z=-5))
        self.assertEqual(self.bridge.calls[-1], ("goto", {"x": 10, "y": 64, "z": -5}))
        asyncio.run(self.plugin.mc_goto(player="小明"))
        self.assertEqual(self.bridge.calls[-1], ("goto", {"player": "小明"}))
        result = asyncio.run(self.plugin.mc_goto())
        self.assertFalse(result["success"])

    def test_mine_and_place_tools(self) -> None:
        asyncio.run(self.plugin.mc_mine(block="木头", count=3))
        self.assertEqual(self.bridge.calls[-1], ("mine", {"block": "木头", "count": 3}))
        asyncio.run(self.plugin.mc_place(block="stone", x=1, y=2, z=3))
        self.assertEqual(self.bridge.calls[-1], ("place", {"block": "stone", "x": 1, "y": 2, "z": 3}))

    def test_task_tool(self) -> None:
        asyncio.run(self.plugin.mc_task(instruction="去挖五个木头"))
        self.assertEqual(self.bridge.calls[-1], ("do", {"instruction": "去挖五个木头"}))

    def test_command_status(self) -> None:
        result = asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "", "args": ""}, stream_id="stream-1"))
        self.assertTrue(result[0])
        self.assertIsInstance(result[2], int)
        self.assertEqual(self.bridge.calls[-1], ("status", {}))

    def test_command_say_and_goto(self) -> None:
        asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "say", "args": "我回来了"}, stream_id="s"))
        self.assertEqual(self.bridge.calls[-1], ("say", {"text": "我回来了"}))
        asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "goto", "args": "10 64 -8"}, stream_id="s"))
        self.assertEqual(self.bridge.calls[-1], ("goto", {"x": 10.0, "y": 64.0, "z": -8.0}))
        asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "mine", "args": "stone 5"}, stream_id="s"))
        self.assertEqual(self.bridge.calls[-1], ("mine", {"block": "stone", "count": 5}))

    def test_parse_host_forms(self) -> None:
        """只写域名、写域名:端口、IPv6、空输入都要能正确拆开。"""
        self.assertEqual(self.plugin._parse_host("mc.example.com"), ("mc.example.com", 0))
        self.assertEqual(self.plugin._parse_host("mc.example.com:25566"), ("mc.example.com", 25566))
        self.assertEqual(self.plugin._parse_host("minecraft://mc.example.com/"), ("mc.example.com", 0))
        self.assertEqual(self.plugin._parse_host("[::1]:25565"), ("::1", 25565))
        self.assertEqual(self.plugin._parse_host(""), (None, None))

    def test_command_connect_without_port_uses_auto(self) -> None:
        asyncio.run(
            self.plugin.handle_mc(matched_groups={"sub": "connect", "args": "mc.example.com"}, stream_id="s")
        )
        self.assertEqual(self.bridge.calls[-1], ("connect", {"host": "mc.example.com", "port": 0}))

    def test_command_connect_with_port(self) -> None:
        asyncio.run(
            self.plugin.handle_mc(matched_groups={"sub": "connect", "args": "mc.example.com:25570"}, stream_id="s")
        )
        self.assertEqual(self.bridge.calls[-1], ("connect", {"host": "mc.example.com", "port": 25570}))

    def test_command_reply_is_sent_to_stream(self) -> None:
        result = asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "status", "args": ""}, stream_id="stream-1"))
        self.assertTrue(result[0])
        self.assertIn(("ok:status", "stream-1"), self.plugin.ctx.send.sent)

    def test_command_diag(self) -> None:
        result = asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "diag", "args": ""}, stream_id="s"))
        self.assertTrue(result[0])
        self.assertIn("插件 v", result[1])
        self.assertIn("目标：", result[1])
        self.assertIn("client_state=play", result[1])

    def test_command_disconnect(self) -> None:
        result = asyncio.run(
            self.plugin.handle_mc(matched_groups={"sub": "disconnect", "args": ""}, stream_id="s")
        )
        self.assertTrue(result[0])
        self.assertEqual(self.bridge.calls[-1], ("disconnect", {}))

    def test_command_unknown_subcommand(self) -> None:
        result = asyncio.run(self.plugin.handle_mc(matched_groups={"sub": "nope", "args": ""}, stream_id="s"))
        self.assertFalse(result[0])

    def test_tool_without_bridge(self) -> None:
        self.plugin._bridge = None
        result = asyncio.run(self.plugin.mc_status())
        self.assertFalse(result["success"])
        self.assertIn("没有初始化", result["content"])

    def test_lifecycle(self) -> None:
        async def scenario() -> None:
            instance = plugin_module.MaiBotMinecraftPlugin()
            instance.config.server.auto_connect = False
            await instance.on_load()
            await asyncio.sleep(0.05)
            self.assertIsNotNone(instance._bridge)
            await instance.on_unload()
            self.assertIsNone(instance._bridge)

        asyncio.run(scenario())

    def test_command_admin_guard(self) -> None:
        self.plugin._bot_config = BotConfig(admin_users="qq:111111")
        denied = asyncio.run(
            self.plugin.handle_mc(
                matched_groups={"sub": "status", "args": ""},
                stream_id="s",
                message={"platform": "qq", "user_info": {"user_id": "222222"}},
            )
        )
        self.assertFalse(denied[0])
        self.assertIn("没有权限", denied[1])
        allowed = asyncio.run(
            self.plugin.handle_mc(
                matched_groups={"sub": "status", "args": ""},
                stream_id="s",
                message={"platform": "qq", "user_info": {"user_id": "111111"}},
            )
        )
        self.assertTrue(allowed[0])

    def test_command_admin_guard_empty_allows(self) -> None:
        self.plugin._bot_config = BotConfig(admin_users="")
        result = asyncio.run(
            self.plugin.handle_mc(
                matched_groups={"sub": "status", "args": ""},
                stream_id="s",
                message={"platform": "qq", "user_info": {"user_id": "222222"}},
            )
        )
        self.assertTrue(result[0])

    def test_command_admin_guard_matches_without_platform(self) -> None:
        """配置 qq:ID，但消息里没有 platform 字段时也要能匹配。"""
        self.plugin._bot_config = BotConfig(admin_users="qq:111111")
        result = asyncio.run(
            self.plugin.handle_mc(
                matched_groups={"sub": "status", "args": ""},
                stream_id="s",
                message={"user_info": {"user_id": "111111"}},
            )
        )
        self.assertTrue(result[0])

    def test_command_admin_guard_matches_bare_id(self) -> None:
        self.plugin._bot_config = BotConfig(admin_users="111111")
        result = asyncio.run(
            self.plugin.handle_mc(
                matched_groups={"sub": "status", "args": ""},
                stream_id="s",
                message={"platform": "snowluma", "user_info": {"user_id": "111111"}},
            )
        )
        self.assertTrue(result[0])

    def test_llm_call_disabled(self) -> None:
        self.plugin._bot_config = BotConfig(llm_enabled=False)
        self.assertIsNone(asyncio.run(self.plugin._llm_call("hi")))

    def test_verification_event_forwarded(self) -> None:
        async def scenario() -> None:
            instance = plugin_module.MaiBotMinecraftPlugin()
            fake = FakeBridge()
            instance._bridge = fake  # type: ignore[assignment]
            instance._bot_config = BotConfig(
                username="MaiBot",
                verify_stream_id="qq:999",
                verify_template="[MC] 进服验证码：{code}",
            )
            task = asyncio.create_task(instance._pump_events())
            await fake.events.put(ChatMessage(kind="verification", text="123456", source="验证码：123456"))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertIn(("[MC] 进服验证码：123456", "qq:999"), instance.ctx.send.sent)
            self.assertIsNotNone(instance._last_verification)

            result = await instance.handle_mc(matched_groups={"sub": "verify", "args": ""}, stream_id="s")
            self.assertTrue(result[0])
            self.assertIn(("[MC] 进服验证码：123456", "qq:999"), instance.ctx.send.sent)
            self.assertIn(("已重新发送验证码 123456", "s"), instance.ctx.send.sent)

        asyncio.run(scenario())

    def test_normalize(self) -> None:
        self.assertEqual(
            plugin_module.MaiBotMinecraftPlugin._normalize({"success": True, "message": "好了", "mined": 2}),
            {"success": True, "content": "好了", "message": "好了", "mined": 2},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

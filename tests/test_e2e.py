"""端到端测试：假服务器 + 真客户端 + 玩家控制器。

覆盖登录、区块解析、物理行走、发包、聊天、挖掘、KeepAlive、压缩。
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_server import FakeMinecraftServer  # noqa: E402
from mcbot.auth import offline_session  # noqa: E402
from mcbot.client import ChatMessage, MinecraftClient  # noqa: E402
from mcbot.humanize import Humanizer  # noqa: E402
from mcbot.player import PlayerController  # noqa: E402
from mcbot.types import BlockPos  # noqa: E402


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


class EndToEndBase(unittest.IsolatedAsyncioTestCase):
    compression: int | None = None

    async def asyncSetUp(self) -> None:
        self.server = FakeMinecraftServer(compression=self.compression)
        await self.server.start()
        self.client = MinecraftClient(
            "127.0.0.1",
            self.server.port,
            username="MaiBotTest",
            session=offline_session("MaiBotTest"),
            view_distance=8,
        )
        await asyncio.wait_for(self.client.connect(), timeout=10.0)
        ready = await wait_until(
            lambda: self.client.world.is_column_loaded(0, 0) and self.client.entity_id == 1,
            timeout=8.0,
        )
        self.assertTrue(ready, "客户端没有完成进入世界")
        self.player = PlayerController(self.client, humanizer=Humanizer(rng=random.Random(7)))
        self.player.attach()
        await self.player.start()
        self.assertTrue(await wait_until(lambda: self.player.physics.on_ground, timeout=3.0))

    async def asyncTearDown(self) -> None:
        await self.player.stop()
        await self.client.disconnect()
        await self.server.stop()

    # ------------------------------------------------------------------ 用例
    async def test_login_and_chunks(self) -> None:
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.state, "play")
        self.assertEqual(self.client.entity_id, 1)
        self.assertEqual(self.client.health, 20.0)
        self.assertGreaterEqual(self.client.loaded_chunks, 9)
        self.assertTrue(self.client.world.is_column_loaded(0, 0))
        self.assertEqual(self.client.world.get_state(0, 1, 0), self.client.registry.state_id("grass_block"))
        self.assertAlmostEqual(self.client.position.y, 2.0, delta=0.2)

    async def test_walk_to_target(self) -> None:
        result = await self.player.goto(BlockPos(6, 2, 0), radius=1.0, timeout=25.0)
        self.assertTrue(result.ok, result.message)
        self.assertGreater(self.player.physics.position.x, 4.5)
        self.assertTrue(self.server.positions, "服务器没有收到位置包")
        self.assertGreater(max(position[0] for position in self.server.positions), 3.0)

    async def test_chat_message_reaches_server(self) -> None:
        self.client.send_chat("你好，我是麦麦")
        self.assertTrue(await wait_until(lambda: bool(self.server.chat_messages), timeout=3.0))
        self.assertEqual(self.server.chat_messages[-1], "你好，我是麦麦")

    async def test_receives_system_chat(self) -> None:
        received: list[ChatMessage] = []
        self.client.on("system_chat", received.append)
        self.server.send_system_chat("服务器公告：测试")
        self.assertTrue(await wait_until(lambda: bool(received), timeout=3.0))
        self.assertIn("服务器公告", received[0].text)

    async def test_mine_block(self) -> None:
        target = BlockPos(2, 2, 2)
        self.server._send_block_change(target.x, target.y, target.z, self.client.registry.state_id("stone"))
        self.assertTrue(
            await wait_until(lambda: self.client.world.get_state(target.x, target.y, target.z) != 0, timeout=3.0)
        )
        result = await self.player.mine(target, timeout=6.0)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.client.world.get_state(target.x, target.y, target.z), 0)
        self.assertTrue(any(record.name == "block_dig" for record in self.server.received))

    async def test_keepalive_roundtrip(self) -> None:
        self.assertTrue(await wait_until(lambda: self.server.keepalive_answered >= 1, timeout=4.0))
        self.assertGreaterEqual(self.server.keepalive_sent, 1)
        self.assertLess(self.client.last_packet_age, 5.0)

    async def test_inventory_packet(self) -> None:
        # 服务器主动下发一次背包内容，客户端应能解析
        from mcbot.buffer import BufferWriter

        writer = BufferWriter()
        writer.write_u8(0)  # window id
        writer.write_varint(1)  # state id
        writer.write_varint(46)  # 46 个槽位
        for slot in range(46):
            if slot == 0:
                writer.write_bool(True)
                writer.write_varint(self.client.registry.item_id("stone_pickaxe"))
                writer.write_i8(1)
                writer.write_bool(False)
            else:
                writer.write_bool(False)
        writer.write_bool(False)  # carried item
        self.server._send(self.__class__._window_items_id(), writer.to_bytes())
        self.assertTrue(
            await wait_until(
                lambda: self.client.inventory.get(0) is not None,
                timeout=3.0,
            )
        )
        item = self.client.inventory[0]
        self.assertEqual(item.item_id, self.client.registry.item_id("stone_pickaxe"))
        self.assertEqual(self.client.held_slot, 0)

    @staticmethod
    def _window_items_id() -> int:
        from mcbot.packets import CLIENTBOUND

        return CLIENTBOUND["window_items"]


class EndToEndTests(EndToEndBase):
    compression = None


class EndToEndCompressedTests(EndToEndBase):
    compression = 64

    async def test_login_and_chunks(self) -> None:
        await super().test_login_and_chunks()
        self.client.send_chat("压缩通道测试")
        self.assertTrue(await wait_until(lambda: bool(self.server.chat_messages), timeout=3.0))
        self.assertEqual(self.server.chat_messages[-1], "压缩通道测试")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """桥接层：连接、动作分发、状态、停止。"""

    async def test_bridge_lifecycle_and_actions(self) -> None:
        from mcbot.bridge import MinecraftBridge
        from mcbot.config import BotConfig

        server = FakeMinecraftServer()
        await server.start()
        bridge = MinecraftBridge(
            BotConfig(
                host="127.0.0.1",
                port=server.port,
                username="BridgeBot",
                autonomous=False,
                auto_connect=False,
            )
        )
        try:
            result = await bridge.connect()
            self.assertTrue(result.ok, result.message)
            self.assertIsNotNone(bridge.player)
            ready = await wait_until(
                lambda: bool(bridge.player and bridge.player.joined and bridge.client and bridge.client.world.is_column_loaded(0, 0)),
                timeout=8.0,
            )
            self.assertTrue(ready)

            status = bridge.status()
            self.assertTrue(status["connected"])
            self.assertEqual(status["username"], "BridgeBot")

            payload = await bridge.execute("say", text="来自 bridge 的消息")
            self.assertTrue(payload["success"], payload)
            self.assertTrue(await wait_until(lambda: bool(server.chat_messages), timeout=3.0))
            self.assertEqual(server.chat_messages[-1], "来自 bridge 的消息")

            payload = await bridge.execute("status")
            self.assertTrue(payload["success"])
            self.assertIn("BridgeBot", payload["content"])

            payload = await bridge.execute("inventory")
            self.assertTrue(payload["success"])
        finally:
            await bridge.stop()
            await server.stop()

    async def test_bridge_captures_verification_code(self) -> None:
        """加载界面的标题验证码要被识别并放进事件队列。"""
        from mcbot.bridge import MinecraftBridge
        from mcbot.config import BotConfig

        server = FakeMinecraftServer()
        await server.start()
        bridge = MinecraftBridge(
            BotConfig(host="127.0.0.1", port=server.port, username="BridgeBot", auto_connect=False)
        )
        try:
            result = await bridge.connect()
            self.assertTrue(result.ok, result.message)
            message = None
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                try:
                    item = bridge.events.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)
                    continue
                if item.kind == "verification":
                    message = item
                    break
            self.assertIsNotNone(message, "没有捕获到验证码")
            assert message is not None
            self.assertEqual(message.text, "654321")
            self.assertIn("验证码", message.source)
        finally:
            await bridge.stop()
            await server.stop()

    async def test_bridge_rejects_unsupported_version(self) -> None:
        """未适配的协议应明确拒绝，不能误用旧协议或推荐代理。"""
        from mcbot.bridge import MinecraftBridge
        from mcbot.config import BotConfig

        server = FakeMinecraftServer()
        await server.start()
        server.version_protocol = 777
        server.version_name = "26.3"
        bridge = MinecraftBridge(
            BotConfig(host="127.0.0.1", port=server.port, username="BridgeBot", auto_connect=False)
        )
        try:
            result = await bridge.connect()
            self.assertFalse(result.ok)
            self.assertIn("777", result.message)
            self.assertIn("26.2", result.message)
        finally:
            await bridge.stop()
            await server.stop()


class BridgeRuleTests(unittest.TestCase):
    """自然语言 → 动作的规则解析（不需要服务器）。"""

    def setUp(self) -> None:
        from mcbot.bridge import MinecraftBridge
        from mcbot.config import BotConfig

        self.bridge = MinecraftBridge(BotConfig(), logger=lambda message: None)

    def test_mine_with_chinese_count(self) -> None:
        self.assertEqual(self.bridge._plan_with_rules("去挖五个木头"), ("mine", {"block": "oak_log", "count": 5}))
        self.assertEqual(self.bridge._plan_with_rules("挖 3 块石头"), ("mine", {"block": "stone", "count": 3}))

    def test_goto_coordinates(self) -> None:
        self.assertEqual(
            self.bridge._plan_with_rules("去 100 64 -20"),
            ("goto", {"x": 100.0, "y": 64.0, "z": -20.0}),
        )

    def test_follow_and_say(self) -> None:
        action, kwargs = self.bridge._plan_with_rules("跟着小明")
        self.assertEqual(action, "follow")
        self.assertEqual(kwargs["target"], "小明")
        self.assertEqual(self.bridge._plan_with_rules("说 我回来了"), ("say", {"text": "我回来了"}))

    def test_place_with_coordinates(self) -> None:
        self.assertEqual(
            self.bridge._plan_with_rules("把石头放在 1 2 3"),
            ("place", {"block": "stone", "x": 1.0, "y": 2.0, "z": 3.0}),
        )

    def test_unknown_instruction(self) -> None:
        self.assertIsNone(self.bridge._plan_with_rules("今天天气不错"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

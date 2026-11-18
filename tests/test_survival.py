"""生存类行为回归：水里持续上浮/上岸、被树叶包住的树也能砍、大脑优先自救。"""
from __future__ import annotations

import asyncio
import unittest

from mcbot.brain import Brain
from mcbot.client import MinecraftClient
from mcbot.config import BotConfig
from mcbot.humanize import Humanizer, HumanProfile
from mcbot.physics import MovementInput, PlayerPhysics
from mcbot.player import PlayerController
from mcbot.registry import registry as default_registry
from mcbot.types import BlockPos, Vec3
from mcbot.world import SimpleWorld


class FakeClient(MinecraftClient):
    """不联网的客户端：dig 直接改世界（模拟服务器确认破坏）。"""

    def __init__(self, world: SimpleWorld) -> None:
        super().__init__("127.0.0.1", 25565, registry=world.registry, logger=lambda _message: None)
        self.world = world
        self.state = "play"
        self.position_received = True
        self.food = 20
        self.health = 20.0
        self.frames: list[tuple[int, bytes]] = []

    def _write_frame(self, packet_id: int, payload: bytes = b"") -> None:
        self.frames.append((packet_id, payload))

    def dig(self, status: int, position: BlockPos, face: int = 1) -> int:
        if status == 2:
            self.world.set_state(position.x, position.y, position.z, 0)
        return 0

    def set_held_slot(self, slot: int) -> None:
        self.held_slot = slot

    def swing_arm(self, hand: int = 0) -> None:
        pass

    def send_position(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def send_rotation(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


FAST_HUMANIZER = Humanizer(profile=HumanProfile(reaction_base=0.01, reaction_min=0.01, reaction_max=0.02, aim_max_speed=180.0))


def build_world(reg) -> SimpleWorld:
    world = SimpleWorld(reg)
    world.min_y = -64
    world.height = 384
    return world


def make_player(world: SimpleWorld, position: Vec3) -> tuple[FakeClient, PlayerController]:
    client = FakeClient(world)
    client.position = position
    player = PlayerController(client, humanizer=FAST_HUMANIZER, logger=lambda _message: None)
    player.attach()
    player.break_ticks = lambda position: 1  # 测试不关心真实破坏耗时  # noqa: ARG005
    return client, player


class WaterPhysicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_registry()
        self.world = build_world(self.registry)
        stone = self.registry.state_id("stone")
        water = self.registry.state_id("water")
        for x in range(-4, 11):
            for z in range(-4, 11):
                self.world.set_state(x, 0, z, stone)
        for x in range(2, 7):
            for z in range(2, 7):
                for y in range(1, 4):
                    self.world.set_state(x, y, z, water)

    def _rise(self, ticks: int, hold_jump: bool) -> float:
        physics = PlayerPhysics(self.world, Vec3(4.5, 1.0, 4.5))
        start = physics.position.y
        for _ in range(ticks):
            physics.tick(MovementInput(jump=hold_jump))
        return physics.position.y - start

    def test_continuous_jump_rises_like_swimming(self) -> None:
        """一直按住跳跃（原版游泳上浮）要明显持续上升。"""
        self.assertGreater(self._rise(12, hold_jump=True), 1.2)

    def test_single_jump_sinks_back(self) -> None:
        """只按一下只能浮起一点点，之后会被重力压回去。"""
        self.assertLess(self._rise(12, hold_jump=False), 0.8)

    def test_holding_jump_beats_single_jump(self) -> None:
        self.assertGreater(
            self._rise(20, hold_jump=True) - self._rise(20, hold_jump=False),
            1.5,
        )


class SwimToLandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.registry = default_registry()
        self.world = build_world(self.registry)
        stone = self.registry.state_id("stone")
        water = self.registry.state_id("water")
        for x in range(-4, 11):
            for z in range(-4, 11):
                self.world.set_state(x, 0, z, stone)
        for x in range(2, 7):
            for z in range(2, 7):
                for y in range(1, 4):
                    self.world.set_state(x, y, z, water)
        self.client, self.player = make_player(self.world, Vec3(4.5, 1.0, 4.5))
        await self.player.start()

    async def asyncTearDown(self) -> None:
        await self.player.stop()

    async def test_swims_up_and_reaches_shore(self) -> None:
        await asyncio.sleep(0.3)  # 让物理循环先跑几帧，进入水中
        self.assertTrue(self.player.physics.in_water)
        result = await asyncio.wait_for(self.player.swim_to_land(timeout=20.0), timeout=30.0)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(self.player.physics.in_water)
        self.assertTrue(self.player.physics.on_ground)
        # 上岸后应该真的站到了水池外面
        self.assertTrue(
            self.player.physics.position.x < 2.0
            or self.player.physics.position.x > 7.0
            or self.player.physics.position.z < 2.0
            or self.player.physics.position.z > 7.0,
            self.player.physics.position,
        )

    async def test_brain_prioritises_escaping_water(self) -> None:
        await asyncio.sleep(0.3)
        brain = Brain(self.client, self.player, BotConfig(), FAST_HUMANIZER, logger=lambda _m: None)
        activity = await brain._choose_activity()
        self.assertEqual(getattr(activity, "__name__", ""), "_swim_out")


class ChopTreeTests(unittest.IsolatedAsyncioTestCase):
    """树被树叶包住时也要能砍到（用户反馈的实际问题）。"""

    async def _start(self, shell: int) -> None:
        self.registry = default_registry()
        self.world = build_world(self.registry)
        grass = self.registry.state_id("grass_block")
        log = self.registry.state_id("oak_log")
        leaves = self.registry.state_id("oak_leaves")
        span = 12 + shell
        for x in range(-span, span + 1):
            for z in range(-span, span + 1):
                self.world.set_state(x, 0, z, grass)
        # 树干：(0, 1..4, 0)；shell 是叶子外壳厚度（0 = 没有叶子）
        for y in range(1, 5):
            self.world.set_state(0, y, 0, log)
        if shell:
            for y in range(1, 5 + shell):
                for dx in range(-shell, shell + 1):
                    for dz in range(-shell, shell + 1):
                        if dx == 0 and dz == 0 and y <= 4:
                            continue
                        self.world.set_state(dx, y, dz, leaves)
        start_x = float(shell + 2) + 0.5
        self.client, self.player = make_player(self.world, Vec3(start_x, 1.0, 0.5))
        self.shell = shell
        await self.player.start()

    async def asyncTearDown(self) -> None:
        await self.player.stop()

    async def _chop(self) -> None:
        result = await asyncio.wait_for(
            self.player.chop_tree(radius=20, max_logs=8, timeout=90.0), timeout=150.0
        )
        self.assertTrue(result.ok, result.message)
        for y in range(1, 5):
            self.assertEqual(self.world.get_state(0, y, 0), 0, f"原木 y={y} 应该已被砍掉")

    async def test_chops_tree_with_thin_leaf_shell(self) -> None:
        await self._start(shell=1)
        await self._chop()

    async def test_chops_tree_buried_inside_thick_leaves(self) -> None:
        """4 格厚的树叶把树干完全包住：必须先扒开树叶再砍。"""
        await self._start(shell=4)
        result = await asyncio.wait_for(
            self.player.chop_tree(radius=24, max_logs=8, timeout=120.0), timeout=180.0
        )
        self.assertTrue(result.ok, result.message)
        for y in range(1, 5):
            self.assertEqual(self.world.get_state(0, y, 0), 0, f"原木 y={y} 应该已被砍掉")

    async def test_chop_reports_no_tree_in_empty_plane(self) -> None:
        await self._start(shell=0)
        for y in range(1, 5):
            self.world.set_state(0, y, 0, 0)  # 把唯一的树也删掉
        result = await self.player.chop_tree(radius=20, max_logs=4, timeout=10.0)
        self.assertFalse(result.ok)
        self.assertEqual(int(result.data.get("mined", 0)), 0)


if __name__ == "__main__":
    unittest.main()

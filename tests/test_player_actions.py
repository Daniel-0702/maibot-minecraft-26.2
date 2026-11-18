"""新增"类人动作"的玩家层测试：下挖、避难所、垫高、装备、观察、家。"""
from __future__ import annotations

import asyncio
import unittest

from mcbot.client import ItemStack, MinecraftClient
from mcbot.humanize import Humanizer, HumanProfile
from mcbot.player import PlayerController
from mcbot.registry import registry as default_registry
from mcbot.types import BlockPos, Vec3
from mcbot.world import SimpleWorld

FAST = Humanizer(profile=HumanProfile(reaction_base=0.01, reaction_min=0.01, reaction_max=0.02, aim_max_speed=180.0))


class StubClient(MinecraftClient):
    def __init__(self, world: SimpleWorld) -> None:
        super().__init__("127.0.0.1", 25565, registry=world.registry, logger=lambda _m: None)
        self.world = world
        self.state = "play"
        self.position_received = True
        self.frames: list[tuple[int, bytes]] = []

    def _write_frame(self, packet_id: int, payload: bytes = b"") -> None:
        self.frames.append((packet_id, payload))

    def dig(self, status: int, position: BlockPos, face: int = 1) -> int:
        if status == 2:
            self.world.set_state(position.x, position.y, position.z, 0)
        return 0

    def set_held_slot(self, slot: int) -> None:
        self.held_slot = slot

    def place_block(self, position: BlockPos, face: int, cursor=(0.5, 0.5, 0.5)) -> int:
        """模拟服务器放置：按点击面把方块放到相邻位置。"""
        offsets = {0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1), 3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0)}
        dx, dy, dz = offsets.get(face, (0, 1, 0))
        target = (position.x + dx, position.y + dy, position.z + dz)
        if self.world.get_state(*target) == 0:
            held = self.selected_item()
            info = self.registry.item(held.item_id) if held is not None and not held.is_empty else None
            name = info.short_name if info is not None else ""
            try:
                self.world.set_state(*target, self.registry.state_id(name))
            except KeyError:
                self.world.set_state(*target, 1)
        return 0

    def swing_arm(self, hand: int = 0) -> None:
        pass

    def send_position(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def send_rotation(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


def make_world(registry) -> SimpleWorld:
    world = SimpleWorld(registry)
    world.min_y = -64
    world.height = 384
    for x in range(-8, 9):
        for z in range(-8, 9):
            world.set_state(x, 0, z, registry.state_id("stone"))
    return world


def make_player(world: SimpleWorld, position: Vec3, yaw: float = 0.0):
    client = StubClient(world)
    client.position = position
    client.yaw = yaw
    player = PlayerController(client, humanizer=FAST, logger=lambda _m: None)
    player.attach()
    player.break_ticks = lambda _pos: 1
    player.physics.on_ground = True
    return client, player


class DigAndBuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_dig_down_mines_front_and_below(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        # 面朝 +z（yaw=0），前方 1 格有石头柱，需要挖两格再前进
        for y in (1, 2):
            world.set_state(0, y, 1, registry.state_id("stone"))
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5), yaw=0.0)
        await player.start()
        try:
            result = await asyncio.wait_for(player.dig_down(2), timeout=40)
        finally:
            await player.stop()
        self.assertTrue(result.ok, result.message)
        self.assertGreaterEqual(int(result.data.get("mined", 0)), 2)
        # 挖过的位置应该空了（说明真的调用了破坏）
        self.assertEqual(world.get_state(0, 1, 1), 0)

    async def test_dig_shelter_into_wall(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        # 在 +x 方向堆一堵 3x3 的石墙
        for y in (1, 2, 3):
            for z in (-1, 0, 1):
                world.set_state(1, y, z, registry.state_id("stone"))
                world.set_state(2, y, z, registry.state_id("stone"))
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        await player.start()
        try:
            result = await asyncio.wait_for(player.dig_shelter(2), timeout=40)
        finally:
            await player.stop()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(world.get_state(1, 1, 0), 0)

    async def test_pillar_needs_blocks(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        result = await player.pillar_up(2)
        self.assertFalse(result.ok)
        self.assertIn("方块", result.message)

    async def test_bridge_needs_blocks(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        result = await player.bridge(0.0, 2)
        self.assertFalse(result.ok)


class ItemActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_equip_finds_item_in_hotbar(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        torch = registry.item_id("torch")
        client.inventory[0] = ItemStack(torch, 3)  # 763 布局：0..8 是快捷栏
        result = await player.equip_item("torch")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(client.held_slot, 0)

    async def test_equip_missing_item(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        result = await player.equip_item("diamond_sword")
        self.assertFalse(result.ok)
        self.assertIn("没有", result.message)

    async def test_drop_item_sends_action(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        client.inventory[0] = ItemStack(registry.item_id("cobblestone"), 5)
        result = await player.drop_item("cobblestone")
        self.assertTrue(result.ok, result.message)

    async def test_place_torch_uses_inventory_torch(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        client.inventory[0] = ItemStack(registry.item_id("torch"), 4)
        client.inventory[1] = ItemStack(registry.item_id("oak_planks"), 8)
        result = await player.place_torch()
        self.assertTrue(result.ok, result.message)


class InfoAndHomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_nearby_summary(self) -> None:
        from mcbot.client import Entity

        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        client.entities[1] = Entity(1, "minecraft:zombie", None, Vec3(2.5, 1.0, 0.5))
        client.entities[2] = Entity(2, "minecraft:cow", None, Vec3(3.5, 1.0, 0.5))
        world.set_state(3, 0, 3, registry.state_id("water"))
        summary = player.nearby_summary(8.0)
        self.assertEqual(summary["entities"].get("zombie"), 1)
        self.assertEqual(summary["entities"].get("cow"), 1)

    async def test_sethome_and_go_home(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        self.assertTrue(player.sethome().ok)
        self.assertIsNotNone(player.home)
        result = await asyncio.wait_for(player.go_home(), timeout=10)
        self.assertTrue(result.ok, result.message)

    async def test_follow_player_by_name(self) -> None:
        from mcbot.client import Entity, PlayerInfo
        import uuid as uuid_lib

        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        identity = uuid_lib.uuid4()
        client.players[identity] = PlayerInfo(identity, "小明")
        client.entities[7] = Entity(7, "minecraft:player", identity, Vec3(3.5, 1.0, 0.5))
        result = await asyncio.wait_for(player.follow_player("小明", timeout=0.4), timeout=10)
        # 跟随超时也算执行过（不是"找不到玩家"）
        self.assertNotIn("没有叫", result.message)

    async def test_follow_unknown_player(self) -> None:
        registry = default_registry()
        world = make_world(registry)
        client, player = make_player(world, Vec3(0.5, 1.0, 0.5))
        result = await player.follow_player("不存在的人")
        self.assertFalse(result.ok)
        self.assertIn("没有叫", result.message)


if __name__ == "__main__":
    unittest.main()

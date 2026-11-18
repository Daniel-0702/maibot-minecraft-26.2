"""Wiki 知识表与接入逻辑的测试（数值对照 Minecraft Wiki 的公式）。"""
from __future__ import annotations

import asyncio
import unittest

from mcbot import knowledge
from mcbot.brain import Brain
from mcbot.client import ItemStack, MinecraftClient
from mcbot.config import BotConfig
from mcbot.humanize import Humanizer, HumanProfile
from mcbot.player import PlayerController
from mcbot.registry import registry as default_registry
from mcbot.types import BlockPos, Vec3
from mcbot.world import SimpleWorld

FAST = Humanizer(profile=HumanProfile(reaction_base=0.01, reaction_min=0.01, reaction_max=0.02))


class StubClient(MinecraftClient):
    def __init__(self, world: SimpleWorld) -> None:
        super().__init__("127.0.0.1", 25565, registry=world.registry, logger=lambda _m: None)
        self.world = world
        self.state = "play"
        self.position_received = True
        self.frames: list[tuple[int, bytes]] = []

    def _write_frame(self, packet_id: int, payload: bytes = b"") -> None:
        self.frames.append((packet_id, payload))

    def send_position(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    def send_rotation(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass


class MiningKnowledgeTests(unittest.TestCase):
    """对照 Wiki「Breaking」的挖掘时间。"""

    def test_stone_by_hand_is_7_5_seconds(self) -> None:
        # 石头硬度 1.5：空手不能收获 → 1.5 × 5 秒
        self.assertEqual(knowledge.mining_ticks(1.5, 1.0, can_harvest=False), 150)

    def test_stone_with_iron_pickaxe_is_0_4_seconds(self) -> None:
        self.assertEqual(knowledge.mining_ticks(1.5, 6.0), 8)

    def test_log_by_hand_is_3_seconds(self) -> None:
        self.assertEqual(knowledge.mining_ticks(2.0, 1.0), 60)

    def test_dirt_with_diamond_shovel(self) -> None:
        self.assertEqual(knowledge.mining_ticks(0.5, 8.0), 2)

    def test_copper_tier_exists(self) -> None:
        self.assertEqual(knowledge.TOOL_SPEED_BY_TIER["copper"], 5.0)

    def test_gold_is_fastest(self) -> None:
        self.assertEqual(knowledge.TOOL_SPEED_BY_TIER["golden"], 12.0)

    def test_water_penalty_quintuples(self) -> None:
        dry = knowledge.mining_ticks(1.5, 6.0)
        wet = knowledge.mining_ticks(1.5, 6.0, submerged=True)
        assert dry is not None and wet is not None
        self.assertAlmostEqual(wet / dry, 4.75, delta=1.0)
        self.assertGreater(wet, dry * 3)

    def test_airborne_penalty(self) -> None:
        grounded = knowledge.mining_ticks(1.5, 6.0)
        airborne = knowledge.mining_ticks(1.5, 6.0, on_ground=False)
        assert grounded is not None and airborne is not None
        self.assertGreater(airborne, grounded * 3)

    def test_instant_and_unbreakable(self) -> None:
        self.assertEqual(knowledge.mining_ticks(0.0), 0)
        self.assertIsNone(knowledge.mining_ticks(-1.0))
        self.assertTrue(knowledge.instant_break(0.0))

    def test_efficiency_adds_level_squared_plus_one(self) -> None:
        base = knowledge.mining_ticks(1.5, 6.0)
        eff2 = knowledge.mining_ticks(1.5, 6.0, efficiency_level=2)
        assert base is not None and eff2 is not None
        self.assertLess(eff2, base)

    def test_seconds_conversion(self) -> None:
        self.assertAlmostEqual(knowledge.mining_seconds(1.5, 6.0), 0.4)


class FallDamageTests(unittest.TestCase):
    """Wiki「Damage#Fall damage」：摔落格数 - 3，向上取整到整数伤害。"""

    def test_safe_three_blocks(self) -> None:
        self.assertEqual(knowledge.fall_damage(3), 0)
        self.assertTrue(knowledge.fall_is_safe(3.0))

    def test_four_blocks_one_damage(self) -> None:
        self.assertEqual(knowledge.fall_damage(4), 1)

    def test_23_blocks_is_fatal_at_full_health(self) -> None:
        self.assertEqual(knowledge.fall_damage(23), 20)


class DrowningTests(unittest.TestCase):
    def test_air_supply_is_15_seconds(self) -> None:
        self.assertEqual(knowledge.DROWNING_AIR_TICKS, 300)
        self.assertAlmostEqual(knowledge.submerged_seconds_until_damage(), 15.0)

    def test_damage_rate(self) -> None:
        self.assertEqual(knowledge.DROWNING_DAMAGE_HP, 2)
        self.assertEqual(knowledge.DROWNING_DAMAGE_INTERVAL_TICKS, 20)


class HungerTests(unittest.TestCase):
    def test_sprint_needs_more_than_six(self) -> None:
        self.assertFalse(knowledge.sprint_allowed(6))
        self.assertTrue(knowledge.sprint_allowed(7))

    def test_natural_regen_needs_18(self) -> None:
        # Wiki「Healing」：饥饿 ≥ 18 才会自然回血
        self.assertFalse(knowledge.natural_regen_allowed(17))
        self.assertTrue(knowledge.natural_regen_allowed(18))


class AttackKnowledgeTests(unittest.TestCase):
    def test_attack_cooldowns(self) -> None:
        self.assertAlmostEqual(knowledge.attack_cooldown_seconds("iron_sword"), 0.625)
        self.assertAlmostEqual(knowledge.attack_cooldown_seconds(""), 0.25)
        self.assertGreater(knowledge.attack_cooldown_seconds("iron_axe"), 1.0)

    def test_damage_factor(self) -> None:
        # Wiki 公式里多算半 tick，所以刚点完不是精确的 0.2
        self.assertAlmostEqual(knowledge.attack_damage_factor(0, "iron_sword"), 0.2, delta=0.01)
        self.assertAlmostEqual(knowledge.attack_damage_factor(13, "iron_sword"), 1.0, places=3)
        middle = knowledge.attack_damage_factor(6, "iron_sword")
        self.assertTrue(0.2 < middle < 1.0)

    def test_critical_multiplier(self) -> None:
        self.assertEqual(knowledge.CRITICAL_MULTIPLIER, 1.5)


class MobKnowledgeTests(unittest.TestCase):
    def test_creeper_explodes(self) -> None:
        profile = knowledge.mob_profile("creeper")
        assert profile is not None
        self.assertTrue(profile.explosive)
        self.assertGreaterEqual(knowledge.explosive_blast_radius("creeper"), 3.0)
        self.assertAlmostEqual(knowledge.CREEPER_FUSE_SECONDS, 1.5)

    def test_spider_is_daylight_neutral(self) -> None:
        self.assertTrue(knowledge.is_daylight_neutral("spider"))

    def test_enderman_is_gaze_provoked(self) -> None:
        profile = knowledge.mob_profile("enderman")
        assert profile is not None
        self.assertFalse(profile.hostile)
        self.assertTrue(profile.gaze_provoked)
        self.assertLess(knowledge.enderman_safe_aim_height(), 1.0)

    def test_zombie_burns_in_daylight(self) -> None:
        self.assertTrue(knowledge.burns_in_daylight("zombie"))
        self.assertTrue(knowledge.burns_in_daylight("skeleton"))
        self.assertFalse(knowledge.burns_in_daylight("creeper"))

    def test_unknown_mob(self) -> None:
        self.assertIsNone(knowledge.mob_profile("not_a_mob"))


class PlayerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _player(self) -> tuple[StubClient, PlayerController, SimpleWorld]:
        reg = default_registry()
        world = SimpleWorld(reg)
        world.min_y = -64
        world.height = 384
        for x in range(-4, 5):
            for z in range(-4, 5):
                world.set_state(x, 0, z, reg.state_id("stone"))
        world.set_state(0, 1, 0, reg.state_id("stone"))
        client = StubClient(world)
        client.position = Vec3(0.5, 1.0, 2.5)
        player = PlayerController(client, humanizer=FAST, logger=lambda _m: None)
        player.attach()
        player.physics.on_ground = True
        return client, player, world

    def test_break_ticks_matches_wiki_by_hand(self) -> None:
        _client, player, _world = self._player()
        self.assertEqual(player.break_ticks(BlockPos(0, 1, 0)), 150)

    def test_break_ticks_uses_best_tool(self) -> None:
        client, player, _world = self._player()
        pickaxe = client.registry.item_id("iron_pickaxe")
        assert pickaxe is not None
        client.inventory[0] = ItemStack(pickaxe, 1)
        player.physics.on_ground = True
        self.assertEqual(player.break_ticks(BlockPos(0, 1, 0)), 8)

    def test_break_ticks_underwater_penalty(self) -> None:
        client, player, _world = self._player()
        pickaxe = client.registry.item_id("iron_pickaxe")
        assert pickaxe is not None
        client.inventory[0] = ItemStack(pickaxe, 1)
        player.physics.head_in_water = True
        self.assertEqual(player.break_ticks(BlockPos(0, 1, 0)), 38)

    def test_break_ticks_airborne_penalty(self) -> None:
        client, player, _world = self._player()
        pickaxe = client.registry.item_id("iron_pickaxe")
        assert pickaxe is not None
        client.inventory[0] = ItemStack(pickaxe, 1)
        player.physics.on_ground = False
        self.assertEqual(player.break_ticks(BlockPos(0, 1, 0)), 38)

    async def test_enderman_aims_low_to_avoid_gaze(self) -> None:
        client, player, _world = self._player()
        from mcbot.client import Entity

        client.entities[7] = Entity(7, "minecraft:enderman", None, Vec3(2.5, 1.0, 2.5))
        aimed: list[Vec3] = []

        async def fake_look_at(target: Vec3, **_kwargs) -> object:  # noqa: ANN001
            aimed.append(target)
            from mcbot.player import ActionResult

            return ActionResult(True, "ok")

        player.look_at = fake_look_at  # type: ignore[assignment]
        await player.look_at_entity(7)
        self.assertTrue(aimed)
        # 瞄脚/躯干，而不是 1.4 高的头
        self.assertAlmostEqual(aimed[0].y, 1.6, places=3)

    def test_can_harvest_needs_right_tool(self) -> None:
        client, player, _world = self._player()
        self.assertFalse(player.can_harvest(BlockPos(0, 1, 0)))  # 石头：徒手没有掉落
        pickaxe = client.registry.item_id("iron_pickaxe")
        assert pickaxe is not None
        client.inventory[0] = ItemStack(pickaxe, 1)
        self.assertTrue(player.can_harvest(BlockPos(0, 1, 0)))

    def test_can_harvest_log_by_hand(self) -> None:
        _client, player, world = self._player()
        world.set_state(0, 2, 0, default_registry().state_id("oak_log"))
        self.assertTrue(player.can_harvest(BlockPos(0, 2, 0)))


class CliffProtectionTests(unittest.IsolatedAsyncioTestCase):
    """Wiki：摔落超过 3 格会掉血，所以走到悬崖边要停住。"""

    async def asyncSetUp(self) -> None:
        reg = default_registry()
        world = SimpleWorld(reg)
        world.min_y = -64
        world.height = 384
        # 只有一小块地面，往外就是深渊
        for x in range(-2, 3):
            for z in range(-2, 3):
                world.set_state(x, 0, z, reg.state_id("stone"))
        self.world = world
        client = StubClient(world)
        client.position = Vec3(0.5, 1.0, 0.5)
        self.player = PlayerController(client, humanizer=FAST, logger=lambda _m: None)
        self.player.attach()
        self.player.physics.on_ground = True
        await self.player.start()

    async def asyncTearDown(self) -> None:
        await self.player.stop()

    async def test_walk_to_stops_at_cliff(self) -> None:
        result = await asyncio.wait_for(
            self.player.walk_to(Vec3(20.5, 1.0, 0.5), radius=0.5, timeout=8.0), timeout=20.0
        )
        self.assertFalse(result.ok)
        self.assertIn("落差", result.message)
        # 没有掉下深渊（深渊会把 y 拉到 min_y）
        self.assertGreater(self.player.physics.position.y, -1.0)
        self.assertLess(abs(self.player.physics.position.x), 4.0)


class BrainKnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def _brain(self, day: bool = True, y: float = 70.0):
        reg = default_registry()
        world = SimpleWorld(reg)
        for x in range(-4, 5):
            for z in range(-4, 5):
                world.set_state(x, 0, z, reg.state_id("stone"))
        client = StubClient(world)
        client.position = Vec3(0.5, y, 0.5)
        client.world.day_time = 6000 if day else 18000
        client.health = 20.0
        client.food = 20
        player = PlayerController(client, humanizer=FAST, logger=lambda _m: None)
        player.attach()
        player.physics.on_ground = True
        brain = Brain(client, player, BotConfig(), FAST, logger=lambda _m: None)
        return client, player, brain

    def _spawn(self, client: StubClient, entity_id: int, kind: str) -> None:
        from mcbot.client import Entity

        client.entities[entity_id] = Entity(entity_id, f"minecraft:{kind}", None, Vec3(2.5, 70.0, 0.5))

    def test_spider_is_not_a_threat_in_daylight(self) -> None:
        client, _player, brain = self._brain(day=True)
        self._spawn(client, 1, "spider")
        entity = client.entities[1]
        self.assertFalse(brain._is_threat(entity))

    def test_spider_is_a_threat_at_night(self) -> None:
        client, _player, brain = self._brain(day=False)
        self._spawn(client, 1, "spider")
        self.assertTrue(brain._is_threat(client.entities[1]))

    def test_enderman_is_not_treated_as_threat(self) -> None:
        client, _player, brain = self._brain()
        self._spawn(client, 2, "enderman")
        entity = client.entities[2]
        self.assertFalse(brain._is_threat(entity))
        self.assertFalse(brain._should_fight(entity))

    async def test_creeper_close_forces_flee(self) -> None:
        client, _player, brain = self._brain()
        self._spawn(client, 3, "creeper")
        activity = await brain._choose_activity()
        self.assertEqual(getattr(activity, "__name__", ""), "_flee")

    async def test_injured_and_hungry_eats_to_regen(self) -> None:
        client, _player, brain = self._brain()
        client.health = 12.0
        client.food = 15
        bread = client.registry.item_id("bread")
        assert bread is not None
        client.inventory[0] = ItemStack(bread, 3)
        activity = await brain._choose_activity()
        self.assertEqual(getattr(activity, "__name__", ""), "_eat")


if __name__ == "__main__":
    unittest.main()

"""Pathfinder / Goal / Path 的单元测试（unittest，不依赖 pytest）。"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcbot.pathfinding import GoalAny, GoalBlock, GoalNear, GoalXZ, Path, Pathfinder
from mcbot.registry import registry
from mcbot.types import BlockPos, Vec3
from mcbot.world import SimpleWorld

STONE = registry().state_id("stone")
LAVA = registry().state_id("lava")
WATER = registry().state_id("water")
SLAB = registry().state_id("oak_slab")


def stone_floor(x0: int, z0: int, x1: int, z1: int, y: int = 0) -> SimpleWorld:
    """造一块从 (x0,z0) 到 (x1,z1) 的石制地板。"""
    world = SimpleWorld()
    world.fill_box(BlockPos(x0, y, z0), BlockPos(x1, y, z1), STONE)
    return world


class PathTestCase(unittest.TestCase):
    """公共断言：路径必须连续、落点合法。"""

    def assert_valid_path(self, world: SimpleWorld, finder: Pathfinder, path: Path | None) -> Path:
        self.assertIsNotNone(path)
        assert path is not None
        self.assertGreaterEqual(path.length, 1)
        for node in path.nodes:
            self.assertTrue(world.is_loaded(node.x, node.z), f"未加载区块: {node}")
            self.assertTrue(finder.can_stand(node), f"不可站立的节点: {node}")
        for first, second in zip(path.nodes, path.nodes[1:]):
            self.assertLessEqual(abs(first.x - second.x), 1, f"{first} -> {second}")
            self.assertLessEqual(abs(first.z - second.z), 1, f"{first} -> {second}")
            self.assertGreaterEqual(second.y - first.y, -3, f"{first} -> {second}")
            self.assertLessEqual(second.y - first.y, 2, f"{first} -> {second}")
        return path


class FlatPathTest(PathTestCase):
    """平地寻路。"""

    def test_flat_path(self) -> None:
        world = stone_floor(-20, -20, 19, 19)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(10, 1, 10), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[0], BlockPos(0, 1, 0))
        self.assertEqual(path.nodes[-1], BlockPos(10, 1, 10))
        self.assertLessEqual(path.length, 24)
        self.assertEqual(path.length, 11)  # 10 个斜步

    def test_start_from_blockpos(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        path = finder.find_path(BlockPos(0, 1, 0), GoalBlock(BlockPos(3, 1, 0), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[0], BlockPos(0, 1, 0))

    def test_start_already_at_goal(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(0, 1, 0), radius=0))
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.length, 1)

    def test_midair_start_falls_back(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 2.5, 0.5), GoalBlock(BlockPos(3, 1, 3), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[0], BlockPos(0, 1, 0))

    def test_sunk_start_falls_back(self) -> None:
        """脚陷进地面一点点时也能规整到上方节点。"""
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 0.98, 0.5), GoalBlock(BlockPos(3, 1, 3), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[0], BlockPos(0, 1, 0))

    def test_no_standable_start(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        self.assertIsNone(finder.find_path(Vec3(0.5, 6.0, 0.5), GoalBlock(BlockPos(3, 1, 3))))

    def test_goal_xz_path(self) -> None:
        world = stone_floor(-20, -20, 19, 19)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalXZ(5, 5, radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual((path.nodes[-1].x, path.nodes[-1].z), (5, 5))


class ObstacleTest(PathTestCase):
    """障碍物绕行。"""

    def test_detour_around_wall(self) -> None:
        world = stone_floor(-20, -20, 19, 19)
        world.fill_box(BlockPos(5, 1, -4), BlockPos(5, 3, 12), STONE)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(10, 1, 10), radius=0))
        path = self.assert_valid_path(world, finder, path)
        for node in path.nodes:
            self.assertFalse(node.x == 5 and -4 <= node.z <= 12, f"穿过墙: {node}")
        max_z = max(node.z for node in path.nodes)
        min_z = min(node.z for node in path.nodes)
        self.assertTrue(max_z > 12 or min_z < -4, "没有绕开墙")

    def test_one_block_ledge(self) -> None:
        world = stone_floor(-3, -3, 2, 3)  # 低地，站在 y=1
        world.fill_box(BlockPos(3, 0, -3), BlockPos(8, 1, 3), STONE)  # 高台，站在 y=2
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(6, 2, 0), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[0].y, 1)
        self.assertEqual(max(node.y for node in path.nodes), 2)
        self.assertEqual(path.nodes[-1], BlockPos(6, 2, 0))

    def test_wall_of_three_is_not_jumped(self) -> None:
        """3 格高的墙不能直接跳过（max_jump_height=1）。"""
        world = stone_floor(-10, 0, 10, 0)
        world.fill_box(BlockPos(3, 1, 0), BlockPos(3, 3, 0), STONE)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(8, 1, 0), radius=0), max_nodes=2000)
        # 地板只有一格宽，绕不过去，也跳不过去
        self.assertIsNone(path)

    def test_avoid_lava_lake(self) -> None:
        world = stone_floor(-5, -5, 14, 14)
        world.fill_box(BlockPos(4, 1, -2), BlockPos(9, 1, 8), LAVA)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(12, 1, 10), radius=0))
        path = self.assert_valid_path(world, finder, path)
        reg = registry()
        for node in path.nodes:
            self.assertFalse(reg.is_lava(world.get_state(node.x, node.y, node.z)), f"踩到岩浆: {node}")
            self.assertFalse(reg.is_lava(world.get_state(node.x, node.y - 1, node.z)), f"岩浆上: {node}")

    def test_enclosed_goal_unreachable(self) -> None:
        world = stone_floor(-5, -5, 14, 14)
        goal_pos = BlockPos(6, 1, 6)
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            world.fill_box(BlockPos(6 + dx, 1, 6 + dz), BlockPos(6 + dx, 2, 6 + dz), STONE)
        world.fill_box(BlockPos(6, 3, 6), BlockPos(6, 3, 6), STONE)
        finder = Pathfinder(world)
        self.assertIsNone(finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(goal_pos, radius=0), max_nodes=4000))

    def test_find_path_to_block(self) -> None:
        world = stone_floor(-5, -5, 14, 14)
        finder = Pathfinder(world)
        target = BlockPos(6, 1, 0)
        path = finder.find_path_to_block(Vec3(0.5, 1.0, 0.5), target)
        path = self.assert_valid_path(world, finder, path)
        last = path.nodes[-1]
        self.assertLessEqual(max(abs(last.x - target.x), abs(last.y - target.y), abs(last.z - target.z)), 1)

    def test_goal_near(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        target = BlockPos(6, 1, 0)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalNear(target, 1.5))
        path = self.assert_valid_path(world, finder, path)
        self.assertLessEqual(path.nodes[-1].distance_to(target), 1.5)

    def test_goal_any(self) -> None:
        world = stone_floor(-10, -10, 10, 10)
        finder = Pathfinder(world)
        goal = GoalAny(lambda pos: pos.x >= 4 and pos.z >= 4)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), goal)
        path = self.assert_valid_path(world, finder, path)
        self.assertTrue(goal.is_reached(path.nodes[-1]))


class UnloadedTest(PathTestCase):
    """不能走进未加载的区块。"""

    def test_unloaded_region_is_wall(self) -> None:
        class PartialWorld(SimpleWorld):
            def is_loaded(self, x: int, z: int) -> bool:
                return z >= 0

        world = PartialWorld()
        world.fill_box(BlockPos(-10, 0, -10), BlockPos(10, 0, 10), STONE)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(5, 1, 5), radius=0))
        self.assertIsNotNone(path)
        assert path is not None
        for node in path.nodes:
            self.assertGreaterEqual(node.z, 0)
        # 目标在未加载区域
        self.assertIsNone(finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(5, 1, -5), radius=0)))
        # 起点在未加载区域
        self.assertIsNone(finder.find_path(Vec3(0.5, 1.0, -5.5), GoalBlock(BlockPos(5, 1, 5), radius=0)))


class WaterTest(PathTestCase):
    """水柱可以游过去。"""

    def test_swim_across_water(self) -> None:
        world = stone_floor(-5, -5, 14, 14)
        world.fill_box(BlockPos(4, 1, -3), BlockPos(4, 2, 3), WATER)
        finder = Pathfinder(world)
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(8, 1, 0), radius=0))
        path = self.assert_valid_path(world, finder, path)
        self.assertEqual(path.nodes[-1], BlockPos(8, 1, 0))


class CanStandTest(unittest.TestCase):
    """节点判定。"""

    def setUp(self) -> None:
        self.world = stone_floor(-3, -3, 3, 3)
        self.finder = Pathfinder(self.world)

    def test_basic(self) -> None:
        self.assertTrue(self.finder.can_stand(BlockPos(0, 1, 0)))
        self.assertFalse(self.finder.can_stand(BlockPos(0, 0, 0)))  # 方块内部
        self.assertFalse(self.finder.can_stand(BlockPos(0, 4, 0)))  # 悬空

    def test_head_blocked(self) -> None:
        self.world.set_state(1, 2, 1, STONE)
        self.assertFalse(self.finder.can_stand(BlockPos(1, 1, 1)))
        self.world.set_state(1, 2, 1, 0)
        self.assertTrue(self.finder.can_stand(BlockPos(1, 1, 1)))

    def test_water_and_lava(self) -> None:
        self.world.fill_box(BlockPos(0, 1, 2), BlockPos(0, 3, 2), WATER)
        self.assertTrue(self.finder.can_stand(BlockPos(0, 2, 2)))
        self.world.set_state(0, 1, -2, LAVA)
        self.assertFalse(self.finder.can_stand(BlockPos(0, 1, -2)))
        self.assertFalse(self.finder.can_stand(BlockPos(10, 1, 10)))  # 地板外悬空

    def test_slab_is_standable_from_above(self) -> None:
        self.world.set_state(-2, 1, 0, SLAB)
        self.assertFalse(self.finder.can_stand(BlockPos(-2, 1, 0)))  # 半砖本身有碰撞箱
        self.assertTrue(self.finder.can_stand(BlockPos(-2, 2, 0)))  # 站在半砖上面

    def test_door_is_blocked(self) -> None:
        door = registry().state_id("oak_door")
        self.world.set_state(2, 1, 0, door)
        self.assertFalse(self.finder.can_stand(BlockPos(2, 1, 0)))


class PathApiTest(unittest.TestCase):
    """Path 的消费接口。"""

    def test_consumption(self) -> None:
        nodes = [BlockPos(0, 1, 0), BlockPos(0, 1, 1), BlockPos(1, 1, 2)]
        path = Path(list(nodes))
        self.assertEqual(path.length, 3)
        self.assertEqual(path.nodes, nodes)
        self.assertFalse(path.is_done)
        self.assertEqual(path.current(), nodes[0])
        self.assertEqual(path.advance(), nodes[1])
        self.assertEqual(path.current(), nodes[1])
        self.assertEqual(path.remaining(), [nodes[1], nodes[2]])
        self.assertEqual(path.advance(), nodes[2])
        self.assertIsNone(path.advance())
        self.assertTrue(path.is_done)
        self.assertEqual(path.current(), nodes[2])

    def test_complete(self) -> None:
        path = Path([BlockPos(0, 1, 0), BlockPos(1, 1, 0)])
        path.complete()
        self.assertTrue(path.is_done)
        self.assertEqual(path.remaining(), [])
        self.assertEqual(path.advance(), None)


class GoalTest(unittest.TestCase):
    """目标判定的边界。"""

    def test_goal_block(self) -> None:
        goal = GoalBlock(BlockPos(5, 1, 5))
        self.assertTrue(goal.is_reached(BlockPos(5, 1, 5)))
        self.assertTrue(goal.is_reached(BlockPos(6, 2, 4)))
        self.assertFalse(goal.is_reached(BlockPos(7, 1, 5)))
        tight = GoalBlock(BlockPos(5, 1, 5), radius=0)
        self.assertTrue(tight.is_reached(BlockPos(5, 1, 5)))
        self.assertFalse(tight.is_reached(BlockPos(6, 1, 5)))

    def test_goal_xz(self) -> None:
        goal = GoalXZ(5, 5)
        self.assertTrue(goal.is_reached(BlockPos(6, 70, 4)))
        self.assertFalse(goal.is_reached(BlockPos(7, 1, 5)))

    def test_goal_near(self) -> None:
        goal = GoalNear(BlockPos(0, 1, 0), 2.0)
        self.assertTrue(goal.is_reached(BlockPos(1, 1, 1)))
        self.assertFalse(goal.is_reached(BlockPos(3, 1, 0)))

    def test_goal_any(self) -> None:
        goal = GoalAny(lambda pos: pos.x == 3 and pos.z == 3)
        self.assertTrue(goal.is_reached(BlockPos(3, 5, 3)))
        self.assertFalse(goal.is_reached(BlockPos(3, 5, 4)))


class PerformanceTest(unittest.TestCase):
    """性能兜底（只做宽松断言）。"""

    def test_large_flat_area(self) -> None:
        world = stone_floor(-25, -25, 24, 24)
        finder = Pathfinder(world)
        start = time.monotonic()
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(20, 1, 20), radius=0))
        elapsed = time.monotonic() - start
        self.assertIsNotNone(path)
        self.assertLess(elapsed, 1.0, f"寻路太慢: {elapsed:.3f}s")

    def test_node_budget_is_respected(self) -> None:
        """节点预算很小时应该老实返回 None，而不是死循环。"""
        world = stone_floor(-20, -20, 19, 19)
        finder = Pathfinder(world)
        start = time.monotonic()
        path = finder.find_path(Vec3(0.5, 1.0, 0.5), GoalBlock(BlockPos(18, 1, 18), radius=0), max_nodes=3)
        self.assertIsNone(path)
        self.assertLess(time.monotonic() - start, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""mcbot.woodcutting 的单元测试（unittest + SimpleWorld，不连服务器）。

覆盖：
* 被树叶完全包裹的树干仍能被 find_trees 识别，base 取最低原木；
* 相距较远的树被分到两个 Tree；
* 树干被树叶完全包住时 approach_points 返回空；
* leaf_blockers 只返回 6 邻接于原木的树叶，离 base 近的排前面；
* 未加载 / 越界坐标安全返回，不抛异常。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcbot.registry import registry
from mcbot.types import BlockPos, Vec3
from mcbot.world import SimpleWorld, World
from mcbot.woodcutting import (
    EYE_HEIGHT,
    Tree,
    approach_points,
    find_trees,
    is_leaves,
    is_wood,
    leaf_blockers,
    surrounding_leaves,
    surrounding_solids,
)

REG = registry()
OAK_LOG = REG.state_id("oak_log")
OAK_LEAVES = REG.state_id("oak_leaves")
STONE = REG.state_id("stone")


def make_ground(
    world: SimpleWorld,
    x0: int = -8,
    z0: int = -8,
    x1: int = 8,
    z1: int = 8,
    y: int = 0,
) -> None:
    """铺一层石制地板。"""
    world.fill_box(BlockPos(x0, y, z0), BlockPos(x1, y, z1), STONE)


def build_trunk(world: SimpleWorld, base: BlockPos = BlockPos(0, 1, 0), height: int = 4) -> None:
    """在 ``base`` 处竖一根 ``height`` 高的橡木树干。"""
    for i in range(height):
        world.set_state(base.x, base.y + i, base.z, OAK_LOG)


def wrap_with_leaves(world: SimpleWorld, base: BlockPos = BlockPos(0, 1, 0), height: int = 4) -> None:
    """用树叶把树干四周和顶部完全包住（1 格厚）。"""
    for i in range(height):
        y = base.y + i
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx == 0 and dz == 0:
                    continue
                world.set_state(base.x + dx, y, base.z + dz, OAK_LEAVES)
    top_y = base.y + height
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            world.set_state(base.x + dx, top_y, base.z + dz, OAK_LEAVES)


def build_wrapped_tree(world: SimpleWorld, base: BlockPos = BlockPos(0, 1, 0), height: int = 4) -> None:
    """树干 + 完全包裹的树叶。"""
    build_trunk(world, base, height)
    wrap_with_leaves(world, base, height)


class WoodClassificationTest(unittest.TestCase):
    """名称判定。"""

    def test_wood_names(self) -> None:
        names = (
            "oak_log",
            "stripped_birch_log",
            "crimson_stem",
            "stripped_warped_stem",
            "oak_wood",
            "stripped_mangrove_wood",
            "warped_hyphae",
            "mushroom_stem",
        )
        for name in names:
            self.assertTrue(is_wood(name), name)
            self.assertTrue(is_wood("minecraft:" + name), name)
            self.assertTrue(is_wood(REG.state_id(name)), name)
        self.assertFalse(is_wood(0))  # air
        self.assertFalse(is_wood("stone"))

    def test_non_wood_stems_are_excluded(self) -> None:
        for name in (
            "pumpkin_stem",
            "melon_stem",
            "attached_pumpkin_stem",
            "attached_melon_stem",
            "big_dripleaf_stem",
        ):
            self.assertFalse(is_wood(name), name)

    def test_leaves_names(self) -> None:
        for name in (
            "oak_leaves",
            "cherry_leaves",
            "flowering_azalea_leaves",
            "azalea_leaves",
            "leaves",
            "azalea_leaves_flowering",
        ):
            self.assertTrue(is_leaves(name), name)
        for name in ("oak_log", "stone", "grass_block"):
            self.assertFalse(is_leaves(name), name)


class FindTreesTest(unittest.TestCase):
    """树木识别与聚类。"""

    def test_wrapped_tree_is_found_with_lowest_log_as_base(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        build_wrapped_tree(world, BlockPos(0, 1, 0), height=4)

        trees = find_trees(world, BlockPos(0, 3, 0), radius=8)
        self.assertEqual(len(trees), 1, trees)
        tree = trees[0]
        self.assertEqual(tree.base, BlockPos(0, 1, 0))
        self.assertEqual(
            [pos.as_tuple() for pos in tree.logs],
            [(0, 1, 0), (0, 2, 0), (0, 3, 0), (0, 4, 0)],
        )
        self.assertEqual(tree.trunk_columns, {(0, 0)})
        self.assertEqual(tree.height, 4)
        self.assertEqual(tree.top, BlockPos(0, 4, 0))
        self.assertEqual(tree.bounds, (BlockPos(0, 1, 0), BlockPos(0, 4, 0)))
        # 树叶不算原木
        self.assertEqual(len(tree.logs), 4)

    def test_vec3_center_works(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        build_wrapped_tree(world, BlockPos(0, 1, 0), height=4)
        trees = find_trees(world, Vec3(0.5, 3.0, 0.5), radius=8)
        self.assertEqual(len(trees), 1)
        self.assertEqual(trees[0].base, BlockPos(0, 1, 0))

    def test_vertical_and_diagonal_adjacency(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        # 2x2 树干 + 一根斜向伸出的原木；只有 26 邻接才能聚成一棵树
        for pos in (
            BlockPos(0, 1, 0),
            BlockPos(1, 1, 0),
            BlockPos(0, 2, 0),
            BlockPos(1, 2, 0),
            BlockPos(1, 3, 1),
        ):
            world.set_state(pos.x, pos.y, pos.z, OAK_LOG)

        trees = find_trees(world, BlockPos(0, 2, 0), radius=6)
        self.assertEqual(len(trees), 1, trees)
        self.assertEqual(
            [pos.as_tuple() for pos in trees[0].logs],
            [(0, 1, 0), (1, 1, 0), (0, 2, 0), (1, 2, 0), (1, 3, 1)],
        )
        self.assertEqual(trees[0].base, BlockPos(0, 1, 0))
        self.assertEqual(trees[0].trunk_columns, {(0, 0), (1, 0), (1, 1)})

    def test_two_far_apart_trees_are_split_and_sorted(self) -> None:
        world = SimpleWorld()
        make_ground(world, -8, -8, 28, 8)
        build_wrapped_tree(world, BlockPos(0, 1, 0), height=4)
        build_wrapped_tree(world, BlockPos(20, 1, 0), height=4)

        trees = find_trees(world, BlockPos(0, 3, 0), radius=24)
        self.assertEqual(len(trees), 2, trees)
        self.assertEqual(trees[0].base, BlockPos(0, 1, 0))
        self.assertEqual(trees[1].base, BlockPos(20, 1, 0))
        self.assertLess(trees[0].distance, trees[1].distance)
        self.assertEqual(trees[0].trunk_columns, {(0, 0)})
        self.assertEqual(trees[1].trunk_columns, {(20, 0)})

        # limit 只保留最近的树
        nearest = find_trees(world, BlockPos(0, 3, 0), radius=24, limit=1)
        self.assertEqual(len(nearest), 1)
        self.assertEqual(nearest[0].base, BlockPos(0, 1, 0))
        self.assertEqual(find_trees(world, BlockPos(0, 3, 0), radius=24, limit=0), [])

    def test_no_loaded_blocks_returns_empty(self) -> None:
        world = World()  # 一个区块都没加载
        self.assertEqual(world.loaded_chunk_count(), 0)
        self.assertEqual(find_trees(world, BlockPos(0, 64, 0), radius=8), [])


class ApproachPointsTest(unittest.TestCase):
    """接近落脚点。"""

    def test_empty_when_trunk_fully_encased_in_leaves(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        base = BlockPos(0, 1, 0)
        build_trunk(world, base, height=4)
        # 厚厚一层树叶把树干连同上方的落脚空间全部包死
        world.fill_box(BlockPos(-4, 1, -4), BlockPos(4, 7, 4), OAK_LEAVES)
        build_trunk(world, base, height=4)  # 恢复被覆盖的原木

        trees = find_trees(world, BlockPos(0, 3, 0), radius=6)
        self.assertEqual(len(trees), 1)
        self.assertEqual(approach_points(world, trees[0]), [])
        # 确认周围确实有挡路的树叶
        self.assertTrue(leaf_blockers(world, trees[0]))

    def test_open_trunk_has_standable_reachable_points(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        base = BlockPos(0, 1, 0)
        build_trunk(world, base, height=4)
        tree = find_trees(world, BlockPos(0, 3, 0), radius=6)[0]

        points = approach_points(world, tree)
        self.assertTrue(points, "空旷树干应有可站立且够得着的落脚点")
        self.assertEqual(len(points), len(set(points)), "落脚点不应重复")

        max_reach = 4.2
        for pos in points:
            # 脚 / 头可穿过、脚下实心（与 approach_points 的判定一致）
            self.assertTrue(world.is_loaded(pos.x, pos.z), pos)
            self.assertTrue(REG.is_passable(world.get_state(pos.x, pos.y, pos.z)), pos)
            self.assertTrue(REG.is_passable(world.get_state(pos.x, pos.y + 1, pos.z)), pos)
            self.assertTrue(REG.is_solid(world.get_state(pos.x, pos.y - 1, pos.z)), pos)
            # 眼睛到某一根原木中心的距离不超过 max_reach
            eye = Vec3(pos.x + 0.5, pos.y + EYE_HEIGHT, pos.z + 0.5)
            nearest = min(eye.distance_to(log.center()) for log in tree.logs)
            self.assertLessEqual(nearest, max_reach + 1e-9, pos)

        # 按到 base 的水平距离升序
        horizontals = [math.hypot(pos.x - base.x, pos.z - base.z) for pos in points]
        self.assertEqual(horizontals, sorted(horizontals))

        # 紧贴树干的地面格子应该在里面
        self.assertIn(BlockPos(1, 1, 0), points)


class LeafBlockersTest(unittest.TestCase):
    """树叶阻挡。"""

    def test_only_adjacent_leaves_and_nearest_first(self) -> None:
        world = SimpleWorld()
        make_ground(world)
        base = BlockPos(0, 1, 0)
        build_trunk(world, base, height=4)

        adjacent_leaves = [
            BlockPos(1, 1, 0),
            BlockPos(-1, 1, 0),
            BlockPos(0, 1, 1),
            BlockPos(1, 2, 0),
            BlockPos(0, 5, 0),
        ]
        for pos in adjacent_leaves:
            world.set_state(pos.x, pos.y, pos.z, OAK_LEAVES)
        # 只对角相邻/离树干两格远的树叶：不是 6 邻接，不应返回
        world.set_state(2, 2, 2, OAK_LEAVES)
        world.set_state(0, 1, 3, OAK_LEAVES)

        tree = find_trees(world, BlockPos(0, 3, 0), radius=8)[0]
        blockers = leaf_blockers(world, tree)
        self.assertEqual(len(blockers), len(adjacent_leaves), blockers)
        self.assertEqual(set(blockers), set(adjacent_leaves))
        self.assertNotIn(BlockPos(2, 2, 2), blockers)
        self.assertNotIn(BlockPos(0, 1, 3), blockers)

        # 全是树叶，且都 6 邻接于某根原木
        for pos in blockers:
            self.assertTrue(is_leaves(world.get_state(pos.x, pos.y, pos.z)), pos)
            self.assertTrue(any(pos in log.neighbors() for log in tree.logs), pos)

        # 离 base 近的排前面
        distances = [pos.distance_to(base) for pos in blockers]
        self.assertEqual(distances, sorted(distances))
        self.assertEqual(blockers[0].distance_to(base), 1.0)
        self.assertEqual(blockers[-1], BlockPos(0, 5, 0))

        # limit 生效
        self.assertEqual(len(leaf_blockers(world, tree, limit=2)), 2)
        self.assertEqual(leaf_blockers(world, tree, limit=0), [])


class SurroundingBlocksTest(unittest.TestCase):
    """邻接方块查询。"""

    def test_surrounding_solids_and_leaves(self) -> None:
        world = SimpleWorld()
        pos = BlockPos(0, 1, 0)
        world.set_state(pos.x, pos.y, pos.z, OAK_LOG)
        world.set_state(1, 1, 0, STONE)
        world.set_state(0, 2, 0, OAK_LEAVES)
        world.set_state(0, 0, 0, STONE)
        world.set_state(-1, 1, 0, OAK_LEAVES)

        solids = surrounding_solids(world, pos)
        self.assertEqual(solids, [BlockPos(0, 0, 0), BlockPos(1, 1, 0)])
        leaves = surrounding_leaves(world, pos)
        self.assertEqual(set(leaves), {BlockPos(0, 2, 0), BlockPos(-1, 1, 0)})
        # 未加载世界安全返回空
        self.assertEqual(surrounding_solids(World(), pos), [])
        self.assertEqual(surrounding_leaves(World(), pos), [])


class SafetyTest(unittest.TestCase):
    """未加载区块 / 越界坐标必须安全返回。"""

    def test_unloaded_world_is_safe(self) -> None:
        world = World()
        tree = Tree(logs=[BlockPos(0, 64, 0)], base=BlockPos(0, 64, 0))
        self.assertEqual(tree.trunk_columns, {(0, 0)})
        self.assertEqual(tree.distance, 0.0)
        self.assertEqual(find_trees(world, BlockPos(0, 64, 0), radius=8), [])
        self.assertEqual(approach_points(world, tree), [])
        self.assertEqual(leaf_blockers(world, tree), [])
        self.assertEqual(surrounding_solids(world, BlockPos(0, 64, 0)), [])
        self.assertEqual(surrounding_leaves(world, BlockPos(0, 64, 0)), [])

    def test_out_of_bounds_coordinates_are_safe(self) -> None:
        world = SimpleWorld()
        # 远超世界高度的搜索中心
        self.assertEqual(find_trees(world, Vec3(0.5, 10_000.0, 0.5), radius=4), [])
        self.assertEqual(
            find_trees(world, BlockPos(-1_000_000, -10_000, 1_000_000), radius=4),
            [],
        )
        far_tree = Tree(
            logs=[BlockPos(10**6, 10**6, 10**6)],
            base=BlockPos(10**6, 10**6, 10**6),
        )
        self.assertEqual(approach_points(world, far_tree), [])
        self.assertEqual(leaf_blockers(world, far_tree), [])
        self.assertEqual(surrounding_solids(world, BlockPos(10**6, -10**6, 10**6)), [])
        # 空树也不应抛异常
        empty_tree = Tree(logs=[], base=BlockPos(0, 64, 0))
        self.assertEqual(approach_points(world, empty_tree), [])
        self.assertEqual(leaf_blockers(world, empty_tree), [])


if __name__ == "__main__":
    unittest.main()

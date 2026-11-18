"""伐木几何逻辑：树木识别、接近落脚点与树叶阻挡计算。

这一层只做纯几何/数据计算，依赖 :mod:`mcbot.world` 与 :mod:`mcbot.registry`，
**不** import player / pathfinding / brain，避免循环依赖。服务器连接、寻路与
实际挖掘由上层负责，本模块只回答"树在哪、从哪站、先清哪片叶子"。

典型用法::

    trees = find_trees(world, player.pos, radius=32, limit=8)
    if trees:
        tree = trees[0]
        for spot in approach_points(world, tree):
            ...
        for leaf in leaf_blockers(world, tree):
            ...  # 先挖掉包裹树干的树叶

坐标约定与 :mod:`mcbot.types` 一致：``BlockPos`` 是方块整数坐标，方块中心为
``BlockPos.center()``；玩家眼睛高度为脚底 + :data:`EYE_HEIGHT`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .registry import BlockInfo, BlockRegistry, registry as default_registry
from .types import BlockPos, Vec3
from .world import World

__all__ = [
    "EYE_HEIGHT",
    "Tree",
    "is_wood",
    "is_leaves",
    "find_trees",
    "approach_points",
    "leaf_blockers",
    "surrounding_leaves",
    "surrounding_solids",
]

#: 玩家眼睛相对脚底的高度（Minecraft 1.20+ 站立姿态）。
EYE_HEIGHT = 1.62

#: 原木类方块名后缀：oak_log / crimson_stem / oak_wood / warped_hyphae ...
_WOOD_SUFFIXES = ("_log", "_stem", "_wood", "_hyphae")

#: 树叶类方块名后缀。
_LEAVES_SUFFIXES = ("_leaves",)

#: 不以 ``_leaves`` 结尾的特殊树叶（旧版 id / 新版本命名）。
_EXTRA_LEAVES = frozenset({"leaves", "azalea_leaves_flowering"})

#: 名字以 ``_stem`` 结尾、但不是树木方块（农作物 / 植物茎）。
_NON_WOOD_STEMS = frozenset(
    {
        "pumpkin_stem",
        "melon_stem",
        "attached_pumpkin_stem",
        "attached_melon_stem",
        "big_dripleaf_stem",
    }
)

#: 6 邻接偏移（面相邻）。
_NEIGHBOR_OFFSETS_6: tuple[tuple[int, int, int], ...] = (
    (0, -1, 0),
    (0, 1, 0),
    (0, 0, -1),
    (0, 0, 1),
    (-1, 0, 0),
    (1, 0, 0),
)

#: 26 邻接偏移（含对角、含竖直），用于把原木聚成一棵树。
_NEIGHBOR_OFFSETS_26: tuple[tuple[int, int, int], ...] = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if (dx, dy, dz) != (0, 0, 0)
)


# --------------------------------------------------------------------------- #
# 方块分类
# --------------------------------------------------------------------------- #
def _short_name(block: int | str | BlockInfo) -> str:
    """把状态 id / 全名 / ``BlockInfo`` 统一成 short name（不带命名空间）。"""
    if isinstance(block, str):
        return block.split(":", 1)[-1]
    if isinstance(block, BlockInfo):
        return block.short_name
    return default_registry().block(int(block)).short_name


def _is_wood_short(short_name: str) -> bool:
    if short_name in _NON_WOOD_STEMS:
        return False
    return short_name.endswith(_WOOD_SUFFIXES) or "log" in short_name


def _is_leaves_short(short_name: str) -> bool:
    return short_name in _EXTRA_LEAVES or short_name.endswith(_LEAVES_SUFFIXES)


def is_wood(block: int | str | BlockInfo) -> bool:
    """判断方块是否属于原木类（log / stem / wood / hyphae）。

    参数可以是方块状态 id（按默认注册表解释）、方块名（可带 ``minecraft:``
    前缀）或 :class:`~mcbot.registry.BlockInfo`。名字以 ``_log`` / ``_stem`` /
    ``_wood`` / ``_hyphae`` 结尾或名字里含 ``log`` 即视为木头；南瓜/西瓜等
    植物茎、大垂滴叶茎会被排除。
    """
    return _is_wood_short(_short_name(block))


def is_leaves(block: int | str | BlockInfo) -> bool:
    """判断方块是否属于树叶类（``*_leaves``、``leaves`` 等）。"""
    return _is_leaves_short(_short_name(block))


def _block_short_name(world: World, state_id: int) -> str:
    """用世界自己的注册表取 short name（兼容 26.2 等不同状态表）。"""
    return world.registry.block(state_id).short_name


def _log_sort_key(pos: BlockPos) -> tuple[int, int, int]:
    """原木排序键：y 升序，同 y 按 x、z。"""
    return (pos.y, pos.x, pos.z)


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Tree:
    """一棵由 3D 邻接原木聚类而成的树。

    :param logs: 原木方块（按 ``(y, x, z)`` 升序，``logs[0]`` 即树根）。
    :param base: 最低原木（树干底部，砍树/接近的基准点）。
    :param distance: ``center`` 到 ``base`` 方块中心的直线距离（``find_trees`` 用）。
    :param trunk_columns: 树干占据的 ``(x, z)`` 列集合。
    """

    logs: list[BlockPos]
    base: BlockPos
    distance: float = 0.0
    trunk_columns: set[tuple[int, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.logs:
            self.logs.sort(key=_log_sort_key)
        if not self.trunk_columns:
            self.trunk_columns = {(pos.x, pos.z) for pos in self.logs}

    @property
    def top(self) -> BlockPos:
        """最高的原木方块。"""
        return self.logs[-1] if self.logs else self.base

    @property
    def height(self) -> int:
        """树干高度（方块数）；空树为 0。"""
        if not self.logs:
            return 0
        return max(pos.y for pos in self.logs) - self.base.y + 1

    @property
    def bounds(self) -> tuple[BlockPos, BlockPos]:
        """所有原木的最小 / 最大方块坐标（含端点）。"""
        if not self.logs:
            return (self.base, self.base)
        xs = [pos.x for pos in self.logs]
        ys = [pos.y for pos in self.logs]
        zs = [pos.z for pos in self.logs]
        return (BlockPos(min(xs), min(ys), min(zs)), BlockPos(max(xs), max(ys), max(zs)))

    def __len__(self) -> int:
        return len(self.logs)


# --------------------------------------------------------------------------- #
# 找树
# --------------------------------------------------------------------------- #
def _center_point(center: BlockPos | Vec3) -> Vec3:
    """把搜索中心统一成浮点世界坐标（BlockPos 取方块中心）。"""
    return center.center() if isinstance(center, BlockPos) else center


def _cluster_logs(positions: list[BlockPos]) -> list[list[BlockPos]]:
    """把原木按 26 邻接（含对角、含竖直）聚成若干簇。"""
    unvisited = set(positions)
    clusters: list[list[BlockPos]] = []
    while unvisited:
        start = min(unvisited, key=_log_sort_key)
        unvisited.remove(start)
        cluster = [start]
        stack = [start]
        while stack:
            current = stack.pop()
            for dx, dy, dz in _NEIGHBOR_OFFSETS_26:
                candidate = BlockPos(current.x + dx, current.y + dy, current.z + dz)
                if candidate in unvisited:
                    unvisited.remove(candidate)
                    cluster.append(candidate)
                    stack.append(candidate)
        cluster.sort(key=_log_sort_key)
        clusters.append(cluster)
    return clusters


def find_trees(
    world: World,
    center: BlockPos | Vec3,
    radius: int = 32,
    limit: int = 8,
) -> list[Tree]:
    """在 ``center`` 附近找树。

    只统计已加载区块；范围内所有原木方块先按 3D 26 邻接聚成树，再按树根
    ``base`` 到 ``center`` 的距离从近到远返回，最多 ``limit`` 棵。

    :param world: 世界模型（``World`` / ``SimpleWorld``）。
    :param center: 搜索中心；``BlockPos`` 取方块中心，``Vec3`` 原样使用。
    :param radius: 搜索半径（方块，3D 欧氏距离）。
    :param limit: 最多返回几棵树；``<= 0`` 返回空列表。
    """
    if limit <= 0:
        return []
    radius = max(0, int(radius))
    center_pos = center.floor() if isinstance(center, Vec3) else center
    center_point = _center_point(center)
    radius_sq = float(radius) * radius

    min_x, max_x = center_pos.x - radius, center_pos.x + radius
    min_z, max_z = center_pos.z - radius, center_pos.z + radius
    min_y = max(world.min_y, center_pos.y - radius)
    max_y = min(world.max_y, center_pos.y + radius)
    if min_y > max_y:
        return []

    registry_: BlockRegistry = world.registry
    positions: list[BlockPos] = []
    for x in range(min_x, max_x + 1):
        for z in range(min_z, max_z + 1):
            if not world.is_loaded(x, z):
                continue
            for y in range(min_y, max_y + 1):
                # 用方块中心到中心的距离过滤，和 Tree.distance 的定义保持一致
                dx = x + 0.5 - center_point.x
                dy = y + 0.5 - center_point.y
                dz = z + 0.5 - center_point.z
                if dx * dx + dy * dy + dz * dz > radius_sq:
                    continue
                state = world.get_state(x, y, z)
                if not state:
                    continue
                if not _is_wood_short(registry_.block(state).short_name):
                    continue
                positions.append(BlockPos(x, y, z))

    trees: list[Tree] = []
    for cluster in _cluster_logs(positions):
        base = cluster[0]
        trees.append(
            Tree(
                logs=cluster,
                base=base,
                distance=base.center().distance_to(center_point),
            )
        )
    trees.sort(key=lambda tree: (tree.distance, _log_sort_key(tree.base)))
    return trees[:limit]


# --------------------------------------------------------------------------- #
# 接近点
# --------------------------------------------------------------------------- #
def _can_stand(world: World, pos: BlockPos) -> bool:
    """``pos`` 处能否站立：脚/头两格可穿过、脚下实心、且不危险。

    直接用注册表判断，对水、台阶/半砖等保持宽松（水可穿过、半砖算实心地面），
    不使用 ``World.is_safe_standing`` 以免误伤。
    """
    if pos.y <= world.min_y or pos.y >= world.max_y:
        return False
    if not world.is_loaded(pos.x, pos.z):
        return False
    registry_ = world.registry
    feet = world.get_state(pos.x, pos.y, pos.z)
    head = world.get_state(pos.x, pos.y + 1, pos.z)
    below = world.get_state(pos.x, pos.y - 1, pos.z)
    if not registry_.is_passable(feet) or not registry_.is_passable(head):
        return False
    if not registry_.is_solid(below):
        return False
    if registry_.is_dangerous(below) or registry_.is_dangerous(feet):
        return False
    return True


def approach_points(world: World, tree: Tree, max_reach: float = 4.2) -> list[BlockPos]:
    """返回可以站着伸手够到树干的所有落脚格。

    判定标准：该格玩家脚可站（脚/头可穿过、脚下实心、不危险），且眼睛
    （脚 + :data:`EYE_HEIGHT`）到 **任意一个** 原木方块中心的距离
    ``<= max_reach``。结果按到树根 ``base`` 的水平距离升序排列
    （同距离时 y 低者优先）。

    树干被树叶完全包住、周围没有可站立空位时返回空列表。
    """
    if not tree.logs or max_reach <= 0:
        return []
    base = tree.base
    log_centers = [pos.center() for pos in tree.logs]
    reach_sq = float(max_reach) * max_reach
    pad = int(math.ceil(max_reach))

    min_x = min(pos.x for pos in tree.logs) - pad
    max_x = max(pos.x for pos in tree.logs) + pad
    min_z = min(pos.z for pos in tree.logs) - pad
    max_z = max(pos.z for pos in tree.logs) + pad

    # 眼睛高度必须能落进某根原木中心 ±max_reach 的竖直范围
    min_log_center_y = min(pos.y for pos in tree.logs) + 0.5
    max_log_center_y = max(pos.y for pos in tree.logs) + 0.5
    min_feet_y = math.ceil(min_log_center_y - max_reach - EYE_HEIGHT)
    max_feet_y = math.floor(max_log_center_y + max_reach - EYE_HEIGHT)
    min_feet_y = max(min_feet_y, world.min_y + 1)
    max_feet_y = min(max_feet_y, world.max_y - 1)
    if min_feet_y > max_feet_y:
        return []

    candidates: list[tuple[float, int, int, int]] = []
    for x in range(min_x, max_x + 1):
        for z in range(min_z, max_z + 1):
            if not world.is_loaded(x, z):
                continue
            horizontal = math.hypot(x - base.x, z - base.z)
            for y in range(min_feet_y, max_feet_y + 1):
                pos = BlockPos(x, y, z)
                if not _can_stand(world, pos):
                    continue
                eye_x, eye_y, eye_z = x + 0.5, y + EYE_HEIGHT, z + 0.5
                for log_center in log_centers:
                    dx = eye_x - log_center.x
                    dy = eye_y - log_center.y
                    dz = eye_z - log_center.z
                    if dx * dx + dy * dy + dz * dz <= reach_sq + 1e-9:
                        candidates.append((horizontal, y, x, z))
                        break

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [BlockPos(x, y, z) for _horizontal, y, x, z in candidates]


# --------------------------------------------------------------------------- #
# 树叶阻挡
# --------------------------------------------------------------------------- #
def leaf_blockers(world: World, tree: Tree, limit: int = 8) -> list[BlockPos]:
    """贴着树干（6 邻接于任意原木）的树叶方块。

    这些是"刚要接近树干时最容易挡路"的叶子，按到树根 ``base`` 的距离升序、
    同距离按 y 从低到高排序；``limit <= 0`` 返回空列表。
    """
    if limit <= 0 or not tree.logs:
        return []
    registry_: BlockRegistry = world.registry
    found: set[BlockPos] = set()
    for log in tree.logs:
        for dx, dy, dz in _NEIGHBOR_OFFSETS_6:
            neighbor = BlockPos(log.x + dx, log.y + dy, log.z + dz)
            if neighbor in found:
                continue
            if not world.is_loaded(neighbor.x, neighbor.z):
                continue
            state = world.get_state(neighbor.x, neighbor.y, neighbor.z)
            if not state:
                continue
            if not _is_leaves_short(registry_.block(state).short_name):
                continue
            found.add(neighbor)
    ordered = sorted(found, key=lambda pos: (pos.distance_to(tree.base), pos.y, pos.x, pos.z))
    return ordered[:limit]


def surrounding_leaves(world: World, pos: BlockPos) -> list[BlockPos]:
    """``pos`` 的 6 邻接树叶方块（按 ``(y, x, z)`` 排序）。"""
    return _surrounding(world, pos, want_leaves=True)


def surrounding_solids(world: World, pos: BlockPos) -> list[BlockPos]:
    """``pos`` 的 6 邻接非空气、非树叶方块（调试 / 报告用）。"""
    return _surrounding(world, pos, want_leaves=False)


def _surrounding(world: World, pos: BlockPos, *, want_leaves: bool) -> list[BlockPos]:
    registry_: BlockRegistry = world.registry
    result: list[BlockPos] = []
    for dx, dy, dz in _NEIGHBOR_OFFSETS_6:
        neighbor = BlockPos(pos.x + dx, pos.y + dy, pos.z + dz)
        if not world.is_loaded(neighbor.x, neighbor.z):
            continue
        state = world.get_state(neighbor.x, neighbor.y, neighbor.z)
        if not state or registry_.is_air(state):
            continue
        leaves = _is_leaves_short(registry_.block(state).short_name)
        if leaves != want_leaves:
            continue
        result.append(neighbor)
    result.sort(key=_log_sort_key)
    return result

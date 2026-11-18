"""A* 寻路：节点是玩家脚所在的方块。

节点 = 玩家双脚站的方块；一个节点可站立的判据见 :meth:`Pathfinder.can_stand`。
移动代价近似原版：平地 1、斜走 ``sqrt2``、起跳 +0.6、下落 1.2 起、水中 ×2.5，
危险方块（岩浆 / 火 / 仙人掌）相邻 +4，站在非整方块（台阶 / 半砖）上 +2。
"""

from __future__ import annotations

import heapq
import itertools
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol

from .types import BlockPos, Vec3
from .world import World

__all__ = [
    "Goal",
    "GoalBlock",
    "GoalXZ",
    "GoalNear",
    "GoalAny",
    "Path",
    "Pathfinder",
]

SQRT2 = math.sqrt(2.0)

COST_WALK = 1.0
COST_DIAGONAL = SQRT2
COST_ASCEND_EXTRA = 0.6  # 起跳附加代价（1.0 + 0.6 = 1.6）
#: 起跳 1 格的真实代价（1.0 + 0.6），启发式里竖直方向用同一个值。
COST_ASCEND = COST_WALK + COST_ASCEND_EXTRA
COST_DESCEND = 1.2
COST_DESCEND_EXTRA = 0.3
#: 水里游得慢、还可能被冲走：除非没有干路，否则尽量绕开（原版也常说"少下水"）
WATER_MULTIPLIER = 6.0
DANGER_PENALTY = 4.0
ROUGH_GROUND_PENALTY = 2.0
LAVA_GOAL_COST = 100.0

#: 八方向（x, z）。先四正后四斜。
DIRECTIONS_8: tuple[tuple[int, int], ...] = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)

#: 门前缀：这些方块交给上层（开门 / 开栅栏门）处理，寻路直接当成障碍。
BARRIER_SUFFIXES = ("_door", "_fence_gate")


def _octile(dx: float, dz: float) -> float:
    """八方向网格的精确最短距离（对角 1.414）。"""
    dx = abs(dx)
    dz = abs(dz)
    return max(dx, dz) + (SQRT2 - 1.0) * min(dx, dz)


class Goal(Protocol):
    """寻路目标。"""

    def is_reached(self, pos: BlockPos) -> bool: ...


@dataclass(frozen=True)
class GoalBlock:
    """走到某个方块附近（默认允许站在相邻格）。"""

    pos: BlockPos
    radius: int = 1

    def is_reached(self, pos: BlockPos) -> bool:
        return (
            abs(pos.x - self.pos.x) <= self.radius
            and abs(pos.y - self.pos.y) <= self.radius
            and abs(pos.z - self.pos.z) <= self.radius
        )

    def distance_estimate(self, pos: BlockPos) -> float:
        """启发式：到目标包围区的八方向距离 + 竖直代价。"""
        dx = max(0, abs(pos.x - self.pos.x) - self.radius)
        dy = max(0, abs(pos.y - self.pos.y) - self.radius)
        dz = max(0, abs(pos.z - self.pos.z) - self.radius)
        return _octile(dx, dz) + COST_ASCEND * dy


@dataclass(frozen=True)
class GoalXZ:
    """只关心水平坐标（竖直方向不限）。"""

    x: int
    z: int
    radius: int = 1

    def is_reached(self, pos: BlockPos) -> bool:
        return abs(pos.x - self.x) <= self.radius and abs(pos.z - self.z) <= self.radius

    def distance_estimate(self, pos: BlockPos) -> float:
        dx = max(0, abs(pos.x - self.x) - self.radius)
        dz = max(0, abs(pos.z - self.z) - self.radius)
        return _octile(dx, dz)


@dataclass(frozen=True)
class GoalNear:
    """离目标点直线距离 ``radius`` 以内。"""

    pos: BlockPos
    radius: float

    def is_reached(self, pos: BlockPos) -> bool:
        return pos.distance_to(self.pos) <= self.radius

    def distance_estimate(self, pos: BlockPos) -> float:
        return max(0.0, pos.distance_to(self.pos) - self.radius)


@dataclass(frozen=True)
class GoalAny:
    """任意满足谓词的节点。"""

    predicate: Callable[[BlockPos], bool]

    def is_reached(self, pos: BlockPos) -> bool:
        return bool(self.predicate(pos))

    def distance_estimate(self, pos: BlockPos) -> float:
        return 0.0  # 没有方向信息，退化成 Dijkstra


class Path:
    """一串待走节点（含起点），配合跟随器逐段消费。"""

    def __init__(self, nodes: list[BlockPos] | tuple[BlockPos, ...], goal: Goal | None = None) -> None:
        self.nodes: list[BlockPos] = list(nodes)
        self.goal = goal
        self._index = 0

    @property
    def length(self) -> int:
        """节点总数（含起点）。"""
        return len(self.nodes)

    @property
    def is_done(self) -> bool:
        """是否已经走完。"""
        return self._index >= len(self.nodes)

    def current(self) -> BlockPos:
        """当前应该走向的节点。"""
        if not self.nodes:
            raise IndexError("path is empty")
        return self.nodes[min(self._index, len(self.nodes) - 1)]

    def advance(self) -> BlockPos | None:
        """前进到下一个节点；走完返回 ``None``。"""
        self._index += 1
        if self._index >= len(self.nodes):
            return None
        return self.nodes[self._index]

    def remaining(self) -> list[BlockPos]:
        """还没走的节点。"""
        return self.nodes[self._index :]

    def complete(self) -> None:
        """立即标记为走完。"""
        self._index = len(self.nodes)


class Pathfinder:
    """在 :class:`~mcbot.world.World` 上做 A* 寻路。"""

    def __init__(self, world: World, max_jump_height: int = 1, max_fall: int = 3) -> None:
        self.world = world
        self.registry = world.registry
        self.max_jump_height = max(0, int(max_jump_height))
        self.max_fall = max(0, int(max_fall))

    # ------------------------------------------------------------------ 节点判定
    def _is_barrier(self, state: int) -> bool:
        """门 / 栅栏门：交给更上层去开，寻路不走。"""
        name = self.registry.block(state).short_name
        return name.endswith(BARRIER_SUFFIXES)

    def _passable_column(self, x: int, y: int, z: int) -> bool:
        """脚部与头部两格都可通过（用于斜走不切角）。"""
        registry = self.registry
        world = self.world
        if not world.is_loaded(x, z):
            return False
        feet = world.get_state(x, y, z)
        head = world.get_state(x, y + 1, z)
        if not registry.is_passable(feet) or not registry.is_passable(head):
            return False
        return not (self._is_barrier(feet) or self._is_barrier(head))

    def can_stand(self, pos: BlockPos) -> bool:
        """``pos``（脚所在方块）是否可站立：身体两格可通过、脚下是实心或液体、不是岩浆。"""
        world = self.world
        registry = self.registry
        if not world.is_loaded(pos.x, pos.z):
            return False
        feet = world.get_state(pos.x, pos.y, pos.z)
        head = world.get_state(pos.x, pos.y + 1, pos.z)
        if not registry.is_passable(feet) or not registry.is_passable(head):
            return False
        if self._is_barrier(feet) or self._is_barrier(head):
            return False
        if registry.is_lava(feet) or registry.is_lava(head):
            return False
        below = world.get_state(pos.x, pos.y - 1, pos.z)
        if registry.is_lava(below):
            return False
        return registry.is_solid(below) or registry.is_liquid(below)

    # ------------------------------------------------------------------ 代价
    def _water_factor(self, pos: BlockPos) -> float:
        if self.registry.is_water(self.world.get_state(pos.x, pos.y, pos.z)):
            return WATER_MULTIPLIER
        return 1.0

    def _danger_penalty(self, pos: BlockPos) -> float:
        """相邻有岩浆 / 火 / 仙人掌就重罚。"""
        world = self.world
        offsets = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0), (0, 2, 0))
        for dx, dy, dz in offsets:
            if world.is_dangerous_at(pos.x + dx, pos.y + dy, pos.z + dz):
                return DANGER_PENALTY
        return 0.0

    def _rough_penalty(self, pos: BlockPos) -> float:
        """站在半砖 / 楼梯等非整方块上时略贵。"""
        registry = self.registry
        below = self.world.get_state(pos.x, pos.y - 1, pos.z)
        if registry.is_solid(below) and not registry.full_cube(below):
            return ROUGH_GROUND_PENALTY
        return 0.0

    def _node_penalty(self, pos: BlockPos) -> float:
        return self._danger_penalty(pos) + self._rough_penalty(pos)

    # ------------------------------------------------------------------ 邻居
    def _jump_clearance(self, pos: BlockPos, height: int) -> bool:
        """起跳需要的头顶净空（至少 2 格）。"""
        registry = self.registry
        world = self.world
        for y in range(pos.y + 1, pos.y + height + 2):
            if not registry.is_passable(world.get_state(pos.x, y, pos.z)):
                return False
        return True

    def _can_ascend_to(self, target: BlockPos) -> bool:
        """跳上 ``target`` 的条件：可站立，且落点是实心地面或水里。"""
        if not self.can_stand(target):
            return False
        registry = self.registry
        world = self.world
        below = world.get_state(target.x, target.y - 1, target.z)
        feet = world.get_state(target.x, target.y, target.z)
        return registry.is_solid(below) or registry.is_liquid(below) or registry.is_water(feet)

    def _fall_clear(self, pos: BlockPos, target: BlockPos) -> bool:
        """下落途中（落点头顶以上）不能有实心方块。"""
        registry = self.registry
        world = self.world
        for y in range(target.y + 1, pos.y):
            if not registry.is_passable(world.get_state(target.x, y, target.z)):
                return False
        return True

    def _is_lava_node(self, pos: BlockPos) -> bool:
        registry = self.registry
        world = self.world
        feet = world.get_state(pos.x, pos.y, pos.z)
        head = world.get_state(pos.x, pos.y + 1, pos.z)
        return registry.is_lava(feet) or registry.is_lava(head)

    def _neighbors(self, pos: BlockPos, goal: Goal) -> Iterator[tuple[BlockPos, float]]:
        """展开一个节点的所有后继。"""
        world = self.world
        for dx, dz in DIRECTIONS_8:
            diagonal = dx != 0 and dz != 0
            base = COST_DIAGONAL if diagonal else COST_WALK
            nx = pos.x + dx
            nz = pos.z + dz
            if not world.is_loaded(nx, nz):
                continue
            if diagonal:
                # 不许切角：两条正方向的邻格都必须能过人
                if not self._passable_column(pos.x + dx, pos.y, pos.z):
                    continue
                if not self._passable_column(pos.x, pos.y, pos.z + dz):
                    continue

            # 同一层
            level = BlockPos(nx, pos.y, nz)
            if self.can_stand(level):
                cost = base * self._water_factor(level) + self._node_penalty(level)
                yield level, cost
            elif goal.is_reached(level) and self._is_lava_node(level):
                # 目标就在岩浆里：允许但重罚（其余岩浆格仍然禁止）
                yield level, base + LAVA_GOAL_COST

            # 向上跳
            for height in range(1, self.max_jump_height + 1):
                if not self._jump_clearance(pos, height):
                    break
                up = BlockPos(nx, pos.y + height, nz)
                if not self._can_ascend_to(up):
                    continue
                cost = (base + COST_ASCEND_EXTRA) * height * self._water_factor(up) + self._node_penalty(up)
                yield up, cost

            # 向下掉
            for depth in range(1, self.max_fall + 1):
                down = BlockPos(nx, pos.y - depth, nz)
                if not self._fall_clear(pos, down):
                    break
                if not self.can_stand(down):
                    continue
                cost = COST_DESCEND + COST_DESCEND_EXTRA * (depth - 1) + (base - COST_WALK)
                cost = cost * self._water_factor(down) + self._node_penalty(down)
                yield down, cost

    # ------------------------------------------------------------------ 起点
    def _resolve_start(self, start: Vec3 | BlockPos) -> BlockPos | None:
        """把起点规整成可站立的节点。

        起点不可站时（悬空、或者脚陷进方块里），在竖直 ±2 格、水平 ±1 格内
        找最近的合法节点；找不到返回 ``None``。
        """
        if isinstance(start, Vec3):
            node = BlockPos(math.floor(start.x), math.floor(start.y + 0.01), math.floor(start.z))
        else:
            node = start
        if self.can_stand(node):
            return node
        best: tuple[float, int, BlockPos] | None = None
        for dy in (0, -1, -2, 1):
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    candidate = node.offset(dx, dy, dz)
                    if not self.can_stand(candidate):
                        continue
                    key = (abs(dx) + abs(dy) + abs(dz), -candidate.y, candidate)
                    if best is None or key < best:
                        best = key
        return None if best is None else best[2]

    # ------------------------------------------------------------------ 搜索
    def find_path(
        self,
        start: Vec3 | BlockPos,
        goal: Goal,
        max_nodes: int = 8000,
        max_time: float = 1.5,
    ) -> Path | None:
        """A* 搜索；超出节点 / 时间预算返回 ``None``。"""
        start_node = self._resolve_start(start)
        if start_node is None:
            return None
        if goal.is_reached(start_node):
            return Path([start_node], goal)

        deadline = time.monotonic() + float(max_time)
        estimate = getattr(goal, "distance_estimate", None)

        def heuristic(node: BlockPos) -> float:
            if estimate is None:
                return 0.0
            return float(estimate(node))

        counter = itertools.count()
        open_heap: list[tuple[float, float, int, BlockPos]] = []
        heapq.heappush(open_heap, (heuristic(start_node), 0.0, next(counter), start_node))
        g_score: dict[BlockPos, float] = {start_node: 0.0}
        came_from: dict[BlockPos, BlockPos] = {}
        closed: set[BlockPos] = set()
        expanded = 0

        while open_heap:
            _f, current_g, _seq, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            closed.add(current)
            expanded += 1
            if goal.is_reached(current):
                return self._build_path(came_from, current, goal)
            if expanded >= max_nodes:
                return None
            if time.monotonic() > deadline:
                return None
            for neighbor, step_cost in self._neighbors(current, goal):
                if neighbor in closed:
                    continue
                tentative = current_g + step_cost
                if tentative < g_score.get(neighbor, math.inf):
                    g_score[neighbor] = tentative
                    came_from[neighbor] = current
                    heapq.heappush(open_heap, (tentative + heuristic(neighbor), tentative, next(counter), neighbor))
        return None

    def find_path_to_block(self, start: Vec3 | BlockPos, target: BlockPos) -> Path | None:
        """走到目标方块旁边（挖矿 / 放方块用）。"""
        return self.find_path(start, GoalBlock(target, 1))

    @staticmethod
    def _build_path(came_from: dict[BlockPos, BlockPos], node: BlockPos, goal: Goal) -> Path:
        nodes = [node]
        while node in came_from:
            node = came_from[node]
            nodes.append(node)
        nodes.reverse()
        return Path(nodes, goal)

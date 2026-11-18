"""体素世界模型：按区块（16×16×384）存储方块状态，提供碰撞、探测与搜索。"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any, Protocol

from .registry import BlockInfo, BlockRegistry, registry as default_registry
from .types import AABB, BlockPos, Vec3

__all__ = ["ColumnLike", "World", "SimpleWorld"]


class ColumnLike(Protocol):
    """区块列需要提供的最小接口（由 :mod:`mcbot.chunk` 实现）。"""

    cx: int
    cz: int

    def get_state(self, local_x: int, y: int, local_z: int) -> int: ...

    def set_state(self, local_x: int, y: int, local_z: int, state_id: int) -> None: ...


class World:
    """已加载区块的集合。未加载区域按空气处理，``is_loaded`` 用于区分。"""

    def __init__(self, registry: BlockRegistry | None = None, min_y: int = -64, height: int = 384) -> None:
        self.registry = registry or default_registry()
        self.min_y = min_y
        self.height = height
        self.max_y = min_y + height - 1
        self.columns: dict[tuple[int, int], ColumnLike] = {}
        self.dimension = "minecraft:overworld"
        self.day_time = 0
        self.raining = False
        self.thundering = False

    # ------------------------------------------------------------------ 区块
    def column(self, chunk_x: int, chunk_z: int) -> ColumnLike | None:
        return self.columns.get((chunk_x, chunk_z))

    def add_column(self, column: ColumnLike) -> None:
        self.columns[(column.cx, column.cz)] = column

    def remove_column(self, chunk_x: int, chunk_z: int) -> None:
        self.columns.pop((chunk_x, chunk_z), None)

    def is_loaded(self, x: int, z: int) -> bool:
        return (x >> 4, z >> 4) in self.columns

    def is_column_loaded(self, chunk_x: int, chunk_z: int) -> bool:
        return (chunk_x, chunk_z) in self.columns

    def clear(self) -> None:
        self.columns.clear()

    def loaded_chunk_count(self) -> int:
        return len(self.columns)

    # ------------------------------------------------------------------ 方块
    def get_state(self, x: int, y: int, z: int) -> int:
        if y < self.min_y or y > self.max_y:
            return 0
        column = self.columns.get((x >> 4, z >> 4))
        if column is None:
            return 0
        return column.get_state(x & 15, y, z & 15)

    def set_state(self, x: int, y: int, z: int, state_id: int) -> None:
        column = self.columns.get((x >> 4, z >> 4))
        if column is None:
            return
        column.set_state(x & 15, y, z & 15, state_id)

    def get_block(self, x: int, y: int, z: int) -> BlockInfo:
        return self.registry.block(self.get_state(x, y, z))

    def block_at(self, pos: BlockPos) -> BlockInfo:
        return self.registry.block(self.get_state(pos.x, pos.y, pos.z))

    def is_solid_at(self, x: int, y: int, z: int) -> bool:
        return self.registry.is_solid(self.get_state(x, y, z))

    def is_passable_at(self, x: int, y: int, z: int) -> bool:
        return self.registry.is_passable(self.get_state(x, y, z))

    def is_liquid_at(self, x: int, y: int, z: int) -> bool:
        return self.registry.is_liquid(self.get_state(x, y, z))

    def is_dangerous_at(self, x: int, y: int, z: int) -> bool:
        return self.registry.is_dangerous(self.get_state(x, y, z))

    def is_climbable_at(self, x: int, y: int, z: int) -> bool:
        return self.registry.is_climbable(self.get_state(x, y, z))

    def block_name(self, x: int, y: int, z: int) -> str:
        return self.registry.block_name(self.get_state(x, y, z))

    # ------------------------------------------------------------------ 碰撞
    def collision_boxes(self, box: AABB) -> list[AABB]:
        """返回与给定包围盒相交的所有方块碰撞箱（世界坐标）。"""
        boxes: list[AABB] = []
        x_start = math.floor(box.min_x)
        x_end = math.floor(box.max_x - 1e-9)
        z_start = math.floor(box.min_z)
        z_end = math.floor(box.max_z - 1e-9)
        y_start = max(self.min_y, math.floor(box.min_y))
        y_end = min(self.max_y, math.floor(box.max_y - 1e-9))
        for y in range(y_start, y_end + 1):
            for x in range(x_start, x_end + 1):
                for z in range(z_start, z_end + 1):
                    state = self.get_state(x, y, z)
                    if not state:
                        continue
                    for local in self.registry.boxes(state):
                        world_box = local.offset(x, y, z)
                        if world_box.intersects(box):
                            boxes.append(world_box)
        return boxes

    # ------------------------------------------------------------------ 探测
    def highest_solid_y(self, x: int, z: int, max_y: int | None = None, min_y: int | None = None) -> int | None:
        """返回该 (x, z) 列最高的实心方块 y；找不到返回 ``None``。"""
        top = self.max_y if max_y is None else min(max_y, self.max_y)
        bottom = self.min_y if min_y is None else max(min_y, self.min_y)
        for y in range(top, bottom - 1, -1):
            if self.is_solid_at(x, y, z):
                return y
        return None

    def surface_y(self, x: int, z: int) -> float | None:
        """该列可站立的地面高度（方块顶面 y），找不到返回 ``None``。"""
        y = self.highest_solid_y(x, z)
        if y is None:
            return None
        return float(y + 1)

    def is_safe_standing(self, pos: BlockPos) -> bool:
        """站在 pos 处是否安全（脚下方块实心、身体两格可通过、不站在危险方块上）。"""
        feet = self.get_state(pos.x, pos.y, pos.z)
        head = self.get_state(pos.x, pos.y + 1, pos.z)
        below = self.get_state(pos.x, pos.y - 1, pos.z)
        if not self.registry.is_passable(feet) or not self.registry.is_passable(head):
            return False
        if not self.registry.is_solid(below):
            return False
        if self.registry.is_dangerous(below) or self.registry.is_dangerous(feet):
            return False
        return True

    # ------------------------------------------------------------------ 搜索
    def iter_blocks_in_box(
        self,
        minimum: BlockPos,
        maximum: BlockPos,
        names: frozenset[str] | None = None,
    ) -> Iterator[BlockPos]:
        for y in range(max(self.min_y, minimum.y), min(self.max_y, maximum.y) + 1):
            for x in range(minimum.x, maximum.x + 1):
                for z in range(minimum.z, maximum.z + 1):
                    if not self.is_loaded(x, z):
                        continue
                    state = self.get_state(x, y, z)
                    if not state:
                        continue
                    if names is not None and self.registry.block_name(state) not in names:
                        continue
                    yield BlockPos(x, y, z)

    def find_blocks(
        self,
        names: set[str] | frozenset[str],
        center: BlockPos | Vec3,
        radius: int = 32,
        limit: int = 32,
        vertical_radius: int | None = None,
    ) -> list[BlockPos]:
        """在 ``center`` 附近搜索指定方块，按距离从近到远返回。"""
        if isinstance(center, Vec3):
            center = center.floor()
        vertical = radius if vertical_radius is None else vertical_radius
        full_names = frozenset(name if ":" in name else f"minecraft:{name}" for name in names)
        minimum = BlockPos(center.x - radius, max(self.min_y, center.y - vertical), center.z - radius)
        maximum = BlockPos(center.x + radius, min(self.max_y, center.y + vertical), center.z + radius)
        found: list[tuple[float, BlockPos]] = []
        for pos in self.iter_blocks_in_box(minimum, maximum, full_names):
            found.append((pos.distance_to(center), pos))
        found.sort(key=lambda item: item[0])
        return [pos for _distance, pos in found[:limit]]

    def nearby_blocks(self, pos: BlockPos, radius: int = 2) -> dict[BlockPos, BlockInfo]:
        result: dict[BlockPos, BlockInfo] = {}
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dz in range(-radius, radius + 1):
                    target = pos.offset(dx, dy, dz)
                    result[target] = self.block_at(target)
        return result


class SimpleWorld(World):
    """测试 / 模拟用世界：方块存在内存字典里，所有区块都视为已加载。"""

    def __init__(self, registry: BlockRegistry | None = None, min_y: int = -64, height: int = 384) -> None:
        super().__init__(registry, min_y, height)
        self._blocks: dict[tuple[int, int, int], int] = {}

    def get_state(self, x: int, y: int, z: int) -> int:
        return self._blocks.get((x, y, z), 0)

    def set_state(self, x: int, y: int, z: int, state_id: int) -> None:
        if state_id == 0:
            self._blocks.pop((x, y, z), None)
        else:
            self._blocks[(x, y, z)] = state_id

    def is_loaded(self, x: int, z: int) -> bool:
        return True

    def is_column_loaded(self, chunk_x: int, chunk_z: int) -> bool:
        return True

    def clear(self) -> None:
        self._blocks.clear()

    def fill_box(self, minimum: BlockPos, maximum: BlockPos, state_id: int) -> None:
        for x in range(minimum.x, maximum.x + 1):
            for y in range(minimum.y, maximum.y + 1):
                for z in range(minimum.z, maximum.z + 1):
                    self.set_state(x, y, z, state_id)

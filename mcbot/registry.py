"""方块 / 物品 / 实体注册表。

数据来自 PrismarineJS minecraft-data（见 ``tools/gen_data.py``），压缩后随插件
一起分发，运行时不联网、不依赖第三方库。

方块状态 id 是全局状态 id（1.20.1 的 registry 顺序），与服务器下发的一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .types import AABB

__all__ = ["BlockInfo", "ItemInfo", "BlockRegistry", "DEFAULT_DATA_PATH"]

DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "data" / "mc_1_20_1.json"

FLAG_DIGGABLE = 1
FLAG_TRANSPARENT = 2
FLAG_LIQUID = 4
FLAG_CLIMBABLE = 8

#: 会把玩家弄伤/弄死的方块（寻路时重罚，主动行为里避开）。
DANGEROUS_BLOCKS = frozenset(
    {
        "lava",
        "fire",
        "soul_fire",
        "magma_block",
        "cactus",
        "sweet_berry_bush",
        "powder_snow",
        "campfire",
        "soul_campfire",
        "wither_rose",
        "pointed_dripstone",
        "dragon_egg",
    }
)

#: 可以直接被方块替换（右键放方块时会被覆盖）的方块。
REPLACEABLE_BLOCKS = frozenset(
    {
        "air",
        "cave_air",
        "void_air",
        "water",
        "lava",
        "short_grass",
        "grass",
        "fern",
        "large_fern",
        "dead_bush",
        "snow",
        "vine",
        "seagrass",
        "tall_seagrass",
        "tall_grass",
        "sunflower",
        "lilac",
        "rose_bush",
        "peony",
        "torch",
        "redstone_wire",
        "rail",
        "powered_rail",
        "detector_rail",
        "activator_rail",
        "light",
    }
)

AIR_BLOCKS = frozenset({"air", "cave_air", "void_air"})


@dataclass(frozen=True, slots=True)
class BlockInfo:
    """一种方块的静态属性（默认状态）。"""

    name: str
    state_id: int
    hardness: float
    diggable: bool
    transparent: bool
    liquid: bool
    climbable: bool
    tool_class: str
    min_tool_level: int

    @property
    def short_name(self) -> str:
        return self.name.split(":", 1)[-1]

    @property
    def requires_tool(self) -> bool:
        return self.min_tool_level >= 0


@dataclass(frozen=True, slots=True)
class ItemInfo:
    """物品（网络物品 id）。"""

    id: int
    name: str
    stack_size: int

    @property
    def short_name(self) -> str:
        return self.name.split(":", 1)[-1]

    @property
    def tool_class(self) -> str:
        """粗略判断物品属于哪种工具：pickaxe/axe/shovel/hoe/sword/shears。"""
        short = self.short_name
        for kind in ("pickaxe", "shovel", "axe", "hoe", "sword", "shears"):
            if short == kind or short.endswith("_" + kind):
                return kind
        return ""

    @property
    def tool_level(self) -> int:
        """工具的采集等级：木/金 0，石 1，铁 2，钻石 3，下界合金 4。"""
        short = self.short_name
        for prefix, level in (("wooden", 0), ("golden", 0), ("stone", 1), ("iron", 2), ("diamond", 3), ("netherite", 4)):
            if short.startswith(prefix + "_"):
                return level
        return -1


class BlockRegistry:
    """方块 / 物品 / 实体数据查询入口（进程内单例足够）。"""

    def __init__(self, data_path: Path | str | None = None) -> None:
        path = Path(data_path) if data_path else DEFAULT_DATA_PATH
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)

        self.minecraft_version: str = payload["version"]
        self.protocol_version: int = payload["protocol"]
        self.min_y: int = int(payload.get("min_y", -64))
        self.height: int = int(payload.get("height", 384))
        self.max_state_id: int = 0

        blocks: list[BlockInfo] = []
        state_block: list[int] = []
        state_shape: list[int] = []
        self._shapes: list[tuple[tuple[float, float, float, float, float, float], ...]] = [
            tuple(tuple(float(v) for v in box) for box in boxes) for boxes in payload["shapes"]
        ]
        self._blocks_by_name: dict[str, BlockInfo] = {}
        self._default_state: dict[str, int] = {}

        for row in payload["blocks"]:
            name, min_state, max_state, hardness_milli, flags, tool_class, min_level, default_state, shape_spec = row
            info = BlockInfo(
                name=f"minecraft:{name}",
                state_id=int(default_state),
                hardness=int(hardness_milli) / 1000.0,
                diggable=bool(flags & FLAG_DIGGABLE),
                transparent=bool(flags & FLAG_TRANSPARENT),
                liquid=bool(flags & FLAG_LIQUID),
                climbable=bool(flags & FLAG_CLIMBABLE),
                tool_class=tool_class,
                min_tool_level=int(min_level),
            )
            blocks.append(info)
            self._blocks_by_name[info.name] = info
            self._default_state[info.name] = int(default_state)
            self.max_state_id = max(self.max_state_id, int(max_state))
            span = int(max_state) - int(min_state) + 1
            if isinstance(shape_spec, list):
                if len(shape_spec) != span:
                    shape_spec = (shape_spec + [0] * span)[:span]
                state_shape.extend(int(v) for v in shape_spec)
            else:
                state_shape.extend([int(shape_spec)] * span)
            state_block.extend([len(blocks) - 1] * span)

        # 极少数状态空洞：用 0 号方块（空气）兜底
        if len(state_block) <= self.max_state_id:
            filler = self.max_state_id + 1 - len(state_block)
            state_block.extend([0] * filler)
            state_shape.extend([0] * filler)

        self._blocks = blocks
        self._state_block = state_block
        self._state_shape = state_shape

        self._items: dict[int, ItemInfo] = {}
        self._item_by_name: dict[str, int] = {}
        for raw_id, (name, stack_size) in payload["items"].items():
            item = ItemInfo(id=int(raw_id), name=f"minecraft:{name}", stack_size=int(stack_size))
            self._items[item.id] = item
            self._item_by_name[item.name] = item.id

        self._foods: dict[int, tuple[float, float]] = {
            int(raw_id): (float(points), float(saturation)) for raw_id, (points, saturation) in payload["foods"].items()
        }
        self._entities: dict[int, str] = {
            int(type_id): f"minecraft:{name}" for type_id, name in payload["entities"].items()
        }
        self._loot: dict[str, list] = {
            f"minecraft:{name}": drops for name, drops in payload.get("loot", {}).items()
        }
        self._box_cache: dict[int, tuple[AABB, ...]] = {}

    # ------------------------------------------------------------------ 方块
    def block(self, state_id: int) -> BlockInfo:
        if state_id < 0 or state_id >= len(self._state_block):
            return self._blocks[0]
        return self._blocks[self._state_block[state_id]]

    def block_name(self, state_id: int) -> str:
        return self.block(state_id).name

    def state_id(self, name: str) -> int:
        """按方块名取默认状态 id；未知方块抛 ``KeyError``。"""
        full = name if ":" in name else f"minecraft:{name}"
        return self._default_state[full]

    def has_block(self, name: str) -> bool:
        full = name if ":" in name else f"minecraft:{name}"
        return full in self._default_state

    def boxes(self, state_id: int) -> tuple[AABB, ...]:
        """该状态方块的碰撞箱（局部坐标，世界坐标由调用方平移）。"""
        cached = self._box_cache.get(state_id)
        if cached is not None:
            return cached
        if state_id < 0 or state_id >= len(self._state_shape):
            return ()
        shape_id = self._state_shape[state_id]
        if shape_id < 0 or shape_id >= len(self._shapes):
            return ()
        boxes = tuple(AABB(*box) for box in self._shapes[shape_id])
        if len(self._box_cache) < 4096:
            self._box_cache[state_id] = boxes
        return boxes

    def is_air(self, state_id: int) -> bool:
        return self.block(state_id).short_name in AIR_BLOCKS

    def is_water(self, state_id: int) -> bool:
        return self.block(state_id).short_name in ("water", "flowing_water")

    def is_lava(self, state_id: int) -> bool:
        return self.block(state_id).short_name in ("lava", "flowing_lava")

    def is_liquid(self, state_id: int) -> bool:
        return self.block(state_id).liquid

    def is_solid(self, state_id: int) -> bool:
        """有碰撞体积（可以站上去 / 会挡路）。"""
        return bool(self.boxes(state_id))

    def is_passable(self, state_id: int) -> bool:
        return not self.boxes(state_id)

    def is_climbable(self, state_id: int) -> bool:
        return self.block(state_id).climbable

    def is_dangerous(self, state_id: int) -> bool:
        return self.block(state_id).short_name in DANGEROUS_BLOCKS

    def is_replaceable(self, state_id: int) -> bool:
        name = self.block(state_id).short_name
        return name in REPLACEABLE_BLOCKS or name.endswith("_air")

    def hardness(self, state_id: int) -> float:
        return self.block(state_id).hardness

    def full_cube(self, state_id: int) -> bool:
        """是否整格实心（用于快速判断可站立地面）。"""
        boxes = self.boxes(state_id)
        if not boxes:
            return False
        return any(
            box.min_x <= 1e-6 and box.min_y <= 1e-6 and box.min_z <= 1e-6 and box.max_x >= 1 - 1e-6 and box.max_y >= 1 - 1e-6 and box.max_z >= 1 - 1e-6
            for box in boxes
        )

    def blocks_matching(self, keyword: str) -> list[BlockInfo]:
        """按名字片段搜索方块（给命令补全 / LLM 用）。"""
        key = keyword.lower()
        return [block for block in self._blocks if key in block.name]

    # ------------------------------------------------------------------ 物品
    def item(self, item_id: int) -> ItemInfo | None:
        return self._items.get(item_id)

    def item_id(self, name: str) -> int | None:
        full = name if ":" in name else f"minecraft:{name}"
        return self._item_by_name.get(full)

    def food(self, item_id: int) -> tuple[float, float] | None:
        return self._foods.get(item_id)

    def items_matching(self, keyword: str) -> list[ItemInfo]:
        key = keyword.lower()
        return [item for item in self._items.values() if key in item.name]

    # ------------------------------------------------------------------ 实体
    def entity_name(self, type_id: int) -> str:
        return self._entities.get(type_id, f"minecraft:unknown_{type_id}")

    # ------------------------------------------------------------------ 掉落
    def loot(self, block_name: str) -> list:
        return self._loot.get(block_name, [])

    def expected_drops(self, block_name: str) -> list[str]:
        """该方块在无附魔工具下最可能的掉落物名称列表。"""
        drops = []
        for item, chance, _low, _high, silk, no_silk in self.loot(block_name):
            if silk or chance <= 0:
                continue
            drops.append(f"minecraft:{item}")
        return drops


_singleton: BlockRegistry | None = None


def registry() -> BlockRegistry:
    """进程级共享注册表（避免重复加载 600KB JSON）。"""
    global _singleton
    if _singleton is None:
        _singleton = BlockRegistry()
    return _singleton


@lru_cache(maxsize=512)
def block_state(name: str) -> int:
    return registry().state_id(name)

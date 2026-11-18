"""玩家 2x2 合成格与 3x3 合成台的配方 / 合成规划（纯逻辑）。

不联网、不读文件、不依赖游戏服务器：输入是背包物品统计，输出是合成步骤。
所有物品名都是不带 ``minecraft:`` 前缀的短名；具体是否存在由
:class:`~mcbot.registry.BlockRegistry` 的 ``item_id`` 校验，未知物品的配方
会被跳过。

坐标约定
--------
``Recipe.pattern`` 是 ``(row, col, 物品短名)`` 三元组，``row`` / ``col`` 从
0 开始：``size=2`` 对应玩家物品栏里的 2x2 合成格，``size=3`` 对应合成台的
3x3 网格。令牌 ``any_planks`` 表示“任意一种木板”（原木配方产出的木板以及
竹板），匹配时展开成木板总数，实际摆放时由调用方选择一种具体木板。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ItemStack
    from .registry import BlockRegistry

__all__ = [
    "ANY_PLANKS",
    "CRAFTING_TABLE",
    "PLANK_TYPES",
    "RECIPES",
    "WOOD_TYPES",
    "Recipe",
    "candidates",
    "inventory_counts",
    "plan",
    "to_grid_slots",
    "to_pattern_slots",
]

#: 通用木板令牌：匹配时按所有木板种类求和，摆放时替换成具体木板。
ANY_PLANKS = "any_planks"

CRAFTING_TABLE = "crafting_table"

#: (原木短名, 对应木板短名)。木头与木板必须一一对应。
WOOD_TYPES: tuple[tuple[str, str], ...] = (
    ("oak_log", "oak_planks"),
    ("birch_log", "birch_planks"),
    ("spruce_log", "spruce_planks"),
    ("jungle_log", "jungle_planks"),
    ("acacia_log", "acacia_planks"),
    ("dark_oak_log", "dark_oak_planks"),
    ("mangrove_log", "mangrove_planks"),
    ("cherry_log", "cherry_planks"),
    ("pale_oak_log", "pale_oak_planks"),
    ("crimson_stem", "crimson_planks"),
    ("warped_stem", "warped_planks"),
)

#: 所有能当作 ``any_planks`` 使用的木板短名（含没有原木配方的竹板）。
PLANK_TYPES: tuple[str, ...] = tuple(planks for _log, planks in WOOD_TYPES) + ("bamboo_planks",)


@dataclass(frozen=True, slots=True)
class Recipe:
    """一条合成配方。

    :param name: 输出物品短名。
    :param output_count: 每次合成产出的物品数量。
    :param size: 网格边长（2=玩家 2x2 合成格，3=合成台 3x3）。
    :param pattern: ``(row, col, 物品短名)``，行列从 0 开始；空槽不出现。
    :param ingredients: ``(物品短名, 数量)``；``any_planks`` 表示任意木板。
    """

    name: str
    output_count: int
    size: int
    pattern: tuple[tuple[int, int, str], ...]
    ingredients: tuple[tuple[str, int], ...]

    @property
    def grid(self) -> tuple[tuple[int, str], ...]:
        """兼容旧接口的“槽位 → 物品”视图。

        槽位按行优先编号（``1 .. size*size``）：2x2 时就是玩家合成格的
        1=左上、2=右上、3=左下、4=右下；3x3 时为 1..9。
        """
        return tuple(sorted((row * self.size + col + 1, item) for row, col, item in self.pattern))


def _make_recipe(name: str, output_count: int, layout: tuple[str, ...]) -> Recipe:
    """从可读的文本网格构造配方，并自动统计 ingredients。

    ``layout`` 每一行用空格分隔，``.`` 表示空槽。
    """
    size = len(layout)
    pattern: list[tuple[int, int, str]] = []
    counts: dict[str, int] = {}
    for row, line in enumerate(layout):
        cells = line.split()
        if len(cells) != size:
            raise ValueError(f"配方 {name} 第 {row} 行应有 {size} 个格子，实际 {len(cells)} 个")
        for col, cell in enumerate(cells):
            if cell == ".":
                continue
            pattern.append((row, col, cell))
            counts[cell] = counts.get(cell, 0) + 1
    return Recipe(
        name=name,
        output_count=output_count,
        size=size,
        pattern=tuple(pattern),
        ingredients=tuple(counts.items()),
    )


def _build_recipes() -> tuple[Recipe, ...]:
    recipes: list[Recipe] = []

    # 原木 -> 对应木板：1 个原木换 4 块木板。
    for log, planks in WOOD_TYPES:
        recipes.append(_make_recipe(planks, 4, (f"{log} .", ". .")))

    # ---- 2x2：玩家合成格 ----
    recipes.append(_make_recipe("stick", 4, ("any_planks .", "any_planks .")))
    recipes.append(_make_recipe("crafting_table", 1, ("any_planks any_planks", "any_planks any_planks")))
    # 火把在 2x2 里也能做（燃料在上、木棍在下）。
    recipes.append(_make_recipe("torch", 4, ("coal .", "stick .")))
    recipes.append(_make_recipe("torch", 4, ("charcoal .", "stick .")))

    # ---- 3x3：合成台 ----
    # 火把在 3x3 里同样竖着放。
    recipes.append(_make_recipe("torch", 4, (". coal .", ". stick .", ". . .")))
    recipes.append(_make_recipe("torch", 4, (". charcoal .", ". stick .", ". . .")))

    # 木制工具：材料在顶部，木棍在中列。
    recipes.append(
        _make_recipe(
            "wooden_pickaxe",
            1,
            ("any_planks any_planks any_planks", ". stick .", ". stick ."),
        )
    )
    recipes.append(
        _make_recipe(
            "wooden_axe",
            1,
            ("any_planks any_planks .", "any_planks stick .", ". stick ."),
        )
    )
    recipes.append(_make_recipe("wooden_shovel", 1, ("any_planks . .", "stick . .", "stick . .")))
    recipes.append(_make_recipe("wooden_sword", 1, ("any_planks . .", "any_planks . .", "stick . .")))

    # 石制工具：用圆石代替木板，形状相同。
    recipes.append(
        _make_recipe("stone_pickaxe", 1, ("cobblestone cobblestone cobblestone", ". stick .", ". stick ."))
    )
    recipes.append(
        _make_recipe("stone_axe", 1, ("cobblestone cobblestone .", "cobblestone stick .", ". stick ."))
    )
    recipes.append(_make_recipe("stone_shovel", 1, ("cobblestone . .", "stick . .", "stick . .")))
    recipes.append(_make_recipe("stone_sword", 1, ("cobblestone . .", "cobblestone . .", "stick . .")))

    # 熔炉 / 箱子：8 个材料围成一圈，中心留空。
    recipes.append(
        _make_recipe(
            "furnace",
            1,
            (
                "cobblestone cobblestone cobblestone",
                "cobblestone . cobblestone",
                "cobblestone cobblestone cobblestone",
            ),
        )
    )
    recipes.append(
        _make_recipe(
            "chest",
            1,
            (
                "any_planks any_planks any_planks",
                "any_planks . any_planks",
                "any_planks any_planks any_planks",
            ),
        )
    )
    return tuple(recipes)


#: 全部内置配方，顺序即 :func:`candidates` 的返回顺序。
RECIPES: tuple[Recipe, ...] = _build_recipes()


def _short_name(name: str) -> str:
    return name.split(":", 1)[-1]


def _normalise_counts(counts: dict[str, int]) -> dict[str, int]:
    """复制并规范化背包统计：短名、丢弃非正数、合并同名条目。"""
    result: dict[str, int] = {}
    for name, count in counts.items():
        if not isinstance(name, str):
            continue
        value = int(count)
        if value <= 0:
            continue
        short = _short_name(name)
        result[short] = result.get(short, 0) + value
    return result


def _name_known(registry: BlockRegistry, name: str) -> bool:
    return name == ANY_PLANKS or registry.item_id(name) is not None


def _has_valid_shape(recipe: Recipe) -> bool:
    if recipe.size not in (2, 3) or recipe.output_count <= 0:
        return False
    seen: set[tuple[int, int]] = set()
    pattern_counts: dict[str, int] = {}
    for row, col, item in recipe.pattern:
        if not (0 <= row < recipe.size and 0 <= col < recipe.size):
            return False
        if (row, col) in seen:
            return False
        seen.add((row, col))
        pattern_counts[item] = pattern_counts.get(item, 0) + 1
    ingredient_counts: dict[str, int] = {}
    for item, amount in recipe.ingredients:
        if amount <= 0:
            return False
        ingredient_counts[item] = ingredient_counts.get(item, 0) + amount
    return bool(seen) and pattern_counts == ingredient_counts


def _valid_recipes(registry: BlockRegistry) -> list[Recipe]:
    """返回注册表中所有物品都存在且形状合法的配方。"""
    valid: list[Recipe] = []
    for recipe in RECIPES:
        if not _has_valid_shape(recipe):
            continue
        names = [recipe.name]
        names.extend(item for _row, _col, item in recipe.pattern)
        names.extend(item for item, _amount in recipe.ingredients)
        if all(_name_known(registry, name) for name in names):
            valid.append(recipe)
    return valid


def _group_by_output(recipes: list[Recipe]) -> dict[str, list[Recipe]]:
    grouped: dict[str, list[Recipe]] = {}
    for recipe in recipes:
        grouped.setdefault(recipe.name, []).append(recipe)
    return grouped


def _plank_total(counts: dict[str, int]) -> int:
    return sum(counts.get(plank, 0) for plank in PLANK_TYPES)


def _recipe_affordable(recipe: Recipe, counts: dict[str, int]) -> bool:
    needed: dict[str, int] = {}
    for item, amount in recipe.ingredients:
        needed[item] = needed.get(item, 0) + amount
    for item, amount in needed.items():
        if item == ANY_PLANKS:
            if _plank_total(counts) < amount:
                return False
        elif counts.get(item, 0) < amount:
            return False
    return True


def inventory_counts(inventory: dict[int, ItemStack | None], registry: BlockRegistry) -> dict[str, int]:
    """把背包（槽位 -> ItemStack）汇总成 ``物品短名 -> 总数``。

    跳过 ``None``、数量 <= 0、物品 id <= 0 以及注册表里查不到的空物品；
    同一物品出现在多个槽位时累加。
    """
    totals: dict[str, int] = {}
    for stack in inventory.values():
        if stack is None:
            continue
        item_id = int(getattr(stack, "item_id", 0) or 0)
        count = int(getattr(stack, "count", 0) or 0)
        if item_id <= 0 or count <= 0:
            continue
        info = registry.item(item_id)
        if info is None:
            continue
        totals[info.short_name] = totals.get(info.short_name, 0) + count
    return totals


def candidates(registry: BlockRegistry, counts: dict[str, int], *, max: int = 8) -> list[Recipe]:
    """按“当前材料是否直接足够”筛出可合成配方（按 :data:`RECIPES` 顺序）。

    ``any_planks`` 会展开成所有木板的总数；不会递归计算可先合成的中间材料，
    那由 :func:`plan` 负责。3x3 配方在这里只检查原料，合成台由 :func:`plan`
    按需补做。``max`` 限制返回条数（<=0 时返回空列表）。
    """
    if max <= 0:
        return []
    totals = _normalise_counts(counts)
    result: list[Recipe] = []
    for recipe in _valid_recipes(registry):
        if _recipe_affordable(recipe, totals):
            result.append(recipe)
            if len(result) >= max:
                break
    return result


def to_pattern_slots(recipe: Recipe, chosen_planks: str) -> dict[tuple[int, int], str]:
    """返回 ``{(row, col): 具体物品短名}``，并把 ``any_planks`` 换成指定木板。"""
    chosen = _short_name(chosen_planks)
    return {
        (row, col): chosen if item == ANY_PLANKS else item
        for row, col, item in recipe.pattern
    }


def to_grid_slots(recipe: Recipe, chosen_planks: str) -> dict[int, str]:
    """返回 ``{槽位: 具体物品短名}``，``any_planks`` 用 ``chosen_planks`` 替换。

    槽位按行优先编号：2x2 配方为 1..4（1=左上、2=右上、3=左下、4=右下），
    3x3 配方为 1..9。
    """
    slots = to_pattern_slots(recipe, chosen_planks)
    return {row * recipe.size + col + 1: item for (row, col), item in sorted(slots.items())}


# --------------------------------------------------------------------------- 规划


@dataclass(slots=True)
class _PlanState:
    stock: dict[str, int]
    steps: list[Recipe] = field(default_factory=list)

    def clone(self) -> _PlanState:
        return _PlanState(dict(self.stock), list(self.steps))


def _consume_planks(amount: int, state: _PlanState) -> int:
    """按木板种类顺序消耗最多 ``amount`` 块木板，返回还缺多少。"""
    missing = amount
    for plank in PLANK_TYPES:
        if missing <= 0:
            break
        available = state.stock.get(plank, 0)
        if available <= 0:
            continue
        take = min(available, missing)
        state.stock[plank] = available - take
        missing -= take
    return missing


def _craft(
    registry: BlockRegistry,
    recipe: Recipe,
    times: int,
    state: _PlanState,
    active: frozenset[Recipe],
    by_output: dict[str, list[Recipe]],
) -> bool:
    """在 ``state`` 上尝试连续执行 ``recipe`` ``times`` 次（会修改 state）。"""
    if times <= 0:
        return True
    if recipe in active:
        return False
    next_active = active | {recipe}
    for item, per_craft in recipe.ingredients:
        if not _ensure(registry, item, per_craft * times, state, next_active, by_output):
            return False
    # 3x3 配方需要合成台；合成台是工具，用过不消耗。只有背包里没有时才补做。
    if recipe.size >= 3 and not _ensure_table(registry, state, next_active, by_output):
        return False
    state.stock[recipe.name] = state.stock.get(recipe.name, 0) + recipe.output_count * times
    state.steps.extend([recipe] * times)
    return True


def _ensure_table(
    registry: BlockRegistry,
    state: _PlanState,
    active: frozenset[Recipe],
    by_output: dict[str, list[Recipe]],
) -> bool:
    if state.stock.get(CRAFTING_TABLE, 0) > 0:
        return True
    for recipe in by_output.get(CRAFTING_TABLE, ()):
        if recipe in active:
            continue
        trial = state.clone()
        if _craft(registry, recipe, 1, trial, active, by_output) and trial.stock.get(CRAFTING_TABLE, 0) > 0:
            state.stock = trial.stock
            state.steps = trial.steps
            return True
    return False


def _ensure_planks(
    registry: BlockRegistry,
    amount: int,
    state: _PlanState,
    active: frozenset[Recipe],
    by_output: dict[str, list[Recipe]],
) -> bool:
    """保证手里有 ``amount`` 块木板（任意种类）；不够则先用原木合成。"""
    if amount <= 0:
        return True
    missing = _consume_planks(amount, state)
    if missing <= 0:
        return True
    if not _craft_planks(registry, missing, state, active, by_output):
        return False
    return _consume_planks(missing, state) == 0


def _craft_planks(
    registry: BlockRegistry,
    amount: int,
    state: _PlanState,
    active: frozenset[Recipe],
    by_output: dict[str, list[Recipe]],
) -> bool:
    """额外合成至少 ``amount`` 块木板（可以混用多种原木）。"""
    if amount <= 0:
        return True
    sources = [recipe for plank in PLANK_TYPES for recipe in by_output.get(plank, ())]
    for recipe in sources:
        if recipe in active:
            continue
        max_crafts = (amount + recipe.output_count - 1) // recipe.output_count
        for crafts in range(max_crafts, 0, -1):
            trial = state.clone()
            if not _craft(registry, recipe, crafts, trial, active, by_output):
                continue
            made = crafts * recipe.output_count
            if made >= amount or _craft_planks(registry, amount - made, trial, active, by_output):
                state.stock = trial.stock
                state.steps = trial.steps
                return True
    return False


def _ensure(
    registry: BlockRegistry,
    item: str,
    amount: int,
    state: _PlanState,
    active: frozenset[Recipe],
    by_output: dict[str, list[Recipe]],
) -> bool:
    """保证 ``state`` 里有（并消耗掉）``amount`` 个 ``item``。"""
    if amount <= 0:
        return True
    if item == ANY_PLANKS:
        return _ensure_planks(registry, amount, state, active, by_output)

    have = state.stock.get(item, 0)
    if have >= amount:
        state.stock[item] = have - amount
        return True

    deficit = amount - have
    for recipe in by_output.get(item, ()):
        if recipe in active:
            continue
        crafts = (deficit + recipe.output_count - 1) // recipe.output_count
        trial = state.clone()
        if not _craft(registry, recipe, crafts, trial, active, by_output):
            continue
        if trial.stock.get(item, 0) < amount:
            continue
        trial.stock[item] -= amount
        state.stock = trial.stock
        state.steps = trial.steps
        return True
    return False


def plan(
    registry: BlockRegistry,
    counts: dict[str, int],
    target: str,
    times: int = 1,
) -> list[Recipe] | None:
    """规划合成 ``target`` 的步骤链；材料不够或目标无配方时返回 ``None``。

    ``times`` 是执行目标配方的次数（不是期望产出的个数）：目标配方产出 4 个
    时，``times=1`` 就得到 4 个。依赖按先后顺序展开：先合成中间材料，再合成
    目标；合成 3x3 配方时如果背包里没有合成台，还会先插入制作合成台的步骤
    （合成台不消耗）。``times <= 0`` 时返回空列表。
    """
    target_name = _short_name(target)
    if registry.item_id(target_name) is None:
        return None

    by_output = _group_by_output(_valid_recipes(registry))
    options = by_output.get(target_name)
    if not options:
        return None
    if times <= 0:
        return []

    state = _PlanState(_normalise_counts(counts))
    for _ in range(times):
        for recipe in options:
            trial = state.clone()
            if _craft(registry, recipe, 1, trial, frozenset(), by_output):
                state = trial
                break
        else:
            return None
    return state.steps

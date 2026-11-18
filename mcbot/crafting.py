"""合成执行器：通过容器点击协议真的在游戏里合成。

Minecraft Java 版 26.2 的合成不是"发一条合成指令"，而是像真人一样把材料
放进合成格，再点输出格。这里把这件事拆成可测试的步骤：

* 2x2 用玩家自带合成格（窗口 0，槽位 1–4；输出槽 0）；
* 3x3 需要合成台（窗口类型 ``minecraft:crafting`` = 12，槽位 1–9，输出 0）。

槽位/点击语义来自官方 26.2 jar 的 ``ServerboundContainerClickPacket`` 与
``CraftingMenu``（javap 核实：结果槽 0、合成格 1–9、背包 10–36、快捷栏 37–45）。

安全阀：任何一步对不上（点击后背包没变化、输出槽为空、state id 不同步）都会
中止并把已放进合成格的材料放回背包，不会硬着头皮瞎点。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

__all__ = ["CraftingController", "grid_window_slots", "CRAFTING_MENU_ID"]

TICK = 0.05
CRAFTING_MENU_ID = 12  # registry: minecraft:crafting = 12（官方 registries 报告）


def grid_window_slots(size: int) -> list[int]:
    """合成格在容器里的槽位（按行优先）。"""
    if size == 2:
        return [1, 2, 3, 4]
    if size == 3:
        return [1, 2, 3, 4, 5, 6, 7, 8, 9]
    raise ValueError(f"不支持的合成格尺寸: {size}")


class CraftingController:
    """在 :class:`~mcbot.player.PlayerController` 之上执行合成。"""

    def __init__(
        self,
        player,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.player = player
        self.client = player.client
        self.registry = player.registry
        self.log = logger or player.log

    # ------------------------------------------------------------------ 基础查询
    def _items(self, window: int) -> dict[int, object]:
        return self.client.window_items.get(window, {})

    def _count(self, window: int, name: str) -> int:
        total = 0
        for stack in self._items(window).values():
            if stack is None or getattr(stack, "is_empty", False):
                continue
            info = self.registry.item(stack.item_id)
            if info is not None and info.short_name == name:
                total += stack.count
        return total

    def _inventory_counts(self, window: int) -> dict[str, int]:
        """窗口里的"物品短名→数量"（含合成格，用于判断材料是否够）。"""
        from . import recipes as recipe_lib

        return recipe_lib.inventory_counts(self._items(window), self.registry)

    def _grid_exclusions(self, window: int) -> set[int]:
        """该窗口里"不能当普通背包用"的槽位（输出槽 + 合成格）。

        窗口 0 = 玩家背包（0 输出、1–4 合成格、5–8 盔甲、9+ 背包/快捷栏）；
        合成台窗口 = 0 输出、1–9 合成格、10+ 背包/快捷栏。
        """
        if window == 0:
            return {0, 1, 2, 3, 4}
        return set(range(0, 10))

    def _find_slot(self, window: int, name: str) -> int | None:
        """找一个装着指定物品、且不在合成格里的槽位。"""
        excluded = self._grid_exclusions(window)
        for slot, stack in sorted(self._items(window).items()):
            if slot in excluded:
                continue
            if stack is None or getattr(stack, "is_empty", False):
                continue
            info = self.registry.item(stack.item_id)
            if info is not None and info.short_name == name:
                return slot
        return None

    def _free_slot(self, window: int) -> int | None:
        """背包里第一个空槽（用于放下合成结果）。"""
        main = range(10, 45) if window != 0 else range(9, 45)
        for slot in main:
            stack = self._items(window).get(slot)
            if stack is None or getattr(stack, "is_empty", False):
                return slot
        return None

    def _planks_kind(self, window: int) -> str | None:
        counts = self._inventory_counts(window)
        for name in sorted(counts):
            if name.endswith("_planks") and counts[name] > 0:
                return name
        return None

    def _log_kind(self, window: int) -> str | None:
        counts = self._inventory_counts(window)
        for name in sorted(counts):
            if (name.endswith("_log") or name.endswith("_stem")) and counts[name] > 0:
                return name
        return None

    # ------------------------------------------------------------------ 点击
    async def _click(self, window: int, slot: int, *, button: int = 0, click: str = "pickup") -> None:
        before = getattr(self.client, "inventory_packets", 0)
        self.client.click_slot(slot, button=button, click=click, window=window)
        # 等服务器把新的容器内容同步回来；没有同步也不能卡死
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if getattr(self.client, "inventory_packets", 0) > before:
                break
            await asyncio.sleep(TICK)
        await asyncio.sleep(0.06)

    def _cursor(self):
        return self.client.carried_item

    def _cursor_empty(self) -> bool:
        stack = self._cursor()
        return stack is None or getattr(stack, "is_empty", False)

    async def _put_one(self, window: int, source_slot: int, target_slot: int) -> bool:
        """从背包拿 1 个放进合成格；失败返回 False。"""
        if not self._cursor_empty():
            return False
        await self._click(window, source_slot)  # 拿起整叠
        await self._click(window, target_slot, button=1)  # 右键放 1 个
        if not self._cursor_empty():
            await self._click(window, source_slot)  # 剩下的放回原槽
        return True

    async def _clear_grid(self, window: int) -> None:
        """把合成格里的东西收回背包（出错/收尾时用）。"""
        for slot in grid_window_slots(3):
            stack = self._items(window).get(slot)
            if stack is None or getattr(stack, "is_empty", False):
                continue
            if not self._cursor_empty():
                # 先把光标里的东西塞回背包
                free = self._free_slot(window)
                if free is not None:
                    await self._click(window, free)
            await self._click(window, slot)  # 拿起
            free = self._free_slot(window)
            if free is not None:
                await self._click(window, free)  # 放进背包

    def _grid_slot_for(self, window: int, size: int, row: int, col: int) -> int:
        slots = grid_window_slots(size)
        return slots[row * size + col]

    def _ingredient_name(self, window: int, wanted: str) -> str | None:
        """展开 "any_planks" 之类的占位符。"""
        if wanted == "any_planks":
            return self._planks_kind(window)
        return wanted if self._count(window, wanted) > 0 else None

    # ------------------------------------------------------------------ 主流程
    async def craft(self, recipe, *, times: int = 1) -> bool:
        """按配方合成 ``times`` 次；返回是否至少成功一次。"""
        window = await self._ensure_window(recipe.size)
        if window is None:
            return False
        success = 0
        try:
            for _ in range(max(1, times)):
                placed = True
                for row, col, wanted in recipe.pattern:
                    name = self._ingredient_name(window, wanted)
                    if name is None:
                        placed = False
                        break
                    source = self._find_slot(window, name)
                    if source is None:
                        placed = False
                        break
                    target = self._grid_slot_for(window, recipe.size, row, col)
                    if not await self._put_one(window, source, target):
                        placed = False
                        break
                if not placed:
                    await self._clear_grid(window)
                    break
                # 点输出槽：把合成结果拿到光标，再放进背包
                result = self._items(window).get(0)
                if result is None or getattr(result, "is_empty", False):
                    await self._clear_grid(window)
                    break
                await self._click(window, 0)
                if self._cursor_empty():
                    break
                free = self._free_slot(window)
                if free is None:
                    # 背包满了：把结果放回去，收工
                    await self._click(window, 0)
                    break
                await self._click(window, free)
                success += 1
        finally:
            if recipe.size == 3:
                self.client.close_window(window)
        if success:
            self.log(f"合成了 {recipe.name} ×{recipe.output_count * success}")
        return success > 0

    async def _ensure_window(self, size: int) -> int | None:
        """2x2 用窗口 0；3x3 需要用合成台并打开它。"""
        if size == 2:
            return 0
        window = self.client.crafting_window
        if window is not None and window.get("type") == CRAFTING_MENU_ID:
            return int(window["id"])
        if not await self._open_crafting_table():
            return None
        window = self.client.crafting_window
        if window is None or window.get("type") != CRAFTING_MENU_ID:
            return None
        return int(window["id"])

    async def _open_crafting_table(self) -> bool:
        """在附近找合成台并右键打开；没有就尝试放一个。"""
        from .types import BlockPos

        world = self.client.world
        registry = self.registry
        table_state = registry.state_id("crafting_table") if registry.has_block("crafting_table") else None
        position = self.player.physics.position
        if table_state is not None:
            block_pos = BlockPos(int(position.x // 1), int(position.y // 1), int(position.z // 1))
            found = world.find_blocks({"crafting_table"}, block_pos, radius=4, limit=4)
            for candidate in found:
                if await self.player.open_block(candidate):
                    if self.client.crafting_window is not None:
                        return True
        # 附近没有：如果背包里有合成台就放一个
        inventory = self._items(0)
        has_table = any(
            stack is not None
            and not getattr(stack, "is_empty", False)
            and (info := registry.item(stack.item_id)) is not None
            and info.short_name == "crafting_table"
            for stack in inventory.values()
        )
        if not has_table:
            return False
        from .types import BlockPos as BP

        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            spot = BP(int(position.x) + dx, int(position.y), int(position.z) + dz)
            if self.registry.is_air(world.get_state(spot.x, spot.y, spot.z)):
                result = await self.player.place_block(spot, "crafting_table")
                if result.ok and await self.player.open_block(spot):
                    return self.client.crafting_window is not None
        return False

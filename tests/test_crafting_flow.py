"""合成执行器流程测试：用一个"本地模拟容器"跑通点击序列。

模拟器按原版点击语义实现 pickup/swap，并按 recipes 里的配方刷新输出槽：
它不是真实服务器，但能验证我们的点击顺序、材料搬运和收尾逻辑。
"""
from __future__ import annotations

import asyncio
import unittest

from mcbot import recipes as recipe_lib
from mcbot.client import ItemStack
from mcbot.crafting import CraftingController, grid_window_slots
from mcbot.modern_client import ModernMinecraftClient
from mcbot.player import PlayerController
from mcbot.registry import registry as default_registry
from mcbot.types import Vec3
from mcbot.world import SimpleWorld

MAX_STACK = 64


class SimulatedContainer:
    """窗口 0（玩家背包/2x2）或合成台窗口的点击模拟。"""

    def __init__(self, registry, size: int = 2) -> None:
        self.registry = registry
        self.size = size
        self.slots: dict[int, ItemStack | None] = {slot: None for slot in range(46)}
        self.carried: ItemStack | None = None
        self.grid = grid_window_slots(size)
        self.result_slot = 0

    def set_item(self, slot: int, name: str, count: int) -> None:
        item_id = self.registry.item_id(name)
        assert item_id is not None, name
        self.slots[slot] = ItemStack(item_id, count)

    def _name(self, stack: ItemStack) -> str:
        info = self.registry.item(stack.item_id)
        return info.short_name if info else ""

    def _match_recipe(self):
        present = {}
        for index, slot in enumerate(self.grid):
            stack = self.slots.get(slot)
            if stack is None or stack.is_empty:
                continue
            row, col = divmod(index, self.size)
            present[(row, col)] = self._name(stack)
        for recipe in recipe_lib.RECIPES:
            if recipe.size != self.size or len(recipe.pattern) != len(present):
                continue
            ok = True
            for row, col, wanted in recipe.pattern:
                have = present.get((row, col))
                if have is None:
                    ok = False
                    break
                if wanted == recipe_lib.ANY_PLANKS:
                    if not have.endswith("_planks"):
                        ok = False
                        break
                elif have != wanted:
                    ok = False
                    break
            if ok:
                return recipe
        return None

    def refresh_output(self) -> None:
        recipe = self._match_recipe()
        if recipe is None:
            self.slots[self.result_slot] = None
            return
        item_id = self.registry.item_id(recipe.name)
        self.slots[self.result_slot] = ItemStack(item_id, recipe.output_count) if item_id else None

    # 点击语义
    def click(self, slot: int, button: int, click: str) -> None:
        if click == "swap":
            hotbar = list(range(36, 45)) if self.size == 2 else list(range(37, 46))
            target = hotbar[button]
            self.slots[slot], self.slots[target] = self.slots.get(target), self.slots.get(slot)
            self.refresh_output()
            return
        stack = self.slots.get(slot)
        if click == "pickup" and slot == self.result_slot:
            if self.carried is None and stack is not None and not stack.is_empty:
                recipe = self._match_recipe()
                if recipe is None:
                    return
                self.carried = ItemStack(stack.item_id, stack.count)
                self.slots[slot] = None
                # 每个合成格消耗 1 个
                for grid_slot in self.grid:
                    cell = self.slots.get(grid_slot)
                    if cell is None or cell.is_empty:
                        continue
                    remaining = cell.count - 1
                    self.slots[grid_slot] = ItemStack(cell.item_id, remaining) if remaining > 0 else None
                self.refresh_output()
            return
        if button == 0:
            if self.carried is None:
                if stack is None or stack.is_empty:
                    return
                self.carried = stack
                self.slots[slot] = None
            else:
                if stack is None or stack.is_empty:
                    self.slots[slot] = self.carried
                    self.carried = None
                elif stack.item_id == self.carried.item_id:
                    room = MAX_STACK - stack.count
                    take = min(room, self.carried.count)
                    stack.count += take
                    left = self.carried.count - take
                    self.carried = ItemStack(stack.item_id, left) if left > 0 else None
                    if left <= 0:
                        self.carried = None
        else:  # 右键：拿一半 / 放一个
            if self.carried is None:
                if stack is None or stack.is_empty:
                    return
                half = max(1, (stack.count + 1) // 2)
                self.carried = ItemStack(stack.item_id, half)
                left = stack.count - half
                self.slots[slot] = ItemStack(stack.item_id, left) if left > 0 else None
            elif stack is None or stack.is_empty:
                self.slots[slot] = ItemStack(self.carried.item_id, 1)
                left = self.carried.count - 1
                self.carried = ItemStack(self.carried.item_id, left) if left > 0 else None
            elif stack.item_id == self.carried.item_id and stack.count < MAX_STACK:
                stack.count += 1
                left = self.carried.count - 1
                self.carried = ItemStack(self.carried.item_id, left) if left > 0 else None
        self.refresh_output()


class SimClient(ModernMinecraftClient):
    def __init__(self, containers: dict[int, "SimulatedContainer"], registry) -> None:  # noqa: ANN001
        super().__init__("127.0.0.1", registry=registry)
        self.state = "play"
        self.position_received = True
        self.containers = containers
        self.crafting_window = None
        for window, container in containers.items():
            self.window_items[window] = container.slots
            self.inventory = container.slots if window == 0 else self.inventory
            self.window_state[window] = 0

    def _write_frame(self, packet_id, payload=b""):  # noqa: ANN001
        pass

    def click_slot(self, slot, *, button=0, click="pickup", window=0):  # noqa: ANN001
        container = self.containers.get(window)
        if container is None:
            return 0
        container.click(slot, button, click)
        self.carried_item = container.carried
        self.window_state[window] = self.window_state.get(window, 0) + 1
        self.inventory_packets += 1
        return self.window_state[window]

    def close_window(self, window=0):
        if self.crafting_window is not None and self.crafting_window.get("id") == window:
            self.crafting_window = None


def make_controller(size: int = 2):
    # 必须用 26.2 的注册表（物品 id 与 1.20.1 不同）
    registry = ModernMinecraftClient("127.0.0.1").registry
    world = SimpleWorld(registry)
    for x in range(-3, 4):
        for z in range(-3, 4):
            world.set_state(x, 0, z, registry.state_id("stone"))
    containers = {0: SimulatedContainer(registry, 2)}
    if size == 3:
        containers[1] = SimulatedContainer(registry, 3)
    client = SimClient(containers, registry)
    client.world = world
    client.position = Vec3(0.5, 1.0, 0.5)
    if size == 3:
        client.crafting_window = {"id": 1, "type": 12, "title": "Crafting"}
    player = PlayerController(client, logger=lambda _m: None)
    player.attach()
    return client, containers, CraftingController(player, logger=lambda _m: None)


class CraftingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_planks_from_log(self) -> None:
        client, containers, crafter = make_controller()
        containers[0].set_item(9, "oak_log", 2)
        recipe = next(r for r in recipe_lib.RECIPES if r.name == "oak_planks")
        self.assertTrue(await crafter.craft(recipe, times=2))
        counts = recipe_lib.inventory_counts(containers[0].slots, client.registry)
        self.assertEqual(counts.get("oak_planks"), 8)
        self.assertEqual(counts.get("oak_log", 0), 0)
        self.assertIsNone(containers[0].carried)

    async def test_sticks_from_planks(self) -> None:
        client, containers, crafter = make_controller()
        containers[0].set_item(9, "oak_planks", 4)
        recipe = next(r for r in recipe_lib.RECIPES if r.name == "stick")
        self.assertTrue(await crafter.craft(recipe, times=2))
        counts = recipe_lib.inventory_counts(containers[0].slots, client.registry)
        self.assertEqual(counts.get("stick"), 8)
        self.assertEqual(counts.get("oak_planks", 0), 0)

    async def test_plan_and_craft_pickaxe_on_table(self) -> None:
        client, containers, crafter = make_controller(size=3)
        # 合成台窗口：0 输出、1–9 合成格、10+ 背包。材料放进背包区。
        containers[1].set_item(10, "crafting_table", 1)
        containers[1].set_item(11, "oak_planks", 3)
        containers[1].set_item(12, "stick", 2)
        counts = recipe_lib.inventory_counts(containers[1].slots, client.registry)
        steps = recipe_lib.plan(client.registry, counts, "wooden_pickaxe", 1)
        self.assertIsNotNone(steps)
        pickaxe = next(r for r in recipe_lib.RECIPES if r.name == "wooden_pickaxe")
        self.assertEqual(pickaxe.size, 3)
        self.assertTrue(await crafter.craft(pickaxe, times=1))
        counts = recipe_lib.inventory_counts(containers[1].slots, client.registry)
        self.assertEqual(counts.get("wooden_pickaxe"), 1)
        self.assertIsNone(containers[1].carried)
        self.assertFalse(any(containers[1].slots.get(slot) for slot in grid_window_slots(3)), "合成格应清空")

    async def test_crafting_table_placed_when_missing(self) -> None:
        """没有合成台时要先做一个（2x2），测试里只验证 2x2 那一步。"""
        client, containers, crafter = make_controller()
        containers[0].set_item(9, "oak_planks", 4)
        recipe = next(r for r in recipe_lib.RECIPES if r.name == "crafting_table")
        self.assertTrue(await crafter.craft(recipe, times=1))
        counts = recipe_lib.inventory_counts(containers[0].slots, client.registry)
        self.assertEqual(counts.get("crafting_table"), 1)

    async def test_torch_from_coal_and_stick(self) -> None:
        client, containers, crafter = make_controller()
        containers[0].set_item(9, "coal", 1)
        containers[0].set_item(10, "stick", 1)
        recipe = next(r for r in recipe_lib.RECIPES if r.name == "torch" and r.size == 2)
        self.assertTrue(await crafter.craft(recipe, times=1))
        counts = recipe_lib.inventory_counts(containers[0].slots, client.registry)
        self.assertEqual(counts.get("torch"), 4)

    async def test_missing_materials_noop(self) -> None:
        client, containers, crafter = make_controller()
        containers[0].set_item(9, "oak_planks", 1)
        recipe = next(r for r in recipe_lib.RECIPES if r.name == "stick")
        self.assertFalse(await crafter.craft(recipe, times=1))
        self.assertIsNone(containers[0].carried)


if __name__ == "__main__":
    unittest.main()

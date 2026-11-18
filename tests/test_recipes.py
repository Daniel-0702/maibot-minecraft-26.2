"""2x2 玩家合成格与 3x3 合成台配方逻辑的测试（纯离线，不连服务器）。

数据用项目自带的 Java 26.2 注册表，避免默认 1.20.1 数据缺少 pale_oak 等物品。
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcbot import recipes
from mcbot.client import ItemStack
from mcbot.registry import BlockRegistry

DATA = ROOT / "mcbot" / "data" / "mc_26_2.json"

_CACHED_REGISTRY: BlockRegistry | None = None


def load_registry() -> BlockRegistry:
    global _CACHED_REGISTRY
    if _CACHED_REGISTRY is None:
        _CACHED_REGISTRY = BlockRegistry(DATA)
    return _CACHED_REGISTRY


class RecipeTestBase(unittest.TestCase):
    registry: BlockRegistry

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_registry()

    def stack(self, name: str, count: int = 1) -> ItemStack:
        item_id = self.registry.item_id(name)
        self.assertIsNotNone(item_id, name)
        return ItemStack(int(item_id), count)

    def all_named(self, name: str, size: int | None = None) -> list[recipes.Recipe]:
        found = [recipe for recipe in recipes.RECIPES if recipe.name == name and (size is None or recipe.size == size)]
        self.assertTrue(found, f"缺少配方 {name} (size={size})")
        return found

    def one(self, name: str, size: int | None = None) -> recipes.Recipe:
        found = self.all_named(name, size)
        self.assertEqual(len(found), 1, f"{name} (size={size}) 应只有一条配方，实际 {len(found)} 条")
        return found[0]

    def names(self, plan: list[recipes.Recipe] | None) -> list[str]:
        self.assertIsNotNone(plan, "预期有合成计划，实际为 None")
        assert plan is not None
        return [recipe.name for recipe in plan]

    def count(self, plan: list[recipes.Recipe] | None, name: str) -> int:
        self.assertIsNotNone(plan, "预期有合成计划，实际为 None")
        assert plan is not None
        return sum(1 for recipe in plan if recipe.name == name)


class RecipeCatalogTests(RecipeTestBase):
    def test_every_wood_log_makes_its_own_planks(self) -> None:
        self.assertEqual(len(recipes.WOOD_TYPES), 11)
        expected = {
            "oak_log",
            "birch_log",
            "spruce_log",
            "jungle_log",
            "acacia_log",
            "dark_oak_log",
            "mangrove_log",
            "cherry_log",
            "pale_oak_log",
            "crimson_stem",
            "warped_stem",
        }
        self.assertEqual({log for log, _planks in recipes.WOOD_TYPES}, expected)
        for log, planks in recipes.WOOD_TYPES:
            with self.subTest(log=log):
                recipe = self.one(planks)
                self.assertEqual(recipe.size, 2)
                self.assertEqual(recipe.output_count, 4)
                self.assertEqual(recipe.pattern, ((0, 0, log),))
                self.assertEqual(recipe.ingredients, ((log, 1),))
                self.assertEqual(recipe.grid, ((1, log),))

    def test_all_recipe_names_exist_and_pattern_matches_ingredients(self) -> None:
        for recipe in recipes.RECIPES:
            with self.subTest(recipe=recipe.name, size=recipe.size):
                self.assertIn(recipe.size, (2, 3))
                self.assertGreater(recipe.output_count, 0)
                self.assertIsNotNone(self.registry.item_id(recipe.name))
                seen: set[tuple[int, int]] = set()
                slots: list[str] = []
                for row, col, item in recipe.pattern:
                    self.assertTrue(0 <= row < recipe.size, (row, col))
                    self.assertTrue(0 <= col < recipe.size, (row, col))
                    self.assertNotIn((row, col), seen)
                    seen.add((row, col))
                    slots.append(item)
                    if item != recipes.ANY_PLANKS:
                        self.assertIsNotNone(self.registry.item_id(item), item)
                self.assertEqual(Counter(slots), Counter(dict(recipe.ingredients)))
                self.assertEqual(len(seen), len(recipe.pattern))
                for item, _amount in recipe.ingredients:
                    if item != recipes.ANY_PLANKS:
                        self.assertIsNotNone(self.registry.item_id(item), item)
        for plank in recipes.PLANK_TYPES:
            self.assertIsNotNone(self.registry.item_id(plank), plank)

    def test_legacy_grid_view_is_row_major(self) -> None:
        sticks = self.one("stick", 2)
        self.assertEqual(sticks.grid, ((1, recipes.ANY_PLANKS), (3, recipes.ANY_PLANKS)))
        table = self.one("crafting_table", 2)
        self.assertEqual(
            table.grid,
            (
                (1, recipes.ANY_PLANKS),
                (2, recipes.ANY_PLANKS),
                (3, recipes.ANY_PLANKS),
                (4, recipes.ANY_PLANKS),
            ),
        )
        pickaxe = self.one("wooden_pickaxe", 3)
        self.assertEqual(
            pickaxe.grid,
            (
                (1, recipes.ANY_PLANKS),
                (2, recipes.ANY_PLANKS),
                (3, recipes.ANY_PLANKS),
                (5, "stick"),
                (8, "stick"),
            ),
        )

    def test_tool_patterns_2x2_style_tools(self) -> None:
        # (材料格, 木棍格)
        shapes = {
            "pickaxe": ({(0, 0), (0, 1), (0, 2)}, {(1, 1), (2, 1)}),
            "axe": ({(0, 0), (0, 1), (1, 0)}, {(1, 1), (2, 1)}),
            "shovel": ({(0, 0)}, {(1, 0), (2, 0)}),
            "sword": ({(0, 0), (1, 0)}, {(2, 0)}),
        }
        for prefix, material in (("wooden", recipes.ANY_PLANKS), ("stone", "cobblestone")):
            for tool, (material_cells, stick_cells) in shapes.items():
                name = f"{prefix}_{tool}"
                with self.subTest(recipe=name):
                    recipe = self.one(name, 3)
                    self.assertEqual(recipe.output_count, 1)
                    expected = {cell: material for cell in material_cells}
                    expected.update({cell: "stick" for cell in stick_cells})
                    self.assertEqual({(row, col): item for row, col, item in recipe.pattern}, expected)
                    self.assertEqual(Counter(item for _row, _col, item in recipe.pattern), Counter(dict(recipe.ingredients)))

    def test_furnace_and_chest_are_eight_block_rings(self) -> None:
        ring = {(row, col) for row in range(3) for col in range(3)} - {(1, 1)}
        for name, material in (("furnace", "cobblestone"), ("chest", recipes.ANY_PLANKS)):
            with self.subTest(recipe=name):
                recipe = self.one(name, 3)
                self.assertEqual(recipe.output_count, 1)
                self.assertEqual({(row, col): item for row, col, item in recipe.pattern}, dict.fromkeys(ring, material))
                self.assertEqual(recipe.ingredients, ((material, 8),))

    def test_torch_has_2x2_and_3x3_coal_and_charcoal_variants(self) -> None:
        torches = self.all_named("torch")
        self.assertEqual(len(torches), 4)
        for size in (2, 3):
            for fuel in ("coal", "charcoal"):
                with self.subTest(size=size, fuel=fuel):
                    matches = [
                        recipe
                        for recipe in torches
                        if recipe.size == size and any(item == fuel for _row, _col, item in recipe.pattern)
                    ]
                    self.assertEqual(len(matches), 1)
                    recipe = matches[0]
                    self.assertEqual(recipe.output_count, 4)
                    self.assertEqual(recipe.ingredients, ((fuel, 1), ("stick", 1)))
                    fuel_position = next((row, col) for row, col, item in recipe.pattern if item == fuel)
                    stick_position = next((row, col) for row, col, item in recipe.pattern if item == "stick")
                    self.assertEqual(fuel_position[1], stick_position[1], "煤炭和木棍必须在同一列")
                    self.assertEqual(stick_position[0], fuel_position[0] + 1, "木棍必须在燃料正下方")


class InventoryCountTests(RecipeTestBase):
    def test_empty_inventory(self) -> None:
        self.assertEqual(recipes.inventory_counts({}, self.registry), {})

    def test_accumulates_same_item_and_skips_empty_stacks(self) -> None:
        inventory = {
            0: self.stack("oak_log", 3),
            1: None,
            2: self.stack("oak_log", 2),
            3: self.stack("oak_planks", 7),
            4: self.stack("oak_log", 0),  # 数量为 0
            5: ItemStack(0, 1),  # 空物品 / air
            6: ItemStack(1_000_000, 4),  # 注册表里不存在
        }
        self.assertEqual(
            recipes.inventory_counts(inventory, self.registry),
            {"oak_log": 5, "oak_planks": 7},
        )

    def test_keys_are_short_names(self) -> None:
        counts = recipes.inventory_counts({0: self.stack("crafting_table", 1)}, self.registry)
        self.assertEqual(counts, {"crafting_table": 1})
        self.assertTrue(all(":" not in name for name in counts))


class CandidateTests(RecipeTestBase):
    def candidate_names(self, counts: dict[str, int], **kwargs: int) -> list[str]:
        return [recipe.name for recipe in recipes.candidates(self.registry, counts, **kwargs)]

    def test_no_materials(self) -> None:
        self.assertEqual(recipes.candidates(self.registry, {}), [])

    def test_single_log_only_allows_its_planks(self) -> None:
        self.assertEqual(self.candidate_names({"oak_log": 1}), ["oak_planks"])
        self.assertEqual(self.candidate_names({"birch_log": 1}), ["birch_planks"])

    def test_sticks_need_two_planks(self) -> None:
        self.assertNotIn("stick", self.candidate_names({"oak_planks": 1}))
        self.assertIn("stick", self.candidate_names({"oak_planks": 2}))
        self.assertIn("stick", self.candidate_names({"oak_planks": 1, "birch_planks": 1}))

    def test_crafting_table_needs_four_planks(self) -> None:
        self.assertNotIn("crafting_table", self.candidate_names({"oak_planks": 3}))
        self.assertIn("crafting_table", self.candidate_names({"oak_planks": 4}))
        self.assertIn("crafting_table", self.candidate_names({"oak_planks": 1, "birch_planks": 3}))

    def test_any_planks_expands_for_chest(self) -> None:
        names = self.candidate_names({"oak_planks": 4, "spruce_planks": 4})
        self.assertIn("chest", names)
        self.assertNotIn("chest", self.candidate_names({"oak_planks": 7}))

    def test_torch_needs_fuel_and_stick(self) -> None:
        with_coal = self.candidate_names({"coal": 1, "stick": 1})
        self.assertEqual(with_coal.count("torch"), 2)  # 2x2 与 3x3 各一条
        with_charcoal = self.candidate_names({"charcoal": 1, "stick": 1})
        self.assertEqual(with_charcoal.count("torch"), 2)
        self.assertNotIn("torch", self.candidate_names({"coal": 1}))
        self.assertNotIn("torch", self.candidate_names({"stick": 4}))

    def test_furnace_needs_eight_cobblestone(self) -> None:
        self.assertIn("furnace", self.candidate_names({"cobblestone": 8}))
        self.assertNotIn("furnace", self.candidate_names({"cobblestone": 7}))

    def test_3x3_tools_are_candidates_with_ingredients(self) -> None:
        names = self.candidate_names({"oak_planks": 3, "stick": 2})
        for tool in ("wooden_pickaxe", "wooden_axe", "wooden_sword", "wooden_shovel"):
            self.assertIn(tool, names)
        stone_names = self.candidate_names({"cobblestone": 3, "stick": 2})
        self.assertIn("stone_pickaxe", stone_names)
        self.assertIn("stone_axe", stone_names)

    def test_candidates_respects_max_and_order(self) -> None:
        counts = {log: 1 for log, _planks in recipes.WOOD_TYPES}
        self.assertEqual(len(recipes.candidates(self.registry, counts, max=5)), 5)
        self.assertEqual(recipes.candidates(self.registry, counts, max=0), [])
        # 默认最多 8 条
        full = {"oak_log": 4, "oak_planks": 8, "stick": 8, "cobblestone": 8, "coal": 1}
        self.assertLessEqual(len(recipes.candidates(self.registry, full)), 8)

    def test_recipes_with_unknown_registry_items_are_skipped(self) -> None:
        known: set[str] = {recipe.name for recipe in recipes.RECIPES}
        for recipe in recipes.RECIPES:
            known.update(item for _row, _col, item in recipe.pattern)
            known.update(item for item, _amount in recipe.ingredients)
        known.discard(recipes.ANY_PLANKS)
        known.discard("oak_log")

        class ItemIdOnlyRegistry:
            def item_id(self, name: str) -> int | None:
                return 1 if name.split(":", 1)[-1] in known else None

        stub = ItemIdOnlyRegistry()
        # 注册表里没有 oak_log：原木 -> 木板配方应被跳过，而不是抛异常
        self.assertEqual(recipes.candidates(stub, {"oak_log": 1}), [])
        self.assertIsNone(recipes.plan(stub, {"oak_log": 1}, "oak_planks"))


class TreePlanTests(RecipeTestBase):
    def test_stick_chain_from_single_log(self) -> None:
        plan = recipes.plan(self.registry, {"oak_log": 1}, "stick")
        self.assertEqual(self.names(plan), ["oak_planks", "stick"])
        assert plan is not None
        self.assertEqual(plan[0].size, 2)
        self.assertEqual(plan[0].output_count, 4)
        self.assertEqual(plan[1].size, 2)
        self.assertEqual(plan[1].output_count, 4)

    def test_one_log_supports_two_stick_crafts(self) -> None:
        # 1 个原木 -> 4 块木板；每根木棍要 2 块木板，4 块可做 2 次（8 根）
        plan = recipes.plan(self.registry, {"oak_log": 1}, "stick", times=2)
        self.assertEqual(self.count(plan, "oak_planks"), 1)
        self.assertEqual(self.count(plan, "stick"), 2)

    def test_three_stick_crafts_need_two_logs(self) -> None:
        # 3 次木棍 = 6 块木板 -> 2 个原木（8 块木板）
        insufficient = recipes.plan(self.registry, {"oak_log": 1}, "stick", times=3)
        self.assertIsNone(insufficient)
        plan = recipes.plan(self.registry, {"oak_log": 2}, "stick", times=3)
        self.assertEqual(self.count(plan, "oak_planks"), 2)
        self.assertEqual(self.count(plan, "stick"), 3)
        self.assertEqual(len(plan or []), 5)

    def test_mixed_log_types_can_cover_planks(self) -> None:
        plan = recipes.plan(self.registry, {"oak_log": 1, "birch_log": 1}, "stick", times=3)
        names = self.names(plan)
        self.assertEqual(self.count(plan, "stick"), 3)
        self.assertEqual(sum(1 for name in names if name.endswith("_planks")), 2)
        self.assertEqual({name for name in names if name.endswith("_planks")}, {"oak_planks", "birch_planks"})

    def test_planks_recipe_is_wood_specific(self) -> None:
        self.assertIsNone(recipes.plan(self.registry, {"birch_log": 1}, "oak_planks"))
        self.assertEqual(self.names(recipes.plan(self.registry, {"birch_log": 1}, "birch_planks")), ["birch_planks"])
        self.assertEqual(
            self.names(recipes.plan(self.registry, {"birch_log": 1}, "stick")),
            ["birch_planks", "stick"],
        )

    def test_existing_planks_avoid_extra_log_craft(self) -> None:
        plan = recipes.plan(self.registry, {"oak_planks": 2, "oak_log": 1}, "stick")
        self.assertEqual(self.names(plan), ["stick"])

    def test_crafting_table_chain(self) -> None:
        self.assertEqual(
            self.names(recipes.plan(self.registry, {"oak_log": 1}, "crafting_table")),
            ["oak_planks", "crafting_table"],
        )
        self.assertIsNone(recipes.plan(self.registry, {}, "crafting_table"))

    def test_torch_chain_from_coal_and_log(self) -> None:
        plan = recipes.plan(self.registry, {"coal": 1, "oak_log": 1}, "torch")
        self.assertEqual(self.names(plan), ["oak_planks", "stick", "torch"])
        assert plan is not None
        self.assertEqual(plan[-1].size, 2)
        self.assertEqual(plan[-1].ingredients, (("coal", 1), ("stick", 1)))

    def test_torch_accepts_charcoal(self) -> None:
        plan = recipes.plan(self.registry, {"charcoal": 1, "stick": 1}, "torch")
        self.assertEqual(self.names(plan), ["torch"])
        assert plan is not None
        self.assertEqual(plan[0].grid, ((1, "charcoal"), (3, "stick")))
        self.assertEqual(plan[0].output_count, 4)

    def test_torch_missing_material_returns_none(self) -> None:
        self.assertIsNone(recipes.plan(self.registry, {"stick": 4}, "torch"))
        self.assertIsNone(recipes.plan(self.registry, {"coal": 1}, "torch"))
        self.assertIsNone(recipes.plan(self.registry, {"charcoal": 1}, "torch"))

    def test_unknown_target_returns_none(self) -> None:
        self.assertIsNone(recipes.plan(self.registry, {"oak_log": 4}, "diamond_sword"))
        self.assertIsNone(recipes.plan(self.registry, {"oak_log": 4}, "definitely_not_an_item"))

    def test_non_positive_times_returns_empty_plan(self) -> None:
        self.assertEqual(recipes.plan(self.registry, {"oak_log": 1}, "stick", times=0), [])
        self.assertEqual(recipes.plan(self.registry, {"oak_log": 1}, "stick", times=-3), [])


class Table3x3PlanTests(RecipeTestBase):
    def test_wooden_pickaxe_from_logs_requires_and_builds_table(self) -> None:
        # 3 木板 + 2 木棍 + 4 木板（合成台）= 9 木板 -> 3 个原木
        plan = recipes.plan(self.registry, {"oak_log": 3}, "wooden_pickaxe")
        names = self.names(plan)
        self.assertEqual(names[-1], "wooden_pickaxe")
        self.assertEqual(self.count(plan, "oak_planks"), 3)
        self.assertEqual(self.count(plan, "stick"), 1)
        self.assertEqual(self.count(plan, "crafting_table"), 1)
        self.assertEqual(self.count(plan, "wooden_pickaxe"), 1)
        self.assertLess(names.index("oak_planks"), names.index("stick"))
        self.assertLess(names.index("stick"), names.index("crafting_table"))
        self.assertLess(names.index("crafting_table"), len(names) - 1)
        assert plan is not None
        self.assertTrue(all(recipe.size in (2, 3) for recipe in plan))

    def test_wooden_pickaxe_does_not_rebuild_existing_table(self) -> None:
        plan = recipes.plan(self.registry, {"oak_log": 2, "crafting_table": 1}, "wooden_pickaxe")
        self.assertEqual(self.count(plan, "oak_planks"), 2)
        self.assertEqual(self.count(plan, "stick"), 1)
        self.assertEqual(self.count(plan, "crafting_table"), 0)
        self.assertEqual(self.names(plan)[-1], "wooden_pickaxe")

    def test_wooden_pickaxe_needs_extra_planks_for_table(self) -> None:
        # 8 块木板只够工具本身，不够再做一个合成台
        self.assertIsNone(recipes.plan(self.registry, {"oak_planks": 8}, "wooden_pickaxe"))
        plan = recipes.plan(self.registry, {"oak_planks": 9}, "wooden_pickaxe")
        self.assertEqual(self.names(plan), ["stick", "crafting_table", "wooden_pickaxe"])

    def test_stone_pickaxe_without_cobblestone_returns_none(self) -> None:
        self.assertIsNone(recipes.plan(self.registry, {"oak_log": 3, "stick": 2}, "stone_pickaxe"))
        self.assertIsNone(recipes.plan(self.registry, {"cobblestone": 2, "oak_log": 3, "stick": 2}, "stone_pickaxe"))

    def test_stone_pickaxe_dependency_chain(self) -> None:
        plan = recipes.plan(
            self.registry,
            {"cobblestone": 3, "oak_log": 2, "crafting_table": 1},
            "stone_pickaxe",
        )
        self.assertEqual(self.names(plan), ["oak_planks", "stick", "stone_pickaxe"])
        # 没有现成合成台时，需要额外 4 块木板做合成台
        no_table = recipes.plan(self.registry, {"cobblestone": 3, "oak_log": 2}, "stone_pickaxe")
        self.assertEqual(self.count(no_table, "oak_planks"), 2)
        self.assertEqual(self.count(no_table, "stick"), 1)
        self.assertEqual(self.count(no_table, "crafting_table"), 1)
        self.assertEqual(self.names(no_table)[-1], "stone_pickaxe")

    def test_furnace_ring_and_table_requirement(self) -> None:
        recipe = self.one("furnace", 3)
        self.assertEqual({(row, col) for row, col, _item in recipe.pattern}, {
            (row, col) for row in range(3) for col in range(3) if (row, col) != (1, 1)
        })
        self.assertEqual(self.names(recipes.plan(self.registry, {"cobblestone": 8, "crafting_table": 1}, "furnace")), ["furnace"])
        with_log = recipes.plan(self.registry, {"cobblestone": 8, "oak_log": 1}, "furnace")
        self.assertEqual(self.names(with_log), ["oak_planks", "crafting_table", "furnace"])
        self.assertIsNone(recipes.plan(self.registry, {"cobblestone": 8}, "furnace"))
        self.assertIsNone(recipes.plan(self.registry, {"cobblestone": 7, "crafting_table": 1}, "furnace"))

    def test_chest_ring_uses_eight_planks(self) -> None:
        recipe = self.one("chest", 3)
        self.assertEqual(recipe.ingredients, ((recipes.ANY_PLANKS, 8),))
        self.assertEqual(
            self.names(recipes.plan(self.registry, {"oak_planks": 8, "crafting_table": 1}, "chest")),
            ["chest"],
        )
        self.assertIsNone(recipes.plan(self.registry, {"oak_planks": 7, "crafting_table": 1}, "chest"))
        # 3 原木 -> 12 木板：8 块做箱子 + 4 块做合成台
        from_logs = recipes.plan(self.registry, {"oak_log": 3}, "chest")
        self.assertEqual(self.count(from_logs, "oak_planks"), 3)
        self.assertEqual(self.count(from_logs, "crafting_table"), 1)
        self.assertEqual(self.names(from_logs)[-1], "chest")

    def test_3x3_any_planks_expands_across_types(self) -> None:
        counts = {"oak_planks": 1, "birch_planks": 2, "stick": 2, "crafting_table": 1}
        candidate_names = [recipe.name for recipe in recipes.candidates(self.registry, counts)]
        self.assertIn("wooden_pickaxe", candidate_names)
        self.assertIn("wooden_axe", candidate_names)
        plan = recipes.plan(self.registry, counts, "wooden_pickaxe")
        self.assertEqual(self.names(plan), ["wooden_pickaxe"])

    def test_to_grid_slots_3x3_uses_row_major_slots(self) -> None:
        pickaxe = self.one("wooden_pickaxe", 3)
        slots = recipes.to_grid_slots(pickaxe, "spruce_planks")
        self.assertEqual(
            slots,
            {1: "spruce_planks", 2: "spruce_planks", 3: "spruce_planks", 5: "stick", 8: "stick"},
        )
        self.assertNotIn(recipes.ANY_PLANKS, slots.values())
        pattern = recipes.to_pattern_slots(pickaxe, "spruce_planks")
        self.assertEqual(
            pattern,
            {
                (0, 0): "spruce_planks",
                (0, 1): "spruce_planks",
                (0, 2): "spruce_planks",
                (1, 1): "stick",
                (2, 1): "stick",
            },
        )

    def test_to_grid_slots_2x2_keeps_original_slot_layout(self) -> None:
        sticks = self.one("stick", 2)
        self.assertEqual(
            recipes.to_grid_slots(sticks, "dark_oak_planks"),
            {1: "dark_oak_planks", 3: "dark_oak_planks"},
        )
        table = self.one("crafting_table", 2)
        self.assertEqual(
            recipes.to_grid_slots(table, "cherry_planks"),
            {1: "cherry_planks", 2: "cherry_planks", 3: "cherry_planks", 4: "cherry_planks"},
        )


if __name__ == "__main__":
    unittest.main()

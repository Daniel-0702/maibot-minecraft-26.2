"""26.2 合成相关的协议编码测试（不改服务器，只验证字节布局）。"""
from __future__ import annotations

import unittest

from mcbot.buffer import BufferReader
from mcbot.client import ItemStack
from mcbot import modern_packets as p


class HashedStackTests(unittest.TestCase):
    def test_empty_stack(self) -> None:
        self.assertEqual(p.encode_hashed_stack(None), b"\x00")

    def test_zero_count_is_empty(self) -> None:
        self.assertEqual(p.encode_hashed_stack(ItemStack(5, 0)), b"\x00")

    def test_plain_item(self) -> None:
        # 存在标记 1 + 物品 id 5 + 数量 3 + 空组件补丁(0,0)
        self.assertEqual(
            p.encode_hashed_stack(ItemStack(5, 3)),
            b"\x01\x05\x03\x00\x00",
        )

    def test_custom_components_rejected(self) -> None:
        with self.assertRaises(p.UnsupportedComponent):
            p.encode_hashed_stack(ItemStack(5, 1, {"custom_name": "x"}))


class ContainerClickTests(unittest.TestCase):
    def test_pickup_empty_hand(self) -> None:
        payload = p.container_click(0, 7, 36)
        reader = BufferReader(payload)
        self.assertEqual(reader.read_varint(), 0)  # window
        self.assertEqual(reader.read_varint(), 7)  # state id
        self.assertEqual(reader.read_i16(), 36)  # slot
        self.assertEqual(reader.read_i8(), 0)  # button
        self.assertEqual(reader.read_varint(), p.CONTAINER_INPUT["pickup"])
        self.assertEqual(reader.read_varint(), 0)  # changed slots
        self.assertEqual(reader.read_rest(), b"\x00")  # 空光标

    def test_right_click_with_carried_item(self) -> None:
        payload = p.container_click(1, 3, 2, button=1, carried=ItemStack(10, 2))
        reader = BufferReader(payload)
        reader.read_varint()
        reader.read_varint()
        reader.read_i16()
        self.assertEqual(reader.read_i8(), 1)
        self.assertEqual(reader.read_varint(), 0)
        self.assertEqual(reader.read_varint(), 0)
        self.assertEqual(reader.read_rest(), b"\x01\x0a\x02\x00\x00")

    def test_swap_to_hotbar(self) -> None:
        payload = p.container_click(0, 1, 20, button=4, click="swap")
        reader = BufferReader(payload)
        reader.read_varint()
        reader.read_varint()
        reader.read_i16()
        self.assertEqual(reader.read_i8(), 4)
        self.assertEqual(reader.read_varint(), p.CONTAINER_INPUT["swap"])

    def test_container_close(self) -> None:
        self.assertEqual(p.container_close(2), b"\x02")


class GridLayoutTests(unittest.TestCase):
    def test_grid_slots(self) -> None:
        from mcbot.crafting import grid_window_slots

        self.assertEqual(grid_window_slots(2), [1, 2, 3, 4])
        self.assertEqual(grid_window_slots(3), [1, 2, 3, 4, 5, 6, 7, 8, 9])

    def test_crafting_menu_id(self) -> None:
        from mcbot.crafting import CRAFTING_MENU_ID

        self.assertEqual(CRAFTING_MENU_ID, 12)


if __name__ == "__main__":
    unittest.main()

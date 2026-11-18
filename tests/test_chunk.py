"""mcbot.chunk 的单元测试：手工构造 1.20.1 区块负载并验证解码。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from mcbot.buffer import BufferWriter
from mcbot.chunk import DEFAULT_HEIGHT, DEFAULT_MIN_Y, ChunkColumn, PaletteError

MASK64 = (1 << 64) - 1


def pack_indices(values, bits):
    """按 LSB-first、无填充规则把条目打包成 i64 列表（最小长度）。"""
    mask = (1 << bits) - 1
    longs = []
    current = 0
    used = 0
    for value in values:
        current |= (value & mask) << used
        used += bits
        if used >= 64:
            longs.append(current & MASK64)
            current >>= 64
            used -= 64
    if used:
        longs.append(current & MASK64)
    return longs


def write_container(writer, bits, palette, values):
    """写一个调色板容器；bits=0 时用单值形式（palette 即该值）。"""
    writer.write_u8(bits)
    if bits == 0:
        writer.write_varint(1)
        writer.write_varint(palette)
        return
    if bits <= 8:
        writer.write_varint(len(palette))
        for entry in palette:
            writer.write_varint(entry)
    longs = pack_indices(values, bits)
    writer.write_varint(len(longs))
    for word in longs:
        writer.write_u64(word)


def make_payload(specs):
    """specs 每项为 (非空气数, 方块bits, 方块palette, 方块values, 生物群系id)。"""
    writer = BufferWriter()
    writer.write_varint(len(specs))
    for non_air, bits, palette, values, biome in specs:
        writer.write_i16(non_air)
        write_container(writer, bits, palette, values)
        write_container(writer, 0, biome, None)
    return writer.to_bytes()


def air_section(biome=1):
    return (0, 0, 0, None, biome)


class ChunkDecodeTest(unittest.TestCase):
    def test_all_air_chunk(self):
        payload = make_payload([air_section()] * 24)
        column = ChunkColumn.from_packet(2, -3, payload)

        self.assertEqual(column.cx, 2)
        self.assertEqual(column.cz, -3)
        self.assertEqual(column.section_count, 24)
        self.assertTrue(column.is_empty())

        for y in range(DEFAULT_MIN_Y, DEFAULT_MIN_Y + DEFAULT_HEIGHT):
            for x in (0, 5, 15):
                for z in (0, 9, 15):
                    self.assertEqual(column.get_state(x, y, z), 0)
        states = column.block_states()
        self.assertEqual(len(states), 24 * 4096)
        self.assertEqual(set(states), {0})

        # 越界坐标返回空气
        self.assertEqual(column.get_state(16, 0, 0), 0)
        self.assertEqual(column.get_state(-1, 0, 0), 0)
        self.assertEqual(column.get_state(0, DEFAULT_MIN_Y - 1, 0), 0)
        self.assertEqual(column.get_state(0, DEFAULT_MIN_Y + DEFAULT_HEIGHT, 0), 0)

    def test_single_value_stone_section(self):
        specs = [air_section()] * 24
        specs[5] = (4096, 0, 1, None, 1)
        column = ChunkColumn.from_packet(0, 0, make_payload(specs))

        self.assertFalse(column.is_empty())
        self.assertEqual(column.non_air_count(5), 4096)
        self.assertEqual(column.non_air_count(4), 0)
        self.assertEqual(column.non_air_count(99), 0)

        states = [column.get_state(x, y, z) for y in range(16, 32) for x in range(16) for z in range(16)]
        self.assertEqual(len(states), 4096)
        self.assertTrue(all(state == 1 for state in states))

        # 相邻 section 仍然是空气
        for y in (15, 32):
            for x in range(16):
                for z in range(16):
                    self.assertEqual(column.get_state(x, y, z), 0)

    def test_paletted_section_bits4(self):
        palette = [101, 202, 303, 404, 505]
        values = [i % 5 for i in range(4096)]
        expected = [palette[value] for value in values]
        payload = make_payload([(4096, 4, palette, values, 1)])
        column = ChunkColumn.from_packet(0, 0, payload)

        self.assertEqual(column.block_states(), expected)

        mismatches = []
        for index in range(4096):
            local_x = index & 15
            local_z = (index >> 4) & 15
            y = DEFAULT_MIN_Y + (index >> 8)
            if column.get_state(local_x, y, local_z) != expected[index]:
                mismatches.append(index)
        self.assertEqual(mismatches, [])

    def test_direct_palette_bits15(self):
        # 直接调色板：值本身就是全局方块状态 id，全部大于 255
        values = [256 + ((i * 37) % 24000) for i in range(4096)]
        payload = make_payload([(4096, 15, None, values, 1)])
        column = ChunkColumn.from_packet(0, 0, payload)

        self.assertEqual(column.block_states(), values)
        for index in list(range(0, 4096, 257)) + [4095]:
            local_x = index & 15
            local_z = (index >> 4) & 15
            y = DEFAULT_MIN_Y + (index >> 8)
            self.assertEqual(column.get_state(local_x, y, local_z), values[index])
            self.assertGreater(values[index], 255)

    def test_paletted_section_bits5_straddling(self):
        # bpe=5 不整除 64，条目会跨越 long 边界
        palette = [7, 77, 777, 7777, 77777, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13]
        values = [(i * 7 + 3) % 17 for i in range(4096)]
        expected = [palette[value] for value in values]
        payload = make_payload([(4096, 5, palette, values, 1)])
        column = ChunkColumn.from_packet(0, 0, payload)

        self.assertEqual(column.block_states(), expected)
        for index in (0, 1, 12, 13, 14, 1023, 2047, 4095):
            local_x = index & 15
            local_z = (index >> 4) & 15
            y = DEFAULT_MIN_Y + (index >> 8)
            self.assertEqual(column.get_state(local_x, y, local_z), expected[index])

    def test_out_of_range_palette_indices(self):
        # 调色板只有 3 项，但 bpe=4 的索引可能取 0..15；越界索引按空气处理并计数
        palette = [10, 20, 30]
        values = [i % 16 for i in range(4096)]
        expected = [palette[value] if value < len(palette) else 0 for value in values]
        payload = make_payload([(4096, 4, palette, values, 1)])
        column = ChunkColumn.from_packet(0, 0, payload)

        self.assertEqual(column.block_states(), expected)
        self.assertEqual(column.invalid_index_count, sum(1 for value in values if value >= 3))
        self.assertGreater(column.invalid_index_count, 0)

    def test_set_state_materializes_uniform_section(self):
        column = ChunkColumn.from_packet(0, 0, make_payload([air_section(biome=7)]))
        self.assertTrue(column.is_empty())

        column.set_state(3, -60, 7, 42)
        self.assertEqual(column.get_state(3, -60, 7), 42)
        self.assertEqual(column.non_air_count(0), 1)
        self.assertFalse(column.is_empty())
        self.assertEqual(sum(1 for state in column.block_states() if state), 1)

        # 改回空气后计数恢复
        column.set_state(3, -60, 7, 0)
        self.assertTrue(column.is_empty())
        self.assertEqual(column.non_air_count(0), 0)

        # 越界写入静默忽略
        column.set_state(16, -40, 0, 1)
        column.set_state(0, DEFAULT_MIN_Y + DEFAULT_HEIGHT, 0, 1)
        self.assertTrue(column.is_empty())

    def test_set_state_in_non_air_section(self):
        specs = [air_section()] * 24
        specs[0] = (4096, 0, 1, None, 1)
        column = ChunkColumn.from_packet(0, 0, make_payload(specs))

        column.set_state(1, -64, 2, 99)
        self.assertEqual(column.get_state(1, -64, 2), 99)
        self.assertEqual(column.get_state(0, -64, 0), 1)
        self.assertEqual(column.non_air_count(0), 4096)  # 仍然是 4096 个非空气方块

    def test_get_biome(self):
        column = ChunkColumn.from_packet(0, 0, make_payload([air_section(biome=7)]))
        self.assertEqual(column.get_biome(0, DEFAULT_MIN_Y, 0), 7)
        self.assertEqual(column.get_biome(15, DEFAULT_MIN_Y + 15, 15), 7)
        self.assertEqual(column.get_biome(0, DEFAULT_MIN_Y + DEFAULT_HEIGHT, 0), 0)
        self.assertEqual(column.get_biome(16, DEFAULT_MIN_Y, 0), 0)

    def test_biome_container_64_entries(self):
        # 真实 1.20.1 服务端的生物群系容器是 4×4×4=64 个条目
        palette = [1, 2, 3]
        values = [i % 3 for i in range(64)]
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        write_container(writer, 0, 0, None)
        write_container(writer, 2, palette, values)
        column = ChunkColumn.from_packet(0, 0, writer.to_bytes())

        for y in range(DEFAULT_MIN_Y, DEFAULT_MIN_Y + 16):
            for x in range(16):
                for z in range(16):
                    local_y = y - DEFAULT_MIN_Y
                    index = ((local_y >> 2) << 4) | ((z >> 2) << 2) | (x >> 2)
                    self.assertEqual(column.get_biome(x, y, z), palette[values[index]])

    def test_section_count_mismatch(self):
        # 只给 1 个 section，其余按空气处理
        column = ChunkColumn.from_packet(0, 0, make_payload([air_section()]))
        self.assertEqual(column.section_count, 1)
        self.assertTrue(column.is_empty())
        self.assertEqual(column.get_state(0, 100, 0), 0)
        self.assertEqual(column.get_biome(0, 100, 0), 0)
        self.assertEqual(len(column.block_states()), 4096)

    def test_empty_factory(self):
        column = ChunkColumn.empty(4, 5)
        self.assertEqual(column.section_count, 24)
        self.assertTrue(column.is_empty())
        self.assertEqual(column.get_state(0, DEFAULT_MIN_Y, 0), 0)
        self.assertEqual(len(column.block_states()), 24 * 4096)

        column.set_state(0, DEFAULT_MIN_Y, 0, 1)
        self.assertFalse(column.is_empty())


class ChunkMalformedTest(unittest.TestCase):
    def test_empty_buffer(self):
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, b"")

    def test_missing_section_data(self):
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, b"\x01")

    def test_negative_section_count(self):
        # section_count = -1（VarInt 负数 5 字节）
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, b"\xff\xff\xff\xff\x0f")

    def test_truncated_palette(self):
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        writer.write_u8(4)  # bits_per_entry
        writer.write_varint(5)  # 声称 5 个调色板条目
        writer.write_varint(10)  # 只给出 1 个就截断
        writer.write(b"\x00" * 8)  # 让 section 数长度检查通过，真正走到调色板读取
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, writer.to_bytes())

    def test_data_length_mismatch(self):
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        writer.write_u8(4)
        writer.write_varint(2)
        writer.write_varint(1)
        writer.write_varint(2)
        writer.write_varint(10)  # bits=4 时必须是 256 个 long
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, writer.to_bytes())

    def test_truncated_data_array(self):
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        writer.write_u8(4)
        writer.write_varint(2)
        writer.write_varint(1)
        writer.write_varint(2)
        writer.write_varint(256)  # 正确的 long 数
        writer.write_u64(0)
        writer.write_u64(0)  # 但只写了 2 个 long
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, writer.to_bytes())

    def test_bits_too_large(self):
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        writer.write_u8(33)  # bits_per_entry > 32
        writer.write(b"\x00" * 64)
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, writer.to_bytes())

    def test_zero_length_palette(self):
        writer = BufferWriter()
        writer.write_varint(1)
        writer.write_i16(0)
        writer.write_u8(4)
        writer.write_varint(0)  # bits>0 时调色板不能为空
        writer.write(b"\x00" * 8)
        self.assertRaises(PaletteError, ChunkColumn.from_packet, 0, 0, writer.to_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)

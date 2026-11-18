"""mcbot.light 的单元测试：手工构造 Java 26.2 光照负载并验证解码（不联网）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from mcbot.buffer import BufferReader, BufferWriter, ProtocolError
from mcbot.light import (
    LIGHT_SECTION_BYTES,
    ChunkLight,
    LightSection,
    decode_light_data,
    is_dark_enough_for_mobs,
    read_bitset,
)

# 世界最低 section y 为 -4 时：光照包多出上下各一个 section。
MIN_LIGHT_SECTION_Y = -5
SECTION_COUNT = 26  # 24 个世界 section + 2

VOLUME = LIGHT_SECTION_BYTES * 2  # 每个 section 4096 个方块


# ---------------------------------------------------------------- 构造工具
def bitset_bytes(bits) -> bytes:
    """按 FriendlyByteBuf.writeBitSet 生成 VarInt long 数量 + big-endian long。"""
    indices = set(bits)
    word_count = (max(indices) // 64 + 1) if indices else 0
    writer = BufferWriter()
    writer.write_varint(word_count)
    for word_index in range(word_count):
        word = 0
        base = word_index * 64
        for bit in indices:
            if base <= bit < base + 64:
                word |= 1 << (bit - base)
        writer.write_u64(word)
    return writer.to_bytes()


def encode_section(levels=None, default: int = 0) -> bytes:
    """把 {方块索引: 光照等级} 按 nibble 规则打包成 2048 字节。

    索引 = (local_y << 8) | (local_z << 4) | local_x；偶数索引写低半字节，
    奇数索引写高半字节。
    """
    values = [default & 0x0F] * VOLUME
    for index, level in (levels or {}).items():
        values[index] = level & 0x0F
    data = bytearray(LIGHT_SECTION_BYTES)
    for index, level in enumerate(values):
        data[index >> 1] |= level << (4 if index & 1 else 0)
    return bytes(data)


def light_payload(sky=None, block=None) -> bytes:
    """生成 light 负载；sky/block 为 {bit: 2048 字节}，值为 None 表示 empty 掩码。"""
    sky = dict(sky or {})
    block = dict(block or {})
    writer = BufferWriter()
    writer.write(bitset_bytes(sky))
    writer.write(bitset_bytes(block))
    writer.write(bitset_bytes(bit for bit, data in sky.items() if data is None))
    writer.write(bitset_bytes(bit for bit, data in block.items() if data is None))
    for layers in (sky, block):
        for bit in sorted(layers):
            data = layers[bit]
            if data is None:
                continue
            writer.write_varint(len(data))
            writer.write(data)
    return writer.to_bytes()


def decode_payload(
    payload: bytes,
    min_light_section_y: int = MIN_LIGHT_SECTION_Y,
    section_count: int = SECTION_COUNT,
):
    reader = BufferReader(payload)
    block_light, sky_light = decode_light_data(
        reader, min_light_section_y, section_count
    )
    return block_light, sky_light, reader


# ---------------------------------------------------------------- BitSet
class TestReadBitset(unittest.TestCase):
    def test_empty(self) -> None:
        reader = BufferReader(b"\x00")
        self.assertEqual(read_bitset(reader), set())
        self.assertEqual(reader.remaining, 0)

    def test_single_long_low_and_high_bits(self) -> None:
        # count=1，唯一 long = 0x8000000000000001 -> bit 0 与 bit 63
        payload = b"\x01\x80" + b"\x00" * 6 + b"\x01"
        reader = BufferReader(payload)
        self.assertEqual(read_bitset(reader), {0, 63})

    def test_multiple_words(self) -> None:
        payload = bitset_bytes({0, 64, 130})
        self.assertEqual(len(payload), 1 + 3 * 8)
        self.assertEqual(read_bitset(BufferReader(payload)), {0, 64, 130})

    def test_negative_count_raises(self) -> None:
        payload = BufferWriter().write_varint(-1).to_bytes()
        with self.assertRaises(ProtocolError):
            read_bitset(BufferReader(payload))

    def test_declared_count_exceeds_remaining_raises(self) -> None:
        payload = b"\x02" + b"\x00" * 8
        with self.assertRaises(ProtocolError):
            read_bitset(BufferReader(payload))

    def test_truncated_long_raises(self) -> None:
        payload = b"\x01" + b"\x00" * 4
        with self.assertRaises(ProtocolError):
            read_bitset(BufferReader(payload))


# ---------------------------------------------------------------- nibble 布局
class TestNibbleLayout(unittest.TestCase):
    def test_even_index_is_low_nibble(self) -> None:
        data = encode_section({0: 15, 1: 3})
        self.assertEqual(len(data), LIGHT_SECTION_BYTES)
        self.assertEqual(data[0], 0x3F)
        self.assertEqual(data[1:], b"\x00" * (LIGHT_SECTION_BYTES - 1))

    def test_index_4095_is_high_nibble_of_last_byte(self) -> None:
        data = encode_section({4094: 9, 4095: 7})
        self.assertEqual(data[-1], 0x79)

    def test_get_uses_local_xyz(self) -> None:
        # bit 0 -> section y = -5 -> 世界 y -80..-65
        data = encode_section({0: 15, 1: 3, 256: 4, 4094: 9, 4095: 7})
        _, sky_light, reader = decode_payload(light_payload(sky={0: data}))
        self.assertEqual(reader.remaining, 0)
        self.assertEqual(sky_light.get("sky", 0, -80, 0), 15)
        self.assertEqual(sky_light.get("sky", 1, -80, 0), 3)  # 索引 1：高半字节
        self.assertEqual(sky_light.get("sky", 0, -79, 0), 4)  # 索引 256：(y=1,z=0,x=0)
        self.assertEqual(sky_light.get("sky", 14, -65, 15), 9)  # 索引 4094
        self.assertEqual(sky_light.get("sky", 15, -65, 15), 7)  # 索引 4095


# ---------------------------------------------------------------- 解码
class TestDecodeLight(unittest.TestCase):
    def test_four_empty_masks(self) -> None:
        block_light, sky_light, reader = decode_payload(b"\x00\x00\x00\x00")
        self.assertEqual(reader.remaining, 0)
        self.assertEqual(block_light.sections, {})
        self.assertEqual(sky_light.sections, {})
        self.assertIsNone(sky_light.get("sky", 0, 0, 0))
        self.assertIsNone(block_light.get("block", 0, 0, 0))

    def test_empty_mask_section_is_all_zero(self) -> None:
        block_light, sky_light, reader = decode_payload(
            light_payload(sky={2: None})
        )
        self.assertEqual(reader.remaining, 0)
        # bit 2 -> section y = -3
        self.assertEqual(sky_light.sections, {-3: LightSection(y=-3, data=None)})
        self.assertEqual(sky_light.get("sky", 0, -48, 0), 0)
        self.assertEqual(sky_light.get("sky", 15, -33, 15), 0)
        self.assertEqual(block_light.sections, {})
        self.assertIsNone(block_light.get("block", 0, -48, 0))

    def test_mask_not_set_section_is_unknown(self) -> None:
        data = encode_section({0: 5})
        _, sky_light, _ = decode_payload(light_payload(block={0: data}))
        self.assertEqual(sky_light.sections, {})
        self.assertIsNone(sky_light.get("sky", 0, -80, 0))

    def test_multiple_sections_with_empty_in_between(self) -> None:
        payload = light_payload(
            sky={0: encode_section({0: 5}), 1: None, 2: encode_section({0: 9})}
        )
        _, sky_light, reader = decode_payload(payload)
        self.assertEqual(reader.remaining, 0)
        self.assertEqual(set(sky_light.sections), {-5, -4, -3})
        self.assertIsNone(sky_light.sections[-4].data)
        self.assertEqual(sky_light.get("sky", 0, -80, 0), 5)
        self.assertEqual(sky_light.get("sky", 0, -64, 0), 0)
        self.assertEqual(sky_light.get("sky", 0, -48, 0), 9)

    def test_block_and_sky_are_independent(self) -> None:
        payload = light_payload(
            sky={3: encode_section({0: 12})},
            block={1: encode_section({0: 4}), 3: None},
        )
        block_light, sky_light, reader = decode_payload(payload)
        self.assertEqual(reader.remaining, 0)
        # 数组顺序：先 sky 再 block，各自的 bit 升序
        self.assertEqual(set(sky_light.sections), {-2})
        self.assertEqual(set(block_light.sections), {-4, -2})
        self.assertEqual(sky_light.get("sky", 0, -32, 0), 12)
        self.assertEqual(block_light.get("block", 0, -64, 0), 4)
        self.assertEqual(block_light.get("block", 0, -32, 0), 0)  # empty 掩码
        # 层名写错时按未知处理
        self.assertIsNone(sky_light.get("block", 0, -32, 0))
        self.assertIsNone(block_light.get("sky", 0, -64, 0))
        self.assertIsNone(sky_light.get("nope", 0, -32, 0))
        self.assertEqual(sky_light.layer, "sky")
        self.assertEqual(block_light.layer, "block")

    def test_arrays_read_in_bit_order(self) -> None:
        payload = light_payload(
            sky={
                0: encode_section({0: 1}),
                2: encode_section({0: 2}),
                5: encode_section({0: 3}),
            }
        )
        _, sky_light, _ = decode_payload(payload)
        self.assertEqual(sky_light.get("sky", 0, -80, 0), 1)  # section -5
        self.assertEqual(sky_light.get("sky", 0, -48, 0), 2)  # section -3
        self.assertEqual(sky_light.get("sky", 0, 0, 0), 3)  # section 0

    def test_out_of_range_and_unknown_coordinates(self) -> None:
        payload = light_payload(sky={0: encode_section({0: 15})})
        _, sky_light, _ = decode_payload(payload)
        self.assertIsNone(sky_light.get("sky", 16, -80, 0))
        self.assertIsNone(sky_light.get("sky", 0, -80, 16))
        self.assertIsNone(sky_light.get("sky", -1, -80, 0))
        self.assertIsNone(sky_light.get("sky", 0, -80, -1))
        self.assertIsNone(sky_light.get("sky", 0, -16, 0))  # section -1 不在掩码中
        self.assertIsNone(sky_light.get("sky", 0, 1000, 0))

    def test_mask_bit_out_of_range_raises(self) -> None:
        payload = light_payload(sky={SECTION_COUNT: encode_section({0: 1})})
        with self.assertRaises(ProtocolError):
            decode_payload(payload)

    def test_invalid_section_data_length_in_get_raises(self) -> None:
        light = ChunkLight({0: LightSection(y=0, data=b"\x00")})
        with self.assertRaises(ProtocolError):
            light.get("block", 0, 0, 0)


# ---------------------------------------------------------------- 截断/非法
class TestTruncation(unittest.TestCase):
    def test_missing_light_array_raises(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes({0}))  # sky mask 置位，但没有数据数组
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_wrong_array_length_raises(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes({0}))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write_varint(LIGHT_SECTION_BYTES - 1)
        writer.write(b"\x00" * (LIGHT_SECTION_BYTES - 1))
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_negative_array_length_raises(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes({0}))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write_varint(-1)
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_short_array_bytes_raise(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes({0}))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write_varint(LIGHT_SECTION_BYTES)
        writer.write(b"\x00" * 10)
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_truncated_block_array_raises(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes({0}))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write_varint(LIGHT_SECTION_BYTES)
        writer.write(b"\x00" * 10)
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_truncated_array_length_varint_raises(self) -> None:
        writer = BufferWriter()
        writer.write(bitset_bytes({0}))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(bitset_bytes(set()))
        writer.write(b"\x80")  # VarInt 未结束
        with self.assertRaises(ProtocolError):
            decode_payload(writer.to_bytes())

    def test_truncated_mask_raises(self) -> None:
        payload = b"\x01" + b"\x00" * 5  # sky mask 声明 1 个 long 但只有 5 字节
        with self.assertRaises(ProtocolError):
            decode_payload(payload)


# ---------------------------------------------------------------- 刷怪判断
class TestMobSpawning(unittest.TestCase):
    def test_day_surface_is_not_dark_enough(self) -> None:
        self.assertFalse(is_dark_enough_for_mobs(0, 15, True))
        self.assertFalse(is_dark_enough_for_mobs(0, 8, True))

    def test_day_shade_is_dark_enough(self) -> None:
        self.assertTrue(is_dark_enough_for_mobs(0, 7, True))
        self.assertTrue(is_dark_enough_for_mobs(0, 0, True))

    def test_night_is_dark_enough_regardless_of_sky(self) -> None:
        self.assertTrue(is_dark_enough_for_mobs(0, 15, False))
        self.assertTrue(is_dark_enough_for_mobs(0, 0, False))

    def test_block_light_prevents_spawn(self) -> None:
        self.assertFalse(is_dark_enough_for_mobs(1, 0, False))
        self.assertFalse(is_dark_enough_for_mobs(15, 0, False))
        self.assertFalse(is_dark_enough_for_mobs(1, 15, True))

    def test_unknown_light_treated_as_dark(self) -> None:
        self.assertTrue(is_dark_enough_for_mobs(None, None, True))
        self.assertFalse(is_dark_enough_for_mobs(None, 15, True))


if __name__ == "__main__":
    unittest.main()

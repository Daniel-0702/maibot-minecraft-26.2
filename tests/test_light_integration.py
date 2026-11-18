"""光照接入测试：掩码位、坐标映射、区块包里的读取顺序。"""
from __future__ import annotations

import unittest

from mcbot.buffer import BufferWriter, BufferReader
from mcbot.modern_client import ModernMinecraftClient


def encode_bitset(bits: set[int]) -> bytes:
    """FriendlyByteBuf.writeBitSet：VarInt long 数 + big-endian long。"""
    if not bits:
        writer = BufferWriter().write_varint(0)
        return writer.to_bytes()
    words = max(bits) // 64 + 1
    longs = [0] * words
    for bit in bits:
        longs[bit // 64] |= 1 << (bit % 64)
    writer = BufferWriter().write_varint(words)
    for value in longs:
        writer.write_u64(value)
    return writer.to_bytes()


def encode_nibble_section(levels: dict[int, int]) -> bytes:
    """把 {方块索引: 光照} 编成 2048 字节 nibble 数据。"""
    data = bytearray(2048)
    for index, level in levels.items():
        byte_index = index >> 1
        if index & 1:
            data[byte_index] = (data[byte_index] & 0x0F) | ((level & 0x0F) << 4)
        else:
            data[byte_index] = (data[byte_index] & 0xF0) | (level & 0x0F)
    return bytes(data)


def light_payload(
    *,
    block_sections: dict[int, dict[int, int]],
    sky_sections: dict[int, dict[int, int]],
    section_count: int = 4,
) -> bytes:
    """按 26.2 顺序：skyMask, blockMask, emptySky, emptyBlock, sky 数组, block 数组。"""
    sky_mask = set(sky_sections)
    block_mask = set(block_sections)
    writer = BufferWriter()
    writer.write(encode_bitset(sky_mask))
    writer.write(encode_bitset(block_mask))
    writer.write(encode_bitset(set()))
    writer.write(encode_bitset(set()))
    for index in sorted(sky_mask):
        raw = encode_nibble_section(sky_sections[index])
        writer.write_varint(len(raw)).write(raw)
    for index in sorted(block_mask):
        raw = encode_nibble_section(block_sections[index])
        writer.write_varint(len(raw)).write(raw)
    return writer.to_bytes()


class LightIntegrationTests(unittest.TestCase):
    def _client(self) -> ModernMinecraftClient:
        client = ModernMinecraftClient("127.0.0.1")
        client.world.min_y = 0
        client.world.height = 32  # 2 个 block section → section_count 4, min light section -1
        return client

    def test_read_light_and_lookup(self) -> None:
        client = self._client()
        # 掩码 bit i 对应 section y = min_light_section_y + i = -1 + i
        payload = light_payload(
            block_sections={1: {0: 15, 4095: 7}},
            sky_sections={1: {0: 4}},
        )
        client._read_light(2, -3, BufferReader(payload))
        self.assertEqual(client.light_at(2 * 16 + 0, 0, -3 * 16 + 0, "block"), 15)
        self.assertEqual(client.light_at(2 * 16 + 15, 15, -3 * 16 + 15, "block"), 7)
        self.assertEqual(client.light_at(2 * 16, 0, -3 * 16, "sky"), 4)
        # 没给的 section 是未知，不能假装是 0
        self.assertIsNone(client.light_at(2 * 16, 16, -3 * 16, "block"))
        self.assertIsNone(client.light_at(99 * 16, 0, 0, "block"))

    def test_chunk_packet_reads_light_after_block_entities(self) -> None:
        client = self._client()
        writer = BufferWriter()
        writer.write_i32(0).write_i32(0)  # chunk x/z
        writer.write_varint(0)  # 没有 heightmap
        writer.write_varint(2).write(b"\x00\x00")  # 故意给 2 字节坏区块数据
        writer.write_varint(1)  # 一个方块实体，必须在光照之前被跳过
        writer.write_u8(0x02).write_i16(5).write_varint(7)
        writer.write(b"\x00")  # 空 NBT
        writer.write(
            light_payload(block_sections={2: {0: 9}}, sky_sections={2: {0: 12}}, section_count=4)
        )
        client._chunk(BufferReader(writer.to_bytes()))
        # 区块数据是坏的，但光照要已经存下来
        self.assertEqual(client.chunk_decode_errors, 1)
        self.assertEqual(client.light_at(0, 16, 0, "block"), 9)
        self.assertEqual(client.light_at(0, 16, 0, "sky"), 12)

    def test_is_dark_here(self) -> None:
        client = self._client()
        client.position = client.position.replace(x=8, y=0, z=8)
        client.world.day_time = 6000
        # 方块光 0、天空光低 → 暗
        client.block_light[(0, 0)] = __import__("mcbot.light", fromlist=["ChunkLight"]).ChunkLight(
            {0: __import__("mcbot.light", fromlist=["LightSection"]).LightSection(0, encode_nibble_section({}))}
        )
        client.sky_light[(0, 0)] = __import__("mcbot.light", fromlist=["ChunkLight"]).ChunkLight(
            {0: __import__("mcbot.light", fromlist=["LightSection"]).LightSection(0, encode_nibble_section({}))}
        )
        self.assertTrue(client.is_dark_here())


if __name__ == "__main__":
    unittest.main()

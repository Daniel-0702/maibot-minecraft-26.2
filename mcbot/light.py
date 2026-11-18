"""Java 26.2 光照数据解析：区块包末尾附带的 ``light`` 负载。

``ClientboundLevelChunkWithLightPacket`` 末尾的 ``light`` 字段是
``ClientboundLightUpdatePacketData``，读取顺序为四个 BitSet：

1. ``skyYMask``（天空光掩码）
2. ``blockYMask``（方块光掩码）
3. ``emptySkyYMask``（天空光全 0 掩码）
4. ``emptyBlockYMask``（方块光全 0 掩码）

随后按 ``skyYMask`` 中逐个置位（且不在 ``emptySkyYMask`` 中）的 section 顺序，
各读一个 2048 字节数组（``DATA_LAYER_STREAM_CODEC``：VarInt 长度 + 2048 字节），
然后对 ``blockYMask`` 做同样处理。BitSet 的 wire 格式为 VarInt long 数量 +
若干 big-endian long。

掩码 bit i 对应 section y = ``min_light_section_y + i``，因此调用方需要传入
``min_light_section_y``（世界最低 section y 减一）和 ``section_count``
（世界 section 数加二）。掩码中未置位的 section 视为“未知”，不会出现在
:attr:`ChunkLight.sections` 中；被 empty 掩码覆盖的 section 记为 ``data=None``，
表示服务端没有发送数据、该层全为 0。

每个 section 的 2048 字节是 4096 个方块的光照等级（0..15），nibble 打包：
``index = (y << 8) | (z << 4) | x``（y、z、x 均为 0..15 的局部坐标），
``byte = data[index >> 1]``；index 为偶数取低半字节、奇数取高半字节。

本模块只依赖 :mod:`mcbot.buffer` 与标准库。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .buffer import BufferReader, ProtocolError

__all__ = [
    "LIGHT_SECTION_BYTES",
    "LIGHT_SECTION_VOLUME",
    "LightSection",
    "ChunkLight",
    "read_bitset",
    "decode_light_data",
    "is_dark_enough_for_mobs",
]

LIGHT_SECTION_BYTES = 2048  # 4096 个方块 × 4 bit
LIGHT_SECTION_VOLUME = 4096  # 16 × 16 × 16
_LIGHT_LAYERS = frozenset({"block", "sky"})
_LONG_BYTES = 8
_LONG_BITS = 64


@dataclass
class LightSection:
    """一个 section 的光照层数据。

    ``data`` 是 2048 字节的 nibble 数据；``None`` 表示该层被 empty 掩码标记为
    全 0（服务端没有发送数据）。若某个 section 根本不在掩码里，它不会出现在
    :class:`ChunkLight` 中，调用方应把整层当作“未知”。
    """

    y: int
    data: bytes | None = None


@dataclass
class ChunkLight:
    """一层光照的 section 集合，``sections`` 的键为 section y。

    ``layer`` 记录本对象属于哪一层（``"block"`` 或 ``"sky"``）；为 ``None`` 时
    不做层名校验（方便单独构造对象）。该字段不参与 dataclass 相等比较。
    """

    sections: dict[int, LightSection] = field(default_factory=dict)
    layer: str | None = field(default=None, compare=False)

    def get(self, layer: str, x: int, y: int, z: int) -> int | None:
        """返回 (x, y, z) 处的光照等级 0..15；未知或越界返回 ``None``。

        ``layer`` 必须是 ``"block"`` 或 ``"sky"``；若本对象记录了所属层且与
        参数不一致，说明调用方问错了对象，按“未知”返回 ``None``。

        x、z 必须落在区块内的 0..15；y 可以是任意世界坐标，其所在 section 不在
        ``sections`` 中时返回 ``None``（未知）。被 empty 掩码覆盖的 section 返回 0。
        """
        if layer not in _LIGHT_LAYERS:
            return None
        if self.layer is not None and layer != self.layer:
            return None
        if not (0 <= x < 16 and 0 <= z < 16):
            return None
        section = self.sections.get(y >> 4)
        if section is None:
            return None
        if section.data is None:
            return 0
        data = section.data
        if len(data) != LIGHT_SECTION_BYTES:
            raise ProtocolError(
                f"section {section.y} 的光照数据长度为 {len(data)}，应为 {LIGHT_SECTION_BYTES}"
            )
        index = ((y & 15) << 8) | (z << 4) | x
        packed = data[index >> 1]
        if index & 1:
            return (packed >> 4) & 0x0F
        return packed & 0x0F


def read_bitset(reader: BufferReader) -> set[int]:
    """读取 BitSet：VarInt long 数量 + 若干 big-endian long，返回置位索引集合。

    与 ``FriendlyByteBuf.readBitSet`` 一致，声明的 long 数量不能超过剩余字节
    所能容纳的数量；截断或数量非法时抛 :class:`ProtocolError`。
    """
    count = reader.read_varint()
    if count < 0:
        raise ProtocolError(f"BitSet 的 long 数量为负数: {count}")
    if count * _LONG_BYTES > reader.remaining:
        raise ProtocolError(
            f"BitSet 数据不足: 声明 {count} 个 long，仅剩 {reader.remaining} 字节"
        )
    bits: set[int] = set()
    for word_index in range(count):
        word = reader.read_u64()
        base = word_index * _LONG_BITS
        while word:
            lowest = word & -word
            bits.add(base + lowest.bit_length() - 1)
            word ^= lowest
    return bits


def decode_light_data(
    reader: BufferReader,
    min_light_section_y: int,
    section_count: int,
) -> tuple[ChunkLight, ChunkLight]:
    """解析 ``light`` 负载，返回 ``(block_light, sky_light)``。

    ``min_light_section_y`` 是世界最低 section y 减一，``section_count`` 是
    世界 section 数加二；掩码 bit i 对应 section y =
    ``min_light_section_y + i``，且必须满足 ``0 <= i < section_count``，
    否则视为非法数据并抛 :class:`ProtocolError`。
    """
    if section_count < 0:
        raise ProtocolError(f"section 数量为负数: {section_count}")
    sky_mask = read_bitset(reader)
    block_mask = read_bitset(reader)
    empty_sky_mask = read_bitset(reader)
    empty_block_mask = read_bitset(reader)
    sky_light = _decode_layer(
        reader, sky_mask, empty_sky_mask, min_light_section_y, section_count, "sky"
    )
    block_light = _decode_layer(
        reader, block_mask, empty_block_mask, min_light_section_y, section_count, "block"
    )
    return block_light, sky_light


def is_dark_enough_for_mobs(block_light: int, sky_light: int, is_day: bool) -> bool:
    """保守近似：判断该位置是否足够暗、可能刷怪。

    这是对 Minecraft Wiki 刷怪规则的**保守近似**，不是逐条复刻原版逻辑：
    只要求方块光为 0，并且（夜晚或天空光 ≤ 7）。白天露天地表（天空光 ≥ 8）
    视为不刷怪。真实刷怪还受生物群系、结构、玩家距离、难度等影响，这里都忽略，
    所以“可能刷怪”只作为风险提示。

    未知光照（``None``）按最暗处理，宁可多报“可能刷怪”。
    """
    block = 0 if block_light is None else block_light
    sky = 0 if sky_light is None else sky_light
    if block > 0:
        return False
    if not is_day:
        return True
    return sky <= 7


# ---------------------------------------------------------------- 内部实现
def _decode_layer(
    reader: BufferReader,
    mask: set[int],
    empty_mask: set[int],
    min_light_section_y: int,
    section_count: int,
    layer: str,
) -> ChunkLight:
    """按 bit 升序读取一层中 mask 置位且不在 empty_mask 中的 section 数据。"""
    sections: dict[int, LightSection] = {}
    for bit in sorted(mask):
        if bit < 0 or bit >= section_count:
            raise ProtocolError(
                f"{layer} 光照掩码 bit {bit} 超出范围 (section_count={section_count})"
            )
        section_y = min_light_section_y + bit
        if bit in empty_mask:
            sections[section_y] = LightSection(y=section_y, data=None)
            continue
        length = reader.read_varint()
        if length != LIGHT_SECTION_BYTES:
            raise ProtocolError(
                f"{layer} 光照 section 数据长度非法: {length}，应为 {LIGHT_SECTION_BYTES}"
            )
        sections[section_y] = LightSection(y=section_y, data=reader.read(length))
    return ChunkLight(sections=sections, layer=layer)

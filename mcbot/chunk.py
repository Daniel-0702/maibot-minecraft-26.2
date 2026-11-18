"""1.20.1 区块数据解码：方块状态与生物群系（调色板容器）。

客户端从 “Chunk Data and Update Light” 包中剥掉 VarInt 长度后，把 ``chunkData``
字节串交给 :meth:`ChunkColumn.from_packet`。每个 section 自下而上排列，依次是
i16 非空气方块数、方块状态调色板容器、生物群系调色板容器。

调色板容器的条目按 LSB-first 紧密打包进 64 位长整数，条目可以跨两个 long；
方块状态索引顺序为 ``(local_y << 8) | (local_z << 4) | local_x``。方块状态
容器固定 4096 个条目；真实服务端的生物群系容器是 4×4×4=64 个条目，本模块会
根据数据数组长度自动识别 64 或 4096 条目，以同时兼容协议文档与真实数据。

本模块只解析方块状态和生物群系，光照数据由网络层处理。
"""

from __future__ import annotations

import sys
from array import array

from .buffer import BufferReader, ProtocolError

__all__ = [
    "PaletteError",
    "ChunkColumn",
    "DEFAULT_MIN_Y",
    "DEFAULT_HEIGHT",
]

DEFAULT_MIN_Y = -64
DEFAULT_HEIGHT = 384

_BLOCK_VOLUME = 4096  # 方块状态：16×16×16
_BIOME_VOLUME = 64  # 生物群系：4×4×4
_MAX_BITS = 32
_MIN_SECTION_BYTES = 8  # 2 + 两个单值容器（各 3 字节）


class PaletteError(ValueError):
    """调色板容器或区块负载结构非法。"""


# ------------------------------------------------------------------ 底层解包


def _read_longs(reader: BufferReader, count: int) -> array:
    """读取 ``count`` 个大端序 i64，返回主机字节序的 ``array('Q')``。"""
    longs = array("Q")
    longs.frombytes(reader.read(count * 8))
    if sys.byteorder == "little":
        longs.byteswap()
    return longs


def _length_bounds(volume: int, bits: int) -> tuple[int, int]:
    """返回该条目数下 data_length 的最小/最大值。

    最小值是“无填充紧密打包”所需的 long 数；最大值是 Java ``BitStorage``
    按 ``floor(64/bits)`` 槽位计算的保守长度（会在末尾留下少量填充）。
    """
    per_long = 64 // bits
    min_longs = (volume * bits + 63) // 64
    max_longs = (volume + per_long - 1) // per_long
    return min_longs, max_longs


def _choose_volume(bits: int, data_length: int, is_biome: bool) -> int:
    """根据 data_length 判断该容器是 4096（方块）还是 64（生物群系）条目。"""
    low, high = _length_bounds(_BLOCK_VOLUME, bits)
    if low <= data_length <= high:
        return _BLOCK_VOLUME
    if is_biome:
        low, high = _length_bounds(_BIOME_VOLUME, bits)
        if low <= data_length <= high:
            return _BIOME_VOLUME
    raise PaletteError(
        f"data_length {data_length} 与 bits_per_entry {bits} 不匹配（预期 {_BLOCK_VOLUME} 或 {_BIOME_VOLUME} 条目）"
    )


def _make_container(bits: int, palette: list[int] | None, volume: int) -> array | list[int]:
    """创建结果容器：能用 ``array('H')`` 就用（省内存、赋值快）。"""
    if palette is None:
        use_array = bits <= 16  # 直接调色板：bits≤16 时值必然 ≤65535
    else:
        use_array = min(palette) >= 0 and max(palette) <= 0xFFFF
    if use_array:
        return array("H", [0]) * volume
    return [0] * volume


def _unpack_entries(
    longs: array, bits: int, palette: list[int] | None, volume: int
) -> tuple[array | list[int], int]:
    """把打包的 long 数组解成条目值；返回 (容器, 越界索引数)。"""
    mask = (1 << bits) - 1
    out = _make_container(bits, palette, volume)
    invalid = 0
    needed = volume * bits // 64  # volume*bits 必为 64 的倍数
    words = longs if len(longs) == needed else longs[:needed]

    if 64 % bits == 0:  # 1/2/4/8/16/32：每个 long 恰好装整数个条目
        per_long = 64 // bits
        pos = 0
        if palette is None:
            for word in words:
                for _ in range(per_long):
                    out[pos] = word & mask
                    word >>= bits
                    pos += 1
            return out, 0

        palette_len = len(palette)
        if palette_len == mask + 1:  # 调色板刚好填满索引空间，无需越界检查
            for word in words:
                for _ in range(per_long):
                    out[pos] = palette[word & mask]
                    word >>= bits
                    pos += 1
            return out, 0

        for word in words:
            for _ in range(per_long):
                value = word & mask
                if value < palette_len:
                    out[pos] = palette[value]
                else:
                    invalid += 1
                word >>= bits
                pos += 1
        return out, invalid

    # 一般情况：条目可能跨越两个 long，用位缓冲累积读取
    if palette is None:
        buffer = 0
        available = 0
        word_index = 0
        for index in range(volume):
            if available < bits:
                buffer |= words[word_index] << available
                word_index += 1
                available += 64
            out[index] = buffer & mask
            buffer >>= bits
            available -= bits
        return out, 0

    palette_len = len(palette)
    if palette_len == mask + 1:
        buffer = 0
        available = 0
        word_index = 0
        for index in range(volume):
            if available < bits:
                buffer |= words[word_index] << available
                word_index += 1
                available += 64
            out[index] = palette[buffer & mask]
            buffer >>= bits
            available -= bits
        return out, 0

    buffer = 0
    available = 0
    word_index = 0
    for index in range(volume):
        if available < bits:
            buffer |= words[word_index] << available
            word_index += 1
            available += 64
        value = buffer & mask
        if value < palette_len:
            out[index] = palette[value]
        else:
            invalid += 1
        buffer >>= bits
        available -= bits
    return out, invalid


def _decode_container(
    reader: BufferReader, is_biome: bool = False
) -> tuple[int | array | list[int], int, int]:
    """解析一个调色板容器，返回 (容器, 越界索引数, 条目数)。

    ``bits_per_entry == 0`` 时用单个 int 表示整段统一值；否则返回
    ``array('H')`` 或 ``list[int]``（值超过 16 位时）。
    """
    bits = reader.read_u8()
    if bits == 0:
        palette_length = reader.read_varint()
        if palette_length != 1:
            raise PaletteError(f"单值调色板的 palette_length 应为 1，实际 {palette_length}")
        return reader.read_varint(), 0, _BLOCK_VOLUME

    if bits > _MAX_BITS:
        raise PaletteError(f"bits_per_entry 过大: {bits}")

    palette: list[int] | None
    if bits <= 8:
        palette_length = reader.read_varint()
        if palette_length <= 0:
            raise PaletteError(f"bits_per_entry={bits} 时调色板长度必须大于 0")
        if palette_length > reader.remaining:
            raise PaletteError(f"调色板长度 {palette_length} 超出剩余数据")
        palette = [reader.read_varint() for _ in range(palette_length)]
    else:
        palette = None  # 直接调色板，不写调色板列表

    data_length = reader.read_varint()
    if data_length < 0:
        raise PaletteError(f"数据数组长度为负数: {data_length}")
    volume = _choose_volume(bits, data_length, is_biome)
    if data_length * 8 > reader.remaining:
        raise PaletteError(
            f"数据数组被截断: 需要 {data_length * 8} 字节，仅剩 {reader.remaining} 字节"
        )

    longs = _read_longs(reader, data_length)
    container, invalid = _unpack_entries(longs, bits, palette, volume)
    return container, invalid, volume


# ------------------------------------------------------------------ 区块列


class ChunkColumn:
    """一个区块列（16 × height × 16），按 section 紧凑存储方块状态与生物群系。"""

    __slots__ = (
        "cx",
        "cz",
        "min_y",
        "height",
        "_sections",
        "_biomes",
        "_biome_volume",
        "_non_air",
        "invalid_index_count",
    )

    def __init__(
        self,
        cx: int,
        cz: int,
        min_y: int = DEFAULT_MIN_Y,
        height: int = DEFAULT_HEIGHT,
    ) -> None:
        if height <= 0:
            raise ValueError(f"区块高度必须为正数: {height}")
        self.cx = cx
        self.cz = cz
        self.min_y = min_y
        self.height = height
        self._sections: list[int | array | list[int]] = []
        self._biomes: list[int | array | list[int]] = []
        self._biome_volume: list[int] = []
        self._non_air: list[int] = []
        #: 解码时遇到的调色板越界索引总数（按 0=空气 处理）
        self.invalid_index_count = 0

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_packet(
        cls,
        cx: int,
        cz: int,
        data: bytes,
        min_y: int = DEFAULT_MIN_Y,
        height: int = DEFAULT_HEIGHT,
    ) -> "ChunkColumn":
        """解析 ``chunkData`` 字节串（已去掉 VarInt 长度前缀）。"""
        column = cls(cx, cz, min_y, height)
        reader = BufferReader(data)
        sections: list[int | array | list[int]] = []
        biomes: list[int | array | list[int]] = []
        biome_volume: list[int] = []
        non_air: list[int] = []
        invalid_total = 0
        try:
            section_count = reader.read_varint()
            if section_count < 0:
                raise PaletteError(f"section 数为负数: {section_count}")
            # 每个 section 至少 8 字节，先做一个廉价的长度上限检查
            if section_count > reader.remaining // _MIN_SECTION_BYTES:
                raise PaletteError(
                    f"section 数 {section_count} 与剩余数据 {reader.remaining} 字节不匹配"
                )
            for _ in range(section_count):
                count = reader.read_i16()
                if count < 0 or count > _BLOCK_VOLUME:
                    raise PaletteError(f"非空气方块数非法: {count}")
                states, invalid_states, _ = _decode_container(reader, False)
                biome_container, invalid_biomes, volume = _decode_container(reader, True)
                invalid_total += invalid_states + invalid_biomes
                sections.append(states)
                biomes.append(biome_container)
                biome_volume.append(volume)
                non_air.append(count)
        except ProtocolError as exc:
            raise PaletteError(f"区块数据被截断: {exc}") from exc

        column._sections = sections
        column._biomes = biomes
        column._biome_volume = biome_volume
        column._non_air = non_air
        column.invalid_index_count = invalid_total
        return column

    @classmethod
    def empty(
        cls,
        cx: int,
        cz: int,
        min_y: int = DEFAULT_MIN_Y,
        height: int = DEFAULT_HEIGHT,
    ) -> "ChunkColumn":
        """创建全空气区块列。"""
        column = cls(cx, cz, min_y, height)
        section_count = (height + 15) // 16
        column._sections = [0] * section_count
        column._biomes = [0] * section_count
        column._biome_volume = [_BLOCK_VOLUME] * section_count
        column._non_air = [0] * section_count
        return column

    # ---------------------------------------------------------------- 方块
    def get_state(self, local_x: int, y: int, local_z: int) -> int:
        """取方块状态 id；越界坐标返回 0（空气）。"""
        if local_x < 0 or local_x > 15 or local_z < 0 or local_z > 15:
            return 0
        relative_y = y - self.min_y
        if relative_y < 0 or relative_y >= self.height:
            return 0
        section_index = relative_y >> 4
        if section_index >= len(self._sections):
            return 0
        section = self._sections[section_index]
        if isinstance(section, int):
            return section
        index = ((relative_y & 15) << 8) | (local_z << 4) | local_x
        return section[index]

    def set_state(self, local_x: int, y: int, local_z: int, state_id: int) -> None:
        """写入方块状态；越界坐标静默忽略，统一 section 会先展开。"""
        if local_x < 0 or local_x > 15 or local_z < 0 or local_z > 15:
            return
        relative_y = y - self.min_y
        if relative_y < 0 or relative_y >= self.height:
            return
        section_index = relative_y >> 4
        self._ensure_section(section_index)
        index = ((relative_y & 15) << 8) | (local_z << 4) | local_x
        section = self._sections[section_index]
        if isinstance(section, int):
            old_value = section
            if old_value == state_id:
                return
            if 0 <= old_value <= 0xFFFF:
                section = array("H", [old_value]) * _BLOCK_VOLUME
            else:
                section = [old_value] * _BLOCK_VOLUME
            self._sections[section_index] = section
        else:
            old_value = section[index]

        if state_id == old_value:
            return
        if not isinstance(section, list) and not 0 <= state_id <= 0xFFFF:
            section = list(section)
            self._sections[section_index] = section
        section[index] = state_id

        if old_value == 0 and state_id != 0:
            self._non_air[section_index] += 1
        elif old_value != 0 and state_id == 0:
            count = self._non_air[section_index] - 1
            self._non_air[section_index] = count if count > 0 else 0

    def _ensure_section(self, section_index: int) -> None:
        """补齐缺失的 section（按全空气处理）。"""
        while len(self._sections) <= section_index:
            self._sections.append(0)
            self._biomes.append(0)
            self._biome_volume.append(_BLOCK_VOLUME)
            self._non_air.append(0)

    # ---------------------------------------------------------------- 生物群系
    def get_biome(self, local_x: int, y: int, local_z: int) -> int:
        """取生物群系 id；越界坐标返回 0。"""
        if local_x < 0 or local_x > 15 or local_z < 0 or local_z > 15:
            return 0
        relative_y = y - self.min_y
        if relative_y < 0 or relative_y >= self.height:
            return 0
        section_index = relative_y >> 4
        if section_index >= len(self._biomes):
            return 0
        container = self._biomes[section_index]
        if isinstance(container, int):
            return container
        local_y = relative_y & 15
        if self._biome_volume[section_index] == _BIOME_VOLUME:
            # 4×4×4 生物群系单元：每个单元覆盖 4×4×4 个方块
            index = ((local_y >> 2) << 4) | ((local_z >> 2) << 2) | (local_x >> 2)
        else:
            index = (local_y << 8) | (local_z << 4) | local_x
        return container[index]

    # ---------------------------------------------------------------- 统计
    @property
    def section_count(self) -> int:
        """已存储的 section 数量。"""
        return len(self._sections)

    def non_air_count(self, section_index: int) -> int:
        """该 section 的非空气方块数（来自包内计数，写方块时同步更新）。"""
        if 0 <= section_index < len(self._non_air):
            return self._non_air[section_index]
        return 0

    def is_empty(self) -> bool:
        """是否全部为空气。"""
        for count in self._non_air:
            if count:
                return False
        for section in self._sections:
            if isinstance(section, int):
                if section:
                    return False
            elif any(section):
                return False
        return True

    def block_states(self) -> list[int]:
        """按 section 顺序展开全部方块状态（16×16×16×section_count，测试用）。"""
        states: list[int] = []
        for section in self._sections:
            if isinstance(section, int):
                states.extend([section] * _BLOCK_VOLUME)
            elif isinstance(section, array):
                states.extend(section.tolist())
            else:
                states.extend(section)
        return states

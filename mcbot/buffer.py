"""Minecraft 协议基础读写器：VarInt、字符串、UUID、方块坐标、角度、NBT 挂载点。

所有多字节整数均为大端序（与 Minecraft 协议一致）。
"""

from __future__ import annotations

import struct
import uuid
from typing import Any

from .types import BlockPos

__all__ = ["ProtocolError", "BufferReader", "BufferWriter"]


class ProtocolError(Exception):
    """协议数据非法或长度不足。"""


class BufferReader:
    """从字节串中按 Minecraft 协议规则读取数据。"""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self._data = data
        self._pos = pos

    # ------------------------------------------------------------------ 基础
    @property
    def position(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def read(self, count: int) -> bytes:
        if count < 0:
            raise ProtocolError(f"读取长度为负数: {count}")
        end = self._pos + count
        if end > len(self._data):
            raise ProtocolError(f"数据不足: 需要 {count} 字节，仅剩 {len(self._data) - self._pos}")
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def skip(self, count: int) -> None:
        self.read(count)

    def read_rest(self) -> bytes:
        return self.read(self.remaining)

    def _unpack(self, fmt: str, size: int) -> Any:
        return struct.unpack(fmt, self.read(size))[0]

    # ------------------------------------------------------------- 整数/浮点
    def read_byte(self) -> int:
        return self.read_u8()

    def read_u8(self) -> int:
        pos = self._pos
        if pos >= len(self._data):
            raise ProtocolError("数据不足: 需要 1 字节")
        self._pos = pos + 1
        return self._data[pos]

    def read_i8(self) -> int:
        return self._unpack(">b", 1)

    def read_u16(self) -> int:
        return self._unpack(">H", 2)

    def read_i16(self) -> int:
        return self._unpack(">h", 2)

    def read_u32(self) -> int:
        return self._unpack(">I", 4)

    def read_i32(self) -> int:
        return self._unpack(">i", 4)

    def read_u64(self) -> int:
        return self._unpack(">Q", 8)

    def read_i64(self) -> int:
        return self._unpack(">q", 8)

    def read_f32(self) -> float:
        return self._unpack(">f", 4)

    def read_f64(self) -> float:
        return self._unpack(">d", 8)

    def read_bool(self) -> bool:
        return self.read_u8() != 0

    def read_varint(self) -> int:
        """读取 32 位 VarInt（有符号，负数固定 5 字节）。"""
        result = 0
        for index in range(5):
            byte = self.read_u8()
            result |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                break
        else:
            raise ProtocolError("VarInt 超过 5 字节")
        if result & 0x80000000:
            result -= 0x100000000
        return result

    def read_varlong(self) -> int:
        """读取 64 位 VarLong。"""
        result = 0
        for index in range(10):
            byte = self.read_u8()
            result |= (byte & 0x7F) << (7 * index)
            if not byte & 0x80:
                break
        else:
            raise ProtocolError("VarLong 超过 10 字节")
        if result & 0x8000000000000000:
            result -= 0x10000000000000000
        return result

    # ------------------------------------------------------------------ 复杂
    def read_string(self, max_chars: int = 32767) -> str:
        length = self.read_varint()
        if length < 0 or length > max_chars * 4:
            raise ProtocolError(f"字符串长度非法: {length}")
        try:
            return self.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"字符串不是合法 UTF-8: {exc}") from exc

    def read_uuid(self) -> uuid.UUID:
        return uuid.UUID(bytes=self.read(16))

    def read_position(self) -> BlockPos:
        value = self.read_u64()
        x = value >> 38
        z = (value >> 12) & 0x3FFFFFF
        y = value & 0xFFF
        if x >= 1 << 25:
            x -= 1 << 26
        if z >= 1 << 25:
            z -= 1 << 26
        if y >= 1 << 11:
            y -= 1 << 12
        return BlockPos(x, y, z)

    def read_angle(self) -> float:
        return self.read_u8() * (360.0 / 256.0)

    def read_nbt(self) -> Any:
        """读取网络 NBT（命名根标签）。"""
        from .nbt import read_nbt

        return read_nbt(self)

    def read_optional_nbt(self) -> Any:
        from .nbt import read_nbt

        value = read_nbt(self)
        return value


class BufferWriter:
    """按 Minecraft 协议规则写字节串。"""

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = bytearray()

    def __len__(self) -> int:
        return len(self._buffer)

    def to_bytes(self) -> bytes:
        return bytes(self._buffer)

    def write(self, data: bytes) -> "BufferWriter":
        self._buffer += data
        return self

    # ------------------------------------------------------------- 整数/浮点
    def write_u8(self, value: int) -> "BufferWriter":
        self._buffer.append(value & 0xFF)
        return self

    write_byte = write_u8

    def write_i8(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">b", value))

    def write_u16(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">H", value & 0xFFFF))

    def write_i16(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">h", value))

    def write_u32(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">I", value & 0xFFFFFFFF))

    def write_i32(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">i", value))

    def write_u64(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF))

    def write_i64(self, value: int) -> "BufferWriter":
        return self.write(struct.pack(">q", value))

    def write_f32(self, value: float) -> "BufferWriter":
        return self.write(struct.pack(">f", value))

    def write_f64(self, value: float) -> "BufferWriter":
        return self.write(struct.pack(">d", value))

    def write_bool(self, value: bool) -> "BufferWriter":
        return self.write_u8(1 if value else 0)

    def write_varint(self, value: int) -> "BufferWriter":
        value &= 0xFFFFFFFF
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                self._buffer.append(byte | 0x80)
            else:
                self._buffer.append(byte)
                return self

    def write_varlong(self, value: int) -> "BufferWriter":
        value &= 0xFFFFFFFFFFFFFFFF
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                self._buffer.append(byte | 0x80)
            else:
                self._buffer.append(byte)
                return self

    # ------------------------------------------------------------------ 复杂
    def write_string(self, value: str) -> "BufferWriter":
        encoded = value.encode("utf-8")
        self.write_varint(len(encoded))
        return self.write(encoded)

    def write_uuid(self, value: uuid.UUID | str) -> "BufferWriter":
        if isinstance(value, str):
            value = uuid.UUID(value)
        return self.write(value.bytes)

    def write_position(self, pos: BlockPos | tuple[int, int, int]) -> "BufferWriter":
        if not isinstance(pos, BlockPos):
            pos = BlockPos(*pos)
        value = ((pos.x & 0x3FFFFFF) << 38) | ((pos.z & 0x3FFFFFF) << 12) | (pos.y & 0xFFF)
        return self.write_u64(value)

    def write_angle(self, degrees: float) -> "BufferWriter":
        return self.write_u8(int(round(degrees * 256.0 / 360.0)) & 0xFF)

    def write_nbt(self, tag: Any) -> "BufferWriter":
        from .nbt import write_nbt

        return self.write(write_nbt(tag))

"""NBT 标签的二进制读写（Java 版网络 NBT）。

网络 NBT 以类型字节开头（0 表示没有数据），随后是 u16 名称长度、UTF-8 名称和
负载；所有多字节数值均为大端序。读取时使用 ``NbtByte`` / ``NbtShort`` /
``NbtLong`` / ``NbtFloat`` 等标记子类保留原始标签类型，写回时不会把 byte 变成
int。``NbtIntArray`` / ``NbtLongArray`` / ``NbtByteArray`` 用来显式指定数组标签。
"""

from __future__ import annotations

import struct
from typing import Any

from .buffer import BufferReader, ProtocolError

__all__ = [
    "NbtError",
    "NbtByte",
    "NbtShort",
    "NbtLong",
    "NbtFloat",
    "NbtByteArray",
    "NbtIntArray",
    "NbtLongArray",
    "read_nbt",
    "parse_nbt",
    "write_nbt",
    "dump_nbt",
    "to_python",
    "TAG_END",
    "TAG_BYTE",
    "TAG_SHORT",
    "TAG_INT",
    "TAG_LONG",
    "TAG_FLOAT",
    "TAG_DOUBLE",
    "TAG_BYTE_ARRAY",
    "TAG_STRING",
    "TAG_LIST",
    "TAG_COMPOUND",
    "TAG_INT_ARRAY",
    "TAG_LONG_ARRAY",
]

#: NBT 标签编号（与 Java 版一致）
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_FLOAT = 5
TAG_DOUBLE = 6
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11
TAG_LONG_ARRAY = 12

_TAG_NAMES = {
    TAG_END: "End",
    TAG_BYTE: "Byte",
    TAG_SHORT: "Short",
    TAG_INT: "Int",
    TAG_LONG: "Long",
    TAG_FLOAT: "Float",
    TAG_DOUBLE: "Double",
    TAG_BYTE_ARRAY: "Byte_Array",
    TAG_STRING: "String",
    TAG_LIST: "List",
    TAG_COMPOUND: "Compound",
    TAG_INT_ARRAY: "Int_Array",
    TAG_LONG_ARRAY: "Long_Array",
}

#: 每种标签负载至少占用的字节数，用于粗筛不可能的集合长度
_MIN_PAYLOAD_SIZE = (0, 1, 2, 4, 8, 4, 8, 4, 2, 5, 1, 4, 4)
_MAX_DEPTH = 256
_MAX_COLLECTION = 1 << 24

_U16 = struct.Struct(">H")
_I16 = struct.Struct(">h")
_I32 = struct.Struct(">i")
_I64 = struct.Struct(">q")
_F32 = struct.Struct(">f")
_F64 = struct.Struct(">d")


class NbtError(ValueError):
    """NBT 数据非法或长度不足。"""


class NbtByte(int):
    """TAG_Byte 标记：普通 ``int`` 会被写成 TAG_Int，用它可保持字节类型。"""

    __slots__ = ()


class NbtShort(int):
    """TAG_Short 标记。"""

    __slots__ = ()


class NbtLong(int):
    """TAG_Long 标记：写回时输出 8 字节整数而不是 4 字节。"""

    __slots__ = ()


class NbtFloat(float):
    """TAG_Float 标记：普通 ``float`` 默认写成 TAG_Double。"""

    __slots__ = ()


class NbtByteArray(bytes):
    """TAG_Byte_Array 标记。"""

    __slots__ = ()


class NbtIntArray(list):
    """TAG_Int_Array 标记（32 位整数列表）。"""

    __slots__ = ()


class NbtLongArray(list):
    """TAG_Long_Array 标记（64 位整数列表，例如区块高度图）。"""

    __slots__ = ()


class _NbtList(list):
    """从 TAG_List 读出的列表标记。

    纯 int 列表按规格会被推断成 TAG_Int_Array，因此需要这个私有子类记住
    “它来自 TAG_List”，写回时才能保持列表标签不变。
    """

    __slots__ = ()


class _Input:
    """包装任意 ``read(n) -> bytes`` 读取器，统一做长度校验。"""

    __slots__ = ("_read", "_remaining")

    def __init__(self, reader: Any) -> None:
        try:
            self._read = reader.read
        except AttributeError as exc:  # pragma: no cover - 调用方错误
            raise NbtError("reader 必须提供 read(n) 方法") from exc
        remaining = getattr(reader, "remaining", None)
        self._remaining = remaining if isinstance(remaining, int) and remaining >= 0 else None

    @property
    def remaining(self) -> int | None:
        return self._remaining

    def read(self, count: int) -> bytes:
        if count < 0:
            raise NbtError(f"NBT 读取长度为负数: {count}")
        if self._remaining is not None and count > self._remaining:
            raise NbtError(f"NBT 数据不足: 需要 {count} 字节，仅剩 {self._remaining} 字节")
        try:
            data = self._read(count)
        except ProtocolError as exc:
            raise NbtError(f"NBT 数据被截断: {exc}") from exc
        if len(data) != count:
            raise NbtError(f"NBT 数据被截断: 需要 {count} 字节，实际 {len(data)} 字节")
        if self._remaining is not None:
            self._remaining -= count
        return data


# ---------------------------------------------------------------------- 读取


def _read_u8(inp: _Input) -> int:
    return inp.read(1)[0]


def _read_u16(inp: _Input) -> int:
    return _U16.unpack(inp.read(2))[0]


def _read_i32(inp: _Input) -> int:
    return _I32.unpack(inp.read(4))[0]


def _check_length(inp: _Input, length: int, min_size: int, what: str) -> None:
    """校验集合长度非负、不过大，且不会超出剩余字节。"""
    if length < 0:
        raise NbtError(f"{what}长度为负数: {length}")
    if length > _MAX_COLLECTION:
        raise NbtError(f"{what}长度过大: {length}")
    remaining = inp.remaining
    if remaining is not None and length * min_size > remaining:
        raise NbtError(f"{what}长度 {length} 超出剩余数据（仅剩 {remaining} 字节）")


def _read_named_string(inp: _Input) -> str:
    """读取 u16 长度 + UTF-8 字符串（TAG_String 负载与标签名称共用）。"""
    length = _read_u16(inp)
    if length == 0:
        return ""
    try:
        return inp.read(length).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NbtError(f"字符串不是合法 UTF-8: {exc}") from exc


def _read_payload(inp: _Input, tag_type: int, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise NbtError("NBT 嵌套层级过深")

    if tag_type == TAG_BYTE:
        return NbtByte(struct.unpack(">b", inp.read(1))[0])
    if tag_type == TAG_SHORT:
        return NbtShort(_I16.unpack(inp.read(2))[0])
    if tag_type == TAG_INT:
        return _I32.unpack(inp.read(4))[0]
    if tag_type == TAG_LONG:
        return NbtLong(_I64.unpack(inp.read(8))[0])
    if tag_type == TAG_FLOAT:
        return NbtFloat(_F32.unpack(inp.read(4))[0])
    if tag_type == TAG_DOUBLE:
        return _F64.unpack(inp.read(8))[0]
    if tag_type == TAG_BYTE_ARRAY:
        length = _read_i32(inp)
        _check_length(inp, length, 1, "字节数组")
        return inp.read(length)
    if tag_type == TAG_STRING:
        return _read_named_string(inp)
    if tag_type == TAG_LIST:
        element_type = _read_u8(inp)
        length = _read_i32(inp)
        if element_type > TAG_LONG_ARRAY:
            raise NbtError(f"列表元素类型非法: {element_type}")
        if element_type == TAG_END and length != 0:
            raise NbtError(f"空元素类型的列表长度应为 0，实际 {length}")
        _check_length(inp, length, _MIN_PAYLOAD_SIZE[element_type], "列表")
        items = [_read_payload(inp, element_type, depth + 1) for _ in range(length)]
        return _NbtList(items)
    if tag_type == TAG_COMPOUND:
        result: dict[str, Any] = {}
        while True:
            child_type = _read_u8(inp)
            if child_type == TAG_END:
                return result
            if child_type > TAG_LONG_ARRAY:
                raise NbtError(f"compound 内的标签类型非法: {child_type}")
            key = _read_named_string(inp)
            result[key] = _read_payload(inp, child_type, depth + 1)
    if tag_type == TAG_INT_ARRAY:
        length = _read_i32(inp)
        _check_length(inp, length, 4, "整数数组")
        return list(struct.unpack(f">{length}i", inp.read(length * 4)))
    if tag_type == TAG_LONG_ARRAY:
        length = _read_i32(inp)
        _check_length(inp, length, 8, "长整数数组")
        return NbtLongArray(struct.unpack(f">{length}q", inp.read(length * 8)))

    raise NbtError(f"未知的 NBT 标签类型: {tag_type}")


def read_nbt(reader: Any) -> Any | None:
    """读取网络 NBT（命名根标签），根标签类型为 0 时返回 ``None``。

    ``reader`` 只需提供 ``read(n) -> bytes``；若还提供 ``remaining`` 属性，
    则能提前拒绝不可能的长度。
    """
    inp = _Input(reader)
    tag_type = _read_u8(inp)
    if tag_type == TAG_END:
        return None
    if tag_type > TAG_LONG_ARRAY:
        raise NbtError(f"未知的 NBT 根标签类型: {tag_type}")
    _read_named_string(inp)  # 根标签名称，内容对调用方无用
    return _read_payload(inp, tag_type, 0)


def parse_nbt(data: bytes) -> Any | None:
    """从完整字节串中解析网络 NBT。"""
    if isinstance(data, memoryview):
        data = data.tobytes()
    elif isinstance(data, bytearray):
        data = bytes(data)
    elif not isinstance(data, bytes):
        raise TypeError("parse_nbt 需要 bytes 类型")
    return read_nbt(BufferReader(data))


# ---------------------------------------------------------------------- 写入


def _tag_of(value: Any) -> int:
    """按 Python 类型推断应使用的 NBT 标签。"""
    if isinstance(value, dict):
        return TAG_COMPOUND
    if isinstance(value, bool):
        return TAG_BYTE
    if isinstance(value, NbtByte):
        return TAG_BYTE
    if isinstance(value, NbtShort):
        return TAG_SHORT
    if isinstance(value, NbtLong):
        return TAG_LONG
    if isinstance(value, int):
        return TAG_INT
    if isinstance(value, NbtFloat):
        return TAG_FLOAT
    if isinstance(value, float):
        return TAG_DOUBLE
    if isinstance(value, str):
        return TAG_STRING
    if isinstance(value, (bytes, bytearray, memoryview)):
        return TAG_BYTE_ARRAY
    if isinstance(value, NbtLongArray):
        return TAG_LONG_ARRAY
    if isinstance(value, NbtIntArray):
        return TAG_INT_ARRAY
    if isinstance(value, _NbtList):
        return TAG_LIST
    if isinstance(value, (list, tuple)):
        # 规格要求：普通 int 列表写成整数数组；从 TAG_List 读出的列表由
        # _NbtList 标记，仍写回列表。
        return TAG_INT_ARRAY if _looks_like_int_list(value) else TAG_LIST
    raise NbtError(f"无法写入 NBT 的值类型: {type(value).__name__}")


def _looks_like_int_list(value: list[Any] | tuple[Any, ...]) -> bool:
    """判断是否为纯 Python int 列表（排除 bool 及其他 int 子类）。"""
    if not value:
        return False
    for item in value:
        if type(item) is not int:
            return False
    return True


def _pack(out: bytearray, fmt: str, *values: Any) -> None:
    try:
        out += struct.pack(fmt, *values)
    except struct.error as exc:
        raise NbtError(f"数值超出 NBT 范围（{fmt}）: {exc}") from exc


def _write_name(out: bytearray, name: str) -> None:
    if not isinstance(name, str):
        raise NbtError(f"标签名称必须是 str，实际 {type(name).__name__}")
    raw = name.encode("utf-8")
    if len(raw) > 0xFFFF:
        raise NbtError(f"标签名称过长: {len(raw)} 字节")
    out += _U16.pack(len(raw))
    out += raw


def _write_payload(out: bytearray, value: Any, tag_type: int, depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise NbtError("NBT 嵌套层级过深")

    if tag_type == TAG_BYTE:
        _pack(out, ">b", int(value))
    elif tag_type == TAG_SHORT:
        _pack(out, ">h", int(value))
    elif tag_type == TAG_INT:
        _pack(out, ">i", int(value))
    elif tag_type == TAG_LONG:
        _pack(out, ">q", int(value))
    elif tag_type == TAG_FLOAT:
        _pack(out, ">f", float(value))
    elif tag_type == TAG_DOUBLE:
        _pack(out, ">d", float(value))
    elif tag_type == TAG_BYTE_ARRAY:
        raw = bytes(value)
        if len(raw) > 0x7FFFFFFF:
            raise NbtError(f"字节数组过长: {len(raw)}")
        out += _I32.pack(len(raw))
        out += raw
    elif tag_type == TAG_STRING:
        raw = str(value).encode("utf-8")
        if len(raw) > 0xFFFF:
            raise NbtError(f"字符串过长: {len(raw)} 字节")
        out += _U16.pack(len(raw))
        out += raw
    elif tag_type == TAG_LIST:
        items = list(value)
        if not items:
            out.append(TAG_END)
            out += _I32.pack(0)
            return
        element_type = _tag_of(items[0])
        out.append(element_type)
        out += _I32.pack(len(items))
        for item in items:
            if _tag_of(item) != element_type:
                raise NbtError("列表元素类型不一致")
            _write_payload(out, item, element_type, depth + 1)
    elif tag_type == TAG_COMPOUND:
        if not isinstance(value, dict):
            raise NbtError(f"compound 需要 dict，实际 {type(value).__name__}")
        for key, item in value.items():
            if not isinstance(key, str):
                raise NbtError(f"compound 的键必须是 str，实际 {type(key).__name__}")
            if item is None:
                raise NbtError(f"compound 键 {key!r} 的值为 None，NBT 不支持空值")
            child_type = _tag_of(item)
            out.append(child_type)
            _write_name(out, key)
            _write_payload(out, item, child_type, depth + 1)
        out.append(TAG_END)
    elif tag_type == TAG_INT_ARRAY:
        values = [int(item) for item in value]
        out += _I32.pack(len(values))
        if values:
            _pack(out, f">{len(values)}i", *values)
    elif tag_type == TAG_LONG_ARRAY:
        values = [int(item) for item in value]
        out += _I32.pack(len(values))
        if values:
            _pack(out, f">{len(values)}q", *values)
    else:  # pragma: no cover - _tag_of 不会返回其他值
        raise NbtError(f"无法写入的 NBT 标签类型: {tag_type}")


def write_nbt(value: Any, name: str = "") -> bytes:
    """把 Python 值写成网络 NBT（根标签名称默认为空串）。"""
    if value is None:
        return b"\x00"
    tag_type = _tag_of(value)
    out = bytearray()
    out.append(tag_type)
    _write_name(out, name)
    _write_payload(out, value, tag_type, 0)
    return bytes(out)


def dump_nbt(value: Any) -> bytes:
    """同 :func:`write_nbt`，根标签名称固定为空串。"""
    return write_nbt(value, "")


# ---------------------------------------------------------------------- 辅助


def to_python(value: Any) -> Any:
    """把标记类型转回普通 ``int`` / ``float`` / ``list`` / ``bytes``。"""
    if isinstance(value, dict):
        return {key: to_python(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_python(item) for item in value]
    if isinstance(value, NbtByteArray):
        return bytes(value)
    if isinstance(value, (NbtByte, NbtShort, NbtLong)):
        return int(value)
    if isinstance(value, NbtFloat):
        return float(value)
    return value

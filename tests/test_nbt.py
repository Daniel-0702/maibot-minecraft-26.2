"""mcbot.nbt 的单元测试：覆盖全部标签类型、类型保持与异常路径。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import io
import struct
import unittest

from mcbot.nbt import (
    TAG_BYTE,
    TAG_BYTE_ARRAY,
    TAG_COMPOUND,
    TAG_DOUBLE,
    TAG_FLOAT,
    TAG_INT,
    TAG_INT_ARRAY,
    TAG_LIST,
    TAG_LONG,
    TAG_LONG_ARRAY,
    TAG_SHORT,
    TAG_STRING,
    NbtByte,
    NbtByteArray,
    NbtError,
    NbtFloat,
    NbtIntArray,
    NbtLong,
    NbtLongArray,
    NbtShort,
    dump_nbt,
    parse_nbt,
    read_nbt,
    to_python,
    write_nbt,
)


def sample_compound():
    """构造包含所有标签类型（含嵌套）的 compound。"""
    return {
        "byte": NbtByte(-5),
        "short": NbtShort(-300),
        "int": -70000,
        "long": NbtLong(1 << 40),
        "float": NbtFloat(1.25),
        "double": -2.5,
        "string": "你好，Minecraft 🌍",
        "byte_array": b"\x00\x01\x7f\xff",
        "int_array": NbtIntArray([1, -2, 3, 2**31 - 1]),
        "long_array": NbtLongArray([1, -2, 1 << 40]),
        "long_array_37": NbtLongArray([i * 1234567 - (1 << 40) for i in range(37)]),
        "list_byte": [NbtByte(1), NbtByte(2)],
        "list_short": [NbtShort(1), NbtShort(2)],
        "list_long": [NbtLong(1), NbtLong(2)],
        "list_float": [NbtFloat(0.5), NbtFloat(1.5)],
        "list_double": [0.5, 1.5],
        "list_string": ["a", "bb"],
        "list_compound": [{"name": "a", "value": 1}, {"name": "b", "value": 2}],
        "empty_list": [],
        "nested": {"inner": {"deep": b"\x01"}},
    }


class NbtReadWriteTest(unittest.TestCase):
    def test_all_tag_types_round_trip(self):
        sample = sample_compound()
        blob = write_nbt(sample, "root")
        parsed = parse_nbt(blob)

        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed, sample)
        # 写回必须逐字节一致，说明标签类型没有变化（根名称由调用方提供）
        self.assertEqual(write_nbt(parsed, "root"), blob)

    def test_reader_returns_marker_types(self):
        parsed = parse_nbt(write_nbt(sample_compound()))

        self.assertIsInstance(parsed["byte"], NbtByte)
        self.assertIsInstance(parsed["short"], NbtShort)
        self.assertIs(type(parsed["int"]), int)
        self.assertIsInstance(parsed["long"], NbtLong)
        self.assertIsInstance(parsed["float"], NbtFloat)
        self.assertIs(type(parsed["double"]), float)
        self.assertNotIsInstance(parsed["double"], NbtFloat)
        self.assertIs(type(parsed["string"]), str)
        self.assertIs(type(parsed["byte_array"]), bytes)
        self.assertNotIsInstance(parsed["byte_array"], NbtByteArray)
        self.assertIs(type(parsed["int_array"]), list)
        self.assertIsInstance(parsed["long_array"], NbtLongArray)
        self.assertIsInstance(parsed["list_compound"], list)
        self.assertIsInstance(parsed["list_compound"][0], dict)
        self.assertIsInstance(parsed["nested"], dict)
        self.assertEqual(parsed["nested"]["inner"]["deep"], b"\x01")

    def test_long_array_37_round_trip(self):
        values = [i * 1_000_003 - (1 << 40) for i in range(37)]
        blob = dump_nbt(NbtLongArray(values))
        parsed = parse_nbt(blob)

        self.assertIsInstance(parsed, NbtLongArray)
        self.assertEqual(len(parsed), 37)
        self.assertEqual(list(parsed), values)
        self.assertEqual(dump_nbt(parsed), blob)

    def test_list_of_compounds(self):
        value = [{"id": i, "tags": ["x", "y"]} for i in range(4)]
        blob = dump_nbt(value)
        parsed = parse_nbt(blob)

        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed, value)
        self.assertEqual(dump_nbt(parsed), blob)

    def test_non_ascii_string_and_name(self):
        value = {"键": "值", "emoji": "🌍🌎🌏", "中文": "麦麦"}
        blob = write_nbt(value, "根标签")
        parsed = parse_nbt(blob)

        self.assertEqual(parsed, value)
        # 根名称是 UTF-8 + u16 长度
        self.assertEqual(blob[1:3], b"\x00\x09")
        self.assertEqual(blob[3:12], "根标签".encode("utf-8"))

    def test_empty_root(self):
        self.assertIsNone(parse_nbt(b"\x00"))
        self.assertEqual(write_nbt(None), b"\x00")
        self.assertEqual(dump_nbt(None), b"\x00")

    def test_list_tag_of_ints_round_trip(self):
        # 手工构造 TAG_List<TAG_Int>；纯 int 列表默认被推断为整数数组，
        # 但读出来的列表会保留列表标签。
        raw = (
            bytes((TAG_LIST,))
            + b"\x00\x00"
            + bytes((TAG_INT,))
            + struct.pack(">i", 3)
            + struct.pack(">iii", 7, 8, 9)
        )
        parsed = parse_nbt(raw)

        self.assertEqual(parsed, [7, 8, 9])
        self.assertEqual(dump_nbt(parsed), raw)

    def test_minimal_reader_without_remaining(self):
        blob = dump_nbt({"a": [1, 2], "b": "x"})
        self.assertEqual(read_nbt(io.BytesIO(blob)), {"a": [1, 2], "b": "x"})

        with self.assertRaises(NbtError):
            read_nbt(io.BytesIO(blob[:-1]))

    def test_buffer_reader_integration(self):
        from mcbot.buffer import BufferReader, BufferWriter

        blob = write_nbt({"x": [1, 2]}, "root")
        reader = BufferReader(blob + b"\xff")
        self.assertEqual(reader.read_nbt(), {"x": [1, 2]})
        self.assertEqual(reader.remaining, 1)  # 只消费 NBT 本身

        writer = BufferWriter()
        writer.write_nbt({"a": 1})
        self.assertEqual(parse_nbt(writer.to_bytes()), {"a": 1})

        self.assertRaises(NbtError, BufferReader(blob[:-1]).read_nbt)

    def test_type_inference(self):
        self.assertEqual(dump_nbt({"a": 1})[0], TAG_COMPOUND)
        self.assertEqual(dump_nbt(True)[0], TAG_BYTE)
        self.assertEqual(dump_nbt(NbtByte(1))[0], TAG_BYTE)
        self.assertEqual(dump_nbt(NbtShort(1))[0], TAG_SHORT)
        self.assertEqual(dump_nbt(1)[0], TAG_INT)
        self.assertEqual(dump_nbt(NbtLong(1))[0], TAG_LONG)
        self.assertEqual(dump_nbt(NbtFloat(1.5))[0], TAG_FLOAT)
        self.assertEqual(dump_nbt(1.5)[0], TAG_DOUBLE)
        self.assertEqual(dump_nbt("x")[0], TAG_STRING)
        self.assertEqual(dump_nbt(b"x")[0], TAG_BYTE_ARRAY)
        self.assertEqual(dump_nbt(NbtByteArray(b"x"))[0], TAG_BYTE_ARRAY)
        self.assertEqual(dump_nbt(NbtIntArray([1, 2]))[0], TAG_INT_ARRAY)
        self.assertEqual(dump_nbt([1, 2, 3])[0], TAG_INT_ARRAY)
        self.assertEqual(dump_nbt(NbtLongArray([1, 2]))[0], TAG_LONG_ARRAY)
        self.assertEqual(dump_nbt([])[0], TAG_LIST)
        self.assertEqual(dump_nbt([NbtByte(1)])[0], TAG_LIST)
        self.assertEqual(dump_nbt([1.5])[0], TAG_LIST)

    def test_bool_round_trip_as_byte(self):
        blob = dump_nbt({"flag": True})
        parsed = parse_nbt(blob)
        self.assertEqual(parsed["flag"], 1)
        self.assertIsInstance(parsed["flag"], NbtByte)
        self.assertEqual(dump_nbt(parsed), blob)

    def test_to_python(self):
        parsed = parse_nbt(write_nbt(sample_compound()))
        plain = to_python(parsed)

        self.assertIs(type(plain), dict)
        self.assertIs(type(plain["byte"]), int)
        self.assertIs(type(plain["short"]), int)
        self.assertIs(type(plain["long"]), int)
        self.assertIs(type(plain["float"]), float)
        self.assertIs(type(plain["double"]), float)
        self.assertIs(type(plain["byte_array"]), bytes)
        self.assertIs(type(plain["int_array"]), list)
        self.assertIs(type(plain["long_array"]), list)
        self.assertIs(type(plain["list_compound"]), list)
        self.assertIs(type(plain["list_compound"][0]), dict)
        self.assertIs(type(plain["nested"]), dict)
        self.assertEqual(plain["long_array"], [1, -2, 1 << 40])
        self.assertEqual(to_python(NbtByteArray(b"\x01\x02")), b"\x01\x02")


class NbtErrorTest(unittest.TestCase):
    def test_truncated_payload(self):
        blob = write_nbt(sample_compound())
        for cut in range(1, len(blob)):
            with self.subTest(cut=cut):
                with self.assertRaises(NbtError):
                    parse_nbt(blob[:cut])

    def test_truncated_at_header(self):
        self.assertRaises(NbtError, parse_nbt, b"")
        self.assertRaises(NbtError, parse_nbt, b"\x08")
        self.assertRaises(NbtError, parse_nbt, b"\x0a\x00\x05ab")

    def test_unknown_tag(self):
        self.assertRaises(NbtError, parse_nbt, b"\x0d\x00\x00")

    def test_impossible_lengths(self):
        # 字节数组声明 1000 字节，实际只有 2 字节
        data = bytes((TAG_BYTE_ARRAY,)) + b"\x00\x00" + struct.pack(">i", 1000) + b"\x00\x00"
        self.assertRaises(NbtError, parse_nbt, data)

        # 列表长度为负
        data = bytes((TAG_LIST,)) + b"\x00\x00" + bytes((TAG_INT,)) + struct.pack(">i", -1)
        self.assertRaises(NbtError, parse_nbt, data)

        # 列表声称 5 个 int，但没有任何负载
        data = bytes((TAG_LIST,)) + b"\x00\x00" + bytes((TAG_INT,)) + struct.pack(">i", 5)
        self.assertRaises(NbtError, parse_nbt, data)

        # 元素类型为 End 却声明非零长度
        data = bytes((TAG_LIST,)) + b"\x00\x00" + b"\x00" + struct.pack(">i", 2)
        self.assertRaises(NbtError, parse_nbt, data)

        # 整数数组长度为负
        data = bytes((TAG_INT_ARRAY,)) + b"\x00\x00" + struct.pack(">i", -3)
        self.assertRaises(NbtError, parse_nbt, data)

    def test_string_length_beyond_data(self):
        data = bytes((TAG_STRING,)) + b"\x00\x00" + struct.pack(">H", 10) + b"abc"
        self.assertRaises(NbtError, parse_nbt, data)

    def test_write_rejects_unsupported_values(self):
        self.assertRaises(NbtError, write_nbt, object())
        self.assertRaises(NbtError, write_nbt, {"a": None})
        # 列表元素类型不一致
        self.assertRaises(NbtError, write_nbt, [NbtByte(1), NbtShort(2)])
        # 超出 TAG_Byte 范围
        self.assertRaises(NbtError, write_nbt, NbtByte(1000))


if __name__ == "__main__":
    unittest.main(verbosity=2)

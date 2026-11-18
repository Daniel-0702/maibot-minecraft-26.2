"""核心协议 / 几何 / 注册表 / 世界模型的单元测试（不依赖网络）。"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcbot import packets
from mcbot.buffer import BufferReader, BufferWriter, ProtocolError
from mcbot.crypto import _PurePythonAES, make_cipher
from mcbot.registry import registry
from mcbot.types import AABB, BlockPos, Vec3, look_at, look_direction, wrap_degrees
from mcbot.world import SimpleWorld


class BufferTests(unittest.TestCase):
    def test_varint_roundtrip(self) -> None:
        for value in (0, 1, 2, 127, 128, 255, 300, 16384, -1, -2147483648, 2147483647):
            data = BufferWriter().write_varint(value).to_bytes()
            self.assertEqual(BufferReader(data).read_varint(), value, value)

    def test_varlong_roundtrip(self) -> None:
        for value in (0, 1, 127, 128, 2**31, -1, -(2**63), 2**63 - 1):
            data = BufferWriter().write_varlong(value).to_bytes()
            self.assertEqual(BufferReader(data).read_varlong(), value, value)

    def test_varint_negative_is_five_bytes(self) -> None:
        self.assertEqual(len(BufferWriter().write_varint(-1).to_bytes()), 5)

    def test_string_unicode(self) -> None:
        text = "麦麦去挖矿了 ⛏"
        data = BufferWriter().write_string(text).to_bytes()
        self.assertEqual(BufferReader(data).read_string(), text)

    def test_position_packing(self) -> None:
        for pos in (BlockPos(0, 0, 0), BlockPos(1, 2, 3), BlockPos(-100, 64, 300), BlockPos(-33554432, -64, 33554431)):
            data = BufferWriter().write_position(pos).to_bytes()
            self.assertEqual(len(data), 8)
            self.assertEqual(BufferReader(data).read_position(), pos)

    def test_uuid(self) -> None:
        value = uuid.uuid4()
        data = BufferWriter().write_uuid(value).to_bytes()
        self.assertEqual(BufferReader(data).read_uuid(), value)

    def test_bool_and_floats(self) -> None:
        data = BufferWriter().write_bool(True).write_bool(False).write_f32(1.5).write_f64(-2.25).to_bytes()
        reader = BufferReader(data)
        self.assertTrue(reader.read_bool())
        self.assertFalse(reader.read_bool())
        self.assertAlmostEqual(reader.read_f32(), 1.5)
        self.assertAlmostEqual(reader.read_f64(), -2.25)

    def test_angle(self) -> None:
        data = BufferWriter().write_angle(90.0).to_bytes()
        self.assertAlmostEqual(BufferReader(data).read_angle(), 90.0, delta=2.0)

    def test_truncated_raises(self) -> None:
        with self.assertRaises(ProtocolError):
            BufferReader(b"\x80").read_varint()
        with self.assertRaises(ProtocolError):
            BufferReader(b"\x05ab").read_string()

    def test_reader_position_and_rest(self) -> None:
        reader = BufferReader(b"abcdef")
        self.assertEqual(reader.read(2), b"ab")
        self.assertEqual(reader.position, 2)
        self.assertEqual(reader.read_rest(), b"cdef")
        self.assertEqual(reader.remaining, 0)


class GeometryTests(unittest.TestCase):
    def test_look_direction_cardinal(self) -> None:
        self.assertEqual(look_direction(0, 0).as_tuple(), (0.0, -0.0, 1.0))
        east = look_direction(-90, 0)
        self.assertAlmostEqual(east.x, 1.0, places=6)
        self.assertAlmostEqual(east.z, 0.0, places=6)

    def test_look_at(self) -> None:
        rotation = look_at(Vec3(0, 0, 0), Vec3(0, 0, 5))
        self.assertAlmostEqual(rotation.yaw, 0.0, places=4)
        self.assertAlmostEqual(rotation.pitch, 0.0, places=4)
        rotation = look_at(Vec3(0, 0, 0), Vec3(5, 0, 0))
        self.assertAlmostEqual(rotation.yaw, -90.0, places=4)
        rotation = look_at(Vec3(0, 10, 0), Vec3(0, 0, 0))
        self.assertAlmostEqual(rotation.pitch, 90.0, places=4)

    def test_wrap_degrees(self) -> None:
        self.assertEqual(wrap_degrees(190), -170)
        self.assertEqual(wrap_degrees(-190), 170)
        self.assertEqual(wrap_degrees(180), -180)

    def test_aabb(self) -> None:
        box = AABB.from_center(Vec3(0, 0, 0), 0.6, 1.8)
        self.assertTrue(box.contains(Vec3(0, 0.5, 0)))
        self.assertFalse(box.contains(Vec3(1, 0.5, 0)))
        self.assertTrue(box.expand(0.5, 0.5, 0.5).contains(Vec3(0.7, 0.5, 0)))


class CryptoTests(unittest.TestCase):
    def test_aes_fips197_vector(self) -> None:
        aes = _PurePythonAES(bytes.fromhex("000102030405060708090a0b0c0d0e0f"))
        cipher = aes.encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"))
        self.assertEqual(cipher.hex(), "69c4e0d86a7b0430d8cdb78070b4c55a")

    def test_cfb8_roundtrip(self) -> None:
        key = b"0123456789abcdef"
        plaintext = b"minecraft protocol encryption test" * 8
        ciphertext = make_cipher(key).encrypt(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(make_cipher(key).decrypt(ciphertext), plaintext)

    def test_cfb8_is_streaming(self) -> None:
        key = b"abcdefghijklmnop"
        plaintext = b"A" * 100
        whole = make_cipher(key).encrypt(plaintext)
        part = make_cipher(key)
        split = part.encrypt(plaintext[:30]) + part.encrypt(plaintext[30:])
        self.assertEqual(whole, split)


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reg = registry()

    def test_version(self) -> None:
        self.assertEqual(self.reg.minecraft_version, "1.20.1")
        self.assertEqual(self.reg.protocol_version, packets.PROTOCOL_VERSION)
        self.assertEqual(self.reg.state_id("air"), 0)
        self.assertEqual(self.reg.state_id("stone"), 1)

    def test_block_properties(self) -> None:
        stone = self.reg.block(self.reg.state_id("stone"))
        self.assertEqual(stone.name, "minecraft:stone")
        self.assertAlmostEqual(stone.hardness, 1.5)
        self.assertEqual(stone.tool_class, "pickaxe")
        self.assertEqual(stone.min_tool_level, 0)
        self.assertTrue(self.reg.is_solid(stone.state_id))
        lava = self.reg.block(self.reg.state_id("lava"))
        self.assertTrue(lava.liquid)
        self.assertTrue(self.reg.is_dangerous(lava.state_id))
        self.assertEqual(self.reg.boxes(lava.state_id), ())

    def test_partial_block_boxes(self) -> None:
        slab = self.reg.state_id("oak_slab")
        boxes = self.reg.boxes(slab)
        self.assertTrue(boxes)
        self.assertLess(max(box.max_y for box in boxes), 1.0)
        self.assertTrue(self.reg.is_solid(slab))

    def test_items_and_food(self) -> None:
        pickaxe = self.reg.item(self.reg.item_id("diamond_pickaxe"))
        self.assertIsNotNone(pickaxe)
        self.assertEqual(pickaxe.tool_class, "pickaxe")
        self.assertEqual(pickaxe.tool_level, 3)
        apple = self.reg.item_id("apple")
        self.assertIsNotNone(self.reg.food(apple))

    def test_entities(self) -> None:
        self.assertEqual(self.reg.entity_name(0), "minecraft:allay")

    def test_loot(self) -> None:
        drops = self.reg.expected_drops("minecraft:stone")
        self.assertIn("minecraft:cobblestone", drops)


class WorldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = SimpleWorld()
        self.stone = registry().state_id("stone")
        self.world.fill_box(BlockPos(-2, 0, -2), BlockPos(2, 0, 2), self.stone)

    def test_get_set(self) -> None:
        self.assertEqual(self.world.get_state(0, 0, 0), self.stone)
        self.assertEqual(self.world.get_state(0, 1, 0), 0)
        self.world.set_state(0, 1, 0, self.stone)
        self.assertEqual(self.world.get_state(0, 1, 0), self.stone)

    def test_collision_boxes(self) -> None:
        boxes = self.world.collision_boxes(AABB(0.2, 0.2, 0.2, 0.8, 0.8, 0.8))
        self.assertEqual(len(boxes), 1)
        self.assertTrue(self.world.is_solid_at(0, 0, 0))
        self.assertFalse(self.world.is_solid_at(0, 1, 0))

    def test_highest_solid(self) -> None:
        self.assertEqual(self.world.highest_solid_y(0, 0), 0)
        self.assertEqual(self.world.surface_y(0, 0), 1.0)
        self.assertIsNone(self.world.highest_solid_y(10, 10))

    def test_find_blocks(self) -> None:
        found = self.world.find_blocks({"stone"}, BlockPos(0, 0, 0), radius=3)
        self.assertEqual(len(found), 25)

    def test_safe_standing(self) -> None:
        self.assertTrue(self.world.is_safe_standing(BlockPos(0, 1, 0)))
        self.assertFalse(self.world.is_safe_standing(BlockPos(0, 0, 0)))


class PacketTests(unittest.TestCase):
    def test_handshake(self) -> None:
        reader = BufferReader(packets.handshake("mc.example.com", 25565, 2))
        self.assertEqual(reader.read_varint(), packets.PROTOCOL_VERSION)
        self.assertEqual(reader.read_string(), "mc.example.com")
        self.assertEqual(reader.read_u16(), 25565)
        self.assertEqual(reader.read_varint(), 2)

    def test_login_start(self) -> None:
        player_uuid = uuid.UUID(int=1)
        reader = BufferReader(packets.login_start("MaiBot", player_uuid))
        self.assertEqual(reader.read_string(), "MaiBot")
        self.assertTrue(reader.read_bool())  # option<UUID> 的存在标记
        self.assertEqual(reader.read_uuid(), player_uuid)

    def test_chat_message(self) -> None:
        reader = BufferReader(packets.chat_message("你好", 1700000000000, 0))
        self.assertEqual(reader.read_string(256), "你好")
        self.assertEqual(reader.read_i64(), 1700000000000)
        self.assertEqual(reader.read_i64(), 0)
        self.assertFalse(reader.read_bool())
        self.assertEqual(reader.read_varint(), 0)
        self.assertEqual(reader.read(3), b"\x00\x00\x00")

    def test_player_action(self) -> None:
        reader = BufferReader(packets.player_action(2, BlockPos(1, 2, 3), 1, 7))
        self.assertEqual(reader.read_varint(), 2)
        self.assertEqual(reader.read_position(), BlockPos(1, 2, 3))
        self.assertEqual(reader.read_i8(), 1)
        self.assertEqual(reader.read_varint(), 7)

    def test_client_information(self) -> None:
        reader = BufferReader(packets.client_information(view_distance=12))
        self.assertEqual(reader.read_string(), "zh_CN")
        self.assertEqual(reader.read_i8(), 12)
        self.assertEqual(reader.read_varint(), 0)
        self.assertTrue(reader.read_bool())

    def test_component_text(self) -> None:
        self.assertEqual(packets.component_text('{"text":"hello"}'), "hello")
        self.assertEqual(packets.component_text('{"text":"a","extra":[{"text":"b"}]}'), "ab")
        self.assertEqual(packets.component_text("plain"), "plain")
        self.assertEqual(packets.component_text(None), "")

    def test_packet_ids_are_unique(self) -> None:
        self.assertEqual(len(set(packets.CLIENTBOUND.values())), len(packets.CLIENTBOUND))
        self.assertEqual(len(set(packets.SERVERBOUND.values())), len(packets.SERVERBOUND))


if __name__ == "__main__":
    unittest.main(verbosity=2)

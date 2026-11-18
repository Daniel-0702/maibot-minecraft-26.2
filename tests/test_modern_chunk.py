"""26.2 wire fixtures: no count/long length prefixes; padded longs."""
import json
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcbot.buffer import BufferWriter
from mcbot.chunk import ChunkColumn, PaletteError
from mcbot.modern_chunk import decode_chunk
from mcbot.registry import BlockRegistry


def container(bits, palette, values=None):
    writer = BufferWriter().write_u8(bits)
    if bits == 0:
        return writer.write_varint(palette).to_bytes()
    if palette is not None:
        writer.write_varint(len(palette))
        for value in palette:
            writer.write_varint(value)
    slots = 64 // bits
    for start in range(0, len(values), slots):
        word = sum(value << (index * bits) for index, value in enumerate(values[start:start + slots]))
        writer.write_u64(word)
    return writer.to_bytes()


def section(blocks=None, biomes=None, count=0, fluid=0):
    return struct.pack('>hh', count, fluid) + (blocks or container(0, 0)) + (biomes or container(0, 1))


class ModernChunkTests(unittest.TestCase):
    def test_official_level_chunk_section_write_fixture(self):
        data = (ROOT / 'tests/fixtures/modern_26_2/sections.bin').read_bytes()
        column = decode_chunk(3, -4, data, -64, 32)
        expected = [0 if (v := (i * 7 + 3) % 19) == 0 else 13332 if v == 18 else v for i in range(4096)]
        self.assertEqual(data[4], 5)  # official serializer selected 5-bit block palette
        self.assertEqual(column.block_states(), expected + [0] * 4096)
        self.assertEqual(column.non_air_count(0), sum(v != 0 for v in expected))
        self.assertEqual(column.non_air_count(1), 0)
        self.assertEqual(set(column._biomes), {0})  # fixture's one-entry plains registry
        self.assertEqual(struct.unpack('>hh', data[:4])[1], expected.count(13332))
        self.assertEqual(column.invalid_index_count, 0)

    def test_twenty_four_singleton_sections(self):
        data = section() * 23 + section(container(0, 1), count=4096)
        column = decode_chunk(2, -3, data, -64, 384)
        self.assertIsInstance(column, ChunkColumn)
        self.assertEqual((column.cx, column.cz, column.section_count), (2, -3, 24))
        self.assertEqual(column.get_state(15, 319, 15), 1)
        self.assertEqual(column.get_state(0, -64, 0), 0)
        self.assertEqual(column.get_biome(15, 319, 15), 1)
        self.assertEqual(column.non_air_count(23), 4096)

    def test_padded_five_bit_longs(self):
        values = [(i * 7 + 3) % 17 for i in range(4096)]
        palette = list(range(1, 18))
        column = decode_chunk(0, 0, section(container(5, palette, values), count=4096), 0, 16)
        self.assertEqual(column.block_states(), [palette[v] for v in values])
        for i in (11, 12, 13, 4095):
            self.assertEqual(column.get_state(i & 15, i >> 8, (i >> 4) & 15), palette[values[i]])

    def test_global_blocks_and_biomes(self):
        registry = BlockRegistry(ROOT / 'mcbot/data/mc_26_2.json')
        bits = registry.max_state_id.bit_length()
        values = [(i * 37) % (registry.max_state_id + 1) for i in range(4096)]
        biome_values = [i % 3 for i in range(64)]
        data = section(container(bits, None, values), container(2, [11, 22, 33], biome_values), count=4096, fluid=1)
        column = decode_chunk(0, 0, data, 0, 16)
        self.assertEqual(column.block_states(), values)
        for x, y, z in ((0, 0, 0), (15, 15, 15), (4, 8, 12)):
            index = ((y >> 2) << 4) | ((z >> 2) << 2) | (x >> 2)
            self.assertEqual(column.get_biome(x, y, z), [11, 22, 33][biome_values[index]])
        direct = decode_chunk(0, 0, section(biomes=container(6, None, list(range(64)))), 0, 16)
        self.assertEqual(direct.get_biome(15, 15, 15), 63)

    def test_strict_malformed(self):
        valid = section()
        for bad in (b'', valid[:-1], valid + b'\x00', b'\x01' + valid,
                    section(count=-1), section(count=4097), section(fluid=-1), section(fluid=4097),
                    section(b'\x00\xff\xff\xff\xff\x07'),
                    section(b'\x00\x80\x00'), section(b'\x00\xff\xff\xff\xff\x0f'),
                    section(b'\x04\x00'), section(b'\x04\x11'),
                    section(container(4, [0, 0], [0] * 4096)),
                    section(container(4, [0, 1], [2] * 4096)),
                    section(b'\x03'), section(b'\x20'), section(b'\x09')):
            with self.subTest(data=bad[:20]):
                with self.assertRaises(PaletteError): decode_chunk(0, 0, bad, 0, 16)
        for min_y, height in ((1, 16), (0, 0), (0, 17), (0, -16), (0, 16.0)):
            with self.assertRaises(PaletteError): decode_chunk(0, 0, valid, min_y, height)

    def test_registry_matches_official_state_ids(self):
        registry = BlockRegistry(ROOT / 'mcbot/data/mc_26_2.json')
        report = ROOT.parent / 'research-26.2/reports/reports/blocks.json'
        if not report.exists(): self.skipTest('official research reports not distributed')
        blocks = json.loads(report.read_text(encoding='utf-8'))
        for name, definition in blocks.items():
            default = next(s['id'] for s in definition['states'] if s.get('default'))
            self.assertEqual(registry.state_id(name), default)
            for state in definition['states']:
                self.assertEqual(registry.block_name(state['id']), name)

    def test_registry_physics_and_items(self):
        r = BlockRegistry(ROOT / 'mcbot/data/mc_26_2.json')
        self.assertEqual((r.minecraft_version, r.protocol_version), ('26.2', 776))
        self.assertEqual(r.boxes(r.state_id('air')), ())
        self.assertTrue(r.full_cube(r.state_id('stone')))
        self.assertEqual(r.hardness(r.state_id('stone')), 1.5)
        self.assertEqual(r.boxes(r.state_id('oak_slab'))[0].max_y, 0.5)
        self.assertEqual(r.item(r.item_id('ender_pearl')).stack_size, 16)
        self.assertEqual(r.item(r.item_id('diamond_pickaxe')).stack_size, 1)
        self.assertEqual(r.food(r.item_id('apple'))[0], 4)
        self.assertIn('minecraft:player', r._entities.values())


if __name__ == '__main__':
    unittest.main()

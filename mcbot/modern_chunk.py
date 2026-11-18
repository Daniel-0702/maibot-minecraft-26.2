"""Strict Java 26.2 section decoder (protocol 776).

Input is only the length-delimited chunk data, not heightmaps/light/block entities.
Sections have two shorts, then block/biome containers; neither section count nor
long-array lengths are transmitted. SimpleBitStorage never straddles longs.
Biome IDs are connection-registry IDs: their upper bound cannot be checked here.
"""
from __future__ import annotations

from array import array
from functools import lru_cache
import json
from pathlib import Path

from .buffer import BufferReader, ProtocolError
from .chunk import ChunkColumn, PaletteError, DEFAULT_MIN_Y, DEFAULT_HEIGHT


@lru_cache(maxsize=1)
def _state_count() -> int:
    path = Path(__file__).with_name("data") / "mc_26_2.json"
    with path.open(encoding="utf-8") as stream:
        return max(row[2] for row in json.load(stream)["blocks"]) + 1


def _varint(reader: BufferReader) -> int:
    """Nonnegative canonical signed-32-bit VarInt, unlike permissive base reader."""
    value = 0
    for index in range(5):
        byte = reader.read_u8()
        if index == 4 and byte > 7:
            raise PaletteError("negative or overflowing palette VarInt")
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            if index and byte == 0:
                raise PaletteError("noncanonical palette VarInt")
            return value
    raise PaletteError("palette VarInt exceeds five bytes")


def _container(reader: BufferReader, biome: bool = False):
    bits = reader.read_u8()
    volume = 64 if biome else 4096
    limit = None if biome else _state_count()

    def entry():
        value = _varint(reader)
        if limit is not None and value >= limit:
            raise PaletteError(f"unknown 26.2 block state ID {value}")
        return value

    if bits == 0:
        return entry()
    local = bits <= (3 if biome else 8)
    if (not biome and bits < 4) or bits > 31:
        raise PaletteError(f"invalid {'biome' if biome else 'block'} bits: {bits}")
    # The vanilla global block registry has a fixed width, not arbitrary direct bits.
    if not biome and not local and bits != (_state_count() - 1).bit_length():
        raise PaletteError(f"invalid 26.2 global block width: {bits}")
    palette = None
    if local:
        length = _varint(reader)
        if not 1 <= length <= (1 << bits):
            raise PaletteError(f"invalid palette length {length} for {bits} bits")
        palette = [entry() for _ in range(length)]
        if len(set(palette)) != length:
            raise PaletteError("duplicate palette entries")
    per_long = 64 // bits
    words = (volume + per_long - 1) // per_long
    out = array("I")
    mask = (1 << bits) - 1
    for _ in range(words):
        word = reader.read_u64()
        for slot in range(min(per_long, volume - len(out))):
            value = (word >> (slot * bits)) & mask
            if palette is not None:
                if value >= len(palette):
                    raise PaletteError(f"palette index {value} out of range {len(palette)}")
                value = palette[value]
            elif limit is not None and value >= limit:
                raise PaletteError(f"unknown 26.2 block state ID {value}")
            out.append(value)
    return out


def decode_chunk(cx: int, cz: int, data: bytes, min_y: int = DEFAULT_MIN_Y,
                 height: int = DEFAULT_HEIGHT) -> ChunkColumn:
    """Decode exactly ``height // 16`` sections or raise :class:`PaletteError`.

    Padding bits are ignored, matching vanilla. Counts are range-checked, not
    recomputed: fluid/air semantics require state registry metadata. fluidCount
    is validated then discarded because the shared ChunkColumn has no slot.
    """
    if type(min_y) is not int or type(height) is not int or min_y % 16 or height <= 0 or height % 16:
        raise PaletteError("min_y and positive height must be multiples of 16")
    if height // 16 > len(data) // 8:
        raise PaletteError("not enough data for dimension section count")
    column = ChunkColumn(cx, cz, min_y, height)
    reader = BufferReader(data)
    try:
        for section in range(height // 16):
            block_count, fluid_count = reader.read_i16(), reader.read_i16()
            if not 0 <= block_count <= 4096 or not 0 <= fluid_count <= 4096:
                raise PaletteError(f"section {section}: invalid block/fluid counts")
            column._sections.append(_container(reader))
            column._biomes.append(_container(reader, True))
            column._biome_volume.append(64)
            column._non_air.append(block_count)
    except ProtocolError as exc:
        raise PaletteError(f"truncated modern chunk at byte {reader.position}: {exc}") from exc
    if reader.remaining:
        raise PaletteError(f"trailing chunk data: {reader.remaining} bytes")
    return column

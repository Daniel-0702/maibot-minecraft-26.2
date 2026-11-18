"""测试用的最小 Minecraft 1.20.1 服务器（离线模式，无加密）。

它实现了握手、状态查询、登录、区块下发、位置同步、挖掘/放置确认、聊天与
KeepAlive，用来在没有真实服务端的情况下做端到端测试。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
import zlib
from dataclasses import dataclass, field

from mcbot.buffer import BufferReader, BufferWriter
from mcbot.nbt import write_nbt
from mcbot.packets import CLIENTBOUND, LOGIN_CLIENTBOUND, SERVERBOUND
from mcbot.registry import registry

STONE = registry().state_id("stone")
DIRT = registry().state_id("dirt")
GRASS = registry().state_id("grass_block")
AIR = 0


def _section_all(state_id: int) -> bytes:
    """整段同一种方块：bpe=0 + 单值调色板。"""
    writer = BufferWriter()
    writer.write_u8(0)
    writer.write_varint(1)
    writer.write_varint(state_id)
    return writer.to_bytes()


def _section_palette(values: list[int]) -> bytes:
    """带调色板 + 4 bit 索引的区段（values 长度 4096）。"""
    writer = BufferWriter()
    writer.write_u8(4)
    palette = sorted(set(values))
    writer.write_varint(len(palette))
    for entry in palette:
        writer.write_varint(entry)
    index_of = {value: index for index, value in enumerate(palette)}
    longs = [0] * ((len(values) * 4 + 63) // 64)
    for position, value in enumerate(values):
        bit = position * 4
        long_index = bit // 64
        shift = bit % 64
        longs[long_index] |= index_of[value] << shift
        if shift + 4 > 64:
            longs[long_index + 1] |= index_of[value] >> (64 - shift)
    writer.write_varint(len(longs))
    for value in longs:
        writer.write_i64(value - (1 << 64) if value >= (1 << 63) else value)
    return writer.to_bytes()


def _biome_section(biome_id: int = 1) -> bytes:
    writer = BufferWriter()
    writer.write_u8(0)
    writer.write_varint(1)
    writer.write_varint(biome_id)
    return writer.to_bytes()


def build_flat_chunk(chunk_x: int, chunk_z: int) -> bytes:
    """构造一个平坦世界的区块负载（y=-64..-1 石头，y=0 泥土，y=1 草方块）。"""
    writer = BufferWriter()
    sections = 24
    writer.write_varint(sections)
    for section in range(sections):
        base_y = -64 + section * 16
        if base_y + 15 < 0:
            # 地下：整段石头
            writer.write_i16(256)
            writer.write(_section_all(STONE))
            writer.write(_biome_section())
            continue
        if base_y <= 0 <= base_y + 15:
            # 地表过渡区段：石头 / 泥土 / 草 / 空气
            values = []
            for y in range(16):
                world_y = base_y + y
                if world_y < 0:
                    state = STONE
                elif world_y == 0:
                    state = DIRT
                elif world_y == 1:
                    state = GRASS
                else:
                    state = AIR
                values.extend([state] * 256)
            writer.write_i16(32)
            writer.write(_section_palette(values))
            writer.write(_biome_section())
            continue
        writer.write_i16(0)
        writer.write(_section_all(AIR))
        writer.write(_biome_section())
    return writer.to_bytes()


def build_air_chunk() -> bytes:
    writer = BufferWriter()
    writer.write_varint(24)
    for _ in range(24):
        writer.write_i16(0)
        writer.write(_section_all(AIR))
        writer.write(_biome_section())
    return writer.to_bytes()


@dataclass
class Received:
    name: str
    data: dict = field(default_factory=dict)
    at: float = 0.0


class FakeMinecraftServer:
    """单连接的最小服务器实现。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, *, compression: int | None = None) -> None:
        self.host = host
        self.port = port
        self.compression = compression
        self.version_protocol = 763
        self.version_name = "1.20.1"
        self.client_protocol = 0
        self.handshake_host = ""
        self.handshake_port = 0
        self.server: asyncio.AbstractServer | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list[Received] = []
        self.chat_messages: list[str] = []
        self.positions: list[tuple[float, float, float]] = []
        self.player_uuid: uuid.UUID | None = None
        self.username = ""
        self.keepalive_sent = 0
        self.keepalive_answered = 0
        self.connected = asyncio.Event()
        self.disconnected = asyncio.Event()
        self._compression_threshold: int | None = None
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._next_keepalive = 1000
        self.tick = 0

    @property
    def address(self) -> tuple[str, int]:
        assert self.server is not None
        sockets = self.server.sockets
        assert sockets
        return sockets[0].getsockname()[:2]

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self.address[1]
        self._tasks.append(asyncio.create_task(self._keepalive_loop()))

    async def stop(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self.writer is not None:
            self.writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    # ------------------------------------------------------------------ 连接
    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.connected.set()
        try:
            await self._handshake_loop()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self.disconnected.set()
            try:
                writer.close()
            except Exception:
                pass

    async def _read_frame(self) -> tuple[int, BufferReader]:
        assert self.reader is not None
        length = 0
        for index in range(5):
            raw = await self.reader.readexactly(1)
            length |= (raw[0] & 0x7F) << (7 * index)
            if not raw[0] & 0x80:
                break
        body = await self.reader.readexactly(length)
        if self._compression_threshold is not None:
            inner = BufferReader(body)
            data_length = inner.read_varint()
            payload = inner.read_rest()
            body = zlib.decompress(payload) if data_length > 0 else payload
        packet = BufferReader(body)
        return packet.read_varint(), packet

    def _send(self, packet_id: int, payload: bytes = b"") -> None:
        if self.writer is None:
            raise RuntimeError("没有客户端连接")
        body = BufferWriter().write_varint(packet_id).write(payload).to_bytes()
        if self._compression_threshold is not None:
            if len(body) >= self._compression_threshold:
                compressed = zlib.compress(body)
                inner = BufferWriter().write_varint(len(body)).write(compressed).to_bytes()
            else:
                inner = BufferWriter().write_varint(0).write(body).to_bytes()
            frame = BufferWriter().write_varint(len(inner)).write(inner).to_bytes()
        else:
            frame = BufferWriter().write_varint(len(body)).write(body).to_bytes()
        self.writer.write(frame)

    async def _handshake_loop(self) -> None:
        packet_id, packet = await self._read_frame()
        if packet_id != 0x00:
            return
        protocol = packet.read_varint()
        self.client_protocol = protocol
        host = packet.read_string()
        self.handshake_host = host
        port = packet.read_u16()
        self.handshake_port = port
        next_state = packet.read_varint()
        if next_state == 1:
            await self._status_loop()
            return
        if next_state == 2:
            await self._login(host, port)

    async def _status_loop(self) -> None:
        packet_id, _ = await self._read_frame()
        if packet_id != 0x00:
            return
        payload = {
            "version": {"name": self.version_name, "protocol": self.version_protocol},
            "players": {"max": 20, "online": 1},
            "description": {"text": "Fake MaiBot Server"},
        }
        self._send(0x00, BufferWriter().write_string(json.dumps(payload)).to_bytes())
        packet_id, packet = await self._read_frame()
        if packet_id == 0x01:
            self._send(0x01, BufferWriter().write_i64(packet.read_i64()).to_bytes())
        await asyncio.sleep(0.05)

    async def _login(self, host: str, port: int) -> None:
        packet_id, packet = await self._read_frame()
        if packet_id != 0x00:
            raise RuntimeError(f"期望 Login Start，收到 0x{packet_id:02x}")
        if self.client_protocol != self.version_protocol:
            reason = json.dumps(
                {"text": f"Incompatible client! Please use {self.version_name}"}, ensure_ascii=False
            )
            self._send(
                LOGIN_CLIENTBOUND["disconnect"],
                BufferWriter().write_string(reason).to_bytes(),
            )
            return
        username = packet.read_string(16)
        self.username = username
        # 1.20.1 Login Start: name + option<UUID>（bool 存在标记 + UUID）
        if packet.read_bool():
            packet.read_uuid()
        self.player_uuid = uuid.UUID(int=0x1234567890ABCDEF1234567890ABCDEF)
        if self.compression is not None:
            self._send(LOGIN_CLIENTBOUND["compress"], BufferWriter().write_varint(self.compression).to_bytes())
            self._compression_threshold = self.compression
        payload = (
            BufferWriter()
            .write_uuid(self.player_uuid)
            .write_string(username)
            .write_varint(0)
            .to_bytes()
        )
        self._send(LOGIN_CLIENTBOUND["success"], payload)
        await self._play_loop()

    async def _play_loop(self) -> None:
        self._send_join()
        for chunk_x in range(-1, 2):
            for chunk_z in range(-1, 2):
                self._send_chunk(chunk_x, chunk_z)
        self._send_position(0.5, 2.0, 0.5)
        self._send_health(20.0, 20, 5.0)
        self._send_time(6000)
        self._send_title("验证码：654321，请在 5 分钟内发送到 QQ 群")
        self.send_system_chat("欢迎来到测试服务器")

        while not self._closed:
            packet_id, packet = await self._read_frame()
            self.tick += 1
            try:
                self._handle_packet(packet_id, packet)
            except Exception:
                pass

    # ------------------------------------------------------------------ 下行
    def _send_join(self) -> None:
        writer = BufferWriter()
        writer.write_i32(1)  # entity id
        writer.write_bool(False)
        writer.write_u8(0)  # survival
        writer.write_i8(-1)
        writer.write_varint(1)
        writer.write_string("minecraft:overworld")
        writer.write(write_nbt({"minecraft:dimension_type": {"type": "minecraft:dimension_type", "value": []}}))
        writer.write_string("minecraft:overworld")
        writer.write_string("minecraft:overworld")
        writer.write_i64(0)
        writer.write_varint(20)
        writer.write_varint(10)
        writer.write_varint(10)
        writer.write_bool(False)
        writer.write_bool(True)
        writer.write_bool(False)
        writer.write_bool(True)
        writer.write_bool(False)  # 无死亡位置
        writer.write_varint(0)
        self._send(CLIENTBOUND["login"], writer.to_bytes())

    def _send_chunk(self, chunk_x: int, chunk_z: int) -> None:
        data = build_flat_chunk(chunk_x, chunk_z)
        writer = BufferWriter()
        writer.write_i32(chunk_x)
        writer.write_i32(chunk_z)
        writer.write(write_nbt({}))
        writer.write_varint(len(data))
        writer.write(data)
        writer.write_varint(0)  # block entities
        writer.write_varint(0)  # sky light mask
        writer.write_varint(0)  # block light mask
        writer.write_varint(0)  # empty sky mask
        writer.write_varint(0)  # empty block mask
        writer.write_varint(0)  # sky light arrays
        writer.write_varint(0)  # block light arrays
        self._send(CLIENTBOUND["map_chunk"], writer.to_bytes())

    def _send_position(self, x: float, y: float, z: float, yaw: float = 0.0, pitch: float = 0.0) -> None:
        payload = (
            BufferWriter()
            .write_f64(x)
            .write_f64(y)
            .write_f64(z)
            .write_f32(yaw)
            .write_f32(pitch)
            .write_i8(0)
            .write_varint(1)
            .to_bytes()
        )
        self._send(CLIENTBOUND["position"], payload)

    def _send_health(self, health: float, food: int, saturation: float) -> None:
        payload = BufferWriter().write_f32(health).write_varint(food).write_f32(saturation).to_bytes()
        self._send(CLIENTBOUND["update_health"], payload)

    def _send_time(self, day_time: int) -> None:
        payload = BufferWriter().write_i64(0).write_i64(day_time).to_bytes()
        self._send(CLIENTBOUND["update_time"], payload)

    def _send_title(self, text: str) -> None:
        payload = BufferWriter().write_string(json.dumps({"text": text}, ensure_ascii=False)).to_bytes()
        self._send(CLIENTBOUND["set_title_text"], payload)

    def send_system_chat(self, text: str) -> None:
        payload = BufferWriter().write_string(json.dumps({"text": text})).write_bool(False).to_bytes()
        self._send(CLIENTBOUND["system_chat"], payload)

    def _send_block_change(self, x: int, y: int, z: int, state_id: int) -> None:
        from mcbot.types import BlockPos

        payload = BufferWriter().write_position(BlockPos(x, y, z)).write_varint(state_id).to_bytes()
        self._send(CLIENTBOUND["block_change"], payload)

    # ------------------------------------------------------------------ 上行
    def _handle_packet(self, packet_id: int, packet: BufferReader) -> None:
        name = _SERVERBOUND_NAMES.get(packet_id, f"unknown_0x{packet_id:02x}")
        record = Received(name=name, at=time.monotonic())
        if packet_id == SERVERBOUND["chat_message"]:
            message = packet.read_string(256)
            self.chat_messages.append(message)
        elif packet_id == SERVERBOUND["chat_command"]:
            command = packet.read_string(256)
            self.chat_messages.append("/" + command)
        elif packet_id in (SERVERBOUND["position"], SERVERBOUND["position_look"]):
            x = packet.read_f64()
            y = packet.read_f64()
            z = packet.read_f64()
            self.positions.append((x, y, z))
            record.data["position"] = (x, y, z)
        elif packet_id == SERVERBOUND["block_dig"]:
            status = packet.read_varint()
            position = packet.read_position()
            packet.read_i8()
            packet.read_varint()
            record.data = {"status": status, "position": position.as_tuple()}
            if status == 2:
                self._send_block_change(position.x, position.y, position.z, AIR)
        elif packet_id == SERVERBOUND["block_place"]:
            packet.read_varint()
            position = packet.read_position()
            record.data = {"position": position.as_tuple()}
            self._send_block_change(position.x, position.y, position.z, STONE)
        elif packet_id == SERVERBOUND["keep_alive"]:
            packet.read_i64()
            self.keepalive_answered += 1
        elif packet_id == SERVERBOUND["teleport_confirm"]:
            record.data["teleport_id"] = packet.read_varint()
        elif packet_id == SERVERBOUND["settings"]:
            record.data["locale"] = packet.read_string(16)
        elif packet_id == SERVERBOUND["use_entity"]:
            record.data["target"] = packet.read_varint()
        elif packet_id == SERVERBOUND["client_command"]:
            record.data["action"] = packet.read_varint()
        self.received.append(record)

    async def _keepalive_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.5)
            if self.writer is None or not self.connected.is_set():
                continue
            self._next_keepalive += 1
            try:
                self._send(
                    CLIENTBOUND["keep_alive"],
                    BufferWriter().write_i64(self._next_keepalive).to_bytes(),
                )
                self.keepalive_sent += 1
            except Exception:
                return


_SERVERBOUND_NAMES = {value: key for key, value in SERVERBOUND.items()}

"""Minecraft 1.20.1 网络客户端：登录、压缩、加密、收包分发、世界与实体状态。

这一层只负责"协议 + 状态"，不包含物理、寻路和决策；更上层是
:mod:`mcbot.player`（移动/动作）与 :mod:`mcbot.brain`（行为）。
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import packets
from .auth import AuthSession, join_server
from .buffer import BufferReader, BufferWriter, ProtocolError
from .chunk import ChunkColumn
from .crypto import encrypt_rsa, make_cipher, server_hash
from .packets import CLIENTBOUND, LOGIN_CLIENTBOUND, LOGIN_SERVERBOUND, SERVERBOUND
from .registry import BlockRegistry, registry as default_registry
from .types import BlockPos, Vec3, wrap_degrees
from .world import World

__all__ = [
    "ClientError",
    "ConnectionClosed",
    "LoginFailed",
    "MinecraftClient",
    "ServerStatus",
    "ping_server",
    "ItemStack",
    "Entity",
    "PlayerInfo",
    "ChatMessage",
]

DIMENSION_BOUNDS: dict[str, tuple[int, int]] = {
    "minecraft:overworld": (-64, 384),
    "minecraft:the_nether": (0, 256),
    "minecraft:the_end": (0, 256),
}


class ClientError(RuntimeError):
    """客户端状态错误。"""


class ConnectionClosed(ClientError):
    """连接被服务器或网络关闭。"""


class LoginFailed(ClientError):
    """登录被拒绝（密码错误、服务器满员、正版验证失败等）。"""


@dataclass(slots=True)
class ItemStack:
    item_id: int
    count: int
    nbt: Any = None

    @property
    def is_empty(self) -> bool:
        return self.item_id <= 0 or self.count <= 0


@dataclass(slots=True)
class Entity:
    entity_id: int
    type_name: str = "minecraft:unknown"
    uuid: uuid.UUID | None = None
    position: Vec3 = field(default_factory=Vec3)
    yaw: float = 0.0
    pitch: float = 0.0
    velocity: Vec3 = field(default_factory=Vec3)

    @property
    def short_name(self) -> str:
        return self.type_name.split(":", 1)[-1]


@dataclass(slots=True)
class PlayerInfo:
    uuid: uuid.UUID
    name: str
    latency: int = 0
    game_mode: int = -1
    listed: bool = True


@dataclass(slots=True)
class ChatMessage:
    kind: str
    text: str
    sender: str = ""
    sender_uuid: uuid.UUID | None = None
    timestamp: float = 0.0
    source: str = ""

    @property
    def is_system(self) -> bool:
        return self.kind != "chat"


@dataclass(slots=True)
class ServerStatus:
    host: str
    port: int
    version_name: str = ""
    protocol: int = 0
    max_players: int = 0
    online_players: int = 0
    motd: str = ""
    favicon: str = ""
    latency_ms: float = 0.0
    raw: dict = field(default_factory=dict)


async def ping_server(
    host: str, port: int = 25565, timeout: float = 5.0, handshake_host: str | None = None
) -> ServerStatus:
    """服务器列表 ping（不登录）。

    ``host`` 是 TCP 目标（可能来自 SRV），``handshake_host`` 是用户填写的原始
    域名，会写进握手包，供虚拟主机 / 转发插件识别。
    """
    started = time.monotonic()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        body = BufferWriter().write_varint(0x00).write(
            packets.handshake(handshake_host or host, port, 1)
        ).to_bytes()
        writer.write(BufferWriter().write_varint(len(body)).write(body).to_bytes())
        body = BufferWriter().write_varint(0x00).to_bytes()
        writer.write(BufferWriter().write_varint(len(body)).write(body).to_bytes())
        await writer.drain()

        async def read_packet() -> tuple[int, BufferReader]:
            length = 0
            for index in range(5):
                raw = await reader.readexactly(1)
                length |= (raw[0] & 0x7F) << (7 * index)
                if not raw[0] & 0x80:
                    break
            data = await reader.readexactly(length)
            inner = BufferReader(data)
            return inner.read_varint(), inner

        packet_id, packet = await asyncio.wait_for(read_packet(), timeout)
        if packet_id != 0x00:
            raise ClientError(f"服务器返回了意外状态包 0x{packet_id:02x}")
        raw_json = packet.read_string(1 << 20)
        payload: dict = {}
        try:
            import json

            payload = json.loads(raw_json)
        except Exception:
            pass
        result = ServerStatus(
            host=host,
            port=port,
            version_name=str(payload.get("version", {}).get("name", "")),
            protocol=int(payload.get("version", {}).get("protocol", 0)),
            max_players=int(payload.get("players", {}).get("max", 0)),
            online_players=int(payload.get("players", {}).get("online", 0)),
            motd=packets.component_text(json.dumps(payload.get("description", ""))),
            favicon=str(payload.get("favicon", "")),
            latency_ms=(time.monotonic() - started) * 1000.0,
            raw=payload,
        )
        return result
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


class MinecraftClient:
    """一个 Minecraft 1.20.1 连接。"""

    def __init__(
        self,
        host: str,
        port: int = 25565,
        username: str = "MaiBot",
        session: AuthSession | None = None,
        view_distance: int = 8,
        registry: BlockRegistry | None = None,
        logger: Callable[[str], None] | None = None,
        handshake_host: str | None = None,
    ) -> None:
        self.host = host
        self.handshake_host = handshake_host or host
        self.port = port
        self.username = username
        self.session = session
        self.view_distance = max(2, min(32, view_distance))
        self.registry = registry or default_registry()
        self.log = logger or (lambda message: None)

        self.world = World(self.registry)
        self.state = "idle"
        self.protocol_version = packets.PROTOCOL_VERSION

        # 连接
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._cipher = None
        self._compression: int | None = None
        self._closed = asyncio.Event()
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._sequence = 0
        self._last_packet_at = time.monotonic()

        # 玩家状态
        self.entity_id = 0
        self.position = Vec3()
        self.position_received = False
        self.yaw = 0.0
        self.pitch = 0.0
        self.on_ground = False
        self.health = 20.0
        self.food = 20
        self.saturation = 5.0
        self.game_mode = 0
        self.experience_level = 0
        self.experience_bar = 0.0
        self.dimension = "minecraft:overworld"
        self.world_name = ""
        self.spawn_position = BlockPos()
        self.death_position: BlockPos | None = None
        self.no_respawn_block = False
        self.can_fly = False
        self.flying = False
        self.is_dead = False

        # 世界 / 实体
        self.players: dict[uuid.UUID, PlayerInfo] = {}
        self.entities: dict[int, Entity] = {}
        self.inventory: dict[int, ItemStack | None] = {}
        self.carried_item: ItemStack | None = None
        self.held_slot = 0
        self.window_id = 0
        self.open_window_type = -1
        self.server_brand = ""
        self.ping_ms = 0.0
        self.chat_log: deque[ChatMessage] = deque(maxlen=200)
        self.loaded_chunks = 0
        self.chunk_packets_received = 0
        self.chunk_decode_errors = 0
        self.last_chunk_error = ""
        self.last_chunk_hex = ""

    # ------------------------------------------------------------------ 生命周期
    @property
    def connected(self) -> bool:
        return self.state in {"login", "play"} and self._writer is not None

    @property
    def last_packet_age(self) -> float:
        return time.monotonic() - self._last_packet_at

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable[..., Any]) -> None:
        listeners = self._listeners.get(event)
        if listeners and callback in listeners:
            listeners.remove(callback)

    def _emit(self, event: str, payload: Any = None) -> None:
        for callback in list(self._listeners.get(event, ())):
            try:
                result = callback(payload)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as exc:  # 监听器异常不能拖垮收包循环
                self.log(f"事件 {event} 处理失败: {exc!r}")

    async def connect(self, timeout: float = 15.0) -> None:
        """建立连接并完成登录；成功后 ``state == "play"``。"""
        if self.connected:
            raise ClientError("已经连接到服务器")
        self.state = "connecting"
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=1 << 20), timeout
        )
        self.log(f"已连接 {self.host}:{self.port}")
        try:
            await self._login()
        except Exception:
            await self.disconnect("登录失败")
            raise
        self.state = "play"
        self._read_task = asyncio.create_task(self._read_loop())
        self.send_named("settings", self._client_information())
        self._emit("login", self)

    async def disconnect(self, reason: str = "") -> None:
        self.state = "closed"
        task = self._read_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._closed.set()
        if reason:
            self._emit("disconnect", reason)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    # ------------------------------------------------------------------ 登录
    async def _login(self) -> None:
        assert self._reader is not None and self._writer is not None
        self.state = "login"
        self._write_frame(0x00, packets.handshake(self.handshake_host, self.port, 2))
        session = self.session or AuthSession(username=self.username, uuid=uuid.uuid4())
        self._write_frame(LOGIN_SERVERBOUND["login_start"], packets.login_start(self.username, session.uuid))
        await self._writer.drain()

        while True:
            packet_id, packet = await asyncio.wait_for(self._read_frame(), 20.0)
            if packet_id == LOGIN_CLIENTBOUND["disconnect"]:
                reason = packets.component_text(packet.read_string(1 << 20))
                raise LoginFailed(reason or "服务器拒绝登录")
            if packet_id == LOGIN_CLIENTBOUND["encryption_request"]:
                await self._handle_encryption_request(packet)
                continue
            if packet_id == LOGIN_CLIENTBOUND["compress"]:
                self._compression = packet.read_varint()
                self.log(f"启用压缩，阈值 {self._compression}")
                continue
            if packet_id == LOGIN_CLIENTBOUND["login_plugin_request"]:
                message_id = packet.read_varint()
                self._write_frame(
                    LOGIN_SERVERBOUND["login_plugin_response"],
                    BufferWriter().write_varint(message_id).write_bool(False).to_bytes(),
                )
                await self._writer.drain()
                continue
            if packet_id == LOGIN_CLIENTBOUND["success"]:
                player_uuid = packet.read_uuid()
                username = packet.read_string(16)
                self.session = AuthSession(
                    username=username,
                    uuid=player_uuid,
                    access_token=session.access_token,
                    source=session.source,
                    expires_at=session.expires_at,
                )
                self.log(f"登录成功: {username} ({player_uuid})")
                return
            # 未知登录包：忽略
            self.log(f"忽略未知登录包 0x{packet_id:02x}")

    async def _handle_encryption_request(self, packet: BufferReader) -> None:
        assert self._writer is not None
        server_id = packet.read_string(20)
        public_key = packet.read(packet.read_varint())
        verify_token = packet.read(packet.read_varint())
        shared_secret = bytes(random.getrandbits(8) for _ in range(16))
        session = self.session
        if session is None or not session.access_token:
            raise LoginFailed(
                "该服务器开启了正版验证（online-mode=true），需要配置微软账号登录；"
                "或者把服务器改成 online-mode=false。"
            )
        digest = server_hash(server_id, shared_secret, public_key)
        await join_server(session, digest)
        response = packets.encryption_response(
            encrypt_rsa(public_key, shared_secret),
            encrypt_rsa(public_key, verify_token),
        )
        self._write_frame(LOGIN_SERVERBOUND["encryption_response"], response)
        await self._writer.drain()
        self._cipher = make_cipher(shared_secret)
        self.log("已启用连接加密")

    def _client_information(self) -> bytes:
        return packets.client_information(
            locale="zh_CN",
            view_distance=self.view_distance,
            chat_mode=0,
            chat_colors=True,
            skin_parts=0x7F,
            main_hand=1,
        )

    # ------------------------------------------------------------------ 收发
    def _write_frame(self, packet_id: int, payload: bytes = b"") -> None:
        if self._writer is None:
            raise ConnectionClosed("连接尚未建立")
        body = BufferWriter().write_varint(packet_id).write(payload).to_bytes()
        if self._compression is not None:
            threshold = self._compression
            if threshold > 0 and len(body) >= threshold:
                compressed = zlib.compress(body)
                inner = BufferWriter().write_varint(len(body)).write(compressed).to_bytes()
            else:
                inner = BufferWriter().write_varint(0).write(body).to_bytes()
            frame = BufferWriter().write_varint(len(inner)).write(inner).to_bytes()
        else:
            frame = BufferWriter().write_varint(len(body)).write(body).to_bytes()
        if self._cipher is not None:
            frame = self._cipher.encrypt(frame)
        self._writer.write(frame)

    def send(self, packet_id: int, payload: bytes = b"") -> None:
        """按数字 ID 发送一个包（会自动加密 / 压缩）。"""
        self._write_frame(packet_id, payload)

    def send_named(self, name: str, payload: bytes = b"") -> None:
        packet_id = SERVERBOUND.get(name)
        if packet_id is None:
            raise ClientError(f"未知的服务端包名: {name}")
        self._write_frame(packet_id, payload)

    async def _read_frame(self) -> tuple[int, BufferReader]:
        if self._reader is None:
            raise ConnectionClosed("连接已关闭")
        self._last_packet_at = time.monotonic()
        length = 0
        for index in range(5):
            raw = await self._read_stream(1)
            length |= (raw[0] & 0x7F) << (7 * index)
            if not raw[0] & 0x80:
                break
        else:
            raise ProtocolError("包长度 VarInt 过长")
        body = await self._read_stream(length)
        if self._compression is not None:
            inner = BufferReader(body)
            data_length = inner.read_varint()
            payload = inner.read_rest()
            if data_length > 0:
                body = zlib.decompress(payload)
                if len(body) != data_length:
                    raise ProtocolError("压缩包解压长度不一致")
            else:
                body = payload
        reader = BufferReader(body)
        packet_id = reader.read_varint()
        return packet_id, reader

    async def _read_stream(self, count: int) -> bytes:
        assert self._reader is not None
        data = await self._reader.readexactly(count)
        if self._cipher is not None:
            return self._cipher.decrypt(data)
        return data

    # ------------------------------------------------------------------ 收包循环
    async def _read_loop(self) -> None:
        try:
            while self.state == "play":
                packet_id, reader = await self._read_frame()
                try:
                    self._dispatch(packet_id, reader)
                except ProtocolError as exc:
                    self.log(f"解析包 0x{packet_id:02x} 失败: {exc}")
                except Exception as exc:  # 单个包处理异常不应断开连接
                    self.log(f"处理包 0x{packet_id:02x} 异常: {exc!r}")
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError) as exc:
            if self.state == "play":
                self.log(f"连接中断: {exc!r}")
                await self.disconnect("连接中断")
        except Exception as exc:
            self.log(f"收包循环异常: {exc!r}")
            await self.disconnect(str(exc))

    def _dispatch(self, packet_id: int, packet: BufferReader) -> None:
        handlers = _PLAY_HANDLERS.get(packet_id)
        if handlers is None:
            return
        handlers(self, packet)

    # ------------------------------------------------------------------ 发送动作
    def next_sequence(self) -> int:
        self._sequence = (self._sequence + 1) & 0x7FFFFFFF
        return self._sequence

    def send_chat(self, text: str) -> None:
        now = int(time.time() * 1000)
        self.send_named("chat_message", packets.chat_message(text, now, 0))

    def send_command(self, command: str) -> None:
        command = command if command.startswith("/") else "/" + command
        now = int(time.time() * 1000)
        self.send_named("chat_command", packets.chat_command(command[1:], now, 0))

    def send_position(self, position: Vec3, yaw: float, pitch: float, on_ground: bool, *, rotation: bool = True) -> None:
        if rotation:
            payload = packets.player_position_rotation(position.x, position.y, position.z, yaw, pitch, on_ground)
            self.send_named("position_look", payload)
        else:
            self.send_named("position", packets.player_position(position.x, position.y, position.z, on_ground))

    def send_rotation(self, yaw: float, pitch: float, on_ground: bool) -> None:
        self.send_named("look", packets.player_rotation(yaw, pitch, on_ground))

    def send_on_ground(self, on_ground: bool) -> None:
        self.send_named("flying", packets.player_on_ground(on_ground))

    def attack(self, entity_id: int) -> None:
        self.send_named("use_entity", packets.interact_entity(entity_id, 1))

    def interact(self, entity_id: int) -> None:
        self.send_named("use_entity", packets.interact_entity(entity_id, 0))

    def dig(self, status: int, position: BlockPos, face: int = 1) -> int:
        sequence = self.next_sequence()
        self.send_named("block_dig", packets.player_action(status, position, face, sequence))
        return sequence

    def place_block(self, position: BlockPos, face: int, cursor: tuple[float, float, float] = (0.5, 0.5, 0.5)) -> int:
        sequence = self.next_sequence()
        self.send_named("block_place", packets.use_item_on(0, position, face, cursor, sequence))
        return sequence

    def use_item(self, hand: int = 0) -> int:
        sequence = self.next_sequence()
        self.send_named("use_item", packets.use_item(hand, sequence))
        return sequence

    def swap_hands(self) -> None:
        self.send_named("block_dig", packets.player_action(6, BlockPos(), 0, self.next_sequence()))

    def drop_item(self, all_items: bool = False) -> None:
        self.send_named("block_dig", packets.player_action(3 if all_items else 4, BlockPos(), 0, self.next_sequence()))

    def set_held_slot(self, slot: int) -> None:
        slot = max(0, min(8, slot))
        self.send_named("held_item_slot", packets.held_item_slot(slot))
        self.held_slot = slot

    def respawn(self) -> None:
        self.send_named("client_command", packets.client_command(0))

    def set_sneaking(self, sneaking: bool) -> None:
        self.send_named(
            "entity_action",
            packets.player_command(self.entity_id, 0 if sneaking else 1),
        )

    def set_sprinting(self, sprinting: bool) -> None:
        self.send_named(
            "entity_action",
            packets.player_command(self.entity_id, 3 if sprinting else 4),
        )

    def swing_arm(self, hand: int = 0) -> None:
        self.send_named("arm_animation", packets.swing_arm(hand))

    # ------------------------------------------------------------------ 便捷查询
    def player_name(self, player_uuid: uuid.UUID) -> str:
        info = self.players.get(player_uuid)
        return info.name if info else str(player_uuid)

    def selected_item(self) -> ItemStack | None:
        return self.inventory.get(self.held_slot)

    def item_name(self, item: ItemStack | None) -> str:
        if item is None or item.is_empty:
            return "空气"
        info = self.registry.item(item.item_id)
        return info.name if info else f"未知物品#{item.item_id}"

    def health_fraction(self) -> float:
        return max(0.0, min(1.0, self.health / 20.0))

    def world_time_of_day(self) -> int:
        return self.world.day_time % 24000

    def _apply_dimension(self, dimension: str) -> None:
        """切换维度：重置世界（高度范围不同）并更新维度名。"""
        bounds = DIMENSION_BOUNDS.get(dimension)
        self.dimension = dimension
        if bounds is None:
            return
        min_y, height = bounds
        if self.world.min_y != min_y or self.world.height != height:
            self.world = World(self.registry, min_y, height)
            self.loaded_chunks = 0
        self.world.dimension = dimension


# --------------------------------------------------------------------------- 处理器
def _handle_keep_alive(client: MinecraftClient, packet: BufferReader) -> None:
    keep_alive_id = packet.read_i64()
    client.send_named("keep_alive", packets.keep_alive(keep_alive_id))


def _handle_ping(client: MinecraftClient, packet: BufferReader) -> None:
    ping_id = packet.read_i32()
    client.send_named("pong", packets.pong(ping_id))


def _handle_position(client: MinecraftClient, packet: BufferReader) -> None:
    x = packet.read_f64()
    y = packet.read_f64()
    z = packet.read_f64()
    yaw = packet.read_f32()
    pitch = packet.read_f32()
    flags = packet.read_i8()
    teleport_id = packet.read_varint()
    client.send_named("teleport_confirm", packets.teleport_confirm(teleport_id))
    relative = {
        "x": bool(flags & 1),
        "y": bool(flags & 2),
        "z": bool(flags & 4),
        "yaw": bool(flags & 8),
        "pitch": bool(flags & 16),
    }
    if relative["x"]:
        x += client.position.x
    if relative["y"]:
        y += client.position.y
    if relative["z"]:
        z += client.position.z
    if relative["yaw"]:
        yaw += client.yaw
    if relative["pitch"]:
        pitch += client.pitch
    client.position = Vec3(x, y, z)
    client.position_received = True
    client.yaw = wrap_degrees(yaw)
    client.pitch = max(-90.0, min(90.0, pitch))
    client.on_ground = False
    client._emit("position", client.position)


def _handle_login(client: MinecraftClient, packet: BufferReader) -> None:
    client.entity_id = packet.read_i32()
    packet.read_bool()  # hardcore
    client.game_mode = packet.read_u8()
    packet.read_i8()  # previous game mode
    world_count = packet.read_varint()
    for _ in range(world_count):
        packet.read_string()
    packet.read_nbt()  # dimension codec
    dimension = packet.read_string()  # world type / dimension name
    client.world_name = packet.read_string()
    packet.read_i64()  # hashed seed
    packet.read_varint()  # max players
    packet.read_varint()  # view distance
    packet.read_varint()  # simulation distance
    packet.read_bool()  # reduced debug
    packet.read_bool()  # respawn screen
    packet.read_bool()  # debug world
    packet.read_bool()  # flat
    if packet.read_bool():
        packet.read_string()  # death dimension
        client.death_position = packet.read_position()
    else:
        client.death_position = None
    packet.read_varint()  # portal cooldown
    client._apply_dimension(dimension)
    client.is_dead = False
    client.log(f"进入世界 {client.world_name}（{client.dimension}）")
    client._emit("join", client.world_name)


def _handle_respawn(client: MinecraftClient, packet: BufferReader) -> None:
    dimension = packet.read_string()
    client.world_name = packet.read_string()
    packet.read_i64()  # hashed seed
    client.game_mode = packet.read_i8()
    packet.read_u8()  # previous
    packet.read_bool()  # debug
    packet.read_bool()  # flat
    packet.read_bool()  # copy metadata
    if packet.read_bool():
        packet.read_string()
        client.death_position = packet.read_position()
    else:
        client.death_position = None
    packet.read_varint()
    client._apply_dimension(dimension)
    client.is_dead = False
    client.health = 20.0
    client._emit("respawn", dimension)


def _decode_slot(packet: BufferReader) -> ItemStack | None:
    if not packet.read_bool():
        return None
    item_id = packet.read_varint()
    count = packet.read_i8()
    nbt = packet.read_nbt() if packet.read_bool() else None
    return ItemStack(item_id, count, nbt)


def _handle_window_items(client: MinecraftClient, packet: BufferReader) -> None:
    window_id = packet.read_u8()
    packet.read_varint()  # state id
    count = packet.read_varint()
    items = [_decode_slot(packet) for _ in range(count)]
    carried = _decode_slot(packet)
    if window_id == 0:
        client.inventory = {index: item for index, item in enumerate(items)}
        client.carried_item = carried
    else:
        client.open_window_type = window_id
    client._emit("inventory", client.inventory)


def _handle_set_slot(client: MinecraftClient, packet: BufferReader) -> None:
    window_id = packet.read_i8()
    packet.read_varint()  # state id
    slot = packet.read_i16()
    item = _decode_slot(packet)
    if window_id == 0:
        client.inventory[slot] = item
        client._emit("inventory", client.inventory)


def _handle_held_item(client: MinecraftClient, packet: BufferReader) -> None:
    client.held_slot = packet.read_i8()


def _handle_health(client: MinecraftClient, packet: BufferReader) -> None:
    health = packet.read_f32()
    food = packet.read_varint()
    saturation = packet.read_f32()
    was_dead = client.health <= 0
    client.health = health
    client.food = food
    client.saturation = saturation
    if health <= 0 and not was_dead:
        client.is_dead = True
        client._emit("death", "生命值归零")
    client._emit("health", health)


def _handle_experience(client: MinecraftClient, packet: BufferReader) -> None:
    client.experience_bar = packet.read_f32()
    client.experience_level = packet.read_varint()
    packet.read_varint()


def _handle_chunk(client: MinecraftClient, packet: BufferReader) -> None:
    chunk_x = packet.read_i32()
    chunk_z = packet.read_i32()
    packet.read_nbt()  # heightmaps
    data = packet.read(packet.read_varint())
    client.chunk_packets_received += 1
    try:
        column = ChunkColumn.from_packet(chunk_x, chunk_z, data, client.world.min_y, client.world.height)
    except Exception as exc:
        client.chunk_decode_errors += 1
        client.last_chunk_error = f"({chunk_x},{chunk_z}) {type(exc).__name__}: {exc}"
        client.last_chunk_hex = data[:48].hex()
        client.log(f"区块 ({chunk_x}, {chunk_z}) 解析失败: {exc}（前 48 字节: {client.last_chunk_hex}）")
        return
    client.world.add_column(column)
    client.loaded_chunks = len(client.world.columns)
    client._emit("chunk", (chunk_x, chunk_z))


def _handle_unload_chunk(client: MinecraftClient, packet: BufferReader) -> None:
    chunk_x = packet.read_i32()
    chunk_z = packet.read_i32()
    client.world.remove_column(chunk_x, chunk_z)
    client.loaded_chunks = len(client.world.columns)


def _handle_block_change(client: MinecraftClient, packet: BufferReader) -> None:
    position = packet.read_position()
    state = packet.read_varint()
    client.world.set_state(position.x, position.y, position.z, state)
    client._emit("block_change", position)


def _handle_multi_block_change(client: MinecraftClient, packet: BufferReader) -> None:
    packed = packet.read_i64()
    section_x = packed >> 42
    section_z = (packed >> 20) & 0x3FFFFF
    section_y = packed & 0xFFFFF
    if section_x & (1 << 21):
        section_x -= 1 << 22
    if section_z & (1 << 21):
        section_z -= 1 << 22
    if section_y & (1 << 19):
        section_y -= 1 << 20
    base_x = section_x << 4
    base_y = section_y << 4
    base_z = section_z << 4
    count = packet.read_varint()
    for _ in range(count):
        record = packet.read_varint()
        state = record >> 12
        local_x = (record >> 8) & 0xF
        local_z = (record >> 4) & 0xF
        local_y = record & 0xF
        client.world.set_state(base_x + local_x, base_y + local_y, base_z + local_z, state)
    client._emit("block_change", None)


def _handle_time(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_i64()  # world age
    client.world.day_time = packet.read_i64()
    client._emit("time", client.world.day_time)


def _handle_system_chat(client: MinecraftClient, packet: BufferReader) -> None:
    text = packets.component_text(packet.read_string(1 << 18))
    action_bar = packet.read_bool()
    if not text:
        return
    message = ChatMessage(kind="action_bar" if action_bar else "system", text=text, timestamp=time.time())
    client.chat_log.append(message)
    client._emit("action_bar" if action_bar else "system_chat", message)


def _handle_player_chat(client: MinecraftClient, packet: BufferReader) -> None:
    sender_uuid = packet.read_uuid()
    packet.read_varint()  # index
    packet.read_bool()  # signature
    text = packet.read_string(256)
    timestamp = packet.read_i64()
    packet.read_i64()  # salt
    message = ChatMessage(
        kind="chat",
        text=text,
        sender=client.player_name(sender_uuid),
        sender_uuid=sender_uuid,
        timestamp=timestamp / 1000.0,
    )
    client.chat_log.append(message)
    client._emit("chat", message)


def _handle_profileless_chat(client: MinecraftClient, packet: BufferReader) -> None:
    text = packets.component_text(packet.read_string(1 << 18))
    packet.read_varint()  # type
    name = packets.component_text(packet.read_string(1 << 18))
    if packet.read_bool():
        packet.read_string(1 << 18)  # target
    message = ChatMessage(kind="system", text=text, sender=name, timestamp=time.time())
    client.chat_log.append(message)
    client._emit("system_chat", message)


def _handle_action_bar(client: MinecraftClient, packet: BufferReader) -> None:
    text = packets.component_text(packet.read_string(1 << 18))
    if not text:
        return
    message = ChatMessage(kind="action_bar", text=text, timestamp=time.time(), source="action_bar")
    client.chat_log.append(message)
    client._emit("action_bar", text)


def _handle_title_text(client: MinecraftClient, packet: BufferReader) -> None:
    text = packets.component_text(packet.read_string(1 << 18))
    if text:
        client._emit("title", text)


def _handle_title_subtitle(client: MinecraftClient, packet: BufferReader) -> None:
    text = packets.component_text(packet.read_string(1 << 18))
    if text:
        client._emit("subtitle", text)


def _handle_playerlist_header(client: MinecraftClient, packet: BufferReader) -> None:
    header = packets.component_text(packet.read_string(1 << 18))
    footer = packets.component_text(packet.read_string(1 << 18))
    text = "\n".join(part for part in (header, footer) if part)
    if text:
        client._emit("playerlist_header", text)


def _handle_disconnect(client: MinecraftClient, packet: BufferReader) -> None:
    reason = packets.component_text(packet.read_string(1 << 20))
    client.log(f"服务器断开连接: {reason}")
    client.state = "closed"
    client._emit("disconnect", reason)
    if client._writer is not None:
        try:
            client._writer.close()
        except Exception:
            pass


def _handle_player_info(client: MinecraftClient, packet: BufferReader) -> None:
    action = packet.read_u8()
    count = packet.read_varint()
    for _ in range(count):
        player_uuid = packet.read_uuid()
        info = client.players.get(player_uuid)
        if action & 0x01:
            name = packet.read_string(16)
            properties = packet.read_varint()
            for _ in range(properties):
                packet.read_string()
                packet.read_string()
                if packet.read_bool():
                    packet.read_string()
            info = PlayerInfo(uuid=player_uuid, name=name)
            client.players[player_uuid] = info
        if action & 0x02:
            if packet.read_bool():
                packet.read_uuid()
                packet.read_i64()
                packet.read(packet.read_varint())
                packet.read(packet.read_varint())
        if action & 0x04:
            game_mode = packet.read_varint()
            if info:
                info.game_mode = game_mode
        if action & 0x08:
            listed = packet.read_bool()
            if info:
                info.listed = listed
        if action & 0x10:
            latency = packet.read_varint()
            if info:
                info.latency = latency
        if action & 0x20:
            if packet.read_bool():
                packet.read_string(1 << 16)
    client._emit("player_info", client.players)


def _handle_player_remove(client: MinecraftClient, packet: BufferReader) -> None:
    count = packet.read_varint()
    for _ in range(count):
        player_uuid = packet.read_uuid()
        client.players.pop(player_uuid, None)
    client._emit("player_info", client.players)


def _handle_spawn_entity(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity_uuid = packet.read_uuid()
    type_id = packet.read_varint()
    x = packet.read_f64()
    y = packet.read_f64()
    z = packet.read_f64()
    pitch = packet.read_i8() * 360.0 / 256.0
    yaw = packet.read_i8() * 360.0 / 256.0
    packet.read_i8()  # head pitch
    packet.read_varint()  # object data
    velocity = Vec3(packet.read_i16() / 8000.0, packet.read_i16() / 8000.0, packet.read_i16() / 8000.0)
    client.entities[entity_id] = Entity(
        entity_id=entity_id,
        type_name=client.registry.entity_name(type_id),
        uuid=entity_uuid,
        position=Vec3(x, y, z),
        yaw=yaw,
        pitch=pitch,
        velocity=velocity,
    )
    client._emit("entity_spawn", client.entities[entity_id])


def _handle_spawn_player(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity_uuid = packet.read_uuid()
    x = packet.read_f64()
    y = packet.read_f64()
    z = packet.read_f64()
    yaw = packet.read_i8() * 360.0 / 256.0
    pitch = packet.read_i8() * 360.0 / 256.0
    client.entities[entity_id] = Entity(
        entity_id=entity_id,
        type_name="minecraft:player",
        uuid=entity_uuid,
        position=Vec3(x, y, z),
        yaw=yaw,
        pitch=pitch,
    )
    client._emit("entity_spawn", client.entities[entity_id])


def _handle_entity_teleport(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity = client.entities.get(entity_id)
    if entity is None:
        return
    entity.position = Vec3(packet.read_f64(), packet.read_f64(), packet.read_f64())
    entity.yaw = packet.read_i8() * 360.0 / 256.0
    entity.pitch = packet.read_i8() * 360.0 / 256.0
    packet.read_bool()


def _handle_rel_move(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity = client.entities.get(entity_id)
    if entity is None:
        return
    dx = packet.read_i16() / 4096.0
    dy = packet.read_i16() / 4096.0
    dz = packet.read_i16() / 4096.0
    entity.position = entity.position + Vec3(dx, dy, dz)
    packet.read_bool()


def _handle_move_look(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity = client.entities.get(entity_id)
    if entity is None:
        return
    dx = packet.read_i16() / 4096.0
    dy = packet.read_i16() / 4096.0
    dz = packet.read_i16() / 4096.0
    entity.position = entity.position + Vec3(dx, dy, dz)
    entity.yaw = packet.read_i8() * 360.0 / 256.0
    entity.pitch = packet.read_i8() * 360.0 / 256.0
    packet.read_bool()


def _handle_look(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity = client.entities.get(entity_id)
    if entity is None:
        return
    entity.yaw = packet.read_i8() * 360.0 / 256.0
    entity.pitch = packet.read_i8() * 360.0 / 256.0
    packet.read_bool()


def _handle_entity_destroy(client: MinecraftClient, packet: BufferReader) -> None:
    count = packet.read_varint()
    for _ in range(count):
        entity_id = packet.read_varint()
        client.entities.pop(entity_id, None)


def _handle_entity_velocity(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    entity = client.entities.get(entity_id)
    if entity is None:
        return
    entity.velocity = Vec3(packet.read_i16() / 8000.0, packet.read_i16() / 8000.0, packet.read_i16() / 8000.0)


def _handle_collect(client: MinecraftClient, packet: BufferReader) -> None:
    collected = packet.read_varint()
    collector = packet.read_varint()
    count = packet.read_varint()
    client.entities.pop(collected, None)
    client._emit("collect", (collected, collector, count))


def _handle_custom_payload(client: MinecraftClient, packet: BufferReader) -> None:
    channel = packet.read_string(256)
    data = packet.read_rest()
    if channel == "minecraft:brand":
        try:
            reader = BufferReader(data)
            client.server_brand = reader.read_string(64)
        except ProtocolError:
            pass
        client._emit("brand", client.server_brand)


def _handle_game_state(client: MinecraftClient, packet: BufferReader) -> None:
    reason = packet.read_u8()
    value = packet.read_f32()
    if reason == 1:
        client.no_respawn_block = True
    elif reason == 0:
        client.no_respawn_block = False
    client._emit("game_state", (reason, value))


def _handle_view_distance(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_varint()


def _handle_view_position(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_varint()
    packet.read_varint()


def _handle_abilities(client: MinecraftClient, packet: BufferReader) -> None:
    flags = packet.read_i8()
    packet.read_f32()
    packet.read_f32()
    client.flying = bool(flags & 0x02)
    client.can_fly = bool(flags & 0x04) or bool(flags & 0x01)


def _handle_spawn_position(client: MinecraftClient, packet: BufferReader) -> None:
    client.spawn_position = packet.read_position()
    packet.read_f32()


def _handle_entity_status(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_i32()
    status = packet.read_i8()
    if status == 3:
        client.entities.pop(entity_id, None)


def _handle_world_event(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_i32()
    packet.read_position()
    packet.read_i32()
    packet.read_bool()


def _handle_difficulty(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_u8()
    packet.read_bool()


def _handle_server_data(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_string(1 << 18)  # motd
    if packet.read_bool():
        packet.read_string(1 << 20)  # icon
    packet.read_bool()  # enforces secure chat


def _handle_damage_event(client: MinecraftClient, packet: BufferReader) -> None:
    entity_id = packet.read_varint()
    source_type = packet.read_varint()
    packet.read_varint()
    packet.read_varint()
    client._emit("damage", entity_id)


def _handle_boss_bar(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_uuid()
    action = packet.read_varint()
    title = ""
    if action == 0:
        title = packets.component_text(packet.read_string(1 << 18))
        packet.read_f32()  # health
        packet.read_varint()  # color
        packet.read_varint()  # division
        packet.read_u8()  # flags
    elif action == 1:
        return
    elif action == 2:
        packet.read_f32()
    elif action == 3:
        title = packets.component_text(packet.read_string(1 << 18))
    elif action == 4:
        packet.read_varint()
        packet.read_varint()
    elif action == 5:
        packet.read_u8()
    if title:
        client._emit("boss_bar", title)


def _handle_block_break_animation(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_varint()
    packet.read_position()
    packet.read_i8()


def _handle_world_particles(client: MinecraftClient, packet: BufferReader) -> None:
    packet.read_i32()
    packet.read_bool()
    packet.read_f64()
    packet.read_f64()
    packet.read_f64()
    packet.read_f32()
    packet.read_i32()


_PLAY_HANDLERS: dict[int, Callable[[MinecraftClient, BufferReader], None]] = {
    CLIENTBOUND["keep_alive"]: _handle_keep_alive,
    CLIENTBOUND["ping"]: _handle_ping,
    CLIENTBOUND["position"]: _handle_position,
    CLIENTBOUND["login"]: _handle_login,
    CLIENTBOUND["respawn"]: _handle_respawn,
    CLIENTBOUND["window_items"]: _handle_window_items,
    CLIENTBOUND["set_slot"]: _handle_set_slot,
    CLIENTBOUND["held_item_slot"]: _handle_held_item,
    CLIENTBOUND["update_health"]: _handle_health,
    CLIENTBOUND["experience"]: _handle_experience,
    CLIENTBOUND["map_chunk"]: _handle_chunk,
    CLIENTBOUND["unload_chunk"]: _handle_unload_chunk,
    CLIENTBOUND["block_change"]: _handle_block_change,
    CLIENTBOUND["multi_block_change"]: _handle_multi_block_change,
    CLIENTBOUND["update_time"]: _handle_time,
    CLIENTBOUND["system_chat"]: _handle_system_chat,
    CLIENTBOUND["player_chat"]: _handle_player_chat,
    CLIENTBOUND["profileless_chat"]: _handle_profileless_chat,
    CLIENTBOUND["action_bar"]: _handle_action_bar,
    CLIENTBOUND["set_title_text"]: _handle_title_text,
    CLIENTBOUND["set_title_subtitle"]: _handle_title_subtitle,
    CLIENTBOUND["playerlist_header"]: _handle_playerlist_header,
    CLIENTBOUND["kick_disconnect"]: _handle_disconnect,
    CLIENTBOUND["player_info"]: _handle_player_info,
    CLIENTBOUND["player_remove"]: _handle_player_remove,
    CLIENTBOUND["spawn_entity"]: _handle_spawn_entity,
    CLIENTBOUND["named_entity_spawn"]: _handle_spawn_player,
    CLIENTBOUND["entity_teleport"]: _handle_entity_teleport,
    CLIENTBOUND["rel_entity_move"]: _handle_rel_move,
    CLIENTBOUND["entity_move_look"]: _handle_move_look,
    CLIENTBOUND["entity_look"]: _handle_look,
    CLIENTBOUND["entity_destroy"]: _handle_entity_destroy,
    CLIENTBOUND["entity_velocity"]: _handle_entity_velocity,
    CLIENTBOUND["collect"]: _handle_collect,
    CLIENTBOUND["custom_payload"]: _handle_custom_payload,
    CLIENTBOUND["game_state_change"]: _handle_game_state,
    CLIENTBOUND["update_view_distance"]: _handle_view_distance,
    CLIENTBOUND["update_view_position"]: _handle_view_position,
    CLIENTBOUND["abilities"]: _handle_abilities,
    CLIENTBOUND["spawn_position"]: _handle_spawn_position,
    CLIENTBOUND["entity_status"]: _handle_entity_status,
    CLIENTBOUND["world_event"]: _handle_world_event,
    CLIENTBOUND["difficulty"]: _handle_difficulty,
    CLIENTBOUND["server_data"]: _handle_server_data,
    CLIENTBOUND["damage_event"]: _handle_damage_event,
    CLIENTBOUND["boss_bar"]: _handle_boss_bar,
    CLIENTBOUND["block_break_animation"]: _handle_block_break_animation,
    CLIENTBOUND["world_particles"]: _handle_world_particles,
}

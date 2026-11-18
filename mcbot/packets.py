"""1.20.1（协议 763）包 ID 表与客户端上行包构造函数。

ID 来自 minecraft-data 的 protocol.json，见 ``tools/`` 目录下的数据生成脚本。
"""

from __future__ import annotations

import json
import uuid

from .buffer import BufferWriter
from .types import BlockPos

__all__ = [
    "PROTOCOL_VERSION",
    "CLIENTBOUND",
    "SERVERBOUND",
    "component_text",
    "handshake",
    "status_request",
    "status_ping",
    "login_start",
    "encryption_response",
    "client_information",
    "chat_message",
    "chat_command",
    "player_position",
    "player_position_rotation",
    "player_rotation",
    "player_on_ground",
    "player_action",
    "interact_entity",
    "use_item_on",
    "use_item",
    "held_item_slot",
    "keep_alive",
    "pong",
    "teleport_confirm",
    "client_command",
    "swing_arm",
    "player_command",
]

PROTOCOL_VERSION = 763

#: 客户端接收（play 状态）
CLIENTBOUND: dict[str, int] = {
    "bundle_delimiter": 0x00,
    "spawn_entity": 0x01,
    "spawn_entity_experience_orb": 0x02,
    "named_entity_spawn": 0x03,
    "animation": 0x04,
    "statistics": 0x05,
    "acknowledge_player_digging": 0x06,
    "block_break_animation": 0x07,
    "tile_entity_data": 0x08,
    "block_action": 0x09,
    "block_change": 0x0A,
    "boss_bar": 0x0B,
    "difficulty": 0x0C,
    "chunk_biomes": 0x0D,
    "clear_titles": 0x0E,
    "tab_complete": 0x0F,
    "declare_commands": 0x10,
    "close_window": 0x11,
    "window_items": 0x12,
    "craft_progress_bar": 0x13,
    "set_slot": 0x14,
    "set_cooldown": 0x15,
    "chat_suggestions": 0x16,
    "custom_payload": 0x17,
    "damage_event": 0x18,
    "hide_message": 0x19,
    "kick_disconnect": 0x1A,
    "profileless_chat": 0x1B,
    "entity_status": 0x1C,
    "explosion": 0x1D,
    "unload_chunk": 0x1E,
    "game_state_change": 0x1F,
    "open_horse_window": 0x20,
    "hurt_animation": 0x21,
    "initialize_world_border": 0x22,
    "keep_alive": 0x23,
    "map_chunk": 0x24,
    "world_event": 0x25,
    "world_particles": 0x26,
    "update_light": 0x27,
    "login": 0x28,
    "map": 0x29,
    "trade_list": 0x2A,
    "rel_entity_move": 0x2B,
    "entity_move_look": 0x2C,
    "entity_look": 0x2D,
    "vehicle_move": 0x2E,
    "open_book": 0x2F,
    "open_window": 0x30,
    "open_sign_entity": 0x31,
    "ping": 0x32,
    "craft_recipe_response": 0x33,
    "abilities": 0x34,
    "player_chat": 0x35,
    "end_combat_event": 0x36,
    "enter_combat_event": 0x37,
    "death_combat_event": 0x38,
    "player_remove": 0x39,
    "player_info": 0x3A,
    "face_player": 0x3B,
    "position": 0x3C,
    "unlock_recipes": 0x3D,
    "entity_destroy": 0x3E,
    "remove_entity_effect": 0x3F,
    "resource_pack_send": 0x40,
    "respawn": 0x41,
    "entity_head_rotation": 0x42,
    "multi_block_change": 0x43,
    "select_advancement_tab": 0x44,
    "server_data": 0x45,
    "action_bar": 0x46,
    "world_border_center": 0x47,
    "world_border_lerp_size": 0x48,
    "world_border_size": 0x49,
    "world_border_warning_delay": 0x4A,
    "world_border_warning_reach": 0x4B,
    "camera": 0x4C,
    "held_item_slot": 0x4D,
    "update_view_position": 0x4E,
    "update_view_distance": 0x4F,
    "spawn_position": 0x50,
    "scoreboard_display_objective": 0x51,
    "entity_metadata": 0x52,
    "attach_entity": 0x53,
    "entity_velocity": 0x54,
    "entity_equipment": 0x55,
    "experience": 0x56,
    "update_health": 0x57,
    "scoreboard_objective": 0x58,
    "set_passengers": 0x59,
    "teams": 0x5A,
    "scoreboard_score": 0x5B,
    "simulation_distance": 0x5C,
    "set_title_subtitle": 0x5D,
    "update_time": 0x5E,
    "set_title_text": 0x5F,
    "set_title_time": 0x60,
    "entity_sound_effect": 0x61,
    "sound_effect": 0x62,
    "stop_sound": 0x63,
    "system_chat": 0x64,
    "playerlist_header": 0x65,
    "nbt_query_response": 0x66,
    "collect": 0x67,
    "entity_teleport": 0x68,
    "advancements": 0x69,
    "entity_update_attributes": 0x6A,
    "feature_flags": 0x6B,
    "entity_effect": 0x6C,
    "declare_recipes": 0x6D,
    "tags": 0x6E,
}

#: 客户端发送（play 状态）
SERVERBOUND: dict[str, int] = {
    "teleport_confirm": 0x00,
    "query_block_nbt": 0x01,
    "set_difficulty": 0x02,
    "message_acknowledgement": 0x03,
    "chat_command": 0x04,
    "chat_message": 0x05,
    "chat_session_update": 0x06,
    "client_command": 0x07,
    "settings": 0x08,
    "tab_complete": 0x09,
    "enchant_item": 0x0A,
    "window_click": 0x0B,
    "close_window": 0x0C,
    "custom_payload": 0x0D,
    "edit_book": 0x0E,
    "query_entity_nbt": 0x0F,
    "use_entity": 0x10,
    "generate_structure": 0x11,
    "keep_alive": 0x12,
    "lock_difficulty": 0x13,
    "position": 0x14,
    "position_look": 0x15,
    "look": 0x16,
    "flying": 0x17,
    "vehicle_move": 0x18,
    "steer_boat": 0x19,
    "pick_item": 0x1A,
    "craft_recipe_request": 0x1B,
    "abilities": 0x1C,
    "block_dig": 0x1D,
    "entity_action": 0x1E,
    "steer_vehicle": 0x1F,
    "pong": 0x20,
    "recipe_book": 0x21,
    "displayed_recipe": 0x22,
    "name_item": 0x23,
    "resource_pack_receive": 0x24,
    "advancement_tab": 0x25,
    "select_trade": 0x26,
    "set_beacon_effect": 0x27,
    "held_item_slot": 0x28,
    "update_command_block": 0x29,
    "update_command_block_minecart": 0x2A,
    "set_creative_slot": 0x2B,
    "update_jigsaw_block": 0x2C,
    "update_structure_block": 0x2D,
    "update_sign": 0x2E,
    "arm_animation": 0x2F,
    "spectate": 0x30,
    "block_place": 0x31,
    "use_item": 0x32,
}

#: 登录阶段
LOGIN_SERVERBOUND = {
    "login_start": 0x00,
    "encryption_response": 0x01,
    "login_plugin_response": 0x02,
}
LOGIN_CLIENTBOUND = {
    "disconnect": 0x00,
    "encryption_request": 0x01,
    "success": 0x02,
    "compress": 0x03,
    "login_plugin_request": 0x04,
}

HANDSHAKE = 0x00
STATUS_REQUEST = 0x00
STATUS_PING = 0x01


# --------------------------------------------------------------------------- 文本
def component_text(raw: str | None) -> str:
    """把 JSON 聊天组件（或纯文本）压平成可读字符串。"""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return str(raw)
    return _flatten(data)


def _flatten(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten(item) for item in node)
    if not isinstance(node, dict):
        return str(node)
    parts = []
    if "text" in node:
        parts.append(str(node["text"]))
    elif "translate" in node:
        parts.append(str(node["translate"]))
        arguments = node.get("with")
        if isinstance(arguments, list):
            parts.append("[" + ", ".join(_flatten(arg) for arg in arguments) + "]")
    if "extra" in node:
        parts.append(_flatten(node["extra"]))
    return "".join(parts)


# --------------------------------------------------------------------------- 构造
def handshake(host: str, port: int, next_state: int) -> bytes:
    return (
        BufferWriter()
        .write_varint(PROTOCOL_VERSION)
        .write_string(host)
        .write_u16(port)
        .write_varint(next_state)
        .to_bytes()
    )


def status_request() -> bytes:
    return b""


def status_ping(payload: int) -> bytes:
    return BufferWriter().write_i64(payload).to_bytes()


def login_start(username: str, player_uuid: uuid.UUID) -> bytes:
    # 1.20.1 的 Login Start 里 UUID 是 option<UUID>：先写 bool 表示存在，再写 16 字节 UUID。
    # 漏掉这个 bool 会让服务器/ViaProxy 把 UUID 的第一个字节当成 bool，
    # 随后读 UUID 的 long 时越界（readerIndex + 8 > writerIndex）。
    return (
        BufferWriter()
        .write_string(username)
        .write_bool(True)
        .write_uuid(player_uuid)
        .to_bytes()
    )


def encryption_response(encrypted_secret: bytes, encrypted_token: bytes) -> bytes:
    return (
        BufferWriter()
        .write_varint(len(encrypted_secret))
        .write(encrypted_secret)
        .write_varint(len(encrypted_token))
        .write(encrypted_token)
        .to_bytes()
    )


def client_information(
    locale: str = "zh_CN",
    view_distance: int = 8,
    chat_mode: int = 0,
    chat_colors: bool = True,
    skin_parts: int = 0x7F,
    main_hand: int = 1,
) -> bytes:
    return (
        BufferWriter()
        .write_string(locale)
        .write_i8(max(2, min(32, view_distance)))
        .write_varint(chat_mode)
        .write_bool(chat_colors)
        .write_u8(skin_parts)
        .write_varint(main_hand)
        .write_bool(False)  # enableTextFiltering
        .write_bool(True)  # enableServerListing
        .to_bytes()
    )


def chat_message(text: str, timestamp: int, salt: int) -> bytes:
    return (
        BufferWriter()
        .write_string(text[:256])
        .write_i64(timestamp)
        .write_i64(salt)
        .write_bool(False)  # 无签名
        .write_varint(0)  # offset
        .write(b"\x00\x00\x00")  # acknowledged bitset
        .to_bytes()
    )


def chat_command(command: str, timestamp: int, salt: int) -> bytes:
    return (
        BufferWriter()
        .write_string(command[:256])
        .write_i64(timestamp)
        .write_i64(salt)
        .write_varint(0)  # argumentSignatures
        .write_varint(0)  # messageCount
        .write(b"\x00\x00\x00")  # acknowledged
        .to_bytes()
    )


def player_position(x: float, y: float, z: float, on_ground: bool) -> bytes:
    return BufferWriter().write_f64(x).write_f64(y).write_f64(z).write_bool(on_ground).to_bytes()


def player_position_rotation(x: float, y: float, z: float, yaw: float, pitch: float, on_ground: bool) -> bytes:
    return (
        BufferWriter()
        .write_f64(x)
        .write_f64(y)
        .write_f64(z)
        .write_f32(yaw)
        .write_f32(pitch)
        .write_bool(on_ground)
        .to_bytes()
    )


def player_rotation(yaw: float, pitch: float, on_ground: bool) -> bytes:
    return BufferWriter().write_f32(yaw).write_f32(pitch).write_bool(on_ground).to_bytes()


def player_on_ground(on_ground: bool) -> bytes:
    return BufferWriter().write_bool(on_ground).to_bytes()


def player_action(status: int, position: BlockPos, face: int, sequence: int) -> bytes:
    return (
        BufferWriter()
        .write_varint(status)
        .write_position(position)
        .write_i8(face)
        .write_varint(sequence)
        .to_bytes()
    )


def interact_entity(entity_id: int, action: int = 1, position: tuple[float, float, float] | None = None, sneaking: bool = False) -> bytes:
    """action: 0=交互, 1=攻击, 2=在指定点交互。"""
    writer = BufferWriter().write_varint(entity_id).write_varint(action)
    if action == 2:
        x, y, z = position or (0.0, 0.0, 0.0)
        writer.write_f32(x).write_f32(y).write_f32(z).write_varint(0)
    return writer.write_bool(sneaking).to_bytes()


def use_item_on(hand: int, position: BlockPos, face: int, cursor: tuple[float, float, float], sequence: int, inside_block: bool = False) -> bytes:
    cursor_x, cursor_y, cursor_z = cursor
    return (
        BufferWriter()
        .write_varint(hand)
        .write_position(position)
        .write_varint(face)
        .write_f32(cursor_x)
        .write_f32(cursor_y)
        .write_f32(cursor_z)
        .write_bool(inside_block)
        .write_varint(sequence)
        .to_bytes()
    )


def use_item(hand: int, sequence: int) -> bytes:
    return BufferWriter().write_varint(hand).write_varint(sequence).to_bytes()


def held_item_slot(slot: int) -> bytes:
    return BufferWriter().write_i16(slot).to_bytes()


def keep_alive(keep_alive_id: int) -> bytes:
    return BufferWriter().write_i64(keep_alive_id).to_bytes()


def pong(ping_id: int) -> bytes:
    return BufferWriter().write_i32(ping_id).to_bytes()


def teleport_confirm(teleport_id: int) -> bytes:
    return BufferWriter().write_varint(teleport_id).to_bytes()


def client_command(action: int) -> bytes:
    return BufferWriter().write_varint(action).to_bytes()


def swing_arm(hand: int = 0) -> bytes:
    return BufferWriter().write_varint(hand).to_bytes()


def player_command(entity_id: int, action: int, jump_boost: int = 0) -> bytes:
    return BufferWriter().write_varint(entity_id).write_varint(action).write_varint(jump_boost).to_bytes()

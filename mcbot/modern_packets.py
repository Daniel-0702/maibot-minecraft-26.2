"""Minecraft 26.2 / protocol 776. IDs copied from Mojang packets report.
Codecs verified with JDK25 javap against the official 26.2 server jar.
No dependency on research files at runtime.
"""
from __future__ import annotations
import json, uuid
from .buffer import BufferReader, BufferWriter, ProtocolError
from .types import BlockPos, Vec3
from . import nbt
PROTOCOL_VERSION = 776


def read_nbt(reader):
    """26.2 network NBT has a type byte but NO root name."""
    kind = reader.read_u8()
    if kind == 0:
        return None
    try:
        return nbt._read_payload(nbt._Input(reader), kind, 0)
    except (ValueError, IndexError) as exc:
        raise ProtocolError(f"Invalid unnamed NBT: {exc}") from exc


def component(reader):
    from .packets import _flatten
    return _flatten(nbt.to_python(read_nbt(reader)))


def lp_vec3(reader):
    first = reader.read_u8()
    if not first:
        return Vec3()
    packed = first | (reader.read_u8() << 8) | (reader.read_u32() << 16)
    scale = first & 3
    if first & 4:
        scale |= (reader.read_varint() & 0xffffffff) << 2
    return Vec3(*( (min((packed >> shift) & 32767, 32766) * 2 / 32766 - 1) * scale for shift in (3, 18, 33)))


def handshake(host, port, next_state=2):
    return BufferWriter().write_varint(776).write_string(host).write_u16(port).write_varint(next_state).to_bytes()


def login_start(name, identity):
    return BufferWriter().write_string(name).write_uuid(identity).to_bytes()


def client_information(view_distance=8):
    # ClientInformation: locale, distance, visibility, colors, skin, hand,
    # filtering, listing, ParticleStatus enum (ALL=0).
    return BufferWriter().write_string('zh_cn').write_i8(view_distance).write_varint(0).write_bool(True).write_u8(127).write_varint(1).write_bool(False).write_bool(True).write_varint(0).to_bytes()


def use_item_on(hand, position, face, cursor, sequence):
    return BufferWriter().write_varint(hand).write_position(position).write_varint(face).write_f32(cursor[0]).write_f32(cursor[1]).write_f32(cursor[2]).write_bool(False).write_bool(False).write_varint(sequence).to_bytes()


def use_item(hand, sequence, yaw, pitch):
    return BufferWriter().write_varint(hand).write_varint(sequence).write_f32(yaw).write_f32(pitch).to_bytes()


def chat_message(text, timestamp, offset=0):
    # No signature; LastSeenMessages.Update includes checksum byte since 1.21.5.
    return BufferWriter().write_string(text[:256]).write_i64(timestamp).write_i64(0).write_bool(False).write_varint(offset).write(b'\x00\x00\x00\x00').to_bytes()


class UnsupportedComponent(ProtocolError):
    pass


def decode_slot(reader, component_names=None):
    """Transactional callers must discard the entire packet on unknown codecs.

    OPTIONAL_STREAM_CODEC uses un-delimited component values; guessing a size
    would silently corrupt subsequent slots. Unknown additions therefore fail
    explicitly, never masquerade as empty stacks.
    """
    from .client import ItemStack
    count = reader.read_varint()
    if count == 0:
        return None
    if count < 0:
        raise ProtocolError('Negative stack count')
    item_id = reader.read_varint()
    added, removed = reader.read_varint(), reader.read_varint()
    if not 0 <= added <= 4096 or not 0 <= removed <= 4096:
        raise ProtocolError('Invalid component patch size')
    values = {}
    # Official DataComponents registration order, stable for this exact version.
    names = component_names or {0:'custom_data', 1:'max_stack_size', 2:'max_damage', 3:'damage', 4:'unbreakable', 5:'use_effects', 6:'custom_name', 7:'minimum_attack_charge', 8:'damage_type', 9:'item_name', 10:'item_model', 11:'lore', 12:'rarity'}
    for _ in range(added):
        component_id = reader.read_varint()
        name = names.get(component_id, f'#{component_id}').removeprefix('minecraft:')
        if name in {'max_stack_size','max_damage','damage','repair_cost','rarity','enchantable','map_id','ominous_bottle_amplifier'}:
            value = reader.read_varint()
        elif name in {'custom_data','custom_name','item_name','intangible_projectile','entity_data','block_entity_data','bucket_entity_data','lock','recipes'}:
            value = read_nbt(reader)
        elif name in {'unbreakable','creative_slot_lock','glider','hide_additional_tooltip','hide_tooltip'}:
            value = True
        elif name == 'enchantment_glint_override':
            value = reader.read_bool()
        else:
            raise UnsupportedComponent(f'Unsupported 26.2 item component {name} (id={component_id}); discarded whole inventory packet')
        values[name] = value
    values['removed_components'] = [reader.read_varint() for _ in range(removed)]
    return ItemStack(item_id, count, values)


#: 26.2 的 ServerboundContainerClickPacket 传输的是 HashedStack（只有"哈希后"的
#: 组件补丁），不是完整 ItemStack。javap 核实：
#:   ActualItem = (holderRegistry(ITEM) VarInt, VAR_INT count, HashedPatchMap)
#:   HashedPatchMap = (map<component, INT>, collection<component>)，空补丁就是两个 0。
#: 因此普通物品（没有改过组件）可以直接编码；带自定义组件的物品不能用来点击。
def encode_hashed_stack(stack, writer=None):
    """把 ItemStack 编码成 26.2 点击包里的 HashedStack。"""
    writer = writer or BufferWriter()
    if stack is None or getattr(stack, 'is_empty', False) or stack.count <= 0:
        writer.write_bool(False)
        return writer.to_bytes()
    components = getattr(stack, 'nbt', None)
    if components:
        raise UnsupportedComponent(
            '带自定义组件的物品无法用 HashedStack 发送（点击背包会失败）'
        )
    writer.write_bool(True)
    writer.write_varint(int(stack.item_id))
    writer.write_varint(int(stack.count))
    writer.write_varint(0)  # added component patch map
    writer.write_varint(0)  # removed component types
    return writer.to_bytes()


#: 容器点击类型（ContainerInput 枚举 id，javap 核实）
CONTAINER_INPUT = {
    'pickup': 0,
    'quick_move': 1,
    'swap': 2,
    'clone': 3,
    'throw': 4,
    'quick_craft': 5,
    'pickup_all': 6,
}


def container_click(container_id, state_id, slot, *, button=0, click='pickup',
                    carried=None, changed=None):
    """构造 26.2 的 ``ServerboundContainerClickPacket``。

    ``changed`` 是 ``{slot: ItemStack}``（通常留空，服务器自己算）。
    """
    writer = BufferWriter()
    writer.write_varint(int(container_id))
    writer.write_varint(int(state_id))
    writer.write_i16(int(slot))
    writer.write_i8(int(button))
    writer.write_varint(CONTAINER_INPUT.get(click, 0))
    changed = changed or {}
    writer.write_varint(len(changed))
    for changed_slot, stack in changed.items():
        writer.write_i16(int(changed_slot))
        writer.write(encode_hashed_stack(stack))
    writer.write(encode_hashed_stack(carried))
    return writer.to_bytes()


def container_close(container_id):
    """``ServerboundContainerClosePacket``。"""
    return BufferWriter().write_varint(int(container_id)).to_bytes()


LOGIN_CLIENTBOUND = {
    "cookie_request": 5,
    "custom_query": 4,
    "hello": 1,
    "login_compression": 3,
    "login_disconnect": 0,
    "login_finished": 2,
}
LOGIN_SERVERBOUND = {
    "cookie_response": 4,
    "custom_query_answer": 2,
    "hello": 0,
    "key": 1,
    "login_acknowledged": 3,
}
CONFIGURATION_CLIENTBOUND = {
    "clear_dialog": 17,
    "code_of_conduct": 19,
    "cookie_request": 0,
    "custom_payload": 1,
    "custom_report_details": 15,
    "disconnect": 2,
    "finish_configuration": 3,
    "keep_alive": 4,
    "ping": 5,
    "registry_data": 7,
    "reset_chat": 6,
    "resource_pack_pop": 8,
    "resource_pack_push": 9,
    "select_known_packs": 14,
    "server_links": 16,
    "show_dialog": 18,
    "store_cookie": 10,
    "transfer": 11,
    "update_enabled_features": 12,
    "update_tags": 13,
}
CONFIGURATION_SERVERBOUND = {
    "accept_code_of_conduct": 9,
    "client_information": 0,
    "cookie_response": 1,
    "custom_click_action": 8,
    "custom_payload": 2,
    "finish_configuration": 3,
    "keep_alive": 4,
    "pong": 5,
    "resource_pack": 6,
    "select_known_packs": 7,
}
PLAY_CLIENTBOUND = {
    "add_entity": 1,
    "animate": 2,
    "award_stats": 3,
    "block_changed_ack": 4,
    "block_destruction": 5,
    "block_entity_data": 6,
    "block_event": 7,
    "block_update": 8,
    "boss_event": 9,
    "bundle_delimiter": 0,
    "change_difficulty": 10,
    "chunk_batch_finished": 11,
    "chunk_batch_start": 12,
    "chunks_biomes": 13,
    "clear_dialog": 139,
    "clear_titles": 14,
    "command_suggestions": 15,
    "commands": 16,
    "container_close": 17,
    "container_set_content": 18,
    "container_set_data": 19,
    "container_set_slot": 20,
    "cookie_request": 21,
    "cooldown": 22,
    "custom_chat_completions": 23,
    "custom_payload": 24,
    "custom_report_details": 136,
    "damage_event": 25,
    "debug/block_value": 26,
    "debug/chunk_value": 27,
    "debug/entity_value": 28,
    "debug/event": 29,
    "debug_sample": 30,
    "delete_chat": 31,
    "disconnect": 32,
    "disguised_chat": 33,
    "entity_event": 34,
    "entity_position_sync": 35,
    "explode": 36,
    "forget_level_chunk": 37,
    "game_event": 38,
    "game_rule_values": 39,
    "game_test_highlight_pos": 40,
    "hurt_animation": 42,
    "initialize_border": 43,
    "keep_alive": 44,
    "level_chunk_with_light": 45,
    "level_event": 46,
    "level_particles": 47,
    "light_update": 48,
    "login": 49,
    "low_disk_space_warning": 50,
    "map_item_data": 51,
    "merchant_offers": 52,
    "mount_screen_open": 41,
    "move_entity_pos": 53,
    "move_entity_pos_rot": 54,
    "move_entity_rot": 56,
    "move_minecart_along_track": 55,
    "move_vehicle": 57,
    "open_book": 58,
    "open_screen": 59,
    "open_sign_editor": 60,
    "ping": 61,
    "place_ghost_recipe": 63,
    "player_abilities": 64,
    "player_chat": 65,
    "player_combat_end": 66,
    "player_combat_enter": 67,
    "player_combat_kill": 68,
    "player_info_remove": 69,
    "player_info_update": 70,
    "player_look_at": 71,
    "player_position": 72,
    "player_rotation": 73,
    "pong_response": 62,
    "projectile_power": 135,
    "recipe_book_add": 74,
    "recipe_book_remove": 75,
    "recipe_book_settings": 76,
    "remove_entities": 77,
    "remove_mob_effect": 78,
    "reset_score": 79,
    "resource_pack_pop": 80,
    "resource_pack_push": 81,
    "respawn": 82,
    "rotate_head": 83,
    "section_blocks_update": 84,
    "select_advancements_tab": 85,
    "server_data": 86,
    "server_links": 137,
    "set_action_bar_text": 87,
    "set_border_center": 88,
    "set_border_lerp_size": 89,
    "set_border_size": 90,
    "set_border_warning_delay": 91,
    "set_border_warning_distance": 92,
    "set_camera": 93,
    "set_chunk_cache_center": 94,
    "set_chunk_cache_radius": 95,
    "set_cursor_item": 96,
    "set_default_spawn_position": 97,
    "set_display_objective": 98,
    "set_entity_data": 99,
    "set_entity_link": 100,
    "set_entity_motion": 101,
    "set_equipment": 102,
    "set_experience": 103,
    "set_health": 104,
    "set_held_slot": 105,
    "set_objective": 106,
    "set_passengers": 107,
    "set_player_inventory": 108,
    "set_player_team": 109,
    "set_score": 110,
    "set_simulation_distance": 111,
    "set_subtitle_text": 112,
    "set_time": 113,
    "set_title_text": 114,
    "set_titles_animation": 115,
    "show_dialog": 140,
    "sound": 117,
    "sound_entity": 116,
    "start_configuration": 118,
    "stop_sound": 119,
    "store_cookie": 120,
    "system_chat": 121,
    "tab_list": 122,
    "tag_query": 123,
    "take_item_entity": 124,
    "teleport_entity": 125,
    "test_instance_block_status": 126,
    "ticking_state": 127,
    "ticking_step": 128,
    "transfer": 129,
    "update_advancements": 130,
    "update_attributes": 131,
    "update_mob_effect": 132,
    "update_recipes": 133,
    "update_tags": 134,
    "waypoint": 138,
}
PLAY_SERVERBOUND = {
    "accept_teleportation": 0,
    "attack": 1,
    "block_entity_tag_query": 2,
    "bundle_item_selected": 3,
    "change_difficulty": 4,
    "change_game_mode": 5,
    "chat": 9,
    "chat_ack": 6,
    "chat_command": 7,
    "chat_command_signed": 8,
    "chat_session_update": 10,
    "chunk_batch_received": 11,
    "client_command": 12,
    "client_information": 14,
    "client_tick_end": 13,
    "command_suggestion": 15,
    "configuration_acknowledged": 16,
    "container_button_click": 17,
    "container_click": 18,
    "container_close": 19,
    "container_slot_state_changed": 20,
    "cookie_response": 21,
    "custom_click_action": 68,
    "custom_payload": 22,
    "debug_subscription_request": 23,
    "edit_book": 24,
    "entity_tag_query": 25,
    "interact": 26,
    "jigsaw_generate": 27,
    "keep_alive": 28,
    "lock_difficulty": 29,
    "move_player_pos": 30,
    "move_player_pos_rot": 31,
    "move_player_rot": 32,
    "move_player_status_only": 33,
    "move_vehicle": 34,
    "paddle_boat": 35,
    "pick_item_from_block": 36,
    "pick_item_from_entity": 37,
    "ping_request": 38,
    "place_recipe": 39,
    "player_abilities": 40,
    "player_action": 41,
    "player_command": 42,
    "player_input": 43,
    "player_loaded": 44,
    "pong": 45,
    "recipe_book_change_settings": 46,
    "recipe_book_seen_recipe": 47,
    "rename_item": 48,
    "resource_pack": 49,
    "seen_advancements": 50,
    "select_trade": 51,
    "set_beacon": 52,
    "set_carried_item": 53,
    "set_command_block": 54,
    "set_command_minecart": 55,
    "set_creative_mode_slot": 56,
    "set_game_rule": 57,
    "set_jigsaw_block": 58,
    "set_structure_block": 59,
    "set_test_block": 60,
    "sign_update": 61,
    "spectator_action": 62,
    "swing": 63,
    "teleport_to_entity": 64,
    "test_instance_block_action": 65,
    "use_item": 67,
    "use_item_on": 66,
}

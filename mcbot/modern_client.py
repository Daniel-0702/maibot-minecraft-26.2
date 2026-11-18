"""Native Minecraft 26.2 (776) client; no Via proxy or legacy codec fallback.

Official codec evidence: research-26.2/client-work/{core,more,extra,actions}.txt.
Unsupported un-delimited item components discard their packet transactionally.
Signed outgoing chat, transfer, dialogs and inventory clicks are not implemented.
"""
from __future__ import annotations
import asyncio
import math
import time
import uuid
from pathlib import Path
from . import modern_packets as p, packets as legacy
from . import client as base
from .auth import AuthSession
from .buffer import BufferReader as R, BufferWriter as W, ProtocolError
from .registry import BlockRegistry
from .types import Vec3, BlockPos, wrap_degrees


class ModernMinecraftClient(base.MinecraftClient):
    def __init__(self, *args, **kwargs):
        if kwargs.get('registry') is None:
            kwargs['registry'] = BlockRegistry(Path(__file__).parent / 'data' / 'mc_26_2.json')
        super().__init__(*args, **kwargs)
        self.protocol_version = 776
        self.registries = {}
        self.velocity = Vec3()
        self.inventory_errors = 0
        self.last_inventory_error = ''
        self._player_loaded = False
        self._chat_offset = 0
        self._input = 0
        #: 各容器窗口的最新 state id 与槽位内容（0 = 玩家背包，含 2x2 合成格）
        self.window_state: dict[int, int] = {}
        self.window_items: dict[int, dict[int, object]] = {}
        #: 打开的界面（合成台等）；None 表示没有打开
        self.crafting_window: dict | None = None
        #: 收到过多少次容器内容包（合成执行器用它判断"服务器同步完了"）
        self.inventory_packets = 0
        #: 区块光照：(chunk_x, chunk_z) → ChunkLight
        self.block_light: dict[tuple[int, int], object] = {}
        self.sky_light: dict[tuple[int, int], object] = {}
        self.light_errors = 0
        self.last_light_error = ''

    @property
    def connected(self):
        return self.state in {'login','configuration','play'} and self._writer is not None

    async def connect(self, timeout=30.0):
        if self.connected:
            raise base.ClientError('Already connected')
        self._closed.clear()
        self.state = 'connecting'
        self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port, limit=1 << 22), timeout)
        try:
            await asyncio.wait_for(self._login(), timeout)
        except Exception:
            await self.disconnect('登录失败')
            raise
        self._read_task = asyncio.create_task(self._read_loop())
        self._emit('login', self)

    def _write_frame(self, packet_id, payload=b''):
        # Legacy transport incorrectly treats threshold=0 as "never compress".
        import zlib
        if self._writer is None: raise base.ConnectionClosed('连接尚未建立')
        body = W().write_varint(packet_id).write(payload).to_bytes()
        if self._compression is not None:
            inner = (W().write_varint(len(body)).write(zlib.compress(body)).to_bytes()
                     if len(body) >= self._compression else W().write_varint(0).write(body).to_bytes())
        else: inner = body
        frame = W().write_varint(len(inner)).write(inner).to_bytes()
        if self._cipher is not None: frame = self._cipher.encrypt(frame)
        self._writer.write(frame)

    async def _handle_encryption_request(self, r):
        import secrets
        from .auth import join_server
        from .crypto import encrypt_rsa, make_cipher, server_hash
        server_id = r.read_string(20)
        key = r.read(r.read_varint()); token = r.read(r.read_varint())
        authenticate = r.read_bool()
        secret = secrets.token_bytes(16)
        if authenticate:
            if self.session is None or not self.session.access_token:
                raise base.LoginFailed('正版服务器需要微软账号 access token')
            await join_server(self.session, server_hash(server_id,secret,key))
        response = legacy.encryption_response(encrypt_rsa(key,secret),encrypt_rsa(key,token))
        self._write_frame(p.LOGIN_SERVERBOUND['key'],response)
        await self._writer.drain()
        self._cipher = make_cipher(secret)

    def _client_information(self):
        return p.client_information(self.view_distance)

    async def _login(self):
        self.state = 'login'
        import hashlib
        offline_uuid = uuid.UUID(bytes=hashlib.md5(('OfflinePlayer:' + self.username).encode()).digest(), version=3)
        session = self.session or AuthSession(username=self.username, uuid=offline_uuid)
        self._write_frame(0, p.handshake(self.handshake_host, self.port))
        self._write_frame(p.LOGIN_SERVERBOUND['hello'], p.login_start(self.username, session.uuid))
        await self._writer.drain()
        while self.state == 'login':
            pid, r = await self._read_frame()
            name = next((k for k,v in p.LOGIN_CLIENTBOUND.items() if v == pid), '')
            if name == 'login_disconnect':
                raise base.LoginFailed(legacy.component_text(r.read_string()))
            elif name == 'hello':
                # Added shouldAuthenticate flag follows token; inherited RSA handler
                # consumes exact original fields and authenticates online accounts.
                await self._handle_encryption_request(r)
            elif name == 'login_compression':
                self._compression = r.read_varint()
            elif name == 'custom_query':
                self._write_frame(p.LOGIN_SERVERBOUND['custom_query_answer'], W().write_varint(r.read_varint()).write_bool(False).to_bytes())
            elif name == 'cookie_request':
                self._cookie(r, p.LOGIN_SERVERBOUND)
            elif name == 'login_finished':
                identity, username = r.read_uuid(), r.read_string(16)
                self.session = AuthSession(username=username, uuid=identity, access_token=session.access_token, source=session.source, expires_at=session.expires_at)
                self._write_frame(p.LOGIN_SERVERBOUND['login_acknowledged'])
                self.state = 'configuration'
                self._write_frame(p.CONFIGURATION_SERVERBOUND['client_information'], self._client_information())
            else:
                raise ProtocolError(f'Unknown login packet {pid}')
            await self._writer.drain()
        while self.state == 'configuration':
            pid, r = await self._read_frame()
            self._configuration(pid, r)
            await self._writer.drain()

    def _cookie(self, r, table):
        # Never persist or replay server cookies; explicit absent response.
        self._write_frame(table['cookie_response'], W().write_string(r.read_string()).write_bool(False).to_bytes())

    def _resource_pack(self, r, table):
        identity = r.read_uuid()
        r.read_string(); r.read_string(); required = r.read_bool()
        if r.read_bool():
            p.component(r)
        self._write_frame(table['resource_pack'], W().write_uuid(identity).write_varint(1).to_bytes())  # DECLINED
        self.log('拒绝资源包' + ('（服务器要求必须安装，可能断开）' if required else ''))

    def _registry(self, r):
        key = r.read_string()
        count = r.read_varint()
        if not 0 <= count <= 1 << 20:
            raise ProtocolError('Invalid registry size')
        entries = []
        for _ in range(count):
            name = r.read_string()
            data = p.read_nbt(r) if r.read_bool() else None
            entries.append((name, data))
        self.registries[key] = entries
        self._emit('registry', (key, entries))

    def _configuration(self, pid, r):
        table = p.CONFIGURATION_SERVERBOUND
        name = next((k for k,v in p.CONFIGURATION_CLIENTBOUND.items() if v == pid), '')
        if name == 'registry_data': self._registry(r)
        elif name == 'select_known_packs':
            # Empty intersection forces server to transmit all registry values.
            for _ in range(r.read_varint()):
                r.read_string(); r.read_string(); r.read_string()
            self._write_frame(table[name], b'\x00')
        elif name == 'keep_alive': self._write_frame(table[name], r.read(8))
        elif name == 'ping': self._write_frame(table['pong'], r.read(4))
        elif name == 'cookie_request': self._cookie(r, table)
        elif name == 'resource_pack_push': self._resource_pack(r, table)
        elif name == 'custom_payload': base._handle_custom_payload(self, r)
        elif name == 'finish_configuration':
            self._write_frame(table[name]); self.state = 'play'
        elif name == 'disconnect': raise base.LoginFailed(p.component(r))
        elif name == 'transfer': raise ProtocolError('Server transfer unsupported (not followed automatically)')
        elif name == 'code_of_conduct':
            raise ProtocolError('Server code of conduct requires human acceptance; not accepted automatically')

    def send_named(self, name, payload=b''):
        aliases = {'settings':'client_information','teleport_confirm':'accept_teleportation', 'position':'move_player_pos','position_look':'move_player_pos_rot','look':'move_player_rot','flying':'move_player_status_only','block_dig':'player_action','entity_action':'player_command','held_item_slot':'set_carried_item','arm_animation':'swing','block_place':'use_item_on','chat_message':'chat','use_entity':'interact'}
        self._write_frame(p.PLAY_SERVERBOUND[aliases.get(name,name)], payload)

    def send_chat(self, text):
        self.send_named('chat', p.chat_message(text, int(time.time()*1000), self._chat_offset))
        self._chat_offset = 0

    def send_command(self, command):
        self.send_named('chat_command', W().write_string(command.removeprefix('/')[:256]).to_bytes())

    def attack(self, entity_id):
        self.send_named('attack', W().write_varint(entity_id).to_bytes())

    def interact(self, entity_id):
        # New dedicated interact codec: id, hand, LP vec3 hit point, secondary.
        self.send_named('interact', W().write_varint(entity_id).write_varint(0).write_u8(0).write_bool(bool(self._input & 32)).to_bytes())

    def place_block(self, position, face, cursor=(.5,.5,.5)):
        seq = self.next_sequence()
        self.send_named('use_item_on', p.use_item_on(0, position, face, cursor, seq))
        return seq

    def use_item(self, hand=0):
        seq = self.next_sequence()
        self.send_named('use_item', p.use_item(hand, seq, self.yaw, self.pitch))
        return seq

    def set_sneaking(self, sneaking):
        self._input = (self._input | 32) if sneaking else (self._input & ~32)
        self.send_named('player_input', bytes([self._input]))

    def set_sprinting(self, sprinting):
        self._input = (self._input | 64) if sprinting else (self._input & ~64)
        self.send_named('player_input', bytes([self._input]))

    def selected_item(self):
        return self.inventory.get(36 + self.held_slot)

    def _player_info(self, r):
        actions, count = r.read_u8(), r.read_varint()
        for _ in range(count):
            identity = r.read_uuid()
            info = self.players.get(identity)
            if actions & 1:
                name = r.read_string(16)
                for _ in range(r.read_varint()):
                    r.read_string(); r.read_string()
                    if r.read_bool(): r.read_string()
                info = base.PlayerInfo(identity, name)
                self.players[identity] = info
            if actions & 2 and r.read_bool():
                r.read_uuid(); r.read_i64(); r.skip(r.read_varint()); r.skip(r.read_varint())
            if actions & 4:
                value = r.read_varint()
                if info: info.game_mode = value
            if actions & 8:
                value = r.read_bool()
                if info: info.listed = value
            if actions & 16:
                value = r.read_varint()
                if info: info.latency = value
            if actions & 32 and r.read_bool(): p.component(r)
            if actions & 64: r.read_varint()  # UPDATE_LIST_ORDER
            if actions & 128: r.read_bool()  # UPDATE_HAT
        self._emit('player_info', self.players)

    def _spawn_info(self, r):
        dimension_id = r.read_varint()
        self.world_name = r.read_string()
        r.read_i64(); self.game_mode = r.read_i8(); r.read_i8()
        r.read_bool(); r.read_bool()
        if r.read_bool(): r.read_string(); self.death_position = r.read_position()
        else: self.death_position = None
        r.read_varint(); r.read_varint()  # portal cooldown, sea level
        entries = self.registries.get('minecraft:dimension_type', [])
        dimension, data = entries[dimension_id] if 0 <= dimension_id < len(entries) else (self.world_name, None)
        self._apply_dimension(dimension)
        if isinstance(data, dict) and 'min_y' in data and 'height' in data:
            from .world import World
            if (self.world.min_y,self.world.height) != (int(data['min_y']),int(data['height'])):
                self.world = World(self.registry, int(data['min_y']), int(data['height']))
        self.is_dead = False
        self.position_received = False
        self._player_loaded = False

    def _position(self, r):
        teleport = r.read_varint()
        xyz = [r.read_f64() for _ in range(3)]
        velocity = [r.read_f64() for _ in range(3)]
        yaw, pitch, flags = r.read_f32(), r.read_f32(), r.read_i32()
        old = self.position
        self.position = Vec3(*(value + (getattr(old, axis) if flags & (1 << i) else 0) for i,(axis,value) in enumerate(zip(('x','y','z'),xyz))))
        old_yaw, old_pitch = self.yaw, self.pitch
        self.yaw = wrap_degrees(yaw + (old_yaw if flags & 8 else 0))
        self.pitch = max(-90,min(90,pitch + (old_pitch if flags & 16 else 0)))
        v = self.velocity
        if flags & 256:
            a,b = math.radians(old_pitch-self.pitch), math.radians(old_yaw-self.yaw)
            v = Vec3(v.x, v.y*math.cos(a)+v.z*math.sin(a), v.z*math.cos(a)-v.y*math.sin(a))
            v = Vec3(v.x*math.cos(b)+v.z*math.sin(b), v.y, v.z*math.cos(b)-v.x*math.sin(b))
        self.velocity = Vec3(*(value + (getattr(v,axis) if flags & (1 << (i+5)) else 0) for i,(axis,value) in enumerate(zip(('x','y','z'),velocity))))
        self.position_received = True
        self.send_named('accept_teleportation', W().write_varint(teleport).to_bytes())
        self.send_position(self.position,self.yaw,self.pitch,False)
        if not self._player_loaded:
            self.send_named('player_loaded'); self._player_loaded = True
        self._emit('position', self.position)

    def _inventory(self, name, r):
        # Stage all decoding first; never partially commit a corrupt/unknown patch.
        try:
            if name == 'container_set_content':
                window = r.read_varint(); state = r.read_varint()
                count = r.read_varint()
                if not 0 <= count <= 4096: raise ProtocolError('Invalid inventory size')
                values = [p.decode_slot(r) for _ in range(count)]
                carried = p.decode_slot(r)
                self.window_state[window] = state
                self.window_items[window] = {index: item for index, item in enumerate(values)}
                self.carried_item = carried
                if window == 0: self.inventory = dict(enumerate(values))
            elif name == 'container_set_slot':
                window = r.read_varint(); state = r.read_varint(); slot = r.read_i16(); item = p.decode_slot(r)
                self.window_state[window] = state
                self.window_items.setdefault(window, {})[slot] = item
                if window == 0: self.inventory[slot] = item
            elif name == 'set_player_inventory':
                slot = r.read_varint(); item = p.decode_slot(r)
                real_slot = 36+slot if slot < 9 else slot
                self.inventory[real_slot] = item
                self.window_items.setdefault(0, {})[real_slot] = item
            else: self.carried_item = p.decode_slot(r)
        except ProtocolError as exc:
            self.inventory_errors += 1; self.last_inventory_error = str(exc)
            self.log(str(exc)); self._emit('inventory_error', str(exc)); return
        self.inventory_packets += 1
        self._emit('inventory', self.inventory)

    # ------------------------------------------------------------------ 容器 / 合成
    def click_slot(self, slot, *, button=0, click='pickup', window=0):
        """发一次容器点击（26.2 的 HashedStack 版本）。"""
        state = int(self.window_state.get(window, 0))
        payload = p.container_click(
            window, state, slot, button=button, click=click, carried=self.carried_item
        )
        self.send_named('container_click', payload)
        return state

    def close_window(self, window=0):
        self.send_named('container_close', p.container_close(window))
        self.window_items.pop(window, None)
        self.window_state.pop(window, None)
        self.crafting_window = None

    def _open_screen(self, r):
        container_id = r.read_varint()
        menu_type = r.read_varint()
        title = p.component(r)
        self.crafting_window = {'id': container_id, 'type': menu_type, 'title': title}
        self._emit('open_screen', self.crafting_window)

    def _close_screen(self, r):
        window = r.read_varint()
        self.window_items.pop(window, None)
        self.window_state.pop(window, None)
        if self.crafting_window is not None and self.crafting_window.get('id') == window:
            self.crafting_window = None
        self._emit('close_screen', window)

    def _dispatch(self, pid, r):
        name = next((k for k,v in p.PLAY_CLIENTBOUND.items() if v == pid), '')
        if name == 'login':
            self.entity_id = r.read_i32(); r.read_bool()
            for _ in range(r.read_varint()): r.read_string()
            r.read_varint(); r.read_varint(); r.read_varint()
            r.read_bool(); r.read_bool(); r.read_bool()
            self._spawn_info(r); self.online_mode = r.read_bool(); self.enforces_secure_chat = r.read_bool()
            self._emit('join',self.world_name)
        elif name == 'respawn': self._spawn_info(r); r.read_u8(); self.health = 20.; self._emit('respawn',self.dimension)
        elif name == 'player_position': self._position(r)
        elif name == 'player_info_update': self._player_info(r)
        elif name == 'player_rotation': self.yaw = r.read_f32(); self.pitch = r.read_f32()
        elif name == 'teleport_entity':
            eid = r.read_varint()
            if eid in self.entities:
                e = self.entities[eid]
                xyz = [r.read_f64() for _ in range(3)]
                velocity = [r.read_f64() for _ in range(3)]
                yaw, pitch, flags = r.read_f32(), r.read_f32(), r.read_i32()
                e.position = Vec3(*(value + (getattr(e.position,axis) if flags & (1 << i) else 0) for i,(axis,value) in enumerate(zip(('x','y','z'),xyz))))
                e.velocity = Vec3(*(value + (getattr(e.velocity,axis) if flags & (1 << (i+5)) else 0) for i,(axis,value) in enumerate(zip(('x','y','z'),velocity))))
                e.yaw = yaw + (e.yaw if flags & 8 else 0); e.pitch = pitch + (e.pitch if flags & 16 else 0)
                r.read_bool()
        elif name == 'keep_alive': self.send_named(name,r.read(8))
        elif name == 'ping': self.send_named('pong',r.read(4))
        elif name == 'chunk_batch_finished': r.read_varint(); self.send_named('chunk_batch_received', W().write_f32(8.0).to_bytes())
        elif name == 'cookie_request': self._cookie(r,p.PLAY_SERVERBOUND)
        elif name == 'resource_pack_push': self._resource_pack(r,p.PLAY_SERVERBOUND)
        elif name == 'start_configuration':
            self.send_named('configuration_acknowledged')
            # Configuration is synchronous in the frame-dispatch loop below.
            self.state = 'configuration'
        elif name == 'disconnect':
            reason=p.component(r); self._emit('disconnect',reason); self.state='closed'; self._closed.set()
            if self._writer: self._writer.close()
        elif name in {'container_set_content','container_set_slot','set_player_inventory','set_cursor_item'}: self._inventory(name,r)
        elif name == 'open_screen': self._open_screen(r)
        elif name == 'container_close': self._close_screen(r)
        elif name == 'set_held_slot': self.held_slot = r.read_varint()
        elif name == 'set_default_spawn_position':
            self.spawn_dimension = r.read_string(); self.spawn_position = r.read_position(); r.read_f32(); r.read_f32()
        elif name == 'set_time':
            self.game_time = r.read_i64()
            for _ in range(r.read_varint()):
                clock_id = r.read_varint(); ticks = r.read_varlong(); r.read_f32(); r.read_f32()
                clocks = self.registries.get('minecraft:world_clock', [])
                if 0 <= clock_id < len(clocks) and clocks[clock_id][0] == 'minecraft:overworld': self.world.day_time = ticks
            self._emit('time', self.world.day_time)
        elif name == 'level_chunk_with_light': self._chunk(r)
        elif name == 'light_update': self._light_update(r)
        elif name == 'forget_level_chunk':
            z,x=r.read_i32(),r.read_i32(); self.world.remove_column(x,z); self.loaded_chunks=len(self.world.columns)
            self.block_light.pop((x, z), None); self.sky_light.pop((x, z), None)
        elif name == 'section_blocks_update':
            packed=r.read_u64(); x=packed>>42; z=(packed>>20)&0x3fffff; y=packed&0xfffff
            if x & 0x200000: x-=1<<22
            if z & 0x200000: z-=1<<22
            if y & 0x80000: y-=1<<20
            for _ in range(r.read_varint()):
                record=r.read_varlong(); self.world.set_state(x*16+((record>>8)&15),y*16+(record&15),z*16+((record>>4)&15),record>>12)
            self._emit('block_change',None)
        elif name in {'system_chat','disguised_chat','set_action_bar_text','set_title_text','set_subtitle_text','tab_list'}: self._text(name,r)
        elif name == 'boss_event':
            identity, action = r.read_uuid(), r.read_varint()
            if action in {0, 3}:
                self._emit('boss_bar', p.component(r))
        elif name == 'player_combat_kill':
            r.read_varint()
            self.is_dead = True
            self._emit('death', p.component(r))
        elif name == 'player_chat': self._player_chat(r)
        elif name == 'add_entity': self._entity(r)
        elif name == 'set_entity_motion':
            eid=r.read_varint(); v=p.lp_vec3(r)
            if eid in self.entities: self.entities[eid].velocity=v
        elif name == 'entity_position_sync':
            eid=r.read_varint(); xyz=Vec3(r.read_f64(),r.read_f64(),r.read_f64()); v=Vec3(r.read_f64(),r.read_f64(),r.read_f64()); yaw,pitch=r.read_f32(),r.read_f32(); r.read_bool()
            if eid in self.entities:
                e=self.entities[eid]; e.position=xyz; e.velocity=v; e.yaw=yaw; e.pitch=pitch
        else:
            handler = _SAFE_HANDLERS.get(name)
            if handler: handler(self,r)

    def _chunk(self,r):
        from .modern_chunk import decode_chunk
        x,z=r.read_i32(),r.read_i32()
        # 26.2 heightmaps are map<enum VarInt,long[]>; NOT NBT.
        for _ in range(r.read_varint()): r.read_varint(); r.skip(r.read_varint()*8)
        data=r.read(r.read_varint())
        # 方块实体：u8 packed (x<<4|z)、i16 y、VarInt 类型、未命名 NBT
        for _ in range(r.read_varint()):
            r.read_u8(); r.read_i16(); r.read_varint(); p.read_nbt(r)
        self._read_light(x, z, r)
        self.chunk_packets_received+=1
        try: column=decode_chunk(x,z,data,self.world.min_y,self.world.height)
        except Exception as exc:
            self.chunk_decode_errors+=1; self.last_chunk_error=str(exc); self.last_chunk_hex=data[:48].hex(); self.log(f'26.2 chunk decode: {exc}'); return
        self.world.add_column(column); self.loaded_chunks=len(self.world.columns); self._emit('chunk',(x,z))

    def _read_light(self, chunk_x, chunk_z, r):
        """解析区块包附带的光照数据（方块光/天空光），失败不影响方块解码。"""
        from .light import decode_light_data

        min_section = self.world.min_y // 16 - 1
        section_count = self.world.height // 16 + 2
        try:
            block_light, sky_light = decode_light_data(r, min_section, section_count)
        except Exception as exc:
            self.light_errors = getattr(self, 'light_errors', 0) + 1
            self.last_light_error = str(exc)
            self.log(f'26.2 光照解析失败: {exc}')
            return
        self.block_light[(chunk_x, chunk_z)] = block_light
        self.sky_light[(chunk_x, chunk_z)] = sky_light

    def _light_update(self, r):
        """单独的「Light Update」包：只更新光照，不动方块。"""
        x, z = r.read_varint(), r.read_varint()
        self._read_light(x, z, r)

    def light_at(self, x, y, z, layer='block'):
        """世界坐标处的光照等级（0–15）；未知返回 ``None``。"""
        chunk_x, chunk_z = int(x) >> 4, int(z) >> 4
        store = self.block_light if layer == 'block' else self.sky_light
        light = store.get((chunk_x, chunk_z))
        if light is None:
            return None
        return light.get(layer, int(x) & 15, int(y), int(z) & 15)

    def is_dark_here(self):
        """当前位置是否暗到可能刷怪（保守近似，详见 :mod:`mcbot.light`）。"""
        from .light import is_dark_enough_for_mobs

        position = self.position
        block = self.light_at(position.x, position.y + 1, position.z, 'block')
        sky = self.light_at(position.x, position.y + 1, position.z, 'sky')
        if block is None:
            return False
        return is_dark_enough_for_mobs(block, sky if sky is not None else 15, int(self.world_time_of_day()) < 13000)

    def _text(self,name,r):
        text=p.component(r)
        if name == 'system_chat':
            action=r.read_bool(); event='action_bar' if action else 'system_chat'
            message=base.ChatMessage(kind='action_bar' if action else 'system',text=text,timestamp=time.time())
            self.chat_log.append(message); self._emit(event,message)
        elif name == 'disguised_chat':
            r.read_varint(); sender=p.component(r)
            if r.read_bool(): p.component(r)
            message=base.ChatMessage(kind='system',text=text,sender=sender,timestamp=time.time()); self.chat_log.append(message); self._emit('system_chat',message)
        elif name == 'tab_list': self._emit('playerlist_header',text+'\n'+p.component(r))
        else: self._emit({'set_action_bar_text':'action_bar','set_title_text':'title','set_subtitle_text':'subtitle'}[name],text)

    def _player_chat(self,r):
        # ClientboundPlayerChatPacket: globalIndex, UUID, index, optional fixed
        # 256-byte signature, SignedMessageBody.Packed, optional component, mask,
        # bound chat type. Decode the stable leading body, never legacy bool-only.
        r.read_varint(); sender=r.read_uuid(); r.read_varint()
        signed=r.read_bool()
        if signed: r.skip(256)
        text=r.read_string(256); stamp=r.read_i64(); r.read_i64()
        message=base.ChatMessage(kind='chat',text=text,sender=self.player_name(sender),sender_uuid=sender,timestamp=stamp/1000)
        self.chat_log.append(message); self._emit('chat',message)
        # Acknowledge consumed signed messages without claiming verification.
        if signed:
            self._chat_offset+=1
            self.send_named('chat_ack',W().write_varint(1).to_bytes()); self._chat_offset=0

    def _entity(self,r):
        eid,identity,kind=r.read_varint(),r.read_uuid(),r.read_varint()
        xyz=Vec3(r.read_f64(),r.read_f64(),r.read_f64()); v=p.lp_vec3(r)
        pitch,yaw=r.read_angle(),r.read_angle(); r.read_angle(); r.read_varint()
        entity=base.Entity(eid,self.registry.entity_name(kind),identity,xyz,yaw,pitch,v)
        self.entities[eid]=entity; self._emit('entity_spawn',entity)

    async def _read_loop(self):
        try:
            while self.state in {'play','configuration'}:
                pid,r=await self._read_frame()
                try:
                    if self.state == 'configuration': self._configuration(pid,r)
                    else: self._dispatch(pid,r)
                except ProtocolError as exc:
                    self.log(f'26.2 packet {pid:#x}: {exc}')
                    self._emit('protocol_error',str(exc))
                if self._writer: await self._writer.drain()
        except asyncio.CancelledError: raise
        except Exception as exc:
            self.log(f'26.2 connection: {exc!r}')
            await self.disconnect(str(exc))


# These individual layouts were retained only where 26.2 field codecs match.
_SAFE_HANDLERS = {
    'set_health':base._handle_health, 'set_experience':base._handle_experience,
    'block_update':base._handle_block_change, 'remove_entities':base._handle_entity_destroy,
    'move_entity_pos':base._handle_rel_move,'move_entity_pos_rot':base._handle_move_look,
    'move_entity_rot':base._handle_look,'take_item_entity':base._handle_collect,
    'player_abilities':base._handle_abilities,'custom_payload':base._handle_custom_payload,
    'game_event':base._handle_game_state,'entity_event':base._handle_entity_status,
    'player_info_remove':base._handle_player_remove,
}

"""Local TCP smoke test: native login/config/play/chunk/verification/movement."""
import asyncio
import pathlib
import unittest
import uuid
from mcbot.buffer import BufferReader as R, BufferWriter as W
from mcbot.modern_client import ModernMinecraftClient
from mcbot import modern_packets as p
from mcbot.nbt import write_nbt
from mcbot.types import Vec3


async def frame(reader):
    length=0
    for i in range(5):
        byte=(await reader.readexactly(1))[0]
        length |= (byte & 127) << (7*i)
        if byte < 128: break
    r=R(await reader.readexactly(length))
    return r.read_varint(), r.read_rest()


def encode(pid, payload=b''):
    body=W().write_varint(pid).write(payload).to_bytes()
    return W().write_varint(len(body)).write(body).to_bytes()


class ModernTextTests(unittest.TestCase):
    def test_bossbar_verification_text(self):
        c=ModernMinecraftClient('localhost'); seen=[]
        c.on('boss_bar', seen.append)
        raw=write_nbt({'text':'验证码：654321'}); raw=raw[:1]+raw[3:]
        body=W().write_uuid(uuid.UUID(int=1)).write_varint(3).write(raw).to_bytes()
        c._dispatch(p.PLAY_CLIENTBOUND['boss_event'],R(body))
        self.assertEqual(seen,['验证码：654321'])


class ModernTcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_transport_world_and_actions(self):
        received=[]; errors=[]; done=asyncio.Event()
        section=(pathlib.Path(__file__).parent/'fixtures/modern_26_2/sections.bin').read_bytes()
        async def serve(reader, writer):
            try:
                pid,body=await frame(reader)
                self.assertEqual(pid,0); self.assertEqual(R(body).read_varint(),776)
                pid,body=await frame(reader)
                self.assertEqual(pid,0); r=R(body); self.assertEqual(r.read_string(),'NativeBot'); r.read_uuid(); self.assertEqual(r.remaining,0)
                login=W().write_uuid(uuid.UUID(int=7)).write_string('NativeBot').write_varint(0).to_bytes()
                writer.write(encode(2,login)); await writer.drain()
                self.assertEqual((await frame(reader))[0],3)
                self.assertEqual((await frame(reader))[0],0)
                writer.write(encode(3)); await writer.drain()
                self.assertEqual((await frame(reader))[0],3)
                # 26.2 区块包：坐标、高度图、区块数据、方块实体（0 个）、光照（四个空掩码）
                chunk=W().write_i32(0).write_i32(0).write_varint(0).write_varint(len(section)).write(section)
                chunk.write_varint(0)
                for _ in range(4):
                    chunk.write_varint(0)  # sky/block/emptySky/emptyBlock 掩码均为空
                chunk=chunk.to_bytes()
                title=write_nbt({'text':'验证码：654321'}); title=title[:1]+title[3:]
                pos=W().write_varint(3)
                for value in (1.,2.,3.,0.,0.,0.): pos.write_f64(value)
                pos.write_f32(0).write_f32(0).write_i32(0)
                writer.write(encode(p.PLAY_CLIENTBOUND['level_chunk_with_light'],chunk)+encode(p.PLAY_CLIENTBOUND['set_title_text'],title)+encode(p.PLAY_CLIENTBOUND['player_position'],pos.to_bytes()))
                await writer.drain()
                for _ in range(4): received.append(await asyncio.wait_for(frame(reader),3))
            except Exception as exc: errors.append(exc)
            finally:
                done.set(); writer.close(); await writer.wait_closed()
        server=await asyncio.start_server(serve,'127.0.0.1',0)
        c=ModernMinecraftClient('127.0.0.1',port=server.sockets[0].getsockname()[1],username='NativeBot')
        c.world.min_y=0; c.world.height=32
        title_event=asyncio.Event(); titles=[]
        def title(text): titles.append(text); title_event.set()
        c.on('title',title)
        try:
            await c.connect()
            await asyncio.wait_for(title_event.wait(),3)
            c.send_position(Vec3(2,2,3),0,0,True)
            await asyncio.wait_for(done.wait(),4)
            if errors: raise errors[0]
            self.assertEqual(c.chunk_decode_errors,0)
            self.assertEqual(c.chunk_packets_received,1)
            self.assertEqual(c.world.loaded_chunk_count(),1)
            self.assertEqual(titles,['验证码：654321'])
            self.assertIn(p.PLAY_SERVERBOUND['accept_teleportation'],[pid for pid,_ in received])
            self.assertIn(p.PLAY_SERVERBOUND['player_loaded'],[pid for pid,_ in received])
            self.assertGreaterEqual(sum(pid==p.PLAY_SERVERBOUND['move_player_pos_rot'] for pid,_ in received),2)
        finally:
            await c.disconnect(); server.close(); await server.wait_closed()

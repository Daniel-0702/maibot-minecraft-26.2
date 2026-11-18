"""地址解析、Minecraft SRV 探测与握手主机名。"""
from __future__ import annotations

import asyncio
import socket
import struct
import threading
import unittest
from unittest.mock import AsyncMock, patch

from mcbot.address import (
    DEFAULT_PORT,
    SrvRecord,
    Target,
    choose_record,
    parse_host_port,
    parse_srv_response,
    query_srv_sync,
    resolve_target,
)
from mcbot.bridge import MinecraftBridge
from mcbot.config import BotConfig


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        out.append(len(label))
        out.extend(label.encode("ascii"))
    out.append(0)
    return bytes(out)


def _srv_response(ident: int, records: list[tuple[int, int, int, str]], questions: int = 1) -> bytes:
    header = struct.pack("!HHHHHH", ident, 0x8180, questions, len(records), 0, 0)
    body = bytearray()
    for _ in range(questions):
        body.extend(_encode_name("_minecraft._tcp.mc.example.com"))
        body.extend(struct.pack("!HH", 33, 1))
    for priority, weight, port, target in records:
        body.extend(_encode_name("_minecraft._tcp.mc.example.com"))
        rdata = struct.pack("!HHH", priority, weight, port) + _encode_name(target)
        body.extend(struct.pack("!HHIH", 33, 1, 300, len(rdata)))
        body.extend(rdata)
    return header + bytes(body)


def _compressed_response(ident: int, port: int, target: str) -> bytes:
    """把答案里的 owner 名字改成压缩指针，验证解析器能处理指针。"""
    header = struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0)
    question = _encode_name("_minecraft._tcp.mc.example.com") + struct.pack("!HH", 33, 1)
    rdata = struct.pack("!HHH", 0, 5, port) + _encode_name(target)
    answer = struct.pack("!H", 0xC00C) + struct.pack("!HHIH", 33, 1, 300, len(rdata)) + rdata
    return header + question + answer


class ParseTests(unittest.TestCase):
    def test_bare_domain(self):
        self.assertEqual(parse_host_port("mc.example.com"), ("mc.example.com", None))
        self.assertEqual(parse_host_port(" mc.example.com/ "), ("mc.example.com", None))

    def test_domain_with_port(self):
        self.assertEqual(parse_host_port("mc.example.com:25566"), ("mc.example.com", 25566))

    def test_scheme_and_path(self):
        self.assertEqual(parse_host_port("minecraft://mc.example.com:25570/play"), ("mc.example.com", 25570))

    def test_ipv4_and_ipv6(self):
        self.assertEqual(parse_host_port("1.2.3.4"), ("1.2.3.4", None))
        self.assertEqual(parse_host_port("[::1]:25565"), ("::1", 25565))
        self.assertEqual(parse_host_port("::1"), ("::1", None))

    def test_invalid(self):
        for text in ("", "   ", "mc.example.com:abc", "mc.example.com:0", "mc.example.com:70000", "[::1"):
            with self.assertRaises(ValueError, msg=text):
                parse_host_port(text)


class SrvPacketTests(unittest.TestCase):
    def test_parse_and_choose(self):
        records = parse_srv_response(_srv_response(7, [(10, 0, 25565, "a.example.com"), (5, 1, 25570, "b.example.com")]), 7)
        self.assertEqual([(r.target, r.port) for r in records], [("a.example.com", 25565), ("b.example.com", 25570)])
        chosen = choose_record(records)
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertEqual((chosen.target, chosen.port), ("b.example.com", 25570))

    def test_compressed_name(self):
        records = parse_srv_response(_compressed_response(9, 25580, "c.example.com"), 9)
        self.assertEqual(records[0].target, "c.example.com")
        self.assertEqual(records[0].port, 25580)

    def test_wrong_id_and_error_code(self):
        with self.assertRaises(ValueError):
            parse_srv_response(_srv_response(1, []), 2)
        bad = bytearray(_srv_response(3, []))
        bad[3] = 3  # NXDOMAIN
        with self.assertRaises(ValueError):
            parse_srv_response(bytes(bad), 3)


class _FakeDNS(threading.Thread):
    """本地 UDP DNS 服务器，按真实查询 ID 回复 SRV 记录。"""

    def __init__(self, port: int, target: str) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.target = target
        self.queries: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", port))
        self._sock.settimeout(3.0)

    def run(self) -> None:
        try:
            while True:
                data, address = self._sock.recvfrom(1024)
                self.queries.append(data)
                ident = struct.unpack("!H", data[:2])[0]
                self._sock.sendto(_srv_response(ident, [(0, 10, 25599, self.target)]), address)
        except OSError:
            return

    def close(self) -> None:
        self._sock.close()


class WireAndResolveTests(unittest.IsolatedAsyncioTestCase):
    def test_sync_query_against_local_dns(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = _FakeDNS(port, "real.example.com")
        server.start()
        try:
            record = query_srv_sync("mc.example.com", ["127.0.0.1"], timeout=1.0, dns_port=port)
        finally:
            server.close()
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual((record.target, record.port), ("real.example.com", 25599))
        query = server.queries[0]
        self.assertIn(b"_minecraft", query)
        self.assertIn(b"_tcp", query)
        # QTYPE = 33 (SRV)
        self.assertEqual(struct.unpack("!H", query[-4:-2])[0], 33)

    async def test_explicit_port_skips_srv(self):
        with patch("mcbot.address.resolve_srv", new=AsyncMock(return_value=SrvRecord("elsewhere", 1))):
            target = await resolve_target("mc.example.com", 25566)
        self.assertEqual((target.connect_host, target.connect_port), ("mc.example.com", 25566))
        self.assertIsNone(target.srv)

    async def test_embedded_port_wins(self):
        with patch("mcbot.address.resolve_srv", new=AsyncMock(return_value=SrvRecord("elsewhere", 1))):
            target = await resolve_target("mc.example.com:25567", None)
        self.assertEqual(target.connect_port, 25567)
        self.assertIsNone(target.srv)

    async def test_auto_uses_srv_then_default(self):
        record = SrvRecord("game.host", 25599, 0, 5)
        with patch("mcbot.address.resolve_srv", new=AsyncMock(return_value=record)):
            target = await resolve_target("mc.example.com", 0)
        self.assertEqual((target.connect_host, target.connect_port, target.handshake_host), ("game.host", 25599, "mc.example.com"))

        with patch("mcbot.address.resolve_srv", new=AsyncMock(return_value=None)):
            target = await resolve_target("mc.example.com", None)
        self.assertEqual((target.connect_host, target.connect_port), ("mc.example.com", DEFAULT_PORT))
        self.assertIsNone(target.srv)

    async def test_ip_skips_srv(self):
        with patch("mcbot.address.query_srv_sync") as query:
            target = await resolve_target("1.2.3.4", 0)
        query.assert_not_called()
        self.assertEqual((target.connect_host, target.connect_port), ("1.2.3.4", DEFAULT_PORT))


class BridgeTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_srv_target_uses_original_domain_in_handshake(self):
        from tests.fake_server import FakeMinecraftServer

        server = FakeMinecraftServer()
        await server.start()
        bridge = MinecraftBridge(BotConfig(host="mc.example.com", port=0, username="SrvBot"), logger=lambda _: None)
        target = Target("127.0.0.1", server.port, "mc.example.com", SrvRecord("127.0.0.1", server.port))
        try:
            with patch("mcbot.bridge.resolve_target", new=AsyncMock(return_value=target)):
                result = await bridge.connect()
            self.assertTrue(result.ok, result.message)
            self.assertEqual(server.handshake_host, "mc.example.com")
            self.assertEqual(server.handshake_port, server.port)
            # 配置保留用户填写的域名，端口保持“自动”
            self.assertEqual((bridge.config.host, bridge.config.port), ("mc.example.com", 0))
            status = bridge.status()
            self.assertEqual(status["configured_host"], "mc.example.com:自动端口")
            self.assertTrue(status["srv"])
        finally:
            await bridge.stop()
            await server.stop()

    async def test_invalid_address_reports_clearly(self):
        bridge = MinecraftBridge(BotConfig(host="", port=0), logger=lambda _: None)
        result = await bridge.connect(host="  ", port=None)
        self.assertFalse(result.ok)
        self.assertIn("地址", result.message)

    async def test_ping_uses_resolved_target(self):
        from tests.fake_server import FakeMinecraftServer

        server = FakeMinecraftServer()
        await server.start()
        bridge = MinecraftBridge(BotConfig(host="mc.example.com", port=0), logger=lambda _: None)
        target = Target("127.0.0.1", server.port, "mc.example.com", SrvRecord("127.0.0.1", server.port))
        try:
            with patch("mcbot.bridge.resolve_target", new=AsyncMock(return_value=target)):
                info = await bridge.ping()
            self.assertEqual(info["target"], "mc.example.com:%d" % server.port)
            self.assertTrue(info["srv"])
            self.assertEqual(server.handshake_host, "mc.example.com")
        finally:
            await bridge.stop()
            await server.stop()


if __name__ == "__main__":
    unittest.main()

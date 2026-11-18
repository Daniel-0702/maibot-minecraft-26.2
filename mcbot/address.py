"""服务器地址解析：默认端口、``host:port``、IPv6 与 Java 版 SRV 探测。

Minecraft Java 版客户端只拿到一个域名时，会先查 ``_minecraft._tcp.<域名>`` 的
SRV 记录，查到就按记录里的目标主机和端口连，查不到才用 25565。很多面板只显示
一个地址，实际端口藏在 SRV 里，所以这里纯标准库实现了同样的行为：

* 地址里显式写了端口 → 直接连，不查 SRV（与原版一致）
* 端口为空或 0 → 先查 SRV，再退回 25565
* 握手包里始终发送用户填写的原始域名，而不是 SRV 目标，避免影响虚拟主机路由

DNS 服务器列表取自 Windows 注册表或 ``/etc/resolv.conf``；两者都读不到时放弃
SRV，静默退回默认端口，不影响正常连接。
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
import struct
import sys
from dataclasses import dataclass

__all__ = [
    "DEFAULT_PORT",
    "SrvRecord",
    "Target",
    "parse_host_port",
    "is_ip_literal",
    "resolve_srv",
    "resolve_target",
    "parse_srv_response",
]

DEFAULT_PORT = 25565
SRV_PREFIX = "_minecraft._tcp."
_QTYPE_SRV = 33
_MAX_LABEL = 63


# ------------------------------------------------------------------ 地址解析


def parse_host_port(text: str) -> tuple[str, int | None]:
    """把用户输入拆成 ``(主机, 端口)``；端口为 ``None`` 表示没写。

    支持 ``mc.example.com``、``mc.example.com:25566``、``[::1]:25565``、
    裸 IPv6、带 ``minecraft://`` 前缀或尾随斜杠的写法。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("服务器地址不能为空")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].strip()
    if not raw:
        raise ValueError("服务器地址不能为空")

    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            raise ValueError("IPv6 地址缺少右括号 ]")
        host, rest = raw[1:end].strip(), raw[end + 1 :].strip()
        if not host:
            raise ValueError("IPv6 地址不能为空")
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise ValueError(f"无法识别的地址：{text!r}")
        return host, _parse_port(rest[1:])

    if raw.count(":") == 1:
        host, _, port_text = raw.partition(":")
        host = host.strip()
        if not host:
            raise ValueError("服务器地址不能为空")
        return host, _parse_port(port_text)

    if raw.count(":") > 1:  # 裸 IPv6，没有端口
        return raw, None
    return raw, None


def _parse_port(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"端口必须是数字：{text!r}")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超出范围 1-65535：{port}")
    return port


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ SRV 查询


@dataclass(slots=True)
class SrvRecord:
    target: str
    port: int
    priority: int = 0
    weight: int = 0


@dataclass(slots=True)
class Target:
    """解析后的连接目标。"""

    connect_host: str
    connect_port: int
    handshake_host: str
    srv: SrvRecord | None = None

    @property
    def display(self) -> str:
        return f"{self.handshake_host}:{self.connect_port}"


def _encode_name(name: str) -> bytes:
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            raise ValueError(f"域名里出现空标签：{name!r}")
        encoded = label.encode("idna") if not label.isascii() else label.encode("ascii")
        if len(encoded) > _MAX_LABEL:
            raise ValueError(f"域名标签过长：{label!r}")
        out.append(len(encoded))
        out.extend(encoded)
    out.append(0)
    return bytes(out)


def _build_query(name: str) -> tuple[int, bytes]:
    ident = random.randrange(1, 0x10000)
    header = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
    return ident, header + _encode_name(name) + struct.pack("!HH", _QTYPE_SRV, 1)


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    resume: int | None = None
    jumps = 0
    while True:
        if offset >= len(data):
            raise ValueError("DNS 名称越界")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("DNS 压缩指针不完整")
            if resume is None:
                resume = offset + 2
            jumps += 1
            if jumps > 16:
                raise ValueError("DNS 压缩指针成环")
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        offset += 1
        if length == 0:
            break
        if offset + length > len(data):
            raise ValueError("DNS 标签越界")
        labels.append(data[offset : offset + length].decode("ascii", "replace"))
        offset += length
    return ".".join(labels), (resume if resume is not None else offset)


def parse_srv_response(data: bytes, ident: int) -> list[SrvRecord]:
    """解析 DNS 响应里的 SRV 记录（忽略其他类型）。"""
    if len(data) < 12:
        raise ValueError("DNS 响应过短")
    rid, flags, questions, answers, _auth, _add = struct.unpack("!HHHHHH", data[:12])
    if rid != ident:
        raise ValueError("DNS 响应 ID 不匹配")
    if flags & 0x000F:
        raise ValueError(f"DNS 返回错误码 {flags & 0x000F}")
    offset = 12
    for _ in range(questions):
        _, offset = _read_name(data, offset)
        offset += 4
    records: list[SrvRecord] = []
    for _ in range(answers):
        _, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("DNS 记录被截断")
        rtype, rclass, _ttl, rdlen = struct.unpack("!HHIH", data[offset : offset + 10])
        offset += 10
        if offset + rdlen > len(data):
            raise ValueError("DNS 记录数据被截断")
        if rtype == _QTYPE_SRV and rclass == 1 and rdlen >= 7:
            priority, weight, port = struct.unpack("!HHH", data[offset : offset + 6])
            target, _ = _read_name(data, offset + 6)
            target = target.rstrip(".")
            if target and 0 < port <= 65535:
                records.append(SrvRecord(target, port, priority, weight))
        offset += rdlen
    return records


def choose_record(records: list[SrvRecord]) -> SrvRecord | None:
    """按优先级取最低、权重取最高的一条（同分时保持稳定顺序）。"""
    if not records:
        return None
    return sorted(records, key=lambda record: (record.priority, -record.weight))[0]


def _split_servers(text: str) -> list[str]:
    result = []
    for chunk in text.replace(",", " ").replace(";", " ").split():
        try:
            ipaddress.ip_address(chunk)
        except ValueError:
            continue
        result.append(chunk)
    return result


def _windows_dns_servers() -> list[str]:
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []
    keys = [
        r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters",
        r"SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters",
    ]
    servers: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, keys[0]) as key:
            interfaces_root = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, interfaces_root) as interfaces:
                count = winreg.QueryInfoKey(interfaces)[0]
                keys.extend(f"{interfaces_root}\\{winreg.EnumKey(interfaces, index)}" for index in range(count))
    except OSError:
        pass
    for path in keys:
        for value_name in ("NameServer", "DhcpNameServer"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
            except OSError:
                continue
            servers.extend(_split_servers(str(value)))
    return servers


def _unix_dns_servers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    servers.extend(_split_servers(parts[1]))
    except OSError:
        pass
    return servers


def dns_servers() -> list[str]:
    """系统 DNS 服务器地址（去重，保留顺序）。"""
    found = (_windows_dns_servers() if sys.platform == "win32" else []) + _unix_dns_servers()
    unique: list[str] = []
    for server in found:
        if server not in unique:
            unique.append(server)
    return unique


def query_srv_sync(
    host: str, servers: list[str], timeout: float = 2.0, dns_port: int = 53
) -> SrvRecord | None:
    """同步查询 ``_minecraft._tcp.<host>``；任何网络错误都返回 ``None``。

    ``dns_port`` 只在测试或本地自建 DNS 时需要改，默认 53。
    """
    ident, packet = _build_query(SRV_PREFIX + host.rstrip("."))
    for server in servers:
        family = socket.AF_INET6 if ":" in server else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(packet, (server, dns_port))
                data, _ = sock.recvfrom(4096)
        except OSError:
            continue
        try:
            records = parse_srv_response(data, ident)
        except ValueError:
            continue
        chosen = choose_record(records)
        if chosen is not None:
            return chosen
    return None


async def resolve_srv(
    host: str, timeout: float = 2.0, servers: list[str] | None = None, dns_port: int = 53
) -> SrvRecord | None:
    """异步版 SRV 查询；IP、空域名或没有可用 DNS 时直接返回 ``None``。"""
    host = (host or "").strip().rstrip(".")
    if not host or is_ip_literal(host):
        return None
    candidates = dns_servers() if servers is None else servers
    if not candidates:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(query_srv_sync, host, candidates, timeout, dns_port), timeout + 0.5
        )
    except (asyncio.TimeoutError, ValueError, OSError):
        return None


async def resolve_target(text: str, port: int | None = None, *, timeout: float = 2.0) -> Target:
    """把用户输入解析成真正要连接的目标。

    ``port`` 为 ``None``/``0`` 表示自动：有 SRV 用 SRV，否则 25565。
    """
    host, embedded = parse_host_port(text)
    if embedded is not None:
        return Target(host, embedded, host)
    if port:
        if not 1 <= int(port) <= 65535:
            raise ValueError(f"端口超出范围 1-65535：{port}")
        return Target(host, int(port), host)
    record = await resolve_srv(host, timeout=timeout)
    if record is not None:
        return Target(record.target, record.port, host, record)
    return Target(host, DEFAULT_PORT, host)

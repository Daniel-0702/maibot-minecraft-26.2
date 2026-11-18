"""命令行入口：不装 MaiBot 也能单独试玩 / 排查连接问题。

用法::

    python -m mcbot.cli status  --host 127.0.0.1 --port 25565
    python -m mcbot.cli play    --host 127.0.0.1 --username MaiBot --offline
    python -m mcbot.cli play    --host ... --once "去 100 64 100"
    python -m mcbot.cli play    --host ... --microsoft
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .bridge import MinecraftBridge
from .config import BotConfig
from .packets import PROTOCOL_VERSION


def _log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcbot", description="MaiBot 的 Minecraft 客户端（命令行测试入口）")
    parser.add_argument("--version", action="version", version="maibot-minecraft 2.0.0")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="ping 服务器，查看版本 / 在线人数 / 延迟")
    status.add_argument("--host", required=True, help="域名或 IP，可带 :端口")
    status.add_argument("--port", type=int, default=0, help="0 = 自动（SRV → 25565）")

    play = sub.add_parser("play", help="进入服务器并交互式游玩")
    play.add_argument("--host", required=True, help="域名或 IP，可带 :端口")
    play.add_argument("--port", type=int, default=0, help="0 = 自动（SRV → 25565）")
    play.add_argument("--username", default="MaiBot")
    play.add_argument("--offline", action="store_true", help="离线模式（默认）")
    play.add_argument("--microsoft", action="store_true", help="使用微软账号登录（正版服务器）")
    play.add_argument("--minecraft-version", choices=["auto", "26.2", "1.20.1"], default="auto")
    play.add_argument("--view-distance", type=int, default=8)
    play.add_argument("--no-brain", action="store_true", help="关闭自主行为，只听指令")
    play.add_argument("--no-auto-eat", action="store_true")
    play.add_argument("--once", default="", help="启动后执行一条指令然后退出")
    play.add_argument("--seconds", type=float, default=0.0, help="配合 --once：执行后继续运行多少秒")
    play.add_argument("--auth-cache", default=str(Path.home() / ".maibot-mc" / "auth.json"))
    return parser


async def _run_status(args: argparse.Namespace) -> int:
    config = BotConfig(host=args.host, port=args.port)
    bridge = MinecraftBridge(config, logger=_log)
    try:
        info = await bridge.ping()
    except Exception as exc:
        _log(f"连接失败：{exc}")
        return 1
    _log(f"{info['host']}:{info['port']} 版本 {info['version']}（协议 {info['protocol']}）")
    _log(f"在线 {info['players']}，延迟 {info['latency_ms']}ms")
    _log(f"简介：{info['motd']}")
    if info["protocol"] and info["protocol"] not in {763, 776}:
        _log("提示：本插件支持 Java 26.2 / 776 和旧版 1.20.1 / 763，未适配此协议。")
    return 0


async def _run_play(args: argparse.Namespace) -> int:
    config = BotConfig(
        host=args.host,
        port=args.port,
        username=args.username,
        minecraft_version=args.minecraft_version,
        auth="microsoft" if args.microsoft else "offline",
        auth_cache=args.auth_cache,
        view_distance=args.view_distance,
        auto_connect=True,
        autonomous=not args.no_brain,
        auto_eat=not args.no_auto_eat,
    )
    bridge = MinecraftBridge(config, logger=_log)
    result = await bridge.connect()
    if not result.ok:
        _log(result.message)
        return 1
    _log("已进入游戏，输入 help 查看指令")
    try:
        if args.once:
            _log(f"> {args.once}")
            payload = await bridge.execute("do", instruction=args.once)
            _log(payload.get("content", str(payload)))
            if args.seconds > 0:
                await asyncio.sleep(args.seconds)
            return 0 if payload.get("success", True) else 1
        await _repl(bridge)
    finally:
        await bridge.stop()
    return 0


async def _repl(bridge: MinecraftBridge) -> None:
    print(
        "指令：status / goto x y z / goto 玩家名 / mine 方块 [数量] / place 方块 x y z / "
        "attack 目标 / follow 玩家 / say 文本 / eat / collect / inventory / players / stop / resume / quit",
        flush=True,
    )
    while True:
        try:
            line = await asyncio.to_thread(input, "mc> ")
        except (EOFError, KeyboardInterrupt):
            return
        line = line.strip()
        if not line:
            continue
        if line in {"quit", "exit", "q"}:
            return
        if line == "help":
            print(__doc__)
            continue
        action, kwargs = _parse_command(line)
        payload = await bridge.execute(action, **kwargs)
        print(payload.get("content", payload), flush=True)


def _parse_command(line: str) -> tuple[str, dict]:
    parts = line.split()
    action = parts[0].lower()
    rest = parts[1:]
    if action in {"goto", "go"}:
        numbers = []
        names = []
        for token in rest:
            try:
                numbers.append(float(token))
            except ValueError:
                names.append(token)
        if len(numbers) >= 3:
            return "goto", {"x": numbers[0], "y": numbers[1], "z": numbers[2]}
        return "goto", {"player": " ".join(names)}
    if action in {"mine", "gather"}:
        if not rest:
            return "mine", {}
        count = 1
        if rest[-1].isdigit():
            count = int(rest[-1])
            rest = rest[:-1]
        return "mine", {"block": " ".join(rest), "count": count}
    if action == "place":
        numbers = [float(token) for token in rest if _is_number(token)]
        names = [token for token in rest if not _is_number(token)]
        if len(numbers) >= 3 and names:
            return "place", {"block": names[0], "x": numbers[0], "y": numbers[1], "z": numbers[2]}
        return "place", {"block": " ".join(names)}
    if action in {"attack", "follow", "come"}:
        return action, {"target": " ".join(rest)}
    if action in {"say", "chat"}:
        return "say", {"text": line.split(" ", 1)[1] if " " in line else ""}
    if action in {"task", "do"}:
        return "do", {"instruction": line.split(" ", 1)[1] if " " in line else ""}
    if action in {"status", "eat", "collect", "inventory", "players", "stop", "resume", "respawn"}:
        return action, {}
    return "do", {"instruction": line}


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return asyncio.run(_run_status(args))
    if args.command == "play":
        return asyncio.run(_run_play(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

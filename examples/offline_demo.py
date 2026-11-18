"""离线演示：用内置假服务器跑一遍「进服 → 状态 → 走过去 → 挖方块 → 说话」。

不需要安装 Minecraft，也不需要联网：

    python examples/offline_demo.py
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_server import FakeMinecraftServer  # noqa: E402
from mcbot.auth import offline_session  # noqa: E402
from mcbot.client import MinecraftClient  # noqa: E402
from mcbot.humanize import Humanizer  # noqa: E402
from mcbot.player import PlayerController  # noqa: E402
from mcbot.types import BlockPos  # noqa: E402


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def main() -> int:
    server = FakeMinecraftServer()
    await server.start()
    print(f"[demo] 假服务器已启动：127.0.0.1:{server.port}")

    client = MinecraftClient(
        "127.0.0.1",
        server.port,
        username="MaiBotDemo",
        session=offline_session("MaiBotDemo"),
        logger=lambda message: print(f"[client] {message}"),
    )
    await client.connect()
    if not await wait_until(lambda: client.world.is_column_loaded(0, 0) and client.entity_id == 1, timeout=8.0):
        print("[demo] 进入世界失败")
        await server.stop()
        return 1

    player = PlayerController(client, humanizer=Humanizer(rng=random.Random(2026)))
    player.attach()
    await player.start()
    await wait_until(lambda: player.physics.on_ground, timeout=3.0)

    print("[demo] 初始状态：", player.status())

    # 服务器在玩家旁边生成一个石头方块，演示挖掘
    target = BlockPos(2, 2, 2)
    server._send_block_change(target.x, target.y, target.z, client.registry.state_id("stone"))
    await wait_until(lambda: client.world.get_state(target.x, target.y, target.z) != 0, timeout=3.0)

    result = await player.mine(target)
    print(f"[demo] 挖 {target.as_tuple()}：{result.ok} {result.message}")
    print(f"[demo] 该位置现在是：{client.world.block_name(target.x, target.y, target.z)}")

    result = await player.goto(BlockPos(6, 2, 0), radius=1.0, timeout=20.0)
    print(f"[demo] 走到 (6, 2, 0)：{result.ok} {result.message}")
    print(f"[demo] 当前坐标：{player.physics.position}")

    client.send_chat("大家好，我是麦麦，刚学会自己挖矿~")
    await wait_until(lambda: bool(server.chat_messages), timeout=3.0)
    print("[demo] 服务器收到的聊天：", server.chat_messages)
    print(f"[demo] 服务器收到 {len(server.positions)} 个位置包，{len(server.received)} 个动作包")

    await player.stop()
    await client.disconnect()
    await server.stop()
    print("[demo] 结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

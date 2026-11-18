"""MaiBot × Minecraft 插件。

把 MaiBot 接入 Minecraft Java 版服务器：插件内置纯 Python 协议客户端、
体素世界模型、物理/寻路和行为层，让麦麦能像普通玩家一样进服、走动、
挖矿、放方块、打架、吃饭，并在游戏里聊天。
"""

from __future__ import annotations

__version__ = "2.0.5"
SUPPORTED_MINECRAFT = "26.2"
SUPPORTED_PROTOCOL = 776

__all__ = ["__version__", "SUPPORTED_MINECRAFT", "SUPPORTED_PROTOCOL"]

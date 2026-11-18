"""桥接层：把客户端、玩家控制器、自治大脑和 MaiBot 工具串起来。

对上层（MaiBot 插件 / CLI）只暴露 :meth:`MinecraftBridge.execute` 和
:meth:`MinecraftBridge.status`，内部负责连接、重连、死亡重生和事件队列。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from .address import DEFAULT_PORT, Target, resolve_target
from .auth import AuthError, AuthSession, microsoft_session, offline_session
from .brain import HOSTILE_MOBS, Brain
from .client import ChatMessage, Entity, MinecraftClient, ServerStatus, ping_server
from .config import BotConfig
from .humanize import Humanizer
from .player import ActionResult, PlayerController
from .types import BlockPos, Vec3
from .verification import CodeExtractor, VerificationTracker

__all__ = ["MinecraftBridge"]

BLOCK_KEYWORDS = {
    "木头": "oak_log",
    "原木": "oak_log",
    "树": "oak_log",
    "石头": "stone",
    "圆石": "cobblestone",
    "煤": "coal_ore",
    "铁": "iron_ore",
    "金": "gold_ore",
    "钻石": "diamond_ore",
    "沙": "sand",
    "泥土": "dirt",
    "草": "grass_block",
    "木头块": "oak_planks",
    "木板": "oak_planks",
}


class MinecraftBridge:
    """一个 Minecraft bot 的完整生命周期。"""

    def __init__(
        self,
        config: BotConfig,
        *,
        logger: Callable[[str], None] = print,
        llm_call: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.config = config
        self.log = logger
        self.llm_call = llm_call
        self.rng = random.Random(config.random_seed or None)
        self.humanizer = Humanizer(profile=config.human_profile(), rng=self.rng)

        self.client: MinecraftClient | None = None
        self.player: PlayerController | None = None
        self.brain: Brain | None = None

        self.running = False
        self._monitor_task: asyncio.Task | None = None
        self._action_lock = asyncio.Lock()
        self._reconnect_attempts = 0
        self._last_respawn_at = 0.0
        self.events: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=200)
        self.on_device_code: Callable[[str, str], Awaitable[None] | None] | None = None
        self.code_extractor = CodeExtractor(config.verify_pattern)
        self.code_tracker = VerificationTracker()
        self._verification_until = 0.0
        self.last_error = ""
        self._connect_lock = asyncio.Lock()
        self._target: Target | None = None

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """启动监控；``auto_connect`` 时立即连接。"""
        if self.running:
            return
        self.running = True
        if self.config.auto_connect:
            await self.connect()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self.running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        await self._teardown()

    async def _teardown(self) -> None:
        if self.brain is not None:
            await self.brain.stop()
            self.brain = None
        if self.player is not None:
            await self.player.stop()
            self.player = None
        if self.client is not None:
            await self.client.disconnect("主动断开")
            self.client = None

    async def connect(self, *, host: str | None = None, port: int | None = None, username: str | None = None) -> ActionResult:
        """建立连接并启动玩家控制与自治行为。"""
        async with self._connect_lock:
            return await self._connect_locked(host=host, port=port, username=username)

    async def _connect_locked(
        self, *, host: str | None = None, port: int | None = None, username: str | None = None
    ) -> ActionResult:
        if self.client is not None and self.client.connected:
            if self.player is not None and self.player.joined and self.client.state == "play":
                return ActionResult(False, "已经连接")
            # 连接还在但没真正进服（卡登录/半断开），先清理再重连
            self.log("检测到连接未完成，先清理旧连接")
            await self._teardown()
        config = self.config
        typed_host = (host or config.host).strip()
        typed_port = port if port is not None else int(config.port or 0)
        username = username or config.username

        # 只给了域名（端口为空 / 0）时按原版行为探测 SRV，否则用显式端口。
        try:
            target = await resolve_target(typed_host, typed_port or None)
        except ValueError as exc:
            self.last_error = f"服务器地址无效：{exc}"
            return ActionResult(False, self.last_error)
        host, port = target.connect_host, target.connect_port
        if target.srv is not None:
            self.log(
                f"SRV 记录：{target.handshake_host} → {host}:{port}"
                f"（优先级 {target.srv.priority}，权重 {target.srv.weight}）"
            )
        elif typed_port in (None, 0):
            self.log(f"没有 SRV 记录，使用默认端口 {port}")
        self._target = target

        # auto 根据状态协议选择后端；状态查询失败时优先原生 26.2。
        # 代理报告可能与其接受的协议不同，此时可显式设置 minecraft_version。
        server_status = None
        try:
            server_status = await ping_server(host, port, timeout=5.0, handshake_host=target.handshake_host)
            self.log(f"服务器 {target.display} 在线：{server_status.version_name} / 协议 {server_status.protocol}")
        except Exception as exc:
            self.log(f"服务器列表查询失败（继续尝试登录）：{exc}")

        try:
            session = await self._make_session()
        except AuthError as exc:
            self.log(f"认证失败: {exc}")
            self.last_error = f"认证失败：{exc}"
            return ActionResult(False, self.last_error)

        version = config.minecraft_version.strip().lower()
        if version not in {"auto", "26.2", "1.20.1"}:
            self.last_error = "minecraft_version 只能是 auto、26.2 或 1.20.1"
            return ActionResult(False, self.last_error)
        if version == "auto":
            protocol = server_status.protocol if server_status else 0
            if protocol not in {0, 763, 776}:
                self.last_error = f"暂不支持服务器协议 {protocol}；本包支持 Java 26.2 (776) 和旧版 1.20.1 (763)"
                return ActionResult(False, self.last_error)
            version = "1.20.1" if protocol == 763 else "26.2"
        client_class = MinecraftClient
        if version == "26.2":
            from .modern_client import ModernMinecraftClient
            client_class = ModernMinecraftClient
        self.log(f"选择原生 Minecraft {version} 后端")
        client = client_class(
            host=host,
            port=port,
            username=username,
            session=session,
            view_distance=config.view_distance,
            logger=self.log,
            handshake_host=target.handshake_host,
        )
        self._verification_until = time.monotonic() + (
            self.config.verify_window if self.config.verify_window > 0 else 10**9
        )
        client.on("chat", self._on_chat)
        client.on("system_chat", self._on_system_chat)
        client.on("death", self._on_death)
        client.on("disconnect", self._on_disconnect)
        for event in ("title", "subtitle", "action_bar", "boss_bar", "playerlist_header"):
            client.on(event, self._on_verification_text)
        try:
            await client.connect()
        except Exception as exc:
            self.log(f"连接 {target.display} 失败: {exc!r}")
            message = f"连接失败：{exc}"
            message += f"（使用原生 {version} 后端，目标 {target.display}；请检查 /mc diag）"
            self.last_error = message
            return ActionResult(False, message)

        self.client = client
        # 配置里保留用户填写的原始地址（而不是 SRV 目标），端口 0 表示自动。
        self.config.host = target.handshake_host
        self.config.port = typed_port if typed_port else (0 if target.srv is not None else port)
        self.config.username = username
        player = PlayerController(
            client,
            humanizer=self.humanizer,
            logger=self.log,
            auto_eat=config.auto_eat,
        )
        player.attach()
        await player.start()
        self.player = player
        brain = Brain(
            client,
            player,
            config,
            self.humanizer,
            logger=self.log,
            llm_call=self.llm_call,
        )
        self.brain = brain
        if config.autonomous:
            await brain.start()
        self._reconnect_attempts = 0
        self.last_error = ""
        self.log(f"已使用原生 {version} 协议连接")
        self.log(f"已进入服务器 {target.display}，角色 {username}")
        return ActionResult(True, f"已进入 {target.display}")

    async def disconnect(self) -> ActionResult:
        await self._teardown()
        return ActionResult(True, "已断开连接")

    async def _make_session(self) -> AuthSession:
        if self.config.auth == "microsoft":
            cache = Path(self.config.auth_cache).expanduser() if self.config.auth_cache else None

            async def device_code(user_code: str, uri: str, expires_in: float) -> None:
                message = f"请打开 {uri} 并输入代码 {user_code}（{int(expires_in // 60)} 分钟内有效）"
                self.log(message)
                if self.on_device_code is not None:
                    result = self.on_device_code(user_code, uri)
                    if asyncio.iscoroutine(result):
                        await result

            return await microsoft_session(
                device_code,
                client_id=self.config.microsoft_client_id,
                cache_path=cache,
            )
        return offline_session(self.config.username)

    # ------------------------------------------------------------------ 监控
    async def _monitor_loop(self) -> None:
        while self.running:
            await asyncio.sleep(1.0)
            try:
                await self._monitor_once()
            except Exception as exc:
                self.log(f"监控异常: {exc!r}")

    async def _monitor_once(self) -> None:
        client = self.client
        if client is None:
            if self.running and self.config.auto_connect and self._reconnect_attempts < self.config.max_reconnect:
                self._reconnect_attempts += 1
                self.log(f"尝试重新连接（第 {self._reconnect_attempts} 次）")
                result = await self.connect()
                if not result.ok:
                    await asyncio.sleep(self.config.reconnect_delay)
            return

        if not client.connected:
            self.log("连接已断开")
            await self._teardown()
            return

        if client.last_packet_age > 35.0:
            self.log("超过 35 秒没有收到服务器数据，重连")
            await self._teardown()
            return

        if client.is_dead and self.player is not None:
            now = time.monotonic()
            if now - self._last_respawn_at > 3.0:
                self._last_respawn_at = now
                await asyncio.sleep(self.humanizer.reaction_delay(2.0))
                client.respawn()
                self.log("角色死亡，已请求重生")

    # ------------------------------------------------------------------ 事件
    def _on_chat(self, message: ChatMessage) -> None:
        self._inspect_verification(message.text, "chat")
        self._push_event(message)
        if self.brain is not None:
            self.brain.queue_chat(message)
            asyncio.create_task(self.brain.handle_chat(message))

    def _on_system_chat(self, message: ChatMessage) -> None:
        self._inspect_verification(message.text, message.kind)
        self._push_event(message)

    def _on_verification_text(self, text: object) -> None:
        if isinstance(text, ChatMessage):
            text = text.text
        self._inspect_verification(str(text), "loading")

    def _inspect_verification(self, text: str, source: str) -> None:
        """在进服窗口内从任意界面文本里提取验证码。"""
        if not self.config.verify_enabled or not text:
            return
        if time.monotonic() > self._verification_until:
            return
        code = self.code_extractor.extract(text)
        if not code or not self.code_tracker.accept(code):
            return
        self.log(f"捕获进服验证码 {code}（来源 {source}）：{text[:120]}")
        message = ChatMessage(
            kind="verification",
            text=code,
            sender="server",
            timestamp=time.time(),
            source=text,
        )
        self._push_event(message)

    def _push_event(self, message: ChatMessage) -> None:
        try:
            self.events.put_nowait(message)
        except asyncio.QueueFull:
            try:
                self.events.get_nowait()
                self.events.put_nowait(message)
            except Exception:
                pass

    def _on_death(self, reason: str) -> None:
        self.log(f"角色死亡: {reason}")
        try:
            self.events.put_nowait(ChatMessage(kind="death", text=reason, timestamp=time.time()))
        except asyncio.QueueFull:
            pass

    def _on_disconnect(self, reason: str) -> None:
        self.log(f"与服务器断开: {reason}")

    # ------------------------------------------------------------------ 状态
    def _configured_label(self) -> str:
        port = self.config.port
        return f"{self.config.host}:{'自动端口' if not port else port}"

    def status(self) -> dict[str, Any]:
        if self.client is None or self.player is None:
            return {
                "connected": False,
                "configured_host": self._configured_label(),
                "last_error": self.last_error,
            }
        payload = self.player.status()
        payload["brain"] = asdict(self.brain.status()) if self.brain is not None else None
        payload["host"] = self.config.host
        payload["port"] = self.config.port
        payload["configured_host"] = self._configured_label()
        if self._target is not None:
            payload["connect_target"] = self._target.display
            payload["srv"] = self._target.srv is not None
        payload["username"] = self.config.username
        payload["last_error"] = self.last_error
        payload["client_state"] = self.client.state
        payload["inventory_errors"] = getattr(self.client, "inventory_errors", 0)
        payload["last_inventory_error"] = getattr(self.client, "last_inventory_error", "")
        payload["light_errors"] = getattr(self.client, "light_errors", 0)
        payload["last_light_error"] = getattr(self.client, "last_light_error", "")
        payload["crafting_window"] = getattr(self.client, "crafting_window", None)
        payload["protocol"] = self.client.protocol_version
        payload["minecraft_version"] = self.client.registry.minecraft_version
        return payload

    async def ping(self) -> dict[str, Any]:
        target = await resolve_target(self.config.host, int(self.config.port or 0) or None)
        status: ServerStatus = await ping_server(
            target.connect_host, target.connect_port, handshake_host=target.handshake_host
        )
        return {
            "host": target.handshake_host,
            "port": target.connect_port,
            "target": target.display,
            "srv": bool(target.srv),
            "version": status.version_name,
            "protocol": status.protocol,
            "players": f"{status.online_players}/{status.max_players}",
            "motd": status.motd,
            "latency_ms": round(status.latency_ms, 1),
        }

    async def _explore_once(self, radius: float = 24.0) -> ActionResult:
        """朝一个方向走一段（给 /mc explore 用，不依赖自治循环）。"""
        import math
        import random

        if self.player is None or self.client is None:
            return ActionResult(False, "尚未进入游戏")
        position = self.player.physics.position
        angle = random.uniform(0, math.tau)
        target_x = position.x + math.sin(angle) * radius
        target_z = position.z + math.cos(angle) * radius
        surface = self.client.world.surface_y(int(target_x), int(target_z))
        if surface is None:
            return ActionResult(False, "那边还没加载出来，换条路")
        goal = BlockPos(int(target_x), int(surface), int(target_z))
        result = await self.player.goto(goal, radius=2.0, timeout=60.0)
        if result.ok:
            return ActionResult(True, f"走到 ({goal.x}, {goal.y}, {goal.z}) 附近了")
        return result

    def _recipes_payload(self, target: str) -> ActionResult:
        """列出能做的配方，或某个目标需要的步骤。"""
        if self.client is None:
            return ActionResult(False, "尚未进入游戏")
        from . import recipes as recipe_lib

        registry = self.client.registry
        counts = recipe_lib.inventory_counts(self.client.inventory, registry)
        if target.strip():
            steps = recipe_lib.plan(registry, counts, target.strip(), times=1)
            if not steps:
                return ActionResult(False, f"材料不够，或者没有 {target.strip()} 的配方")
            names = " → ".join(step.name for step in steps)
            return ActionResult(True, f"{target.strip()} 的做法：{names}", {"steps": [step.name for step in steps]})
        available = recipe_lib.candidates(registry, counts, max=12)
        if not available:
            return ActionResult(False, "现在材料不够，什么都做不了", {"recipes": []})
        names = "、".join(sorted({f"{recipe.name}×{recipe.output_count}" for recipe in available}))
        return ActionResult(True, f"现在能做：{names}", {"recipes": [recipe.name for recipe in available]})

    def _format_nearby(self, summary: dict[str, Any]) -> str:
        entities = summary.get("entities") or {}
        entity_text = "、".join(f"{name}×{count}" for name, count in sorted(entities.items())) or "没有"
        players = "、".join(summary.get("players") or []) or "没有"
        blocks = summary.get("blocks") or {}
        block_text = "、".join(sorted(blocks)) or "没什么特别的"
        return f"附近生物：{entity_text}；玩家：{players}；周围方块：{block_text}"

    async def _craft_once(self, item: str) -> ActionResult:
        """按名字合成一次（带回依赖步骤），给 /mc craft 用。"""
        if not item:
            return ActionResult(False, "用法：/mc craft 物品名（如 wooden_pickaxe）")
        if self.client is None or self.player is None:
            return ActionResult(False, "尚未进入游戏")
        from . import recipes as recipe_lib
        from .crafting import CraftingController

        registry = self.client.registry
        counts = recipe_lib.inventory_counts(self.client.inventory, registry)
        steps = recipe_lib.plan(registry, counts, item, times=1)
        if not steps:
            return ActionResult(False, f"材料不够，或者没有 {item} 的配方")
        crafter = CraftingController(self.player, logger=self.log)
        done = 0
        for step in steps:
            try:
                if await crafter.craft(step):
                    done += 1
            except Exception as exc:  # 合成失败不能让命令崩掉
                self.log(f"合成 {step.name} 失败: {exc!r}")
                break
        if done == 0:
            return ActionResult(False, "合成失败（可能服务器拒绝了点击，或材料被移动）")
        return ActionResult(True, f"合成完成：{item}（{done} 步）")

    # ------------------------------------------------------------------ 动作入口
    async def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """执行一个高层动作，返回可序列化结果。"""
        action = (action or "").strip().lower()
        if action in {"status", "state"}:
            return {"success": True, "content": self._format_status(), "status": self.status()}
        if action == "connect":
            result = await self.connect(
                host=kwargs.get("host"),
                port=kwargs.get("port"),
                username=kwargs.get("username"),
            )
            return result.to_dict()
        if action == "disconnect":
            return (await self.disconnect()).to_dict()
        if action == "ping":
            try:
                info = await self.ping()
                return {"success": True, "content": f"服务器在线：{info['players']} 人，延迟 {info['latency_ms']}ms", "server": info}
            except Exception as exc:
                return {"success": False, "content": f"服务器 ping 失败：{exc}"}

        if not self._ready():
            return {"success": False, "content": "还没进入游戏，请先 /mc connect"}

        assert self.client is not None and self.player is not None and self.brain is not None
        self.brain.pause()
        try:
            async with self._action_lock:
                result = await self._dispatch(action, kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"执行动作 {action} 异常: {exc!r}")
            result = ActionResult(False, f"执行失败：{exc}")
        finally:
            if action not in {"stop", "disconnect"}:
                self.brain.resume()
        return result.to_dict()

    def _ready(self) -> bool:
        return bool(self.client and self.client.connected and self.player and self.player.joined and self.brain)

    async def _dispatch(self, action: str, kwargs: dict[str, Any]) -> ActionResult:
        assert self.client is not None and self.player is not None and self.brain is not None
        player = self.player
        client = self.client

        if action == "goto":
            target = self._resolve_position(kwargs)
            if target is None:
                return ActionResult(False, "请提供坐标（x/y/z）或玩家名")
            return await player.goto(target, radius=float(kwargs.get("radius", 1.5)), timeout=float(kwargs.get("timeout", 120.0)))

        if action in {"mine", "gather"}:
            block = self._resolve_block(kwargs.get("block") or kwargs.get("name") or "")
            if block is None:
                return ActionResult(False, "没有识别出要挖的方块名")
            count = max(1, min(64, int(kwargs.get("count", 1))))
            return await player.mine_nearest({block}, radius=int(kwargs.get("radius", 32)), max_blocks=count)

        if action == "place":
            block = self._resolve_block(kwargs.get("block") or "")
            if block is None:
                return ActionResult(False, "没有识别出要放的方块名")
            position = self._resolve_position(kwargs)
            if not isinstance(position, BlockPos):
                return ActionResult(False, "放置需要方块坐标")
            return await player.place_block(position, block)

        if action == "attack":
            target = str(kwargs.get("target") or "").strip()
            entity = self._find_target_entity(target)
            if entity is None:
                return ActionResult(False, "附近没有找到攻击目标")
            return await player.attack(entity.entity_id, max_seconds=float(kwargs.get("seconds", 10.0)))

        if action == "follow":
            target = str(kwargs.get("target") or kwargs.get("player") or "").strip()
            entity = self._find_player_entity(target)
            if entity is None:
                return ActionResult(False, f"附近没有玩家 {target or '（未指定）'}")
            return await player.follow(entity.entity_id, distance=float(kwargs.get("distance", 3.0)), timeout=float(kwargs.get("timeout", 60.0)))

        if action == "come":
            entity = self._find_player_entity(str(kwargs.get("target") or "")) or self._nearest_player_entity()
            if entity is None:
                return ActionResult(False, "附近没有玩家")
            return await player.goto(entity.position, radius=2.0, timeout=90.0)

        if action == "say":
            text = str(kwargs.get("text") or kwargs.get("message") or "")
            return await player.say(text)

        if action == "eat":
            return await player.eat()

        if action == "craft":
            return await self._craft_once(str(kwargs.get("item") or kwargs.get("target") or ""))

        if action == "collect":
            return await player.collect_items(radius=float(kwargs.get("radius", 12.0)))

        if action in {"chop", "chop_tree"}:
            result = await player.chop_tree(
                radius=int(kwargs.get("radius", 40)),
                max_logs=max(1, int(kwargs.get("count", kwargs.get("max_logs", 8)))),
            )
            return result

        if action in {"dig", "dig_down", "mine_down"}:
            return await player.dig_down(int(kwargs.get("steps", kwargs.get("count", 3))))

        if action in {"pillar", "pillar_up"}:
            return await player.pillar_up(int(kwargs.get("count", 3)))

        if action == "bridge":
            return await player.bridge(
                float(kwargs.get("degrees", kwargs.get("direction", 0.0))),
                int(kwargs.get("length", kwargs.get("count", 3))),
            )

        if action in {"shelter", "hide"}:
            return await player.dig_shelter(int(kwargs.get("depth", 2)))

        if action in {"torch", "place_torch"}:
            return await player.place_torch()

        if action in {"equip", "hold_item"}:
            return await player.equip_item(str(kwargs.get("item") or kwargs.get("name") or ""))

        if action in {"drop", "toss"}:
            return await player.drop_item(str(kwargs.get("item") or kwargs.get("name") or ""))

        if action in {"swim", "swim_out"}:
            return await player.swim_to_land(timeout=float(kwargs.get("timeout", 25.0)))

        if action == "explore":
            return await self._explore_once(float(kwargs.get("radius", 24.0)))

        if action in {"jump", "hop"}:
            if player.physics.on_ground:
                player.physics.jump()
                return ActionResult(True, "跳了一下")
            return ActionResult(False, "现在跳不了（不在地上）")

        if action in {"flee", "run_away"}:
            await self.brain._flee()
            return ActionResult(True, "已从危险方向跑开")

        if action in {"home", "go_home"}:
            return await player.go_home()

        if action in {"sethome", "set_home"}:
            return player.sethome()

        if action in {"follow_player", "follow_name"}:
            return await player.follow_player(str(kwargs.get("target") or kwargs.get("player") or ""))

        if action in {"recipes", "recipe"}:
            return self._recipes_payload(str(kwargs.get("target") or kwargs.get("item") or ""))

        if action == "nearby":
            summary = player.nearby_summary(float(kwargs.get("radius", 10.0)))
            return ActionResult(True, self._format_nearby(summary), {"nearby": summary})

        if action in {"where", "position"}:
            position = player.physics.position
            return ActionResult(
                True,
                f"我在 {position.x:.1f} {position.y:.1f} {position.z:.1f}（{client.dimension}）",
                {"position": [position.x, position.y, position.z]},
            )

        if action in {"time", "clock"}:
            ticks = int(client.world_time_of_day())
            night = ticks >= 13000
            return ActionResult(
                True,
                f"游戏时间 {ticks} tick（{'夜晚' if night else '白天'}）",
                {"time_of_day": ticks, "night": night},
            )

        if action in {"light", "brightness"}:
            if not hasattr(client, "light_at"):
                return ActionResult(False, "旧版后端没有光照数据")
            position = player.physics.position
            block = client.light_at(position.x, position.y + 1, position.z, "block")
            sky = client.light_at(position.x, position.y + 1, position.z, "sky")
            dark = client.is_dark_here() if hasattr(client, "is_dark_here") else None
            return ActionResult(
                True,
                f"方块光 {block}，天空光 {sky}，{'这里很暗（可能刷怪）' if dark else '亮度还行' if dark is not None else '光照未知'}",
                {"block": block, "sky": sky, "dark": dark},
            )

        if action == "look":
            target = self._resolve_position(kwargs)
            if target is None:
                return ActionResult(False, "请提供看向的坐标")
            return await player.look_at(target if isinstance(target, Vec3) else target.center())

        if action == "respawn":
            return await player.respawn()

        if action == "stop":
            self.brain.pause()
            player.stop_moving()
            self.brain.cancel_activity()
            return ActionResult(True, "已停下")

        if action == "resume":
            self.brain.resume()
            return ActionResult(True, "继续自主活动")

        if action == "inventory":
            return ActionResult(True, self._format_inventory(), {"items": self._inventory_payload()})

        if action == "players":
            players = self._players_payload()
            return ActionResult(True, self._format_players(), {"players": players})

        if action in {"do", "task"}:
            return await self._natural_language(str(kwargs.get("instruction") or kwargs.get("text") or ""))

        return ActionResult(False, f"未知动作：{action}")

    # ------------------------------------------------------------------ 自然语言
    async def _natural_language(self, instruction: str) -> ActionResult:
        if not instruction.strip():
            return ActionResult(False, "指令为空")
        plan = None
        if self.llm_call is not None and self.config.llm_enabled:
            plan = await self._plan_with_llm(instruction)
        if plan is None:
            plan = self._plan_with_rules(instruction)
        if plan is None:
            return ActionResult(False, f"没听懂「{instruction}」，可以试试：去 x y z / 挖 5 个木头 / 跟着 玩家名")
        action, kwargs = plan
        return await self._dispatch(action, kwargs)

    async def _plan_with_llm(self, instruction: str) -> tuple[str, dict[str, Any]] | None:
        assert self.llm_call is not None and self.player is not None
        prompt = (
            "把玩家的话翻译成一个 Minecraft 动作。只输出 JSON，格式："
            '{"action": "goto|mine|place|attack|follow|say|eat|collect|look|stop", "args": {...}}\n'
            "参数：goto 用 x/y/z 或 player；mine 用 block/count；place 用 block/x/y/z；"
            "attack/follow 用 target（玩家名可留空表示最近目标）；say 用 text。\n"
            f"当前状态：{self._compact_status()}\n"
            f"玩家说：{instruction}"
        )
        try:
            answer = await asyncio.wait_for(self.llm_call(prompt), timeout=20.0)
        except Exception:
            return None
        if not answer:
            return None
        start, end = answer.find("{"), answer.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            import json

            data = json.loads(answer[start : end + 1])
        except Exception:
            return None
        action = str(data.get("action", "")).strip().lower()
        args = data.get("args") or data.get("parameters") or {}
        if not isinstance(args, dict):
            return None
        allowed = {"goto", "mine", "place", "attack", "follow", "say", "eat", "collect", "look", "stop"}
        if action not in allowed:
            return None
        return action, args

    def _plan_with_rules(self, instruction: str) -> tuple[str, dict[str, Any]] | None:
        text = instruction.strip()
        for keyword, block in BLOCK_KEYWORDS.items():
            if keyword in text:
                if any(word in text for word in ("放", "摆", "盖")):
                    coordinates = self._extract_coords(text)
                    if coordinates is not None:
                        return "place", {"block": block, "x": coordinates[0], "y": coordinates[1], "z": coordinates[2]}
                if any(word in text for word in ("挖", "采", "砍", "收集", "要")):
                    return "mine", {"block": block, "count": self._extract_count(text)}
        if "跟着" in text or "跟随" in text:
            return "follow", {"target": self._extract_name(text)}
        if text.startswith("说") or text.startswith("喊"):
            return "say", {"text": text[1:].strip(" ：:")}
        if "吃" in text:
            return "eat", {}
        if "捡" in text or "收集掉落" in text:
            return "collect", {}
        if "打" in text:
            return "attack", {"target": self._extract_name(text)}
        if "停" in text:
            return "stop", {}
        coordinates = self._extract_coords(text)
        if coordinates is not None:
            return "goto", {"x": coordinates[0], "y": coordinates[1], "z": coordinates[2]}
        name = self._extract_name(text)
        if name and any(word in text for word in ("去", "找", "过来", "走到")):
            return "goto", {"player": name}
        if any(word in text for word in ("逛逛", "走走", "探索")):
            return "goto", self._random_nearby_target()
        return None

    @staticmethod
    def _extract_coords(text: str) -> tuple[float, float, float] | None:
        import re

        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        if len(numbers) >= 3:
            return float(numbers[0]), float(numbers[1]), float(numbers[2])
        return None

    @staticmethod
    def _extract_count(text: str) -> int:
        import re

        numbers = re.findall(r"\d+", text)
        for number in numbers:
            value = int(number)
            if 1 <= value <= 64:
                return value
        # 中文数字："五个木头""三块石头"
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
        match = re.search(r"([一二两三四五六七八九十])\s*[个块根棵座条]", text)
        if match:
            return chinese.get(match.group(1), 1)
        return 1

    def _extract_name(self, text: str) -> str:
        for info in self.client.players.values() if self.client else ():
            if info.name and info.name in text:
                return info.name
        import re

        match = re.search(r"(?:跟着|跟随|打|去|找|走到|过来)\s*([A-Za-z0-9_\u4e00-\u9fff]{2,16})", text)
        return match.group(1) if match else ""

    def _random_nearby_target(self) -> dict[str, Any]:
        if self.player is None or self.client is None:
            return {}
        position = self.player.physics.position
        x = int(position.x + self.rng.uniform(-12, 12))
        z = int(position.z + self.rng.uniform(-12, 12))
        surface = self.client.world.surface_y(x, z)
        y = int(surface) if surface is not None else int(position.y)
        return {"x": x, "y": y, "z": z}

    # ------------------------------------------------------------------ 解析工具
    def _resolve_position(self, kwargs: dict[str, Any]) -> BlockPos | Vec3 | None:
        if all(key in kwargs for key in ("x", "y", "z")) and kwargs.get("x") is not None:
            try:
                return BlockPos(int(float(kwargs["x"])), int(float(kwargs["y"])), int(float(kwargs["z"])))
            except (TypeError, ValueError):
                return None
        player_name = str(kwargs.get("player") or kwargs.get("target") or "").strip()
        if player_name:
            entity = self._find_player_entity(player_name)
            if entity is not None:
                return entity.position
        return None

    def _resolve_block(self, name: str) -> str | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in BLOCK_KEYWORDS:
            name = BLOCK_KEYWORDS[name]
        if ":" in name:
            return name
        if self.client is None:
            return name
        candidates = self.client.registry.blocks_matching(name)
        if not candidates:
            return None
        # 优先精确匹配短名，否则取第一个可挖掘的方块
        for block in candidates:
            if block.short_name == name:
                return block.name
        for block in candidates:
            if block.diggable:
                return block.name
        return candidates[0].name

    def _find_player_entity(self, name: str) -> Entity | None:
        if self.client is None or not name:
            return None
        for info in self.client.players.values():
            if info.name.lower() != name.lower():
                continue
            for entity in self.client.entities.values():
                if entity.uuid == info.uuid:
                    return entity
        for entity in self.client.entities.values():
            if entity.type_name == "minecraft:player" and entity.uuid is not None:
                if self.client.player_name(entity.uuid).lower() == name.lower():
                    return entity
        return None

    def _nearest_player_entity(self) -> Entity | None:
        if self.client is None or self.player is None:
            return None
        own_uuid = self.client.session.uuid if self.client.session else None
        candidates = [
            entity
            for entity in self.client.entities.values()
            if entity.type_name == "minecraft:player" and entity.uuid != own_uuid
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda entity: self.player.physics.position.distance_to(entity.position))

    def _find_target_entity(self, name: str) -> Entity | None:
        if self.client is None or self.player is None:
            return None
        if name:
            player_entity = self._find_player_entity(name)
            if player_entity is not None:
                return player_entity
            for entity in self.client.entities.values():
                if entity.short_name == name or entity.type_name == name:
                    return entity
        hostiles = [
            entity
            for entity in self.client.entities.values()
            if entity.short_name in HOSTILE_MOBS
        ]
        if hostiles:
            return min(hostiles, key=lambda entity: self.player.physics.position.distance_to(entity.position))
        return None

    # ------------------------------------------------------------------ 文本输出
    def _compact_status(self) -> str:
        status = self.status()
        return (
            f"位置 {status.get('position')} 血量 {status.get('health')} 饥饿 {status.get('food')} "
            f"手持 {status.get('held_item')} 维度 {status.get('dimension')}"
        )

    def _format_status(self) -> str:
        status = self.status()
        if not status.get("connected"):
            return f"当前未连接（配置目标 {self._configured_label()}）"
        brain = status.get("brain") or {}
        return (
            f"在线 {status['username']} @ {status.get('connect_target') or status['configured_host']}，"
            f"坐标 ({status['position'][0]:.1f}, {status['position'][1]:.1f}, {status['position'][2]:.1f})，"
            f"血量 {status['health']}，饥饿 {status['food']}，"
            f"手持 {status['held_item']}，正在 {brain.get('activity', '未知')}，"
            f"附近 {status['entities']} 个实体 / {status['players']} 名玩家，区块 {status['chunks']}"
        )

    def _inventory_payload(self) -> list[dict[str, Any]]:
        if self.client is None:
            return []
        payload = []
        for slot in range(9):
            item = self.client.inventory.get(slot)
            if item is None or item.is_empty:
                continue
            info = self.client.registry.item(item.item_id)
            payload.append(
                {
                    "slot": slot,
                    "item": info.name if info else f"unknown:{item.item_id}",
                    "count": item.count,
                    "held": slot == self.client.held_slot,
                }
            )
        return payload

    def _format_inventory(self) -> str:
        items = self._inventory_payload()
        if not items:
            return "快捷栏是空的"
        parts = [f"{item['item'].split(':', 1)[-1]}×{item['count']}" + ("（手持）" if item["held"] else "") for item in items]
        return "快捷栏：" + "、".join(parts)

    def _players_payload(self) -> list[dict[str, Any]]:
        if self.client is None or self.player is None:
            return []
        result = []
        for info in self.client.players.values():
            entity = self._find_player_entity(info.name)
            result.append(
                {
                    "name": info.name,
                    "uuid": str(info.uuid),
                    "latency": info.latency,
                    "position": [round(entity.position.x, 1), round(entity.position.y, 1), round(entity.position.z, 1)] if entity else None,
                    "distance": round(self.player.physics.position.distance_to(entity.position), 1) if entity else None,
                }
            )
        return result

    def _format_players(self) -> str:
        players = self._players_payload()
        if not players:
            return "服务器里好像只有我"
        parts = []
        for player in players:
            if player["distance"] is not None:
                parts.append(f"{player['name']}（{player['distance']}m）")
            else:
                parts.append(player["name"])
        return "在线玩家：" + "、".join(parts)

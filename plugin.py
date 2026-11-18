"""MaiBot 插件入口：把 Minecraft 客户端接进麦麦。

安装：把整个 ``maibot-minecraft`` 目录放进 MaiBot 的 ``plugins/`` 下，
在 WebUI 的插件配置里填好服务器地址即可。推荐的体验方式：

* 在 QQ 里发 ``/mc connect`` 让麦麦进服；
* 让 LLM 调用 ``mc_goto``、``mc_mine``、``mc_say`` 等工具指挥她干活；
* 打开 ``autonomous`` 后，没人管的时候她会自己砍树、挖矿、打怪、回消息。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# 插件目录可能不在 sys.path 中，确保能导入同目录下的 mcbot 包。
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, MaiBotPlugin, PluginConfigBase, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from mcbot import __version__  # noqa: E402
from mcbot.actions import format_help, parse_coords, parse_number, resolve  # noqa: E402
from mcbot.bridge import MinecraftBridge  # noqa: E402
from mcbot.config import DEFAULT_PERSONA, BotConfig  # noqa: E402

__all__ = ["MaiBotMinecraftPlugin", "create_plugin"]


# --------------------------------------------------------------------------- 配置模型
class PluginSection(PluginConfigBase):
    """插件基础配置。

    MaiBot Runner 要求配置里必须有 ``[plugin].config_version``。
    ``[plugin].enabled`` 是宿主保留键，由 Runner 自己维护；插件模型**不要声明它**，
    否则 1.3.x 上 SDK 归一化会丢掉这个键，导致启停开关/重载反复与状态不一致
    （MaiBot issue #2080，修复见 #2103）。
    """

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置结构版本，升级时由插件维护")


class ServerSection(PluginConfigBase):
    """Minecraft 服务器连接配置。"""

    __ui_label__ = "服务器"
    __ui_icon__ = "dns"
    __ui_order__ = 1

    host: str = Field(default="127.0.0.1", description="服务器地址，可只填域名（如 mc.example.com），也可写成 域名:端口")
    port: int = Field(default=0, description="端口；填 0 或留空 = 自动（域名有 SRV 记录就跟随，否则用默认 25565）")
    minecraft_version: str = Field(default="auto", description="协议：auto 自动检测、26.2 原生协议、1.20.1 旧版协议；状态查询被禁用时默认 26.2")
    username: str = Field(default="MaiBot", description="游戏内角色名（离线模式就是昵称）")
    auth: str = Field(default="offline", description="认证方式：offline（离线）或 microsoft（正版）")
    microsoft_client_id: str = Field(default="00000000402b5328", description="微软登录使用的 Azure 应用 ID")
    auth_cache: str = Field(default="", description="正版登录令牌缓存路径，留空则用 ~/.maibot-mc/auth.json")
    view_distance: int = Field(default=8, description="视距（2-32）")
    auto_connect: bool = Field(default=False, description="插件加载后自动进服")
    reconnect_delay: float = Field(default=8.0, description="断线重连间隔（秒）")
    max_reconnect: int = Field(default=5, description="连续重连次数上限")


class BehaviorSection(PluginConfigBase):
    """行为与人类化配置。"""

    __ui_label__ = "行为"
    __ui_icon__ = "smart_toy"
    __ui_order__ = 2

    autonomous: bool = Field(default=True, description="没人指挥时自己找事做（砍树/挖矿/合成/打怪）")
    auto_eat: bool = Field(default=True, description="饥饿时自动进食")
    crafting: bool = Field(default=True, description="允许自动合成（背包 2x2 与合成台 3x3，需 26.2 原生后端）")
    humanize: bool = Field(default=True, description="模拟真人反应、鼠标移动与打字节奏")
    chat_reply: bool = Field(default=True, description="在游戏里回应其他玩家聊天")
    chat_reply_cooldown: float = Field(default=8.0, description="两次回复之间的最短间隔（秒）")
    chat_reply_probability: float = Field(default=0.75, description="未被点名时回复的概率")
    reaction_base: float = Field(default=0.2, description="平均反应时间（秒）")
    typing_cps: float = Field(default=5.5, description="打字速度（字符/秒）")


class LLMSection(PluginConfigBase):
    """可选的 LLM 配置（用麦麦自己的模型生成对话与自主目标）。"""

    __ui_label__ = "LLM"
    __ui_icon__ = "psychology"
    __ui_order__ = 3

    llm_enabled: bool = Field(default=True, description="允许用 LLM 生成游戏内回复和自主目标")
    llm_model: str = Field(default="", description="模型名，留空使用默认模型")
    llm_task: str = Field(default="", description="模型任务名，留空自动选择")
    persona: str = Field(default=DEFAULT_PERSONA, description="游戏内人设提示词")


class ReportSection(PluginConfigBase):
    """把游戏事件转发到 QQ。"""

    __ui_label__ = "消息上报"
    __ui_icon__ = "campaign"
    __ui_order__ = 4

    report_stream_id: str = Field(default="", description="上报目标聊天流 ID；留空则用最近一次对话的聊天流")
    report_chat: bool = Field(default=True, description="转发游戏内玩家聊天")
    report_death: bool = Field(default=True, description="死亡时提醒")
    report_prefix: str = Field(default="[MC]", description="消息前缀")


class SecuritySection(PluginConfigBase):
    """命令权限：MaiBot 1.3.x 没有插件命令级权限，需要在这里手动授权。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    admin_users: str = Field(
        default="",
        description="允许使用 /mc 的用户，逗号分隔，例如 qq:123456789,telegram:987654321；留空表示不额外限制",
    )


class VerifySection(PluginConfigBase):
    """首次进服的验证码：从加载界面识别后转发到 QQ 群。"""

    __ui_label__ = "进服验证"
    __ui_icon__ = "key"
    __ui_order__ = 5

    verify_enabled: bool = Field(default=True, description="自动识别加载界面里的验证码并转发")
    verify_stream_id: str = Field(
        default="",
        description="验证码发到哪个聊天流（通常是服务器验证群）；留空复用消息上报的聊天流",
    )
    verify_pattern: str = Field(
        default="",
        description="自定义提取正则（第一个捕获组为验证码）；留空则按关键词+字母数字串自动识别",
    )
    verify_template: str = Field(
        default="[MC] 进服验证码：{code}",
        description="发送模板，可用 {code} {username} {source}",
    )
    verify_window: float = Field(default=180.0, description="进服后监听验证码的秒数，0 表示一直监听")
    verify_forward_source: bool = Field(default=False, description="是否把原始提示文本也一并发出")


class MaiBotMinecraftConfig(PluginConfigBase):
    """Minecraft 插件完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    server: ServerSection = Field(default_factory=ServerSection)
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    report: ReportSection = Field(default_factory=ReportSection)
    verify: VerifySection = Field(default_factory=VerifySection)
    security: SecuritySection = Field(default_factory=SecuritySection)


# --------------------------------------------------------------------------- 插件
class MaiBotMinecraftPlugin(MaiBotPlugin):
    """让麦麦进入《我的世界》并像普通玩家一样行动。"""

    config_model = MaiBotMinecraftConfig

    def __init__(self) -> None:
        super().__init__()
        self._bridge: MinecraftBridge | None = None
        self._bridge_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None
        self._bot_config = BotConfig()
        self._stream_id = ""
        self._last_verification: tuple[str, str] | None = None

    # ------------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        self._bot_config = self._build_bot_config()
        self._stream_id = self._bot_config.report_stream_id
        await self._start_bridge()
        self.ctx.logger.info(
            "Minecraft 插件 %s 已加载（MaiBot %s）：目标 %s:%s，角色 %s，自主行为 %s",
            __version__,
            _detect_maibot_version(),
            self._bot_config.host,
            self._bot_config.port,
            self._bot_config.username,
            "开启" if self._bot_config.autonomous else "关闭",
        )
        if not self._bot_config.admin_users:
            self.ctx.logger.info(
                "未配置 security.admin_users：在 MaiBot 1.3.x 上 /mc 没有内置操作员校验，"
                "建议填写允许操作的用户（如 qq:123456789）"
            )

    async def on_unload(self) -> None:
        await self._stop_bridge()
        self.ctx.logger.info("Minecraft 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        old = self._bot_config
        self._bot_config = self._build_bot_config()
        self._stream_id = self._bot_config.report_stream_id or self._stream_id
        connection_changed = (
            old.host != self._bot_config.host
            or old.port != self._bot_config.port
            or old.username != self._bot_config.username
            or old.auth != self._bot_config.auth
            or old.auto_connect != self._bot_config.auto_connect
            or old.autonomous != self._bot_config.autonomous
        )
        if connection_changed:
            self.ctx.logger.info("连接配置变化，重启 Minecraft 连接")
            await self._stop_bridge()
            await self._start_bridge()

    # ------------------------------------------------------------------ 桥接管理
    def _build_bot_config(self) -> BotConfig:
        try:
            raw = self.config.model_dump()
        except AttributeError:
            raw = self.get_plugin_config_data()
        merged: dict[str, Any] = {}
        for section in ("server", "behavior", "llm", "report", "verify", "security"):
            value = raw.get(section) or {}
            if isinstance(value, dict):
                merged.update(value)
        if not merged.get("auth_cache"):
            merged["auth_cache"] = ""
        return BotConfig.from_dict(merged)

    async def _start_bridge(self) -> None:
        bridge = MinecraftBridge(self._bot_config, logger=self._log, llm_call=self._llm_call)
        bridge.on_device_code = self._announce_device_code
        self._bridge = bridge
        self._bridge_task = asyncio.create_task(bridge.start())
        self._pump_task = asyncio.create_task(self._pump_events())

    async def _stop_bridge(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        if self._bridge_task is not None:
            self._bridge_task.cancel()
            self._bridge_task = None
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None

    def _log(self, message: str) -> None:
        try:
            self.ctx.logger.info("[mc] %s", message)
        except Exception:
            pass

    async def _llm_call(self, prompt: str) -> str | None:
        config = self._bot_config
        if not config.llm_enabled:
            return None
        kwargs: dict[str, Any] = {}
        if config.llm_model:
            kwargs["model"] = config.llm_model
        if config.llm_task:
            kwargs["task_name"] = config.llm_task
        try:
            result = await self.ctx.llm.generate(prompt, **kwargs)
        except TypeError:
            # 兼容较早的 SDK：generate 可能不接受 task_name 关键字
            try:
                result = await self.ctx.llm.generate(prompt, model=config.llm_model or "")
            except Exception as exc:
                self._log(f"LLM 调用失败: {exc!r}")
                return None
        except Exception as exc:
            self._log(f"LLM 调用失败: {exc!r}")
            return None
        if not isinstance(result, dict):
            return None
        if result.get("success") is False:
            return None
        return result.get("response") or result.get("content") or None

    # ------------------------------------------------------------------ 事件上报
    def _resolve_stream(self, stream_id: str = "") -> str:
        if stream_id:
            self._stream_id = stream_id
        return self._stream_id or self._bot_config.report_stream_id

    async def _announce_device_code(self, user_code: str, uri: str) -> None:
        stream = self._resolve_stream()
        text = f"需要登录微软账号：打开 {uri}，输入代码 {user_code}（几分钟内有效）"
        if stream:
            try:
                await self.ctx.send.text(f"{self._bot_config.report_prefix} {text}", stream)
                return
            except Exception:
                pass
        self.ctx.logger.warning(text)

    async def _pump_events(self) -> None:
        while True:
            bridge = self._bridge
            if bridge is None:
                await asyncio.sleep(0.5)
                continue
            message = await bridge.events.get()
            config = self._bot_config
            stream = self._resolve_stream()
            try:
                if message.kind == "verification":
                    target = config.verify_stream_id or stream
                    if not target:
                        self._log("捕获到验证码，但没有配置转发聊天流；请设置 verify_stream_id 或 report_stream_id")
                        continue
                    text = self._format_verification(config, message)
                    self._last_verification = (message.text, text)
                    await self.ctx.send.text(text, target)
                    if config.verify_forward_source and message.source:
                        await self.ctx.send.text(f"{config.report_prefix} 原始提示：{message.source}", target)
                    continue
                if not stream:
                    continue
                if message.kind == "chat" and config.report_chat:
                    await self.ctx.send.text(f"{config.report_prefix} <{message.sender}> {message.text}", stream)
                elif message.kind == "death" and config.report_death:
                    await self.ctx.send.text(f"{config.report_prefix} 麦麦在游戏里阵亡了（{message.text}），正在重生…", stream)
            except Exception as exc:
                self._log(f"上报消息失败: {exc!r}")

    @staticmethod
    def _format_verification(config: BotConfig, message: Any) -> str:
        template = config.verify_template or "[MC] 进服验证码：{code}"
        try:
            return template.format(code=message.text, username=config.username, source=message.source or "")
        except (KeyError, IndexError, ValueError):
            return f"{template} {message.text}"

    # ------------------------------------------------------------------ 工具
    def _bridge_or_error(self) -> MinecraftBridge | None:
        if self._bridge is None:
            return None
        return self._bridge

    async def _run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        bridge = self._bridge_or_error()
        if bridge is None:
            return {"success": False, "content": "Minecraft 插件还没有初始化"}
        payload = await bridge.execute(action, **kwargs)
        normalized = self._normalize(payload)
        if not normalized.get("success"):
            self._log(f"动作 {action} 失败：{normalized.get('content')}")
        return normalized

    async def _execute_spec(self, spec, args: str) -> dict[str, Any]:
        """按动作注册表执行一次动作（工具和 /mc 共用同一套解析）。"""
        if spec.name == "diag":
            ok, text, _weight = await self._mc_diag()
            return {"success": ok, "content": text}
        if spec.name == "verify":
            ok, text, _weight = await self._mc_verify()
            return {"success": ok, "content": text}
        parsed = self._parse_action_args(spec, args)
        if parsed is None:
            return {"success": False, "content": f"用法：/mc {spec.usage}"}
        return await self._run(spec.name, **parsed)

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content") or payload.get("message") or ("完成" if payload.get("success") else "失败")
        # content 给新版本 Host / LLM 读取，message 兼容较早版本的工具结果展示
        result = {"success": bool(payload.get("success")), "content": str(content), "message": str(content)}
        for key, value in payload.items():
            if key in {"success", "content", "message"}:
                continue
            result[key] = value
        return result

    @Tool(
        "mc_status",
        description="查看麦麦在《我的世界》里的状态：是否在线、坐标、血量、饥饿、手持物品、附近玩家与方块。",
        core_tool=True,
    )
    async def mc_status(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run("status")

    @Tool(
        "mc_connect",
        description="让麦麦登录 Minecraft 服务器。不填参数时使用插件配置里的服务器。",
        parameters=[
            ToolParameterInfo(name="host", param_type=ToolParamType.STRING, description="服务器地址", required=False, default=""),
            ToolParameterInfo(name="port", param_type=ToolParamType.INTEGER, description="端口", required=False, default=0),
            ToolParameterInfo(name="username", param_type=ToolParamType.STRING, description="游戏角色名", required=False, default=""),
        ],
    )
    async def mc_connect(self, host: str = "", port: int = 0, username: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._run(
            "connect",
            host=host or None,
            port=port or None,
            username=username or None,
        )

    @Tool("mc_disconnect", description="让麦麦退出 Minecraft 服务器。")
    async def mc_disconnect(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run("disconnect")

    @Tool(
        "mc_goto",
        description="让麦麦走到指定坐标或某个玩家身边。",
        parameters=[
            ToolParameterInfo(name="x", param_type=ToolParamType.NUMBER, description="目标 X 坐标", required=False, default=None),
            ToolParameterInfo(name="y", param_type=ToolParamType.NUMBER, description="目标 Y 坐标", required=False, default=None),
            ToolParameterInfo(name="z", param_type=ToolParamType.NUMBER, description="目标 Z 坐标", required=False, default=None),
            ToolParameterInfo(name="player", param_type=ToolParamType.STRING, description="要去找的玩家名（与坐标二选一）", required=False, default=""),
        ],
    )
    async def mc_goto(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        player: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if player:
            return await self._run("goto", player=player)
        if x is None or y is None or z is None:
            return {"success": False, "content": "请给出 x/y/z 三个坐标，或者一个玩家名"}
        return await self._run("goto", x=x, y=y, z=z)

    @Tool(
        "mc_mine",
        description="让麦麦在附近寻找并挖掘指定方块（例如 oak_log、stone、iron_ore），会自动寻路、换工具、捡掉落物。",
        parameters=[
            ToolParameterInfo(name="block", param_type=ToolParamType.STRING, description="方块名（支持中文：木头/石头/铁/钻石）", required=True),
            ToolParameterInfo(name="count", param_type=ToolParamType.INTEGER, description="挖多少个", required=False, default=1),
        ],
    )
    async def mc_mine(self, block: str, count: int = 1, **kwargs: Any) -> dict[str, Any]:
        return await self._run("mine", block=block, count=count)

    @Tool(
        "mc_place",
        description="让麦麦在指定位置放一个方块（需要快捷栏里有这个方块）。",
        parameters=[
            ToolParameterInfo(name="block", param_type=ToolParamType.STRING, description="方块名", required=True),
            ToolParameterInfo(name="x", param_type=ToolParamType.INTEGER, description="X", required=True),
            ToolParameterInfo(name="y", param_type=ToolParamType.INTEGER, description="Y", required=True),
            ToolParameterInfo(name="z", param_type=ToolParamType.INTEGER, description="Z", required=True),
        ],
    )
    async def mc_place(self, block: str, x: int, y: int, z: int, **kwargs: Any) -> dict[str, Any]:
        return await self._run("place", block=block, x=x, y=y, z=z)

    @Tool(
        "mc_attack",
        description="让麦麦攻击最近的敌对生物，或者指定的玩家/生物。",
        parameters=[
            ToolParameterInfo(name="target", param_type=ToolParamType.STRING, description="目标名，留空表示最近的怪物", required=False, default=""),
        ],
    )
    async def mc_attack(self, target: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._run("attack", target=target)

    @Tool(
        "mc_follow",
        description="让麦麦跟着某个玩家走。",
        parameters=[
            ToolParameterInfo(name="target", param_type=ToolParamType.STRING, description="玩家名", required=True),
        ],
    )
    async def mc_follow(self, target: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run("follow", target=target)

    @Tool(
        "mc_say",
        description="让麦麦在 Minecraft 游戏内聊天框说一句话。",
        parameters=[
            ToolParameterInfo(name="text", param_type=ToolParamType.STRING, description="要说的话", required=True),
        ],
    )
    async def mc_say(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run("say", text=text)

    @Tool("mc_eat", description="让麦麦吃点东西恢复饥饿值。")
    async def mc_eat(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run("eat")

    @Tool(
        "mc_collect",
        description="让麦麦把附近的掉落物捡起来。",
        parameters=[
            ToolParameterInfo(name="radius", param_type=ToolParamType.NUMBER, description="搜索半径", required=False, default=12.0),
        ],
    )
    async def mc_collect(self, radius: float = 12.0, **kwargs: Any) -> dict[str, Any]:
        return await self._run("collect", radius=radius)

    @Tool("mc_inventory", description="查看麦麦快捷栏里的物品。")
    async def mc_inventory(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run("inventory")

    @Tool(
        "mc_stop",
        description="让麦麦停下当前动作并暂停自主行为，等待下一步指示。",
        core_tool=True,
    )
    async def mc_stop(self, **kwargs: Any) -> dict[str, Any]:
        return await self._run("stop")

    @Tool(
        "mc_task",
        description="用一句自然语言给麦麦下达任务，例如「去挖五个木头」「跟着小明」「说一句我回来了」「去 100 64 100」。",
        parameters=[
            ToolParameterInfo(name="instruction", param_type=ToolParamType.STRING, description="自然语言指令", required=True),
        ],
    )
    async def mc_task(self, instruction: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run("do", instruction=instruction)

    @Tool(
        "mc_command",
        description=(
            "执行任意 Minecraft 动作，参数与 /mc 子命令相同。常用：chop 6（砍树）、craft wooden_pickaxe（合成）、"
            "dig 4（向下挖）、bridge 6（铺路）、pillar 3（垫高）、shelter（挖避难所）、"
            "nearby 12（看周围）、light/time/where（信息）、recipes（能做什么）、equip torch（拿火把）。"
        ),
        parameters=[
            ToolParameterInfo(name="subcommand", param_type=ToolParamType.STRING, description="动作名，支持中英别名", required=True),
            ToolParameterInfo(name="args", param_type=ToolParamType.STRING, description="参数，如坐标、数量、物品名", required=False, default=""),
        ],
    )
    async def mc_command(self, subcommand: str, args: str = "", **kwargs: Any) -> dict[str, Any]:
        spec = resolve(subcommand)
        if spec is None:
            return {"success": False, "content": f"没有「{subcommand}」这个动作，可能的名字：{format_help()[:300]}"}
        return await self._execute_spec(spec, str(args or ""))

    @Tool(
        "mc_chop",
        description="让麦麦找一棵树并砍下来（会自动清理挡路的树叶、从下往上砍、捡掉落物）。",
        parameters=[
            ToolParameterInfo(name="count", param_type=ToolParamType.INTEGER, description="砍多少个原木", required=False, default=6),
        ],
    )
    async def mc_chop(self, count: int = 6, **kwargs: Any) -> dict[str, Any]:
        return await self._run("chop", count=max(1, int(count)))

    @Tool(
        "mc_craft",
        description="让麦麦合成物品（自动做依赖：木板→木棍→合成台→工具/火把）。例如 wooden_pickaxe、torch、crafting_table。",
        parameters=[
            ToolParameterInfo(name="item", param_type=ToolParamType.STRING, description="要合成的物品名", required=True),
        ],
    )
    async def mc_craft(self, item: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run("craft", item=item)

    @Tool(
        "mc_recipes",
        description="查看现在能合成什么，或某个物品需要什么材料。",
        parameters=[
            ToolParameterInfo(name="target", param_type=ToolParamType.STRING, description="想查的物品，留空表示列出现在能做的", required=False, default=""),
        ],
    )
    async def mc_recipes(self, target: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._run("recipes", target=target)

    @Tool(
        "mc_nearby",
        description="看看麦麦周围有什么：生物、玩家、树/水/矿洞等。",
        parameters=[
            ToolParameterInfo(name="radius", param_type=ToolParamType.NUMBER, description="搜索半径", required=False, default=10.0),
        ],
    )
    async def mc_nearby(self, radius: float = 10.0, **kwargs: Any) -> dict[str, Any]:
        return await self._run("nearby", radius=radius)

    @Tool(
        "mc_home",
        description="让麦麦回家；没设过家就把当前位置设为家。",
        parameters=[
            ToolParameterInfo(name="set_home", param_type=ToolParamType.BOOLEAN, description="true 表示把当前位置设为家", required=False, default=False),
        ],
    )
    async def mc_home(self, set_home: bool = False, **kwargs: Any) -> dict[str, Any]:
        return await self._run("sethome" if set_home else "home")

    @Tool(
        "mc_equip",
        description="让麦麦把背包里的某个物品拿到手上（例如 torch、iron_sword）。",
        parameters=[
            ToolParameterInfo(name="item", param_type=ToolParamType.STRING, description="物品名", required=True),
        ],
    )
    async def mc_equip(self, item: str, **kwargs: Any) -> dict[str, Any]:
        return await self._run("equip", item=item)

    @Tool(
        "mc_build",
        description="建造类动作：pillar 垫高、bridge 铺路、shelter 挖避难所。",
        parameters=[
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, description="pillar / bridge / shelter", required=True),
            ToolParameterInfo(name="count", param_type=ToolParamType.INTEGER, description="数量（高度/长度）", required=False, default=3),
        ],
    )
    async def mc_build(self, mode: str, count: int = 3, **kwargs: Any) -> dict[str, Any]:
        spec = resolve(mode)
        if spec is None or spec.name not in {"pillar", "bridge", "shelter"}:
            return {"success": False, "content": "mode 只能是 pillar / bridge / shelter"}
        args = "" if spec.name == "shelter" else str(max(1, int(count)))
        return await self._execute_spec(spec, args)

    @Tool(
        "mc_survive",
        description="生存类动作：swim 游上岸、flee 逃跑、eat 吃东西、torch 点火把。",
        parameters=[
            ToolParameterInfo(name="mode", param_type=ToolParamType.STRING, description="swim / flee / eat / torch", required=True),
        ],
    )
    async def mc_survive(self, mode: str, **kwargs: Any) -> dict[str, Any]:
        spec = resolve(mode)
        if spec is None or spec.name not in {"swim", "flee", "eat", "torch"}:
            return {"success": False, "content": "mode 只能是 swim / flee / eat / torch"}
        return await self._execute_spec(spec, "")

    # ------------------------------------------------------------------ 命令
    @Command(
        "mc",
        description="控制麦麦的 Minecraft 角色：status/connect/stop/say/come/goto/mine",
        pattern=r"^/mc(?:\s+(?P<sub>\w+))?(?:\s+(?P<args>.*))?$",
        permission="operator",
    )
    async def handle_mc(self, **kwargs: Any) -> tuple[bool, str, int]:
        success, response, weight = await self._handle_mc(kwargs)
        stream_id = str(kwargs.get("stream_id") or (kwargs.get("message") or {}).get("stream_id") or "")
        if stream_id and response:
            try:
                # 命令的 response 在本版本不一定自动发送，这里显式回一条，避免"没反应"
                await self.ctx.send.text(response, stream_id)
            except Exception as exc:
                self._log(f"发送命令结果失败: {exc!r}")
        return success, response, weight

    async def _handle_mc(self, kwargs: dict[str, Any]) -> tuple[bool, str, int]:
        if not self._can_use_command(kwargs):
            return False, "没有权限使用 /mc：请让管理员在插件配置 security.admin_users 里加入你的 平台:ID（如 qq:123456789）", 1
        matched = kwargs.get("matched_groups") or {}
        sub = (matched.get("sub") or "status").strip().lower()
        args = (matched.get("args") or "").strip()
        stream_id = kwargs.get("stream_id", "")
        self._resolve_stream(stream_id)

        # 帮助与未知命令：直接从动作注册表生成，不用维护第二份清单
        if sub in {"help", "帮助", "命令", "?", ""}:
            return True, format_help(args or None), 2
        spec = resolve(sub)
        if spec is None:
            return False, f"没有「{sub}」这个动作。\n{format_help()}", 1

        # 少数动作需要插件上下文（发送消息/发验证码/连接），单独处理
        if spec.name == "status":
            payload = await self._run("status")
            return bool(payload.get("success")), str(payload.get("content", "")), 2
        if spec.name == "diag":
            return await self._mc_diag()
        if spec.name == "verify":
            return await self._mc_verify()
        if spec.name == "connect":
            host, port = self._parse_host(args)
            payload = await self._run("connect", host=host, port=port)
            return bool(payload.get("success")), str(payload.get("content", "")), 2

        parsed = self._parse_action_args(spec, args)
        if parsed is None:
            return False, f"用法：/mc {spec.usage}", 1
        payload = await self._run(spec.name, **parsed)
        return bool(payload.get("success")), str(payload.get("content", "")), 2

    # ------------------------------------------------------------------ 动作参数解析
    def _parse_action_args(self, spec, args: str) -> dict[str, Any] | None:
        """把 "/mc <动作> <参数>" 的字符串解析成动作参数；解析不了返回 None。"""
        name = spec.name
        text = (args or "").strip()

        if name == "goto":
            coords = parse_coords(text)
            if coords is not None:
                return {"x": coords[0], "y": coords[1], "z": coords[2]}
            return {"player": text} if text else None
        if name == "look":
            coords = parse_coords(text)
            if coords is not None:
                return {"x": coords[0], "y": coords[1], "z": coords[2]}
            return {"target": text} if text else None
        if name == "place":
            parts = text.split()
            if len(parts) < 4:
                return None
            coords = parse_coords(" ".join(parts[1:]))
            if coords is None:
                return None
            return {"block": parts[0], "x": coords[0], "y": coords[1], "z": coords[2]}
        if name == "mine":
            parts = text.split()
            if not parts:
                return None
            count = 1
            if parts[-1].isdigit():
                count = int(parts[-1])
                parts = parts[:-1]
            return {"block": " ".join(parts), "count": max(1, count)}
        if name in {"chop", "dig", "pillar", "bridge", "nearby", "explore"}:
            number = parse_number(text, 0)
            if name == "chop":
                return {"count": max(1, number or 6)}
            if name == "dig":
                return {"steps": max(1, number or 3)}
            if name == "pillar":
                return {"count": max(1, number or 3)}
            if name == "bridge":
                return {"length": max(1, number or 3)}
            if name == "nearby":
                return {"radius": max(1.0, float(number or 10))}
            if name == "explore":
                return {"radius": max(4.0, float(number or 24))}
        if name in {"equip", "drop", "craft"}:
            return {"item": text} if text else None
        if name == "eat":
            return {"item": text} if text else {}
        if name == "recipes":
            return {"target": text}
        if name in {"follow", "follow_player", "attack", "come"}:
            return {"target": text} if text or name != "follow_player" else None
        if name == "say":
            return {"text": text} if text else None
        if name in {"disconnect", "stop", "resume", "swim", "home", "sethome", "jump", "flee",
                    "collect", "torch", "shelter", "inventory", "players", "where", "time",
                    "light"}:
            return {}
        return {}

    async def _mc_verify(self) -> tuple[bool, str, int]:
        if self._last_verification is None:
            return False, "还没有捕获到验证码：进服时加载界面出现验证码会自动转发到群", 1
        code, text = self._last_verification
        target = self._bot_config.verify_stream_id or self._resolve_stream()
        if not target:
            return False, "没有配置验证码转发聊天流（verify_stream_id / report_stream_id）", 1
        await self.ctx.send.text(text, target)
        return True, f"已重新发送验证码 {code}", 2

    async def _mc_diag(self) -> tuple[bool, str, int]:
        lines = [f"插件 v{__version__} / MaiBot {_detect_maibot_version()}"]
        bridge = self._bridge
        if bridge is None:
            lines.append("bridge 未初始化：插件可能未加载完成，或 WebUI 里处于停用状态")
        else:
            status = bridge.status()
            lines.append(f"目标：{status.get('configured_host', self._bot_config.host)}，角色 {self._bot_config.username}")
            if status.get("connect_target"):
                lines.append(f"实际连接：{status['connect_target']}（SRV {'是' if status.get('srv') else '否'}）")
            lines.append(
                f"client_state={status.get('client_state', 'none')} "
                f"connected={status.get('connected')} joined={status.get('joined')}"
            )
            lines.append(f"原生后端：Minecraft {status.get('minecraft_version', self._bot_config.minecraft_version)} / 协议 {status.get('protocol', '待连接')}")
            if status.get("last_error"):
                lines.append(f"最近错误：{status['last_error']}")
            lines.append(
                f"区块：已加载 {status.get('chunks')}，收到区块包 {status.get('chunk_packets')}，"
                f"解析失败 {status.get('chunk_errors')}"
            )
            if status.get("last_chunk_error"):
                lines.append(f"最后区块错误：{status['last_chunk_error']}")
            if status.get("last_chunk_hex"):
                lines.append(f"区块原始字节(前48)：{status['last_chunk_hex']}")
            if status.get("last_inventory_error"):
                lines.append(f"背包解析失败 {status.get('inventory_errors')} 次：{status['last_inventory_error']}")
            if status.get("light_errors"):
                lines.append(f"光照解析失败 {status['light_errors']} 次：{status.get('last_light_error', '')}")
            if status.get("crafting_window") is not None:
                lines.append(f"已打开容器：窗口 {status['crafting_window'].get('id')} 类型 {status['crafting_window'].get('type')}")
            lines.append(f"最近收包：{status.get('packet_age')} 秒前")
            lines.append(
                f"验证码监听：{'开' if self._bot_config.verify_enabled else '关'}，"
                f"目标流 {self._bot_config.verify_stream_id or self._bot_config.report_stream_id or '未配置'}"
            )
        return True, "\n".join(lines), 2

    def _can_use_command(self, kwargs: dict[str, Any]) -> bool:
        """MaiBot 1.3.x 没有命令级权限；配置了 admin_users 时由插件自己校验。

        兼容不同消息结构：平台字段可能缺失，配置写成 ``qq:123`` 或 ``123`` 都能匹配。
        """
        raw = (self._bot_config.admin_users or "").replace("，", ",")
        allowed = {item.strip() for item in raw.split(",") if item.strip()}
        if not allowed:
            return True

        message = kwargs.get("message") or {}
        user_info = self._field(message, "user_info") or {}
        chat_info = self._field(message, "chat_info") or {}
        user_id = str(
            self._field(user_info, "user_id") or self._field(message, "user_id") or ""
        ).strip()
        platform = str(
            self._field(message, "platform")
            or self._field(user_info, "platform")
            or self._field(chat_info, "platform")
            or kwargs.get("platform")
            or ""
        ).strip().lower()

        candidates = {user_id}
        if platform:
            candidates.add(f"{platform}:{user_id}")

        # 配置里写 qq:123 但消息里没有 platform 字段时，也允许按纯 ID 匹配
        allowed_values = set(allowed)
        for item in allowed:
            if ":" in item:
                suffix = item.split(":", 1)[1].strip()
                if suffix:
                    allowed_values.add(suffix)

        matched = bool(candidates & allowed_values)
        if not matched:
            self._log(f"命令权限拒绝：candidates={sorted(candidates)} allowed={sorted(allowed)}")
        return matched

    @staticmethod
    def _field(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)


    @staticmethod
    def _parse_host(text: str) -> tuple[str | None, int | None]:
        """``/mc connect host[:port]`` → (主机, 端口)。

        只写地址时返回端口 0，表示“按原版规则自动”：先查 SRV，再退回 25565。
        """
        from mcbot.address import parse_host_port

        text = (text or "").strip()
        if not text:
            return None, None
        try:
            host, port = parse_host_port(text)
        except ValueError:
            # 交给连接阶段给出具体错误，不在解析层吞掉用户输入
            return text, 0
        return host, port if port is not None else 0


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _detect_maibot_version() -> str:
    """读取当前 MaiBot 主程序版本（用于日志与兼容性排查）。"""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("MaiBot")
        except PackageNotFoundError:
            return "未知（非包安装）"
    except Exception:
        return "未知"


def create_plugin() -> MaiBotMinecraftPlugin:
    """MaiBot 插件工厂函数。"""
    return MaiBotMinecraftPlugin()

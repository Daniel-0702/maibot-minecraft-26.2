"""运行配置：插件配置模型、CLI 参数和内部默认值共用这一份结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .humanize import HumanProfile

__all__ = ["BotConfig", "DEFAULT_PERSONA"]

DEFAULT_PERSONA = (
    "你是麦麦，一个正在玩《我的世界》的普通玩家。你说话简短、口语化、偶尔用颜文字，"
    "会像真人一样吐槽、分享自己在挖矿/盖房子/打怪时的见闻。不要提自己是 AI 或程序，"
    "不要长篇大论，每次回复不超过 40 个字。"
)


@dataclass(slots=True)
class BotConfig:
    """一个 bot 实例的完整配置。"""

    # 连接
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 自动：先查 SRV，再退回 25565
    username: str = "MaiBot"
    minecraft_version: str = "auto"  # auto / 26.2 / 1.20.1
    auth: str = "offline"  # offline / microsoft
    microsoft_client_id: str = "00000000402b5328"
    auth_cache: str = ""
    view_distance: int = 8
    auto_connect: bool = True
    reconnect_delay: float = 8.0
    max_reconnect: int = 5

    # 行为
    autonomous: bool = True
    auto_eat: bool = True
    #: 允许自动合成（26.2 原生：玩家 2x2 与合成台 3x3）
    crafting: bool = True
    humanize: bool = True
    random_seed: int = 0
    chat_reply: bool = True
    chat_reply_cooldown: float = 8.0
    chat_reply_probability: float = 0.75
    persona: str = DEFAULT_PERSONA

    # LLM（可选，用于聊天与自主决策）
    llm_enabled: bool = False
    llm_model: str = ""
    llm_task: str = ""

    # 上报到 MaiBot
    report_stream_id: str = ""
    report_chat: bool = True
    report_death: bool = True
    report_prefix: str = "[MC]"

    # 权限（MaiBot 1.3.x 没有插件命令级权限时由插件自行校验）
    admin_users: str = ""

    # 进服验证码（加载界面显示的验证码 → 转发到 QQ 群）
    verify_enabled: bool = True
    verify_stream_id: str = ""  # 留空 = 用 report_stream_id / 最近会话
    verify_pattern: str = ""  # 留空 = 关键词 + 字母数字串自动识别
    verify_template: str = "[MC] 进服验证码：{code}"
    verify_window: float = 180.0  # 进服后多少秒内监听验证码
    verify_forward_source: bool = False  # 是否把原始提示文本也一起发出去

    # 人类化参数（None 表示用 HumanProfile 默认值）
    reaction_base: float | None = None
    typing_cps: float | None = None
    aim_speed: float | None = None

    def human_profile(self) -> HumanProfile:
        profile = HumanProfile()
        if self.reaction_base is not None:
            profile.reaction_base = max(0.05, float(self.reaction_base))
        if self.typing_cps is not None:
            profile.typing_cps = max(1.0, float(self.typing_cps))
        if self.aim_speed is not None:
            profile.aim_max_speed = max(2.0, float(self.aim_speed))
        return profile

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BotConfig":
        config = cls()
        if not data:
            return config
        for key, value in data.items():
            if not hasattr(config, key) or value is None:
                continue
            current = getattr(config, key)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int) and not isinstance(current, bool):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(config, key, value)
        return config

    def merged(self, **overrides: Any) -> "BotConfig":
        merged = BotConfig.from_dict(self.to_dict())
        for key, value in overrides.items():
            if value is not None and hasattr(merged, key):
                setattr(merged, key, value)
        return merged

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__  # type: ignore[attr-defined]
        }

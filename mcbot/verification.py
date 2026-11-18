"""进服验证码识别：从标题 / 副标题 / 动作栏 / 聊天 / Boss 栏里提取验证码。

有些服务器第一次进服时会把验证码显示在加载界面（标题或动作栏），要求玩家把验证码
发到 QQ 群完成绑定。这里只负责"从文本里找码 + 去重"，发送由插件层完成。
"""

from __future__ import annotations

import re
import time

__all__ = ["CodeExtractor", "VerificationTracker", "DEFAULT_KEYWORDS"]

#: 出现这些词才认为文本里可能有验证码（可用配置 verify_pattern 绕过）。
DEFAULT_KEYWORDS = (
    "验证码",
    "驗證碼",
    "认证码",
    "校验码",
    "verification",
    "verify",
    "code",
    "auth code",
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{4,10}")


class CodeExtractor:
    """从一段文本里提取验证码。

    * 未配置 ``pattern`` 时：要求文本包含关键词，再取第一个"像验证码"的字母数字串
      （优先包含数字、长度 4–10、排除关键词本身）。
    * 配置 ``pattern`` 时：按自定义正则搜索，优先取第 1 个捕获组。
    """

    def __init__(self, pattern: str = "", keywords: tuple[str, ...] | None = None) -> None:
        self.pattern = (pattern or "").strip()
        self.keywords = tuple(word.lower() for word in (keywords or DEFAULT_KEYWORDS))
        self._compiled = re.compile(self.pattern, re.IGNORECASE) if self.pattern else None

    def extract(self, text: str | None) -> str | None:
        if not text:
            return None
        if self._compiled is not None:
            match = self._compiled.search(text)
            if not match:
                return None
            value = match.group(1) if match.groups() else match.group(0)
            value = value.strip()
            return value or None
        lowered = text.lower()
        if not any(keyword in lowered for keyword in self.keywords):
            return None
        candidates = [
            token
            for token in _TOKEN_PATTERN.findall(text)
            if token.lower() not in self.keywords and not token.lower().startswith(("minecraft", "http"))
        ]
        if not candidates:
            return None
        # 优先含数字的候选，其次取第一个
        for token in candidates:
            if any(char.isdigit() for char in token):
                return token
        return candidates[0]


class VerificationTracker:
    """同一验证码在冷却时间内只处理一次（标题 + 聊天往往会重复出现）。"""

    def __init__(self, cooldown: float = 60.0) -> None:
        self.cooldown = max(0.0, float(cooldown))
        self._last: dict[str, float] = {}

    def accept(self, code: str) -> bool:
        if not code:
            return False
        now = time.monotonic()
        last = self._last.get(code)
        if last is not None and now - last < self.cooldown:
            return False
        self._last[code] = now
        # 限制记忆条数，避免长期运行无限增长
        if len(self._last) > 64:
            oldest = sorted(self._last.items(), key=lambda item: item[1])[:32]
            for key, _value in oldest:
                self._last.pop(key, None)
        return True

    def reset(self) -> None:
        self._last.clear()

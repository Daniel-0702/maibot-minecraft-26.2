"""麦麦能做的事：动作注册表与纯函数参数解析。

``/mc 子命令`` 和 LLM 工具都需要知道"麦麦到底会什么"。这个模块把动作集中成
一张可扩展的注册表，并提供中英文别名解析、分类帮助和几个纯函数参数解析辅助。
这里只有纯数据和纯函数：不访问网络、不读写文件、不碰游戏状态。

分类键用英文（见 :data:`CATEGORY_ORDER`），展示名用中文；:func:`categories`
按固定顺序返回中文名。``inventory``（背包）单独一类，让"合成"和"背包"在帮助
里分开显示，方便以后继续加动作而不改动调用方。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "ActionSpec",
    "ACTIONS",
    "CATEGORY_ORDER",
    "CATEGORY_LABELS",
    "CATEGORY_KEYS",
    "resolve",
    "format_help",
    "categories",
    "parse_coords",
    "parse_number",
    "parse_address",
]


# ------------------------------------------------------------------ 分类


CATEGORY_ORDER: tuple[tuple[str, str], ...] = (
    ("movement", "移动"),
    ("gather", "采集"),
    ("craft", "合成"),
    ("inventory", "背包"),
    ("build", "建造"),
    ("combat", "战斗"),
    ("info", "信息"),
    ("system", "系统"),
)
"""分类键与中文展示名的固定顺序。"""

CATEGORY_LABELS: dict[str, str] = dict(CATEGORY_ORDER)
CATEGORY_KEYS: tuple[str, ...] = tuple(key for key, _ in CATEGORY_ORDER)


# ------------------------------------------------------------------ 动作描述


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个可执行动作的静态描述。

    ``name`` 是规范名（英文小写），``aliases`` 同时收英文与中文别名；
    ``args`` 只描述参数名，具体怎么解析由调用方决定，这里不碰游戏状态。
    """

    name: str
    category: str
    summary: str
    usage: str
    aliases: tuple[str, ...] = ()
    args: tuple[str, ...] = ()


def _action(
    name: str,
    category: str,
    summary: str,
    usage: str,
    aliases: tuple[str, ...] = (),
    args: tuple[str, ...] = (),
) -> ActionSpec:
    """构造 :class:`ActionSpec`，同时按声明顺序去掉重复别名/参数。"""
    return ActionSpec(
        name=name,
        category=category,
        summary=summary,
        usage=usage,
        aliases=tuple(dict.fromkeys(aliases)),
        args=tuple(dict.fromkeys(args)),
    )


ACTIONS: tuple[ActionSpec, ...] = (
    # ------------------------------------------------------------ 移动
    _action(
        "goto",
        "movement",
        "走到指定坐标或玩家身边",
        "goto <x> <y> <z>",
        ("go", "去", "走到", "前往", "导航"),
        ("x", "y", "z"),
    ),
    _action("come", "movement", "走到正在说话的玩家身边", "come", ("来", "过来")),
    _action("follow", "movement", "跟在指定玩家身后", "follow [玩家]", ("跟随", "跟着"), ("player",)),
    _action(
        "look",
        "movement",
        "转头看向指定坐标或玩家",
        "look <x> <y> <z>",
        ("lookat", "看向", "看"),
        ("x", "y", "z"),
    ),
    _action("explore", "movement", "在附近探索，顺路找资源", "explore [半径]", ("探索", "逛逛"), ("radius",)),
    _action("swim", "movement", "游向岸边并爬上岸", "swim", ("上岸", "游上岸")),
    _action("home", "movement", "走回之前设置的家", "home", ("回家",)),
    _action("sethome", "movement", "把当前位置设为家", "sethome", ("设家", "设置家")),
    _action("jump", "movement", "原地跳一下", "jump", ("跳", "跳一下")),
    _action("stop", "movement", "立即停下当前动作", "stop", ("停", "停下")),
    _action("resume", "movement", "继续刚才被打断的动作", "resume", ("继续", "恢复")),
    # ------------------------------------------------------------ 采集
    _action(
        "mine",
        "gather",
        "挖掉指定方块，可指定数量",
        "mine <方块> [数量]",
        ("挖", "挖掘"),
        ("block", "count"),
    ),
    _action("chop", "gather", "砍树", "chop [数量]", ("砍树", "砍木"), ("count",)),
    _action("dig", "gather", "朝前下方挖阶梯（下矿用）", "dig [数量]", ("下挖", "挖矿"), ("count",)),
    _action(
        "collect",
        "gather",
        "捡起附近掉落的物品",
        "collect [半径]",
        ("捡", "捡东西", "拾取"),
        ("radius",),
    ),
    _action("eat", "gather", "吃东西补充饥饿值", "eat [食物]", ("吃", "吃东西"), ("item",)),
    # ------------------------------------------------------------ 合成
    _action(
        "craft",
        "craft",
        "按配方合成物品",
        "craft <物品> [数量]",
        ("合成", "制作"),
        ("item", "count"),
    ),
    _action(
        "recipes",
        "craft",
        "查看能做什么或某个物品的配方",
        "recipes [物品]",
        ("配方", "能做什么"),
        ("item",),
    ),
    _action("torch", "craft", "在脚下放一支火把照明", "torch", ("火把", "点火把")),
    # ------------------------------------------------------------ 背包
    _action("inventory", "inventory", "查看背包物品", "inventory", ("背包", "物品栏")),
    _action("equip", "inventory", "把物品拿在主手", "equip <物品>", ("装备", "拿在手上", "手持"), ("item",)),
    _action(
        "drop",
        "inventory",
        "丢弃背包里的物品",
        "drop [物品] [数量]",
        ("丢弃", "扔掉"),
        ("item", "count"),
    ),
    # ------------------------------------------------------------ 建造
    _action(
        "place",
        "build",
        "在指定位置放置方块",
        "place <方块> [x y z]",
        ("放置", "放方块"),
        ("block", "x", "y", "z"),
    ),
    _action("pillar", "build", "边跳边垫方块升高", "pillar [高度]", ("垫高", "起柱"), ("height",)),
    _action("bridge", "build", "向前搭桥或铺路", "bridge [长度]", ("搭桥", "铺路"), ("length",)),
    _action("shelter", "build", "就地挖一个避难所", "shelter", ("避难所", "躲起来")),
    # ------------------------------------------------------------ 战斗
    _action(
        "attack",
        "combat",
        "攻击指定目标",
        "attack [目标] [次数]",
        ("攻击", "打"),
        ("target", "count"),
    ),
    _action("flee", "combat", "从危险中逃跑", "flee", ("逃跑", "快跑")),
    # ------------------------------------------------------------ 信息
    _action("status", "info", "查看连接与角色状态", "status", ("状态",)),
    _action("say", "info", "在游戏里说一句话", "say 文字", ("说", "聊天", "说话", "发言"), ("text",)),
    _action("diag", "info", "输出诊断信息，排查连接问题", "diag", ("诊断",)),
    _action("nearby", "info", "列出附近的东西", "nearby [半径]", ("附近", "周围"), ("radius",)),
    _action("players", "info", "列出服务器在线玩家", "players", ("玩家", "在线玩家")),
    _action("where", "info", "报告当前坐标与维度", "where", ("坐标", "位置")),
    _action("time", "info", "查看游戏内时间", "time", ("时间",)),
    _action("light", "info", "查看当前光照亮度", "light", ("光照", "亮度")),
    _action("help", "info", "显示动作帮助", "help [分类]", ("帮助", "命令"), ("category",)),
    # ------------------------------------------------------------ 系统
    _action(
        "connect",
        "system",
        "连接到 Minecraft 服务器",
        "connect <地址[:端口]>",
        ("连接", "进服"),
        ("address",),
    ),
    _action("disconnect", "system", "断开当前服务器连接", "disconnect", ("断开", "退服", "下线")),
    _action("verify", "system", "重新发送进服验证码", "verify", ("验证码",)),
)


# ------------------------------------------------------------------ 解析


def _normalise(text: str) -> str:
    """把用户输入规范成可比较的动作名。

    去掉首尾空白与开头的 ``/``，并把 ``/mc craft``、``mc craft`` 这类带命令
    前缀的写法还原成 ``craft``。非字符串一律当成空串，让调用方返回 ``None``。
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.strip().replace("\u3000", " ").strip()
    while cleaned.startswith("/"):
        cleaned = cleaned[1:].lstrip()
    if not cleaned:
        return ""
    parts = cleaned.split(None, 1)
    if parts[0].casefold() == "mc":
        return parts[1].strip() if len(parts) > 1 else ""
    return cleaned


def resolve(name_or_alias: str) -> ActionSpec | None:
    """按规范名或别名查找动作；先精确匹配 ``name``，再匹配 ``aliases``。

    大小写不敏感，容忍首尾空白、开头的 ``/`` 以及 ``/mc`` 命令前缀。
    找不到时返回 ``None``。
    """
    needle = _normalise(name_or_alias)
    if not needle:
        return None
    folded = needle.casefold()

    for spec in ACTIONS:
        if spec.name.casefold() == folded:
            return spec
    for spec in ACTIONS:
        for alias in spec.aliases:
            if alias.casefold() == folded:
                return spec
    return None


_USAGE_WIDTH = max(19, max(len(spec.usage) for spec in ACTIONS) + 1)


def _format_section(category: str) -> str:
    """输出一个分类的小节，含标题和每个动作的一行说明。"""
    lines = [f"【{CATEGORY_LABELS[category]}】"]
    for spec in ACTIONS:
        if spec.category != category:
            continue
        padding = " " * max(1, _USAGE_WIDTH - len(spec.usage))
        line = f"  {spec.usage}{padding}{spec.summary}"
        if spec.aliases:
            line += f"（别名：{'/'.join(spec.aliases)}）"
        lines.append(line)
    return "\n".join(lines)


def _category_key(category: str | None) -> str | None:
    """把中文展示名或英文分类键统一成英文键；未知分类返回 ``None``。"""
    if not isinstance(category, str):
        return None
    needle = category.strip().casefold()
    if not needle:
        return None
    for key, label in CATEGORY_ORDER:
        if needle in (key.casefold(), label.casefold()):
            return key
    return None


def format_help(category: str | None = None) -> str:
    """按分类输出中文帮助。

    ``category`` 可以是中文展示名（``"采集"``）或英文分类键（``"gather"``）；
    为 ``None``、空串或未知分类时输出全部分类。
    """
    key = _category_key(category)
    if key is not None:
        return _format_section(key)
    return "\n\n".join(_format_section(key) for key, _ in CATEGORY_ORDER)


def categories() -> tuple[str, ...]:
    """按固定顺序返回中文分类名。"""
    return tuple(label for _, label in CATEGORY_ORDER)


# ------------------------------------------------------------------ 参数解析


def parse_coords(text: str) -> tuple[float, float, float] | None:
    """解析 ``x y z`` 坐标，支持空格、逗号与一层圆括号。

    ``"10 64 -8"``、``"10,64,-8"``、``"(10 64 -8)"`` 都返回三个浮点数；
    不是恰好三个有限数字（或格式不对）时返回 ``None``。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.startswith("(") or raw.endswith(")"):
        if not (raw.startswith("(") and raw.endswith(")")):
            return None
        raw = raw[1:-1].strip()
        if not raw:
            return None

    parts = raw.replace("，", " ").replace(",", " ").split()
    if len(parts) != 3:
        return None
    try:
        x, y, z = (float(part) for part in parts)
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in (x, y, z)):
        return None
    return x, y, z


_CN_DIGITS: dict[str, int] = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_TEN = "十"


def _parse_chinese_number(text: str) -> int | None:
    """解析一~九十九的中文数字（支持``十``、``十一``、``二十``、``二十五``）。"""
    if not text:
        return None
    if _CN_TEN not in text:
        return _CN_DIGITS.get(text)
    if text.count(_CN_TEN) != 1:
        return None

    tens_text, _, ones_text = text.partition(_CN_TEN)
    if not tens_text:
        tens = 1
    elif len(tens_text) == 1 and tens_text in _CN_DIGITS:
        tens = _CN_DIGITS[tens_text]
    else:
        return None

    if not ones_text:
        ones = 0
    elif len(ones_text) == 1 and ones_text in _CN_DIGITS:
        ones = _CN_DIGITS[ones_text]
    else:
        return None
    return tens * 10 + ones


def parse_number(text: str, default: int | None = None) -> int | None:
    """解析非负整数，支持阿拉伯数字与中文数字。

    ``"12"``、``"十二"``、``"二十"``、``"两"`` 都能解析；解析失败时返回
    ``default``（默认 ``None``）。
    """
    if not isinstance(text, str):
        return default
    raw = text.strip()
    if not raw:
        return default
    if raw.isdigit():
        try:
            return int(raw)
        except ValueError:  # 某些 Unicode 数字字符能通过 isdigit 但不能转 int
            return default
    value = _parse_chinese_number(raw)
    return default if value is None else value


def _parse_port(text: str) -> int | None:
    """解析 1..65535 的 ASCII 十进制端口，非法时返回 ``None``。"""
    port_text = text.strip()
    if not port_text or not port_text.isascii() or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 1 <= port <= 65535:
        return None
    return port


def parse_address(text: str) -> tuple[str, int | None] | None:
    """解析 ``host``、``host:25565``、``[::1]:25565`` 三种服务器地址写法。

    这里刻意不依赖 :mod:`mcbot.address`，只做"拆主机和端口"这件小事：
    没写端口时端口为 ``None``；空地址、空主机或端口不在 1..65535 时返回
    ``None``。未加方括号的 IPv6（如 ``::1``）整段视为主机。
    """
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    if raw.startswith("["):
        close = raw.find("]")
        if close < 0:
            return None
        host = raw[1:close].strip()
        if not host:
            return None
        rest = raw[close + 1 :].strip()
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return None
        port = _parse_port(rest[1:])
        if port is None:
            return None
        return host, port

    colons = raw.count(":")
    if colons == 0:
        return raw, None
    if colons == 1:
        host, _, port_text = raw.partition(":")
        host = host.strip()
        if not host:
            return None
        port = _parse_port(port_text)
        if port is None:
            return None
        return host, port

    # 多于一个冒号且没加方括号：按裸 IPv6 处理，不解析端口。
    return raw, None


# ------------------------------------------------------------------ 注册表自检


def _validate_registry() -> None:
    """导入时检查注册表的基本不变量，扩展动作时尽早暴露手误。"""
    names = [spec.name for spec in ACTIONS]
    if len(names) != len(set(names)):
        raise ValueError("ACTIONS 里存在重复的 name")
    name_set = set(names)
    covered = {spec.category for spec in ACTIONS}
    missing = [key for key, _ in CATEGORY_ORDER if key not in covered]
    if missing:
        raise ValueError(f"这些分类还没有动作：{missing}")

    alias_owner: dict[str, str] = {}
    for spec in ACTIONS:
        if spec.category not in CATEGORY_LABELS:
            raise ValueError(f"{spec.name} 的分类非法：{spec.category!r}")
        if not spec.name.isascii() or spec.name != spec.name.lower() or not spec.name.isidentifier():
            raise ValueError(f"name 必须是英文小写标识符：{spec.name!r}")
        if not spec.summary.strip() or not spec.usage.strip():
            raise ValueError(f"{spec.name} 缺少 summary 或 usage")
        if not spec.usage.startswith(spec.name):
            raise ValueError(f"{spec.name} 的 usage 应以 name 开头：{spec.usage!r}")
        if len(spec.args) != len(set(spec.args)):
            raise ValueError(f"{spec.name} 的参数名重复：{spec.args!r}")
        for arg in spec.args:
            if not arg.isascii() or not arg.isidentifier():
                raise ValueError(f"{spec.name} 的参数名不合法：{arg!r}")
        for alias in spec.aliases:
            if not alias or alias != alias.strip():
                raise ValueError(f"{spec.name} 的别名不能为空或带首尾空白：{alias!r}")
            folded = alias.casefold()
            if folded in name_set:
                raise ValueError(f"{spec.name} 的别名和某个 name 冲突：{alias!r}")
            owner = alias_owner.get(folded)
            if owner is not None:
                raise ValueError(f"别名 {alias!r} 同时属于 {owner} 和 {spec.name}")
            alias_owner[folded] = spec.name


_validate_registry()

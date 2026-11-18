"""从 Minecraft Wiki 整理的 Java 版机制常量与计算。

这个模块集中放"游戏规则"，让上层逻辑（物理、采集、战斗、大脑）不再靠猜。
所有数值都来自 Minecraft Wiki（Java 版条目），文档字符串里标注了来源：

* 破坏方块（硬度 / 工具速度 / 水下与悬空惩罚 / 瞬间破坏）：
  https://minecraft.wiki/w/Breaking
* 伤害（摔落、溺水、攻击冷却、暴击、岩浆、爆炸）：
  https://minecraft.wiki/w/Damage
* 饥饿与饱和、疾跑门槛：https://minecraft.wiki/w/Hunger 、
  https://minecraft.wiki/w/Sprinting
* 生物行为：爬行者 https://minecraft.wiki/w/Creeper 、
  末影人 https://minecraft.wiki/w/Enderman 、
  蜘蛛 https://minecraft.wiki/w/Spider 、
  僵尸 https://minecraft.wiki/w/Zombie 、
  骷髅 https://minecraft.wiki/w/Skeleton

只收录插件真正用得上的规则；没有把握的数值宁可不写，也不要瞎编。
"""

from __future__ import annotations

import math

__all__ = [
    "TOOL_SPEED_BY_TIER",
    "SHEARS_BLOCK_MULTIPLIERS",
    "PROPERTY_MULTIPLIERS",
    "HARDNESS_INSTANT",
    "mining_ticks",
    "mining_seconds",
    "instant_break",
    "FALL_SAFE_BLOCKS",
    "fall_damage",
    "fall_is_safe",
    "DROWNING_AIR_TICKS",
    "DROWNING_DAMAGE_HP",
    "DROWNING_DAMAGE_INTERVAL_TICKS",
    "submerged_seconds_until_damage",
    "HUNGER_MAX",
    "SPRINT_MIN_HUNGER",
    "HEALTH_REGEN_MIN_HUNGER",
    "sprint_allowed",
    "natural_regen_allowed",
    "weapon_attack_speed",
    "attack_cooldown_seconds",
    "attack_damage_factor",
    "CRITICAL_MULTIPLIER",
    "MobProfile",
    "MOB_PROFILES",
    "mob_profile",
    "is_gaze_provoked_mob",
    "is_daylight_neutral",
    "burns_in_daylight",
    "explosive_blast_radius",
    "DEFAULT_FOLLOW_RANGE",
    "DEFAULT_DETECT_RANGE",
    "CREEPER_FUSE_SECONDS",
    "enderman_safe_aim_height",
]

# ------------------------------------------------------------------ 破坏方块
# Wiki: 工具速度——空手 1，木 2，石 4，铜 5，铁 6，钻石 8，下界合金 9，金 12，
# 剪刀 2，剑 1.5。
TOOL_SPEED_BY_TIER = {
    "wooden": 2.0,
    "wood": 2.0,
    "golden": 12.0,
    "gold": 12.0,
    "stone": 4.0,
    "copper": 5.0,
    "iron": 6.0,
    "diamond": 8.0,
    "netherite": 9.0,
}

#: 方块专属加成（Wiki: 剪刀对树叶/羊毛/蛛网等有额外倍数）。
SHEARS_BLOCK_MULTIPLIERS = {
    "leaves": 15.0,
    "wool": 5.0,
    "cobweb": 15.0,
    "vine": 15.0,
    "glow_lichen": 15.0,
    "moss_carpet": 15.0,
    "bamboo": 30.0,
}

#: 剑砍植物类方块偏快的倍数（Wiki: Bamboo 30）。
PROPERTY_MULTIPLIERS = {"sword_vs_plant": 1.5}

#: 原版把 hardness=0 的方块（花草等）视为瞬间破坏。
HARDNESS_INSTANT = 0.0

#: 水下降速：没有水下速掘附魔时，破块速度 ÷5；悬空再 ÷5（合计 25 倍）。
SUBMERGED_MINING_DIVISOR = 5.0
AIRBORNE_MINING_DIVISOR = 5.0


def mining_ticks(
    hardness: float,
    tool_speed: float = 1.0,
    *,
    can_harvest: bool = True,
    submerged: bool = False,
    on_ground: bool = True,
    efficiency_level: int = 0,
) -> int | None:
    """按 Wiki 公式返回破坏该方块所需 tick 数；``None`` 表示无法破坏。

    Wiki 公式：``damage = speed / hardness / 30（可收获）或 /100``，
    每 tick 施加一次 ``damage``，``ticks = ceil(1 / damage)``；
    ``damage >= 1`` 时瞬间破坏（返回 0）。
    """
    if hardness < 0:
        return None
    if hardness <= HARDNESS_INSTANT:
        return 0
    speed = max(1.0, float(tool_speed))
    if efficiency_level > 0:
        speed += efficiency_level * efficiency_level + 1
    if submerged:
        speed /= SUBMERGED_MINING_DIVISOR
    if not on_ground:
        speed /= AIRBORNE_MINING_DIVISOR
    damage = speed / float(hardness)
    damage /= 30.0 if can_harvest else 100.0
    if damage <= 0.0:
        return None
    if damage >= 1.0:
        return 0
    return max(1, math.ceil(1.0 / damage))


def mining_seconds(
    hardness: float,
    tool_speed: float = 1.0,
    *,
    can_harvest: bool = True,
    submerged: bool = False,
    on_ground: bool = True,
    efficiency_level: int = 0,
) -> float | None:
    """同 :func:`mining_ticks`，返回秒数。"""
    ticks = mining_ticks(
        hardness,
        tool_speed,
        can_harvest=can_harvest,
        submerged=submerged,
        on_ground=on_ground,
        efficiency_level=efficiency_level,
    )
    return None if ticks is None else ticks / 20.0


def instant_break(
    hardness: float,
    tool_speed: float = 1.0,
    *,
    can_harvest: bool = True,
    submerged: bool = False,
    on_ground: bool = True,
) -> bool:
    return (
        mining_ticks(
            hardness,
            tool_speed,
            can_harvest=can_harvest,
            submerged=submerged,
            on_ground=on_ground,
        )
        == 0
    )


# ------------------------------------------------------------------ 摔落
# Wiki: 摔落伤害 = max(0, 下落格数 - 3)，每点 = 半颗心；3 格以内不受伤。
FALL_SAFE_BLOCKS = 3.0


def fall_damage(distance: float) -> int:
    """摔落 ``distance`` 格造成的伤害（HP，2 HP = 1 颗心）。"""
    if distance <= FALL_SAFE_BLOCKS:
        return 0
    return int(math.floor(distance) - FALL_SAFE_BLOCKS)


def fall_is_safe(distance: float) -> bool:
    return distance <= FALL_SAFE_BLOCKS


# ------------------------------------------------------------------ 溺水
# Wiki: 大多数生物氧气上限 300 tick（15 秒）；耗尽后每秒受到 2 HP 溺水伤害。
DROWNING_AIR_TICKS = 300
DROWNING_DAMAGE_HP = 2
DROWNING_DAMAGE_INTERVAL_TICKS = 20


def submerged_seconds_until_damage() -> float:
    """从"头刚入水"到开始掉血的安全秒数。"""
    return DROWNING_AIR_TICKS / 20.0


# ------------------------------------------------------------------ 饥饿
HUNGER_MAX = 20
#: Wiki: 饥饿值 ≤ 6 时不能疾跑。
SPRINT_MIN_HUNGER = 7
#: Wiki「Healing」：饥饿 ≥ 18 才会自然回血（1 HP / 4 秒）；20 且有饱和度时更快。
HEALTH_REGEN_MIN_HUNGER = 18


def sprint_allowed(food: int) -> bool:
    return int(food) >= SPRINT_MIN_HUNGER


def natural_regen_allowed(food: int) -> bool:
    """按 Wiki：饥饿值 ≥ 18 时受伤会自然回血。"""
    return int(food) >= HEALTH_REGEN_MIN_HUNGER


# ------------------------------------------------------------------ 攻击
# Wiki: 攻击冷却 T = 20 / 攻击速度（tick）；伤害倍率 = 0.2 + ((t+0.5)/T)^2 × 0.8。
WEAPON_ATTACK_SPEED = {
    "sword": 1.6,
    "axe": 0.9,
    "pickaxe": 1.2,
    "shovel": 1.0,
    "hoe": 1.0,
    "trident": 1.1,
    "mace": 0.6,
}
FIST_ATTACK_SPEED = 4.0
#: 暴击伤害倍率（Wiki: 150%）。
CRITICAL_MULTIPLIER = 1.5


def weapon_attack_speed(item_short_name: str = "") -> float:
    """按物品短名推断攻击速度（原版值）。空手 4.0。"""
    name = (item_short_name or "").lower()
    for kind, speed in WEAPON_ATTACK_SPEED.items():
        if name == kind or name.endswith("_" + kind):
            if kind == "axe":
                # 斧头攻击速度按材质略有差别，取 Wiki 的 0.8~1.0 区间中值
                return 0.9
            return speed
    return FIST_ATTACK_SPEED


def attack_cooldown_seconds(item_short_name: str = "") -> float:
    """把攻击条充满需要的秒数；等满再点击才有全额伤害。"""
    speed = max(0.1, weapon_attack_speed(item_short_name))
    return 1.0 / speed


def attack_damage_factor(ticks_since_last: float, item_short_name: str = "") -> float:
    """距离上次攻击 ``ticks`` 个 tick 时的伤害倍率（0.2 ~ 1.0）。"""
    speed = max(0.1, weapon_attack_speed(item_short_name))
    total = 20.0 / speed
    t = max(0.0, min(float(ticks_since_last), total))
    ratio = (t + 0.5) / total
    return min(1.0, 0.2 + (ratio * ratio) * 0.8)


# ------------------------------------------------------------------ 生物
DEFAULT_FOLLOW_RANGE = 16.0
#: 僵尸的跟随距离比一般生物远得多（Wiki/Mojira MC-81199：16 → 僵尸 40）。
ZOMBIE_FOLLOW_RANGE = 40.0
#: 大多数敌对生物发现玩家的距离。
DEFAULT_DETECT_RANGE = 16.0
CREEPER_FUSE_SECONDS = 1.5
CREEPER_BLAST_RADIUS = 3.5


class MobProfile:
    """一种生物的威胁特征（只列插件用得上的）。"""

    __slots__ = (
        "name",
        "hostile",
        "burns_in_daylight",
        "daylight_neutral",
        "explosive",
        "gaze_provoked",
        "ranged",
        "follow_range",
        "blast_radius",
    )

    def __init__(
        self,
        name: str,
        *,
        hostile: bool = True,
        burns_in_daylight: bool = False,
        daylight_neutral: bool = False,
        explosive: bool = False,
        gaze_provoked: bool = False,
        ranged: bool = False,
        follow_range: float = DEFAULT_FOLLOW_RANGE,
        blast_radius: float = 0.0,
    ) -> None:
        self.name = name
        self.hostile = hostile
        self.burns_in_daylight = burns_in_daylight
        self.daylight_neutral = daylight_neutral
        self.explosive = explosive
        self.gaze_provoked = gaze_provoked
        self.ranged = ranged
        self.follow_range = follow_range
        self.blast_radius = blast_radius

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"MobProfile({self.name!r})"


def _profile(name: str, **kwargs) -> MobProfile:
    return MobProfile(name, **kwargs)


#: 关键生物档案。未列出的敌对生物按"普通近战敌人"处理。
MOB_PROFILES: dict[str, MobProfile] = {
    # 僵尸系：白天会在阳光下着火
    "zombie": _profile("zombie", burns_in_daylight=True, follow_range=ZOMBIE_FOLLOW_RANGE),
    "zombie_villager": _profile("zombie_villager", burns_in_daylight=True, follow_range=ZOMBIE_FOLLOW_RANGE),
    "husk": _profile("husk", follow_range=ZOMBIE_FOLLOW_RANGE),
    "drowned": _profile("drowned", follow_range=ZOMBIE_FOLLOW_RANGE),
    # 骷髅系：白天着火，远程
    "skeleton": _profile("skeleton", burns_in_daylight=True, ranged=True),
    "stray": _profile("stray", burns_in_daylight=True, ranged=True),
    "bogged": _profile("bogged", burns_in_daylight=True, ranged=True),
    "phantom": _profile("phantom", burns_in_daylight=True),
    # 爬行者：会爆炸，靠近到约 1 格开始 1.5 秒引信
    "creeper": _profile("creeper", explosive=True, blast_radius=CREEPER_BLAST_RADIUS),
    # 末影人：中立，盯着眼睛会被激怒
    "enderman": _profile("enderman", hostile=False, gaze_provoked=True),
    # 蜘蛛：亮度足够时中立（白天基本不主动）
    "spider": _profile("spider", daylight_neutral=True),
    "cave_spider": _profile("cave_spider"),
    # 远程
    "witch": _profile("witch", ranged=True),
    "pillager": _profile("pillager", ranged=True),
    "blaze": _profile("blaze", ranged=True),
    "ghast": _profile("ghast", ranged=True, follow_range=100.0),
    "slime": _profile("slime"),
    "magma_cube": _profile("magma_cube"),
    "silverfish": _profile("silverfish"),
    "endermite": _profile("endermite"),
    "vex": _profile("vex"),
    "evoker": _profile("evoker"),
    "vindicator": _profile("vindicator"),
    "ravager": _profile("ravager"),
    "warden": _profile("warden"),
    "breeze": _profile("breeze", ranged=True),
    "piglin": _profile("piglin"),
    "piglin_brute": _profile("piglin_brute"),
    "hoglin": _profile("hoglin"),
    "zoglin": _profile("zoglin"),
    "shulker": _profile("shulker"),
    "guardian": _profile("guardian", ranged=True),
    "elder_guardian": _profile("elder_guardian", ranged=True),
    "wither_skeleton": _profile("wither_skeleton"),
}


def mob_profile(short_name: str) -> MobProfile | None:
    """按短名取生物档案；未知生物返回 ``None``。"""
    return MOB_PROFILES.get((short_name or "").lower())


def is_gaze_provoked_mob(short_name: str) -> bool:
    profile = mob_profile(short_name)
    return bool(profile and profile.gaze_provoked)


def is_daylight_neutral(short_name: str) -> bool:
    profile = mob_profile(short_name)
    return bool(profile and profile.daylight_neutral)


def burns_in_daylight(short_name: str) -> bool:
    profile = mob_profile(short_name)
    return bool(profile and profile.burns_in_daylight)


def explosive_blast_radius(short_name: str) -> float:
    profile = mob_profile(short_name)
    if profile is None or not profile.explosive:
        return 0.0
    return profile.blast_radius


def enderman_safe_aim_height(eye_avoid: bool = True) -> float:
    """末影人：盯着它的眼睛会被激怒，所以瞄躯干/脚。

    Wiki（Enderman）：直视头部（眼睛）会被激怒；对着身体下方打是安全的。
    """
    return 0.6 if eye_avoid else 1.4

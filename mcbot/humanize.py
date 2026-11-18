"""把机器输入伪装成真人操作：反应、瞄准、打字、待机与休息。

本模块只负责生成"计划"，绝不主动 sleep；所有节奏都由调用方按返回的
延迟/增量去执行。只要注入同一个随机种子，行为就是完全确定的。
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from .types import Rotation, wrap_degrees

__all__ = [
    "HumanProfile",
    "AimPlan",
    "TypingEvent",
    "TypingPlan",
    "Humanizer",
    "IDLE_ACTIONS",
]

#: 小幅度张望时的边界（度）。
_SCAN_YAW_LIMIT = 35.0
_SCAN_PITCH_LIMIT = 15.0

#: 常被当作"词边界"的标点。
_BOUNDARY_CHARS = frozenset(" \t\n.,!?;:，。！？；：、…")

#: QWERTY 键盘上相邻的键，用来模拟按偏。
_QWERTY_NEIGHBORS: dict[str, tuple[str, ...]] = {
    "q": ("w", "a"),
    "w": ("q", "e", "s"),
    "e": ("w", "r", "d"),
    "r": ("e", "t", "f"),
    "t": ("r", "y", "g"),
    "y": ("t", "u", "h"),
    "u": ("y", "i", "j"),
    "i": ("u", "o", "k"),
    "o": ("i", "p", "l"),
    "p": ("o", "l"),
    "a": ("s", "q", "z"),
    "s": ("a", "d", "w", "x"),
    "d": ("s", "f", "e", "c"),
    "f": ("d", "g", "r", "v"),
    "g": ("f", "h", "t", "b"),
    "h": ("g", "j", "y", "n"),
    "j": ("h", "k", "u", "m"),
    "k": ("j", "l", "i"),
    "l": ("k", "p", "o"),
    "z": ("x", "a"),
    "x": ("z", "c", "s"),
    "c": ("x", "v", "d"),
    "v": ("c", "b", "f"),
    "b": ("v", "n", "g"),
    "n": ("b", "m", "h"),
    "m": ("n", "j"),
}

_LOWER_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _finite(value: float, fallback: float = 0.0) -> float:
    """把 NaN/Inf 换成安全值，保证计划里不会出现 NaN。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _clamp(value: float, low: float, high: float) -> float:
    """夹取，并容忍 low > high。"""
    if low > high:
        low, high = high, low
    return max(low, min(high, value))


def _clamp_pitch(pitch: float) -> float:
    """pitch 的合法范围是 [-90, 90]。"""
    return _clamp(_finite(pitch, 0.0), -90.0, 90.0)


def _ease_out_cubic(progress: float) -> float:
    """三次缓出：开头快、结尾慢。"""
    t = _clamp(progress, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


@dataclass(slots=True)
class HumanProfile:
    """一组可调的"人类化"参数。"""

    reaction_base: float = 0.20
    reaction_sigma: float = 0.45
    reaction_min: float = 0.08
    reaction_max: float = 0.9
    aim_max_speed: float = 14.0
    aim_ease_ticks_min: int = 4
    aim_ease_ticks_max: int = 14
    aim_overshoot: float = 2.5
    aim_tremor: float = 0.5
    typing_cps: float = 5.5
    typing_jitter: float = 0.35
    typo_rate: float = 0.025
    think_pause_rate: float = 0.06
    break_after_min: float = 480.0
    break_after_max: float = 1500.0
    break_duration_min: float = 15.0
    break_duration_max: float = 120.0


class AimPlan:
    """把一次转向拆成若干 tick 的视角增量。"""

    def __init__(
        self,
        current: Rotation,
        target: Rotation,
        profile: HumanProfile,
        rng: random.Random,
    ) -> None:
        self.steps: list[Rotation] = []
        self._index = 0

        start_yaw = wrap_degrees(_finite(current.yaw, 0.0))
        start_pitch = _clamp_pitch(current.pitch)
        target_yaw = wrap_degrees(_finite(target.yaw, 0.0))
        target_pitch = _clamp_pitch(target.pitch)

        delta_yaw = wrap_degrees(target_yaw - start_yaw)
        delta_pitch = target_pitch - start_pitch
        distance = max(abs(delta_yaw), abs(delta_pitch))

        max_speed = max(1e-6, abs(_finite(profile.aim_max_speed, 14.0)))
        min_ticks = max(1, int(_finite(profile.aim_ease_ticks_min, 4)))
        max_ticks = max(min_ticks, int(_finite(profile.aim_ease_ticks_max, 14)))
        ticks = int(math.ceil(distance / max_speed))
        ticks = max(min_ticks, min(max_ticks, ticks))

        step_limit = max_speed * 1.5
        tremor_amp = max(0.0, _finite(profile.aim_tremor, 0.0))
        overshoot_max = max(0.0, _finite(profile.aim_overshoot, 0.0))
        # 过冲量不能比整段位移还大，也不能让最后一步无法收敛。
        overshoot = rng.uniform(0.0, overshoot_max) if overshoot_max > 0.0 else 0.0
        overshoot = min(overshoot, distance * 0.5, step_limit * 0.5)
        overshoot_frac = overshoot / distance if distance > 1e-9 else 0.0

        previous = Rotation(start_yaw, start_pitch)
        for index in range(1, ticks + 1):
            if index == ticks:
                desired_yaw = target_yaw
                desired_pitch = target_pitch
            else:
                progress = index / ticks
                # 约 70% 处略微冲过目标，随后收敛回来。
                if progress <= 0.7:
                    bump = _ease_out_cubic(progress / 0.7)
                else:
                    tail = (progress - 0.7) / 0.3
                    bump = (1.0 - tail) ** 2
                travel = _ease_out_cubic(progress) + overshoot_frac * bump
                decay = max(0.0, 1.0 - progress)
                desired_yaw = (
                    start_yaw
                    + delta_yaw * travel
                    + rng.gauss(0.0, 1.0) * tremor_amp * decay
                )
                desired_pitch = (
                    start_pitch
                    + delta_pitch * travel
                    + rng.gauss(0.0, 1.0) * tremor_amp * decay
                )

            desired_pitch = _clamp_pitch(desired_pitch)
            yaw_step = _clamp(
                wrap_degrees(desired_yaw - previous.yaw),
                -step_limit,
                step_limit,
            )
            pitch_step = _clamp(
                desired_pitch - previous.pitch,
                -step_limit,
                step_limit,
            )
            # 再按二维步长限制一次，避免 yaw/pitch 同时拉满时超速。
            step_length = math.hypot(yaw_step, pitch_step)
            if step_length > step_limit:
                scale = step_limit / step_length
                yaw_step *= scale
                pitch_step *= scale
            yaw = wrap_degrees(previous.yaw + yaw_step)
            pitch = _clamp_pitch(previous.pitch + pitch_step)
            if not (math.isfinite(yaw) and math.isfinite(pitch)):
                yaw, pitch = previous.yaw, previous.pitch
            previous = Rotation(yaw, pitch)
            self.steps.append(previous)

        # 最终务必精确收敛；tremor/过冲只影响过程。
        final = Rotation(target_yaw, target_pitch)
        if self.steps:
            self.steps[-1] = final
        else:
            self.steps.append(final)

    @property
    def done(self) -> bool:
        """是否已经取完所有 tick。"""
        return self._index >= len(self.steps)

    def next_rotation(self) -> Rotation | None:
        """取下一个 tick 的视角；耗尽后返回 None。"""
        if self.done:
            return None
        rotation = self.steps[self._index]
        self._index += 1
        return rotation

    def __iter__(self) -> Iterator[Rotation]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)


@dataclass(frozen=True, slots=True)
class TypingEvent:
    """一个打字事件；``delay`` 表示事件发生前需要等待的秒数。"""

    kind: str
    text: str = ""
    delay: float = 0.0


class TypingPlan:
    """一段聊天文本的"真人打字"计划。"""

    def __init__(self, text: str, profile: HumanProfile, rng: random.Random) -> None:
        self.text = str(text)
        self.events: list[TypingEvent] = []
        self._cps = max(1e-3, abs(_finite(profile.typing_cps, 5.5)))
        self._jitter = max(0.0, _finite(profile.typing_jitter, 0.0))
        self._typo_rate = _finite(profile.typo_rate, 0.0)
        self._think_rate = _finite(profile.think_pause_rate, 0.0)
        self._rng = rng
        self._build()

    def _char_delay(self) -> float:
        """单个字符的击键间隔，带 ±jitter 抖动。"""
        factor = 1.0 + self._rng.uniform(-self._jitter, self._jitter)
        return max(0.0, (1.0 / self._cps) * factor)

    def _backspace_delay(self) -> float:
        """退格键的间隔通常比正常击键更短、更急。"""
        return self._rng.uniform(0.04, 0.12)

    def _append(self, kind: str, text: str, delay: float) -> None:
        self.events.append(TypingEvent(kind, text, max(0.0, _finite(delay, 0.0))))

    def _maybe_pause(self, char: str) -> None:
        """在空格/标点后插入一个短暂的思考停顿。"""
        if char not in _BOUNDARY_CHARS:
            return
        if self.events and self.events[-1].kind == "pause":
            return
        if self._rng.random() < self._think_rate:
            delay = self._rng.uniform(0.08, 0.45)
        else:
            delay = 0.15
        self._append("pause", "", delay)

    def _wrong_char(self, char: str) -> str:
        """返回 QWERTY 邻键；没有映射时退回随机小写字母。"""
        lower = char.lower()
        candidates = _QWERTY_NEIGHBORS.get(lower)
        if candidates:
            wrong = self._rng.choice(candidates)
        else:
            wrong = self._rng.choice(_LOWER_LETTERS)
        if wrong == lower:
            wrong = "x" if lower != "x" else "z"
        return wrong.upper() if char.isupper() else wrong

    def _build(self) -> None:
        text = self.text
        length = len(text)
        index = 0
        while index < length:
            char = text[index]
            if self._rng.random() < self._typo_rate:
                # 打错一个字后继续敲 1~3 个字符，发现不对再一起退格重打。
                extra = self._rng.randint(1, 3)
                extra = min(extra, length - index - 1)
                span = text[index : index + extra + 1]
                self._append("char", self._wrong_char(char), self._char_delay())
                for temp in span[1:]:
                    self._append("char", temp, self._char_delay())
                for _ in range(len(span)):
                    self._append("backspace", "", self._backspace_delay())
                for correct in span:
                    self._append("char", correct, self._char_delay())
                self._maybe_pause(span[-1])
                index += len(span)
            else:
                self._append("char", char, self._char_delay())
                self._maybe_pause(char)
                index += 1

    @property
    def duration(self) -> float:
        """整段打字计划的总时长（秒）。"""
        return sum(event.delay for event in self.events)

    def apply(self, typed: str = "") -> str:
        """在已有文本上模拟执行事件，返回最终文本（测试用）。"""
        result = str(typed)
        for event in self.events:
            if event.kind == "char":
                result += event.text
            elif event.kind == "backspace" and result:
                result = result[:-1]
        return result

    def chunks(self) -> list[tuple[float, str]]:
        """合并连续击键，返回 ``(等待秒数, 文本)``。

        文本里的 ``\\b`` 表示退格；停顿会打断合并并单独成为一个空文本块。
        每个块的等待时间包含该块内所有事件的延迟之和。
        """
        result: list[tuple[float, str]] = []
        buffer: list[str] = []
        delay = 0.0

        def flush() -> None:
            nonlocal buffer, delay
            if buffer:
                result.append((delay, "".join(buffer)))
                buffer = []
                delay = 0.0

        for event in self.events:
            if event.kind == "char":
                buffer.append(event.text)
                delay += event.delay
            elif event.kind == "backspace":
                buffer.append("\b")
                delay += event.delay
            elif event.kind == "pause":
                flush()
                result.append((event.delay, ""))
        flush()
        return result


#: 待机动作及其权重。
IDLE_ACTIONS: tuple[tuple[str, float], ...] = (
    ("look_around", 3.0),
    ("strafe_left", 1.5),
    ("strafe_right", 1.5),
    ("jump", 1.0),
    ("sneak", 1.0),
    ("pause", 3.5),
    ("spin", 0.4),
    ("swing_arm", 2.0),
)

_IDLE_HOLDS: dict[str, tuple[float, float]] = {
    "look_around": (1.0, 3.5),
    "strafe_left": (0.3, 1.4),
    "strafe_right": (0.3, 1.4),
    "jump": (0.2, 0.7),
    "sneak": (0.4, 1.6),
    "pause": (0.8, 3.0),
    "spin": (0.5, 1.2),
    "swing_arm": (0.2, 0.8),
}


class Humanizer:
    """生成人类化输入计划的门面。"""

    def __init__(
        self,
        profile: HumanProfile | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.profile = profile if profile is not None else HumanProfile()
        self.rng = rng if rng is not None else random.Random()
        self.clock = clock
        self._break_threshold: float | None = None

    def reaction_delay(self, urgency: float = 1.0) -> float:
        """采样一次反应延迟：对数正态 + 紧迫度缩放。"""
        base = max(1e-6, abs(_finite(self.profile.reaction_base, 0.20)))
        sigma = max(0.0, _finite(self.profile.reaction_sigma, 0.45))
        low = _finite(self.profile.reaction_min, 0.08)
        high = _finite(self.profile.reaction_max, 0.9)
        if low > high:
            low, high = high, low
        scale = max(1e-6, _finite(urgency, 1.0))
        try:
            sample = self.rng.lognormvariate(math.log(base), sigma) / scale
        except (OverflowError, ValueError):
            sample = base / scale
        return _clamp(_finite(sample, base / scale), low, high)

    def plan_aim(self, current: Rotation, target: Rotation) -> AimPlan:
        """规划一次带缓动、过冲和手抖的转向。"""
        return AimPlan(current, target, self.profile, self.rng)

    def plan_typing(self, text: str) -> TypingPlan:
        """规划一段带停顿和偶发错字的输入。"""
        return TypingPlan(text, self.profile, self.rng)

    def idle_action(self) -> str:
        """按权重抽一个待机小动作。"""
        total = sum(max(0.0, weight) for _, weight in IDLE_ACTIONS)
        if total <= 0.0:
            return IDLE_ACTIONS[0][0]
        roll = self.rng.random() * total
        for action, weight in IDLE_ACTIONS:
            roll -= max(0.0, weight)
            if roll < 0.0:
                return action
        return IDLE_ACTIONS[-1][0]

    def idle_hold_seconds(self, action: str) -> float:
        """该待机动作大概持续多久（秒）。"""
        low, high = _IDLE_HOLDS.get(action, (0.4, 1.6))
        return self.rng.uniform(low, high)

    def should_take_break(self, session_seconds: float) -> bool:
        """连续在线够久就该歇一会；阈值每个实例只抽一次。"""
        if self._break_threshold is None:
            low = _finite(self.profile.break_after_min, 480.0)
            high = _finite(self.profile.break_after_max, 1500.0)
            if low > high:
                low, high = high, low
            self._break_threshold = self.rng.uniform(low, high)
        return _finite(session_seconds, 0.0) > self._break_threshold

    def break_duration(self) -> float:
        """休息时长（秒）。"""
        low = _finite(self.profile.break_duration_min, 15.0)
        high = _finite(self.profile.break_duration_max, 120.0)
        if low > high:
            low, high = high, low
        return self.rng.uniform(low, high)

    def scan_pattern(self, ticks: int) -> list[tuple[float, float]]:
        """生成四处张望的逐 tick 视角增量（dyaw, dpitch）。"""
        try:
            count = max(0, int(ticks))
        except (TypeError, ValueError):
            count = 0
        result: list[tuple[float, float]] = []
        yaw = 0.0
        pitch = 0.0
        for _ in range(count):
            # 小幅随机游走；偶尔大幅反向，像是回头看一眼。
            if self.rng.random() < 0.10:
                step = -math.copysign(self.rng.uniform(8.0, 18.0), yaw if abs(yaw) > 1e-9 else 1.0)
            else:
                step = self.rng.gauss(0.0, 4.0)
            new_yaw = _clamp(yaw + step, -_SCAN_YAW_LIMIT, _SCAN_YAW_LIMIT)
            dyaw = _clamp(
                wrap_degrees(new_yaw - yaw),
                -_SCAN_YAW_LIMIT,
                _SCAN_YAW_LIMIT,
            )
            new_pitch = _clamp(
                pitch + self.rng.gauss(0.0, 1.5),
                -_SCAN_PITCH_LIMIT,
                _SCAN_PITCH_LIMIT,
            )
            dpitch = _clamp(
                new_pitch - pitch,
                -_SCAN_PITCH_LIMIT,
                _SCAN_PITCH_LIMIT,
            )
            yaw = _clamp(yaw + dyaw, -_SCAN_YAW_LIMIT, _SCAN_YAW_LIMIT)
            pitch = _clamp(pitch + dpitch, -_SCAN_PITCH_LIMIT, _SCAN_PITCH_LIMIT)
            result.append((dyaw, dpitch))
        return result

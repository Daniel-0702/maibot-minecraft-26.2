"""humanize 行为层的单元测试（unittest，无第三方依赖）。"""

from __future__ import annotations

import math
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcbot.humanize import (  # noqa: E402
    IDLE_ACTIONS,
    AimPlan,
    Humanizer,
    HumanProfile,
    TypingPlan,
)
from mcbot.types import Rotation, wrap_degrees  # noqa: E402


def _typed_from_chunks(chunks: list[tuple[float, str]]) -> str:
    """按发送层的方式回放 chunks：\\b 表示退格。"""
    result = ""
    for _, chunk in chunks:
        for char in chunk:
            if char == "\b":
                result = result[:-1]
            else:
                result += char
    return result


class DeterminismTests(unittest.TestCase):
    def test_seeded_humanizers_are_identical(self) -> None:
        first = Humanizer(rng=random.Random(20241115))
        second = Humanizer(rng=random.Random(20241115))

        for urgency in (1.0, 0.5, 3.0):
            self.assertEqual(
                first.reaction_delay(urgency),
                second.reaction_delay(urgency),
            )

        for current, target in (
            (Rotation(10.0, -12.0), Rotation(200.0, 35.0)),
            (Rotation(-179.0, 88.0), Rotation(179.0, -90.0)),
        ):
            aim_a = first.plan_aim(current, target)
            aim_b = second.plan_aim(current, target)
            self.assertEqual(aim_a.steps, aim_b.steps)
            self.assertEqual(len(aim_a), len(aim_b))

        for text in ("hello world", "麦麦来了，今天去挖矿吗？"):
            plan_a = first.plan_typing(text)
            plan_b = second.plan_typing(text)
            self.assertEqual(plan_a.events, plan_b.events)
            self.assertEqual(plan_a.duration, plan_b.duration)

        self.assertEqual(
            [first.idle_action() for _ in range(20)],
            [second.idle_action() for _ in range(20)],
        )


class ReactionDelayTests(unittest.TestCase):
    def test_bounds_and_urgency(self) -> None:
        profile = HumanProfile()
        normal = Humanizer(profile, random.Random(123))
        urgent = Humanizer(profile, random.Random(123))

        normal_samples = [normal.reaction_delay(1.0) for _ in range(200)]
        urgent_samples = [urgent.reaction_delay(3.0) for _ in range(200)]

        for sample in normal_samples + urgent_samples:
            self.assertTrue(math.isfinite(sample))
            self.assertGreaterEqual(sample, profile.reaction_min)
            self.assertLessEqual(sample, profile.reaction_max)

        mean_normal = sum(normal_samples) / len(normal_samples)
        mean_urgent = sum(urgent_samples) / len(urgent_samples)
        self.assertLess(mean_urgent, mean_normal)


class AimPlanTests(unittest.TestCase):
    def _assert_valid_plan(
        self,
        plan: AimPlan,
        start: Rotation,
        target_yaw: float,
        target_pitch: float,
    ) -> None:
        self.assertGreaterEqual(len(plan), 4)
        self.assertLessEqual(len(plan), 14)
        self.assertGreater(len(plan), 0)

        previous = start
        for step in plan.steps:
            self.assertTrue(math.isfinite(step.yaw))
            self.assertTrue(math.isfinite(step.pitch))
            self.assertGreaterEqual(step.pitch, -90.0)
            self.assertLessEqual(step.pitch, 90.0)
            yaw_step = abs(wrap_degrees(step.yaw - previous.yaw))
            self.assertLessEqual(yaw_step, 14.0 * 1.5 + 1e-6)
            self.assertLessEqual(abs(step.pitch - previous.pitch), 14.0 * 1.5 + 1e-6)
            previous = step

        final = plan.steps[-1]
        self.assertLessEqual(abs(wrap_degrees(final.yaw - target_yaw)), 1e-6)
        self.assertLessEqual(abs(final.pitch - target_pitch), 1e-6)

    def test_converges_and_eases(self) -> None:
        humanizer = Humanizer(rng=random.Random(7))
        small = humanizer.plan_aim(Rotation(0.0, 0.0), Rotation(10.0, 5.0))
        large = humanizer.plan_aim(Rotation(0.0, 0.0), Rotation(180.0, 30.0))

        self._assert_valid_plan(small, Rotation(0.0, 0.0), 10.0, 5.0)
        self._assert_valid_plan(large, Rotation(0.0, 0.0), wrap_degrees(180.0), 30.0)
        self.assertGreater(len(large), len(small))

        wrapped = humanizer.plan_aim(Rotation(170.0, 0.0), Rotation(-170.0, 0.0))
        self._assert_valid_plan(wrapped, Rotation(170.0, 0.0), -170.0, 0.0)

        clamped = humanizer.plan_aim(Rotation(0.0, 0.0), Rotation(360.0, 120.0))
        self._assert_valid_plan(clamped, Rotation(0.0, 0.0), 0.0, 90.0)

    def test_iterator_and_consumption(self) -> None:
        humanizer = Humanizer(rng=random.Random(11))
        plan = humanizer.plan_aim(Rotation(0.0, 0.0), Rotation(45.0, -20.0))
        self.assertFalse(plan.done)
        self.assertEqual(list(plan), plan.steps)

        drained = [rotation for rotation in iter(lambda: plan.next_rotation(), None)]
        self.assertEqual(drained, plan.steps)
        self.assertTrue(plan.done)
        self.assertIsNone(plan.next_rotation())


class TypingPlanTests(unittest.TestCase):
    def test_reconstructs_ascii_and_chinese(self) -> None:
        humanizer = Humanizer(rng=random.Random(42))
        for text in ("hello world", "麦麦来了，今天去挖矿吗？"):
            plan = humanizer.plan_typing(text)
            self.assertEqual(plan.apply(), text)
            self.assertEqual(_typed_from_chunks(plan.chunks()), text)
            self.assertGreater(plan.duration, len(text) / 5.5 * 0.5)
            for event in plan.events:
                self.assertIn(event.kind, {"char", "backspace", "pause"})
                self.assertGreaterEqual(event.delay, 0.0)
                self.assertTrue(math.isfinite(event.delay))
            for delay, _ in plan.chunks():
                self.assertGreaterEqual(delay, 0.0)
            self.assertAlmostEqual(
                sum(delay for delay, _ in plan.chunks()),
                plan.duration,
                places=9,
            )

    def test_long_text_has_backspaces(self) -> None:
        text = ("The quick brown fox jumps over the lazy dog. " * 8)[:300]
        plan = Humanizer(rng=random.Random(0)).plan_typing(text)
        self.assertEqual(len(text), 300)
        self.assertTrue(any(event.kind == "backspace" for event in plan.events))
        self.assertEqual(plan.apply(), text)
        self.assertEqual(_typed_from_chunks(plan.chunks()), text)
        self.assertTrue(any(not chunk for _, chunk in plan.chunks()))

    def test_injected_prefix_is_preserved(self) -> None:
        text = "abc def"
        plan = Humanizer(rng=random.Random(5)).plan_typing(text)
        self.assertEqual(plan.apply("前缀"), "前缀" + text)


class IdleTests(unittest.TestCase):
    def test_actions_and_holds(self) -> None:
        humanizer = Humanizer(rng=random.Random(2026))
        allowed = {action for action, _ in IDLE_ACTIONS}
        draws = [humanizer.idle_action() for _ in range(200)]
        self.assertTrue(set(draws) <= allowed)
        self.assertGreaterEqual(len(set(draws)), 3)

        for action in allowed:
            for _ in range(10):
                hold = humanizer.idle_hold_seconds(action)
                self.assertGreater(hold, 0.0)
                self.assertLessEqual(hold, 5.0)


class BreakTests(unittest.TestCase):
    def test_break_threshold_and_duration(self) -> None:
        profile = HumanProfile()
        humanizer = Humanizer(profile, random.Random(321))
        self.assertFalse(humanizer.should_take_break(0.0))

        took_break_at = None
        for elapsed in range(0, 2001, 10):
            if humanizer.should_take_break(float(elapsed)):
                took_break_at = elapsed
                break
        self.assertIsNotNone(took_break_at)
        self.assertGreaterEqual(took_break_at, profile.break_after_min)
        self.assertLessEqual(took_break_at, profile.break_after_max + 10)

        for _ in range(50):
            duration = humanizer.break_duration()
            self.assertGreaterEqual(duration, profile.break_duration_min)
            self.assertLessEqual(duration, profile.break_duration_max)

    def test_threshold_is_stable(self) -> None:
        humanizer = Humanizer(rng=random.Random(77))
        self.assertFalse(humanizer.should_take_break(100.0))
        self.assertFalse(humanizer.should_take_break(200.0))
        self.assertTrue(humanizer.should_take_break(2000.0))
        self.assertFalse(humanizer.should_take_break(100.0))


class ScanPatternTests(unittest.TestCase):
    def test_scan_bounds(self) -> None:
        pattern = Humanizer(rng=random.Random(8)).scan_pattern(50)
        self.assertEqual(len(pattern), 50)

        cumulative_yaw = 0.0
        for dyaw, dpitch in pattern:
            self.assertTrue(math.isfinite(dyaw))
            self.assertTrue(math.isfinite(dpitch))
            self.assertLessEqual(abs(dyaw), 35.0)
            self.assertLessEqual(abs(dpitch), 15.0)
            cumulative_yaw += dyaw
            self.assertLessEqual(abs(cumulative_yaw), 360.0)

    def test_zero_ticks(self) -> None:
        self.assertEqual(Humanizer(rng=random.Random(8)).scan_pattern(0), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

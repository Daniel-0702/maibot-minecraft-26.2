"""PlayerPhysics 的单元测试（unittest，不依赖 pytest）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcbot.physics import MovementInput, PlayerPhysics
from mcbot.registry import registry
from mcbot.types import AABB, BlockPos, Vec3
from mcbot.world import SimpleWorld

STONE = registry().state_id("stone")
SLAB = registry().state_id("oak_slab")
WATER = registry().state_id("water")
LAVA = registry().state_id("lava")
GLASS = registry().state_id("glass")


def flat_world(radius: int = 10, floor_y: int = 0) -> SimpleWorld:
    """造一块以原点为中心的石制地板。"""
    world = SimpleWorld()
    world.fill_box(BlockPos(-radius, floor_y, -radius), BlockPos(radius, floor_y, radius), STONE)
    return world


def tick_many(physics: PlayerPhysics, movement: MovementInput, ticks: int) -> None:
    for _ in range(ticks):
        physics.tick(movement)


class FallTest(unittest.TestCase):
    """重力与落地。"""

    def test_fall_onto_floor(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 5.0, 0.5))
        self.assertFalse(physics.on_ground)
        tick_many(physics, MovementInput(), 40)
        self.assertAlmostEqual(physics.position.y, 1.0, delta=0.01)
        self.assertTrue(physics.on_ground)
        self.assertEqual(physics.velocity.y, 0.0)
        self.assertTrue(physics.is_standing_on_solid())
        self.assertEqual(physics.block_below().short_name, "stone")

    def test_void_guard(self) -> None:
        world = SimpleWorld()  # 没有任何方块
        physics = PlayerPhysics(world, Vec3(0.5, 10.0, 0.5))
        tick_many(physics, MovementInput(), 200)
        self.assertGreaterEqual(physics.position.y, float(world.min_y))
        self.assertTrue(physics.on_ground)


class WalkTest(unittest.TestCase):
    """水平移动速度。"""

    def _run(self, ticks: int = 20, **kwargs) -> float:
        world = flat_world(24)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)  # yaw=-90 => +X
        start = physics.position.x
        tick_many(physics, MovementInput(forward=1.0, **kwargs), ticks)
        self.assertTrue(physics.on_ground)
        return physics.position.x - start

    def test_walk_speed(self) -> None:
        distance = self._run()
        speed = distance / 1.0  # 20 tick = 1 s
        self.assertAlmostEqual(speed, 4.3, delta=4.3 * 0.25)
        self.assertAlmostEqual(distance, 4.18, delta=0.6)

    def test_sprint_faster_than_walk(self) -> None:
        walk = self._run()
        sprint = self._run(sprint=True)
        self.assertGreater(sprint, walk)
        self.assertGreater(sprint, walk * 1.15)

    def test_sneak_slower_than_walk(self) -> None:
        walk = self._run()
        sneak = self._run(sneak=True)
        self.assertLess(sneak, walk)
        self.assertLess(sneak, walk * 0.5)

    def test_strafe_uses_right_vector(self) -> None:
        world = flat_world(24)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=0.0)  # 面朝 +Z
        tick_many(physics, MovementInput(strafe=1.0), 10)
        # 右向量 = (cos0, 0, sin0) = +X
        self.assertGreater(physics.position.x, 1.0)
        self.assertAlmostEqual(physics.position.z, 0.5, delta=0.3)

    def test_friction_stops_player(self) -> None:
        world = flat_world(24)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        tick_many(physics, MovementInput(forward=1.0), 10)
        speed_before = abs(physics.velocity.x)
        self.assertGreater(speed_before, 0.1)
        tick_many(physics, MovementInput(), 10)
        self.assertLess(abs(physics.velocity.x), speed_before * 0.1)


class WallTest(unittest.TestCase):
    """撞墙不穿模。"""

    def test_wall_blocks_movement(self) -> None:
        world = flat_world(10)
        world.fill_box(BlockPos(3, 1, -5), BlockPos(3, 3, 5), STONE)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        collided = False
        for _ in range(60):
            physics.tick(MovementInput(forward=1.0))
            collided = collided or physics.horizontal_collision
        self.assertTrue(collided)
        self.assertLessEqual(physics.position.x + physics.WIDTH / 2, 3.0 + 1e-6)
        self.assertGreater(physics.position.x, 2.0)
        # 完全没插进墙里
        wall = AABB.of_block(BlockPos(3, 1, 0))
        self.assertFalse(physics.aabb().intersects(wall))
        self.assertFalse(physics.aabb().intersects(AABB.of_block(BlockPos(3, 2, 0))))

    def test_slide_along_wall(self) -> None:
        """斜着撞墙时应该能沿墙面滑动。"""
        world = flat_world(10)
        world.fill_box(BlockPos(3, 1, -5), BlockPos(3, 3, 5), STONE)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-45.0)  # 朝 +X/+Z 斜走
        start_z = physics.position.z
        tick_many(physics, MovementInput(forward=1.0), 40)
        self.assertLessEqual(physics.position.x + physics.WIDTH / 2, 3.0 + 1e-6)
        self.assertGreater(physics.position.z - start_z, 3.0)


class JumpTest(unittest.TestCase):
    """跳跃高度与落地。"""

    def test_jump_height(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        tick_many(physics, MovementInput(), 5)  # 先稳定站在地上
        self.assertTrue(physics.on_ground)
        ground = physics.position.y
        peak = ground
        for index in range(40):
            physics.tick(MovementInput(jump=index == 0))
            peak = max(peak, physics.position.y)
        self.assertAlmostEqual(peak - ground, 1.25, delta=0.2)
        tick_many(physics, MovementInput(), 20)
        self.assertAlmostEqual(physics.position.y, ground, delta=0.01)
        self.assertTrue(physics.on_ground)

    def test_jump_helper(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        tick_many(physics, MovementInput(), 3)
        physics.jump()
        self.assertAlmostEqual(physics.velocity.y, 0.42, delta=1e-9)
        self.assertFalse(physics.on_ground)


class StepUpTest(unittest.TestCase):
    """半砖台阶：不跳也能走上去。"""

    def test_slab_step_up(self) -> None:
        world = flat_world()
        world.set_state(3, 1, 0, SLAB)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        for _ in range(60):
            physics.tick(MovementInput(forward=1.0))
            if physics.position.x >= 3.2:
                break
        self.assertGreaterEqual(physics.position.x, 3.2)
        self.assertAlmostEqual(physics.position.y, 1.5, delta=0.05)
        self.assertTrue(physics.on_ground)
        # 站在半砖上，脚下是半砖
        self.assertEqual(physics.block_below().short_name, "oak_slab")

    def test_full_block_is_not_a_step(self) -> None:
        """整格方块（高 1.0）超过 STEP_HEIGHT，走不上去。"""
        world = flat_world()
        world.set_state(3, 1, 0, STONE)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        tick_many(physics, MovementInput(forward=1.0), 40)
        self.assertLessEqual(physics.position.x + physics.WIDTH / 2, 3.0 + 1e-6)
        self.assertAlmostEqual(physics.position.y, 1.0, delta=0.05)


class WaterTest(unittest.TestCase):
    """水与岩浆。"""

    def _water_world(self) -> SimpleWorld:
        world = SimpleWorld()
        world.fill_box(BlockPos(-5, 0, -5), BlockPos(5, 0, 5), STONE)
        world.fill_box(BlockPos(-5, 1, -5), BlockPos(5, 3, 5), WATER)
        return world

    def test_swim_up(self) -> None:
        world = self._water_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        physics.tick(MovementInput())
        self.assertTrue(physics.in_water)
        self.assertFalse(physics.in_lava)
        start_y = physics.position.y
        tick_many(physics, MovementInput(jump=True), 8)
        self.assertGreater(physics.position.y, start_y + 0.5)
        self.assertTrue(physics.in_water)

    def test_sink_slowly_without_input(self) -> None:
        world = self._water_world()
        physics = PlayerPhysics(world, Vec3(0.5, 3.0, 0.5))
        tick_many(physics, MovementInput(), 10)
        self.assertLess(physics.position.y, 3.0)
        self.assertGreater(physics.position.y, 1.5)  # 水里下落比空气慢得多

    def test_lava_detected(self) -> None:
        world = SimpleWorld()
        world.fill_box(BlockPos(-3, 0, -3), BlockPos(3, 0, 3), STONE)
        world.fill_box(BlockPos(-3, 1, -3), BlockPos(3, 2, 3), LAVA)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        physics.tick(MovementInput())
        self.assertTrue(physics.in_lava)
        self.assertFalse(physics.in_water)


class CeilingTest(unittest.TestCase):
    """天花板挡住跳跃。"""

    def test_jump_under_ceiling(self) -> None:
        world = flat_world()
        world.fill_box(BlockPos(-5, 3, -5), BlockPos(5, 3, 5), STONE)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        ceiling = AABB.of_block(BlockPos(0, 3, 0))
        for _ in range(30):
            physics.tick(MovementInput(jump=True))
            box = physics.aabb()
            self.assertLessEqual(box.max_y, 3.0 + 1e-6)
            self.assertFalse(box.intersects(ceiling))
        # 天花板下只能抬高 0.2
        self.assertLess(physics.position.y, 1.4)

    def test_head_not_in_solid(self) -> None:
        world = flat_world()
        # 头顶两格处放玻璃，确认碰撞箱不会插进去
        world.set_state(0, 3, 0, GLASS)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        for _ in range(20):
            physics.tick(MovementInput(jump=True))
            self.assertFalse(physics.aabb().intersects(AABB.of_block(BlockPos(0, 3, 0))))


class MiscTest(unittest.TestCase):
    """其它公开 API。"""

    def test_aabb_and_eye(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(1.5, 1.0, 1.5))
        box = physics.aabb()
        self.assertAlmostEqual(box.min_x, 1.2)
        self.assertAlmostEqual(box.max_x, 1.8)
        self.assertAlmostEqual(box.min_y, 1.0)
        self.assertAlmostEqual(box.max_y, 2.8)
        eye = physics.eye_position()
        self.assertAlmostEqual(eye.y, 2.62, delta=1e-9)

    def test_sneak_lowers_height(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        physics.tick(MovementInput(sneak=True))
        self.assertTrue(physics.sneaking)
        self.assertAlmostEqual(physics.aabb().size_y, physics.SNEAK_HEIGHT)
        self.assertFalse(physics.sprinting)

    def test_set_rotation_wraps_yaw(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world)
        physics.set_rotation(370.0, 120.0)
        self.assertAlmostEqual(physics.yaw, 10.0)
        self.assertEqual(physics.pitch, 90.0)
        self.assertAlmostEqual(physics.look_vector().length(), 1.0, delta=1e-9)

    def test_stop_and_knockback(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        tick_many(physics, MovementInput(forward=1.0), 5)
        physics.stop()
        self.assertAlmostEqual(physics.velocity.x, 0.0)
        physics.apply_knockback(0.4, 0.3, 0.0)
        self.assertAlmostEqual(physics.velocity.x, 0.4)
        self.assertFalse(physics.on_ground)

    def test_move_with_collision(self) -> None:
        world = flat_world()
        world.fill_box(BlockPos(2, 1, -3), BlockPos(2, 3, 3), STONE)
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5))
        physics.move(0.0, 0.0, 1.5)
        self.assertAlmostEqual(physics.position.z, 2.0)
        physics.move(3.0, 0.0, 0.0)  # 被墙挡住
        self.assertLessEqual(physics.position.x + physics.WIDTH / 2, 2.0 + 1e-6)
        self.assertTrue(physics.horizontal_collision)

    def test_deterministic(self) -> None:
        """同样的输入序列必须得到同样的位置（dt=0.05 时确定）。"""
        world = flat_world(24)
        first = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        second = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        for index in range(30):
            movement = MovementInput(forward=1.0, jump=index % 7 == 0, sprint=index % 5 == 0)
            first.tick(movement)
            second.tick(movement)
        self.assertEqual(first.position.as_tuple(), second.position.as_tuple())
        self.assertEqual(first.velocity.as_tuple(), second.velocity.as_tuple())

    def test_zero_dt_is_noop(self) -> None:
        world = flat_world()
        physics = PlayerPhysics(world, Vec3(0.5, 1.0, 0.5), yaw=-90.0)
        physics.tick(MovementInput(forward=1.0), dt=0.0)
        self.assertEqual(physics.position.as_tuple(), (0.5, 1.0, 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)

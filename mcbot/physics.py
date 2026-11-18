"""玩家物理：重力、跳跃、碰撞、台阶与液体。

坐标约定与 :mod:`mcbot.types` 一致：``position`` 是脚底中心，``y`` 向上。
所有速度单位都是「每个 tick（0.05 s）的位移」，``dt`` 不是 0.05 时按比例
线性缩放，因此 ``dt=0.05`` 时模拟完全确定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .registry import BlockInfo
from .types import AABB, Vec3, look_direction, wrap_degrees
from .world import World

__all__ = ["MovementInput", "PlayerPhysics"]

# ---- 调参常量（近似原版 1.20.1，单位：方块 / tick）----
GRAVITY = 0.08
AIR_DRAG = 0.98
WATER_GRAVITY = 0.02
WATER_DRAG = 0.8
LAVA_GRAVITY = 0.02
LAVA_DRAG = 0.8
JUMP_VELOCITY = 0.42
SPRINT_JUMP_IMPULSE = 0.2
SWIM_UP_VELOCITY = 0.16
LAVA_SWIM_UP_VELOCITY = 0.10

WALK_SPEED = 0.216
SPRINT_SPEED = 0.281
SNEAK_SPEED = 0.065
WATER_SPEED = 0.09
LAVA_SPEED = 0.045

GROUND_ACCEL = 0.6
AIR_ACCEL = 0.15
AIR_ACCEL_MIN = 0.02
FLUID_ACCEL = 0.10

GROUND_FRICTION = 0.6
AIR_FRICTION = 0.91
FLUID_FRICTION = 0.8

#: 碰撞判定的容差：让玩家停在方块表面外 1e-7，避免浮点误差造成「已经穿模」。
EPS = 1e-7
#: 贴地探针的高度：站在地面时每 tick 向下探一点，用来刷新 ``on_ground``。
GROUND_PROBE = 0.001
#: 已经重叠超过这个深度的方块不再参与裁剪（防止把玩家往回弹）。
PENETRATION_MARGIN = 1e-4


@dataclass(slots=True)
class MovementInput:
    """一 tick 的移动输入。"""

    forward: float = 0.0  # -1..1，正值朝视线方向
    strafe: float = 0.0  # -1..1，正值向右
    jump: bool = False
    sneak: bool = False
    sprint: bool = False


def _overlaps(a_min: float, a_max: float, b_min: float, b_max: float) -> bool:
    """两段区间是否真正重叠（只接触不算）。"""
    return a_min < b_max - EPS and a_max > b_min + EPS


def _swept_box(box: AABB, axis: int, delta: float) -> AABB:
    """单轴移动扫过的包围盒（外扩一点，保证贴面的方块也能被收集到）。"""
    if axis == 0:
        return AABB(
            min(box.min_x, box.min_x + delta) - EPS,
            box.min_y - EPS,
            box.min_z - EPS,
            max(box.max_x, box.max_x + delta) + EPS,
            box.max_y + EPS,
            box.max_z + EPS,
        )
    if axis == 1:
        return AABB(
            box.min_x - EPS,
            min(box.min_y, box.min_y + delta) - EPS,
            box.min_z - EPS,
            box.max_x + EPS,
            max(box.max_y, box.max_y + delta) + EPS,
            box.max_z + EPS,
        )
    return AABB(
        box.min_x - EPS,
        box.min_y - EPS,
        min(box.min_z, box.min_z + delta) - EPS,
        box.max_x + EPS,
        box.max_y + EPS,
        max(box.max_z, box.max_z + delta) + EPS,
    )


def _scan_collision_boxes(world: World, box: AABB) -> list[AABB]:
    """逐格扫描碰撞箱。

    ``World.collision_boxes`` 依赖区块列；测试用的 :class:`~mcbot.world.SimpleWorld`
    没有区块列，所以这里提供等价的逐格扫描作为兜底。
    """
    registry = world.registry
    result: list[AABB] = []
    x0 = math.floor(box.min_x) - 1
    x1 = math.floor(box.max_x) + 1
    z0 = math.floor(box.min_z) - 1
    z1 = math.floor(box.max_z) + 1
    y0 = max(world.min_y, math.floor(box.min_y) - 1)
    y1 = min(world.max_y, math.floor(box.max_y) + 1)
    for x in range(x0, x1 + 1):
        for z in range(z0, z1 + 1):
            for y in range(y0, y1 + 1):
                state = world.get_state(x, y, z)
                if not state:
                    continue
                for local in registry.boxes(state):
                    world_box = local.offset(x, y, z)
                    if world_box.intersects(box):
                        result.append(world_box)
    return result


class PlayerPhysics:
    """玩家物理状态机：每个 tick 吃一次输入，推进位置与速度。"""

    WIDTH = 0.6
    HEIGHT = 1.8
    EYE_HEIGHT = 1.62
    SNEAK_HEIGHT = 1.5
    STEP_HEIGHT = 0.6

    def __init__(self, world: World, position: Vec3 | None = None, yaw: float = 0.0, pitch: float = 0.0) -> None:
        self.world = world
        #: 脚底中心坐标
        self.position: Vec3 = position if position is not None else Vec3(0.5, 1.0, 0.5)
        #: 每 tick 位移
        self.velocity: Vec3 = Vec3()
        self.yaw: float = wrap_degrees(yaw)
        self.pitch: float = max(-90.0, min(90.0, pitch))
        self.on_ground: bool = False
        self.sprinting: bool = False
        self.sneaking: bool = False
        self.in_water: bool = False
        self.in_lava: bool = False
        #: 头部是否浸在水里（用于判断会不会淹死 / 该不该一直上浮）
        self.head_in_water: bool = False
        self.horizontal_collision: bool = False
        self._update_liquid_state()

    # ------------------------------------------------------------------ 查询
    def _height(self) -> float:
        """当前碰撞箱高度（潜行时变矮）。"""
        return self.SNEAK_HEIGHT if self.sneaking else self.HEIGHT

    def _aabb_at(self, position: Vec3, height: float | None = None) -> AABB:
        return AABB.from_center(position, self.WIDTH, self._height() if height is None else height)

    def aabb(self) -> AABB:
        """当前玩家的碰撞箱。"""
        return self._aabb_at(self.position)

    def eye_position(self) -> Vec3:
        """眼睛位置（用于视线 / 交互）。"""
        eye_height = self.EYE_HEIGHT
        if self.sneaking:
            eye_height = min(eye_height, self.SNEAK_HEIGHT - 0.2)
        return self.position.replace(y=self.position.y + eye_height)

    def look_vector(self) -> Vec3:
        """单位视线向量。"""
        return look_direction(self.yaw, self.pitch)

    def block_below(self) -> BlockInfo:
        """脚下（含半个台阶）所在方块的方块信息。"""
        pos = self.position.replace(y=self.position.y - 0.05).floor()
        return self.world.block_at(pos)

    def is_standing_on_solid(self) -> bool:
        """脚下是否有实心碰撞地面（用探针，能识别台阶 / 半砖）。"""
        probe = self._aabb_at(self.position.replace(y=self.position.y - 0.02))
        return self._collides(probe)

    # ------------------------------------------------------------------ 状态
    def reset(self, position: Vec3 | None = None) -> None:
        """复位物理状态（可同时指定新位置）。"""
        if position is not None:
            self.position = position
        self.velocity = Vec3()
        self.on_ground = False
        self.sprinting = False
        self.sneaking = False
        self.horizontal_collision = False
        self.in_water = False
        self.in_lava = False
        self._update_liquid_state()

    def set_rotation(self, yaw: float, pitch: float) -> None:
        """设置视角（yaw 规范化到 [-180, 180)）。"""
        self.yaw = wrap_degrees(yaw)
        self.pitch = max(-90.0, min(90.0, pitch))

    def stop(self) -> None:
        """清空水平速度。"""
        self.velocity = Vec3(0.0, self.velocity.y, 0.0)

    def apply_knockback(self, dx: float, dy: float, dz: float) -> None:
        """叠加一个击退冲量（速度）。"""
        self.velocity = Vec3(self.velocity.x + dx, self.velocity.y + dy, self.velocity.z + dz)
        if dy > 0.0:
            self.on_ground = False

    def jump(self) -> None:
        """跳跃 / 上浮：地面起跳，水里则向上游。

        水里站在浅滩上（头还没进水）仍然给普通起跳初速，这样才能跳上岸；
        真正泡在水里时按游泳速度上浮。持续按住跳跃（每 tick 调用）就是原版
        的"一直往上游"，这也是从水里起来的关键。
        """
        if self.in_water:
            if self.on_ground and not self.head_in_water:
                self.velocity = self.velocity.replace(y=JUMP_VELOCITY)
                self.on_ground = False
            else:
                self.velocity = self.velocity.replace(y=SWIM_UP_VELOCITY)
        elif self.in_lava:
            self.velocity = self.velocity.replace(y=LAVA_SWIM_UP_VELOCITY)
        elif self.on_ground:
            self.velocity = self.velocity.replace(y=JUMP_VELOCITY)
            self.on_ground = False

    # ------------------------------------------------------------------ 主循环
    def tick(self, movement: MovementInput, dt: float = 0.05) -> None:
        """推进一个物理 tick。"""
        if dt <= 0.0:
            return
        scale = dt / 0.05

        self._update_liquid_state()

        wish = self._wish_direction(movement)
        self.sneaking = bool(movement.sneak)
        self.sprinting = bool(movement.sprint) and not self.sneaking and movement.forward > 0.0

        # 水平加速 / 摩擦
        self._apply_horizontal(wish, scale)

        # 跳跃（在水里是上浮）
        if movement.jump:
            was_on_ground = self.on_ground
            self.jump()
            if (
                was_on_ground
                and self.sprinting
                and not self.in_water
                and not self.in_lava
                and wish.horizontal_length() > 1e-6
            ):
                impulse = wish * SPRINT_JUMP_IMPULSE
                self.velocity = Vec3(self.velocity.x + impulse.x, self.velocity.y, self.velocity.z + impulse.z)

        # 先竖直（拿到本 tick 的落地状态），再水平（被挡住时才好试台阶）
        self._move_vertical(scale)
        self._move_horizontal(scale)

        # 重力在移动之后结算：跳跃那一 tick 才能拿到完整的 0.42 初速度
        self._apply_gravity(scale)
        self._clamp_void()
        self._update_liquid_state()

    # ------------------------------------------------------------------ 输入
    def _wish_direction(self, movement: MovementInput) -> Vec3:
        """由 yaw 与输入求水平期望方向（长度 0..1）。"""
        forward = max(-1.0, min(1.0, float(movement.forward)))
        strafe = max(-1.0, min(1.0, float(movement.strafe)))
        if forward == 0.0 and strafe == 0.0:
            return Vec3()
        yaw = math.radians(self.yaw)
        # 前 = (-sin, 0, cos)，右 = (cos, 0, sin)
        wish = Vec3(
            -math.sin(yaw) * forward + math.cos(yaw) * strafe,
            0.0,
            math.cos(yaw) * forward + math.sin(yaw) * strafe,
        )
        if wish.horizontal_length() > 1.0:
            wish = wish.normalized()
        return wish

    def _target_speed(self) -> float:
        """当前状态的目标水平速度（方块 / tick）。"""
        if self.in_water:
            return WATER_SPEED
        if self.in_lava:
            return LAVA_SPEED
        if self.sneaking:
            return SNEAK_SPEED
        if self.sprinting:
            return SPRINT_SPEED
        return WALK_SPEED

    def _apply_horizontal(self, wish: Vec3, scale: float) -> None:
        """向目标速度靠拢；没有输入时施加摩擦。"""
        horizontal = Vec3(self.velocity.x, 0.0, self.velocity.z)
        in_fluid = self.in_water or self.in_lava
        if wish.horizontal_length() > 1e-6:
            target = wish * self._target_speed()
            if in_fluid:
                factor = FLUID_ACCEL
            elif self.on_ground:
                factor = GROUND_ACCEL
            else:
                factor = AIR_ACCEL
            gap = target - horizontal
            gap_length = gap.length()
            if gap_length > 1e-9:
                step = gap_length * factor
                if not self.on_ground and not in_fluid:
                    # 空中保持动量，但至少允许一点点控制力
                    step = max(step, min(AIR_ACCEL_MIN * scale, gap_length))
                step = min(step, gap_length)
                horizontal = horizontal + gap.normalized() * step
        else:
            if in_fluid:
                friction = FLUID_FRICTION
            elif self.on_ground:
                friction = GROUND_FRICTION
            else:
                friction = AIR_FRICTION
            horizontal = horizontal * (friction**scale)
        self.velocity = Vec3(horizontal.x, self.velocity.y, horizontal.z)

    def _apply_gravity(self, scale: float) -> None:
        """重力 + 空气阻力。"""
        in_fluid = self.in_water or self.in_lava
        gravity = WATER_GRAVITY if in_fluid else GRAVITY
        drag = WATER_DRAG if in_fluid else AIR_DRAG
        vertical = self.velocity.y
        if self.on_ground and vertical < 0.0:
            vertical = 0.0
        vertical = (vertical - gravity * scale) * (drag**scale)
        if self.on_ground and vertical < 0.0:
            vertical = 0.0
        self.velocity = self.velocity.replace(y=vertical)

    def _update_liquid_state(self) -> None:
        """刷新 ``in_water`` / ``in_lava``（脚所在格、脚上方一点、头部）。"""
        x = math.floor(self.position.x)
        z = math.floor(self.position.z)
        feet_y = math.floor(self.position.y + 1e-3)
        above_y = math.floor(self.position.y + 0.1)
        head_y = math.floor(self.position.y + self.EYE_HEIGHT)
        registry = self.world.registry
        states = (
            self.world.get_state(x, feet_y, z),
            self.world.get_state(x, above_y, z),
        )
        head_state = self.world.get_state(x, head_y, z)
        self.in_water = any(registry.is_water(state) for state in states)
        self.in_lava = any(registry.is_lava(state) for state in states)
        self.head_in_water = registry.is_water(head_state)

    def _clamp_void(self) -> None:
        """虚空保护：不让玩家掉到世界底部以下。"""
        floor_y = float(self.world.min_y)
        if self.position.y < floor_y:
            self.position = self.position.replace(y=floor_y)
            self.velocity = self.velocity.replace(y=0.0)
            self.on_ground = True

    # ------------------------------------------------------------------ 竖直
    def _move_vertical(self, scale: float) -> None:
        delta = self.velocity.y * scale
        if delta == 0.0:
            if not self.on_ground:
                return
            # 贴地探针：站在地上时向下探一点，用来确认 on_ground
            delta = -GROUND_PROBE
        blocked = self._move_axis(1, delta)[0]
        if delta < 0.0:
            self.on_ground = blocked
            if blocked:
                self.velocity = self.velocity.replace(y=0.0)
        else:
            self.on_ground = False
            if blocked:
                self.velocity = self.velocity.replace(y=0.0)

    # ------------------------------------------------------------------ 水平
    def _move_horizontal(self, scale: float) -> None:
        self.horizontal_collision = False
        dx = self.velocity.x * scale
        dz = self.velocity.z * scale
        if dx == 0.0 and dz == 0.0:
            return
        start_x = self.position.x
        start_z = self.position.z
        blocked_x = False
        blocked_z = False
        if dx != 0.0:
            blocked_x = self._move_axis(0, dx)[0]
        if dz != 0.0:
            blocked_z = self._move_axis(2, dz)[0]
        if blocked_x or blocked_z:
            self.horizontal_collision = True
            # 只重试被挡住、还没走完的那部分位移
            remaining_x = dx - (self.position.x - start_x)
            remaining_z = dz - (self.position.z - start_z)
            if self.on_ground and not self.sneaking and self._try_step_up(remaining_x, remaining_z):
                self.horizontal_collision = False
                blocked_x = False
                blocked_z = False
        if blocked_x:
            self.velocity = self.velocity.replace(x=0.0)
        if blocked_z:
            self.velocity = self.velocity.replace(z=0.0)

    def _try_step_up(self, dx: float, dz: float) -> bool:
        """被挡住时尝试抬升 <= STEP_HEIGHT 后重新水平移动（自动上台阶 / 半砖）。"""
        if abs(dx) + abs(dz) < 1e-6:
            return False
        base = self.position
        height = self._height()
        requested = abs(dx) + abs(dz)
        steps = int(round(self.STEP_HEIGHT / 0.1))
        for index in range(1, steps + 1):
            lift = 0.1 * index
            if lift > self.STEP_HEIGHT + 1e-9:
                break
            lifted = base.replace(y=base.y + lift)
            if self._collides(self._aabb_at(lifted, height)):
                continue
            moved = self._trial_move(lifted, height, dx, dz)
            gained = abs(moved.x - base.x) + abs(moved.z - base.z)
            if gained < requested - 1e-6:
                continue
            self.position = moved
            self.on_ground = True
            return True
        return False

    def _trial_move(self, position: Vec3, height: float, dx: float, dz: float) -> Vec3:
        """在给定位置试推一段水平位移，返回落点（不改自身状态）。"""
        if dx != 0.0:
            allowed, _ = self._clip(self._aabb_at(position, height), 0, dx)
            position = position.replace(x=position.x + allowed)
        if dz != 0.0:
            allowed, _ = self._clip(self._aabb_at(position, height), 2, dz)
            position = position.replace(z=position.z + allowed)
        return position

    # ------------------------------------------------------------------ 碰撞
    def _collision_boxes(self, box: AABB) -> list[AABB]:
        """收集与 ``box`` 相交的方块碰撞箱。

        查询范围向下多带一格：栅栏 / 墙这类方块的碰撞箱会高出自身格子，
        贴着地面站立时如果只查当前格就会漏掉它们的上半截。

        有区块列的世界走 ``World.collision_boxes``；没有区块列的世界
        （测试用的 ``SimpleWorld``）退回逐格扫描。
        """
        query = AABB(box.min_x, box.min_y - 1.0, box.min_z, box.max_x, box.max_y, box.max_z)
        if self.world.columns:
            return self.world.collision_boxes(query)
        return _scan_collision_boxes(self.world, query)

    def _collides(self, box: AABB) -> bool:
        """``box`` 是否与任意方块碰撞箱真正相交。"""
        for other in self._collision_boxes(box):
            if other.intersects(box):
                return True
        return False

    def _clip(self, box: AABB, axis: int, delta: float) -> tuple[float, bool]:
        """把沿单轴的位移裁剪到不穿模的值，返回 (允许位移, 是否被挡)。"""
        if delta == 0.0:
            return 0.0, False
        result = delta
        for other in self._collision_boxes(_swept_box(box, axis, delta)):
            if axis == 0:
                if not _overlaps(other.min_y, other.max_y, box.min_y, box.max_y):
                    continue
                if not _overlaps(other.min_z, other.max_z, box.min_z, box.max_z):
                    continue
                gap = (other.min_x - box.max_x) if delta > 0.0 else (other.max_x - box.min_x)
            elif axis == 1:
                if not _overlaps(other.min_x, other.max_x, box.min_x, box.max_x):
                    continue
                if not _overlaps(other.min_z, other.max_z, box.min_z, box.max_z):
                    continue
                gap = (other.min_y - box.max_y) if delta > 0.0 else (other.max_y - box.min_y)
            else:
                if not _overlaps(other.min_x, other.max_x, box.min_x, box.max_x):
                    continue
                if not _overlaps(other.min_y, other.max_y, box.min_y, box.max_y):
                    continue
                gap = (other.min_z - box.max_z) if delta > 0.0 else (other.max_z - box.min_z)
            if delta > 0.0:
                if gap <= -PENETRATION_MARGIN:
                    continue  # 已经重叠，不往回推
                result = min(result, max(0.0, gap - EPS))
            else:
                if gap >= PENETRATION_MARGIN:
                    continue
                result = max(result, min(0.0, gap + EPS))
        return result, abs(result) < abs(delta) - 1e-9

    def _move_axis(self, axis: int, delta: float) -> tuple[bool, float]:
        """沿单轴做一次带碰撞的位移，返回 (是否被挡, 实际位移)。"""
        if delta == 0.0:
            return False, 0.0
        allowed, blocked = self._clip(self.aabb(), axis, delta)
        if allowed != 0.0:
            if axis == 0:
                self.position = self.position.replace(x=self.position.x + allowed)
            elif axis == 1:
                self.position = self.position.replace(y=self.position.y + allowed)
            else:
                self.position = self.position.replace(z=self.position.z + allowed)
        return blocked, allowed

    def move(self, dx: float, dy: float, dz: float) -> None:
        """带碰撞的位移（供动作层微调用，不施加物理）。"""
        if dy != 0.0:
            blocked = self._move_axis(1, dy)[0]
            if dy < 0.0:
                self.on_ground = blocked
            else:
                self.on_ground = False
            if blocked:
                self.velocity = self.velocity.replace(y=0.0)
        if dx != 0.0:
            blocked = self._move_axis(0, dx)[0]
            if blocked:
                self.velocity = self.velocity.replace(x=0.0)
                self.horizontal_collision = True
        if dz != 0.0:
            blocked = self._move_axis(2, dz)[0]
            if blocked:
                self.velocity = self.velocity.replace(z=0.0)
                self.horizontal_collision = True

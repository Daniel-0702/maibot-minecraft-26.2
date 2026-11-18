"""玩家控制器：把"客户端状态"变成"能走、能挖、能打"的角色。

* 20Hz 物理与发包循环（:class:`PlayerController`）
* 行走 / 寻路跟随 / 采集 / 放置 / 攻击 / 进食 / 拾取等原子动作
* 视角控制接 :class:`mcbot.humanize.Humanizer`，让转头、反应、点击节奏更像人

所有动作都返回 :class:`ActionResult`，方便上层（LLM 工具、自治大脑）判断成败。
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import knowledge
from .client import Entity, ItemStack, MinecraftClient
from .humanize import AimPlan, Humanizer
from .pathfinding import GoalNear, Path, Pathfinder
from .physics import MovementInput, PlayerPhysics
from .registry import BlockInfo, BlockRegistry
from .types import BlockPos, Rotation, Vec3, look_at, wrap_degrees

__all__ = ["ActionResult", "PlayerController"]

TICK = 0.05
#: 工具速度（与方块匹配时）。数值取自 Minecraft Wiki「Breaking」的工具速度表。
TOOL_SPEEDS = dict(knowledge.TOOL_SPEED_BY_TIER)


@dataclass(slots=True)
class ActionResult:
    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        payload = {"success": self.ok, "message": self.message}
        payload.update(self.data)
        return payload


class PlayerController:
    """在 :class:`MinecraftClient` 之上提供角色能力。"""

    def __init__(
        self,
        client: MinecraftClient,
        *,
        humanizer: Humanizer | None = None,
        logger: Callable[[str], None] | None = None,
        auto_eat: bool = True,
    ) -> None:
        self.client = client
        self.registry: BlockRegistry = client.registry
        self.log = logger or client.log
        self.humanizer = humanizer or Humanizer(rng=random.Random())
        self.physics = PlayerPhysics(client.world, client.position, client.yaw, client.pitch)
        self.pathfinder = Pathfinder(client.world)

        self.running = False
        self.joined = False
        self._tick_task: asyncio.Task | None = None
        self._has_position = False
        self._movement = MovementInput()
        self._waypoint: Vec3 | None = None
        self._shore_target: Vec3 | None = None
        self._walk_radius = 0.55
        self._sprint_walking = False
        self._sneak_walking = False
        self._aim_plan: AimPlan | None = None
        self._last_sent = 0.0
        self._last_ground = False
        self._stuck_ticks = 0
        self._last_pos = Vec3()
        self._action_lock = asyncio.Lock()
        self._auto_eat = auto_eat
        self._last_eat_at = 0.0
        self._jump_cooldown = 0.0
        self._shore_scan_at = 0.0
        #: 头部连续浸在水里的秒数（Wiki：约 15 秒后会开始溺水掉血）
        self.submerged_seconds = 0.0
        #: 前方是否有摔下去会掉血的落差（悬崖保护）
        self._cliff_ahead = False
        #: "家"的坐标（玩家/命令设置的常驻目标）
        self.home: Vec3 | None = None
        self._swing_at = 0.0
        self._sprint_state = False
        self._sneak_state = False

        client.on("join", self._on_join)
        client.on("position", self._on_server_position)
        client.on("respawn", self._on_respawn)
        client.on("disconnect", self._on_disconnect)

    # ------------------------------------------------------------------ 生命周期
    def _on_join(self, _payload: Any) -> None:
        self.joined = True
        self.physics = PlayerPhysics(self.client.world, self.client.position, self.client.yaw, self.client.pitch)
        self.pathfinder = Pathfinder(self.client.world)

    def _on_respawn(self, _payload: Any) -> None:
        self._has_position = False
        self._waypoint = None
        self._shore_target = None
        self.submerged_seconds = 0.0
        self.physics.velocity = Vec3()
        self.physics.position = self.client.position

    def _on_disconnect(self, _payload: Any) -> None:
        self.joined = False

    def _on_server_position(self, position: Vec3) -> None:
        # 服务器校正位置（传送、回弹、抗作弊检查）
        if not self._has_position:
            self.physics.position = position
            self.physics.velocity = Vec3()
        else:
            delta = position.distance_to(self.physics.position)
            self.physics.position = position
            # 小幅例行核对就贴过去、保留速度：否则每收到一次核对就被清速，
            # 走起来一顿一顿的，看起来像在"乱晃"。
            if delta > 0.35:
                self.physics.velocity = Vec3()
        self._has_position = True
        self._last_pos = position

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._tick_task = asyncio.create_task(self._tick_loop())

    def attach(self) -> None:
        """在客户端进入游戏后同步一次世界 / 坐标，允许开始物理循环。"""
        self.physics = PlayerPhysics(self.client.world, self.client.position, self.client.yaw, self.client.pitch)
        self.pathfinder = Pathfinder(self.client.world)
        self.joined = self.client.state == "play"
        self._has_position = self.joined and self.client.position_received
        self._last_pos = self.client.position
        self._last_ground = False

    async def stop(self) -> None:
        self.running = False
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None
        self.client.state = "closed"

    # ------------------------------------------------------------------ 主循环
    async def _tick_loop(self) -> None:
        next_tick = time.monotonic()
        while self.running:
            next_tick += TICK
            try:
                self._tick()
            except Exception as exc:  # 单帧异常不应终止循环
                self.log(f"物理帧异常: {exc!r}")
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                next_tick = time.monotonic()

    def _tick(self) -> None:
        if not self.joined or not self._has_position or self.client.state != "play":
            return
        if self.client.is_dead:
            self._movement = MovementInput()
            self.physics.tick(MovementInput(), TICK)
            return

        self._update_aim()
        self._update_water_escape()
        self._movement = self._compute_movement()
        self.physics.tick(self._movement, TICK)
        if self.physics.head_in_water:
            self.submerged_seconds += TICK
        else:
            self.submerged_seconds = 0.0
        self._sync_client_state()
        self._send_state()

        if self._auto_eat and time.monotonic() - self._last_eat_at > 4.0:
            if self.client.food <= 16 and self._find_food_slot() is not None:
                self._last_eat_at = time.monotonic()
                asyncio.create_task(self._auto_eat_once())

    def _update_water_escape(self) -> None:
        """在水里又没有明确目标时，定期找一个岸边游过去，避免原地淹死。"""
        if not self.physics.in_water:
            self._shore_target = None
            return
        if self._waypoint is not None:
            return
        now = time.monotonic()
        if self._shore_target is None and now - self._shore_scan_at >= 1.0:
            self._shore_scan_at = now
            self._shore_target = self._find_shore()

    def _sync_client_state(self) -> None:
        self.client.position = self.physics.position
        self.client.yaw = self.physics.yaw
        self.client.pitch = self.physics.pitch
        self.client.on_ground = self.physics.on_ground

    def _send_state(self) -> None:
        now = time.monotonic()
        moved = self.physics.position.distance_to(self._last_pos) > 1e-4
        if moved or now - self._last_sent >= 0.5 or self.physics.on_ground != self._last_ground:
            self.client.send_position(
                self.physics.position,
                self.physics.yaw,
                self.physics.pitch,
                self.physics.on_ground,
            )
            self._last_sent = now
            self._last_ground = self.physics.on_ground
            self._last_pos = self.physics.position

    # ------------------------------------------------------------------ 移动
    def _compute_movement(self) -> MovementInput:
        if self.client.is_dead:
            return MovementInput()
        movement = MovementInput(sneak=self._sneak_walking)
        # MC 里"游泳上浮"= 一直按住空格：每 tick 都要给一次向上的冲量，
        # 只按一下会被水里的重力/阻力马上压回去（用户观察到的"只跳了一下"）。
        if self.physics.in_water:
            movement.jump = True
        target = self._waypoint if self._waypoint is not None else self._shore_target
        if target is None:
            return movement
        delta = target - self.physics.position
        horizontal = math.hypot(delta.x, delta.z)
        radius = self._walk_radius if self._waypoint is not None else 0.7
        if horizontal <= radius and abs(delta.y) < 1.4:
            if self._waypoint is None:
                self._shore_target = None
            return movement
        # 朝向目标（限速转动，避免"瞬移视角"）
        target_yaw = math.degrees(math.atan2(-delta.x, delta.z))
        difference = wrap_degrees(target_yaw - self.physics.yaw)
        max_turn = 22.0
        self.physics.yaw = wrap_degrees(self.physics.yaw + max(-max_turn, min(max_turn, difference)))
        movement.forward = 1.0
        if self._waypoint is not None:
            if self._sprint_walking and horizontal > 2.5 and knowledge.sprint_allowed(self.client.food):
                movement.sprint = True
            # 需要上台阶 / 卡住时起跳
            need_jump = delta.y > 0.15 and horizontal < 2.0 and self.physics.on_ground
            if need_jump or self.physics.horizontal_collision and self.physics.on_ground:
                if time.monotonic() >= self._jump_cooldown:
                    movement.jump = True
                    self._jump_cooldown = time.monotonic() + 0.35
        elif self.physics.on_ground and horizontal < 2.5:
            # 游到岸边被挡住时也要跳：配合持续上浮才能爬出水池
            movement.jump = True
        # 悬崖保护：往前走一步会掉超过 3 格（Wiki：3 格以内不摔伤）就刹住
        self._cliff_ahead = False
        if movement.forward > 0.0 and not self.physics.in_water:
            drop = self._drop_ahead(movement)
            if drop is not None and drop > knowledge.FALL_SAFE_BLOCKS:
                movement.forward = 0.0
                movement.sprint = False
                self._cliff_ahead = True
        return movement

    def _drop_ahead(self, movement: MovementInput) -> float | None:
        """朝行进方向探一步：返回脚下到最近落脚面的落差；``None`` 表示有支撑。

        只看水平前方约 0.9 格的一列；水面/液体当作安全落点（摔进水里不掉血）。
        """
        yaw = math.radians(self.physics.yaw)
        forward = movement.forward
        strafe = movement.strafe
        dx = -math.sin(yaw) * forward + math.cos(yaw) * strafe
        dz = math.cos(yaw) * forward + math.sin(yaw) * strafe
        length = math.hypot(dx, dz)
        if length < 1e-6:
            return None
        x = math.floor(self.physics.position.x + dx / length * 0.9)
        z = math.floor(self.physics.position.z + dz / length * 0.9)
        world = self.client.world
        if not world.is_loaded(x, z):
            return None
        registry = self.registry
        feet_y = math.floor(self.physics.position.y + 0.01)
        for y in range(feet_y, feet_y - 8, -1):
            state = world.get_state(x, y, z)
            if registry.is_liquid(state):
                return None  # 掉进水里不掉血
            if registry.is_passable(state):
                continue
            return max(0.0, self.physics.position.y - (y + 1.0))
        return float(knowledge.FALL_SAFE_BLOCKS + 1.0)  # 深坑

    def _update_aim(self) -> None:
        if self._aim_plan is None:
            return
        rotation = self._aim_plan.next_rotation()
        if rotation is None:
            self._aim_plan = None
            return
        self.physics.set_rotation(rotation.yaw, rotation.pitch)
        self.client.send_rotation(self.physics.yaw, self.physics.pitch, self.physics.on_ground)

    def _request_sprint(self, sprint: bool) -> None:
        if sprint != self._sprint_state:
            self._sprint_state = sprint
            self.client.set_sprinting(sprint)

    def _request_sneak(self, sneak: bool) -> None:
        if sneak != self._sneak_state:
            self._sneak_state = sneak
            self.client.set_sneaking(sneak)

    # ------------------------------------------------------------------ 水中自救
    def _can_stand_cell(self, x: int, y: int, z: int) -> bool:
        """(x, y, z) 是否为"干燥、可站立"的格子（脚+头可过、脚下实心、不是水/岩浆）。"""
        world = self.client.world
        registry = self.registry
        if not world.is_loaded(x, z):
            return False
        feet = world.get_state(x, y, z)
        head = world.get_state(x, y + 1, z)
        below = world.get_state(x, y - 1, z)
        if not registry.is_passable(feet) or not registry.is_passable(head):
            return False
        if registry.is_liquid(feet) or registry.is_liquid(head):
            return False
        if registry.is_dangerous(feet) or registry.is_dangerous(below):
            return False
        return registry.is_solid(below)

    def _find_shore(self, radius: int = 14) -> Vec3 | None:
        """找一个能爬上去的岸边落脚点（返回脚所在坐标）。

        从近到远一圈圈找：优先离自己最近、高度接近当前水面的干燥可站立格。
        """
        world = self.client.world
        position = self.physics.position
        px, py, pz = math.floor(position.x), math.floor(position.y), math.floor(position.z)
        best: tuple[float, Vec3] | None = None
        for ring in range(1, radius + 1):
            for dx in range(-ring, ring + 1):
                for dz in range(-ring, ring + 1):
                    if max(abs(dx), abs(dz)) != ring:
                        continue
                    x, z = px + dx, pz + dz
                    if not world.is_loaded(x, z):
                        continue
                    # 从高一点往下找，避免把水底当成岸边
                    for y in range(py + 3, py - 5, -1):
                        if not self._can_stand_cell(x, y, z):
                            continue
                        target = Vec3(x + 0.5, float(y), z + 0.5)
                        distance = math.hypot(x + 0.5 - position.x, z + 0.5 - position.z)
                        if best is None or distance < best[0]:
                            best = (distance, target)
                        break
            if best is not None:
                return best[1]
        return None

    async def swim_to_land(self, *, timeout: float = 25.0) -> ActionResult:
        """掉进水里时：持续上浮 + 游向最近的岸边，直到脚踏实地。"""
        if not self.joined:
            return ActionResult(False, "尚未进入游戏")
        if not self.physics.in_water:
            self._shore_target = None
            return ActionResult(True, "不在水里")
        started = time.monotonic()
        self._shore_target = self._find_shore()
        if self._shore_target is None:
            # 四周都是水：先原地持续上浮别淹死，再重新找岸
            anchor = time.monotonic()
            while time.monotonic() - anchor < min(6.0, timeout) and self.physics.in_water:
                await asyncio.sleep(TICK)
            self._shore_target = self._find_shore()
        while time.monotonic() - started < timeout:
            if not self.joined:
                return ActionResult(False, "与服务器断开连接")
            if not self.physics.in_water and self.physics.on_ground:
                self._shore_target = None
                return ActionResult(True, "已上岸")
            if self._shore_target is None:
                self._shore_target = self._find_shore()
                if self._shore_target is None:
                    await asyncio.sleep(TICK)
                    continue
            await asyncio.sleep(TICK)
        self._shore_target = None
        return ActionResult(False, "没能游上岸（附近可能没有可站的陆地）")

    async def walk_to(self, target: Vec3, *, radius: float = 0.55, timeout: float = 12.0, sprint: bool = False, sneak: bool = False) -> ActionResult:
        """直线走到目标点（不做寻路）。"""
        self._waypoint = target
        self._walk_radius = radius
        self._sprint_walking = sprint
        self._sneak_walking = sneak
        self._request_sneak(sneak)
        if sprint:
            self._request_sprint(True)
        started = time.monotonic()
        last_progress = time.monotonic()
        last_position = self.physics.position
        try:
            while time.monotonic() - started < timeout:
                if not self.joined:
                    return ActionResult(False, "与服务器断开连接")
                if self.physics.position.distance_to(target) <= radius + 0.15:
                    return ActionResult(True, "已到达")
                if time.monotonic() - last_progress > 1.6:
                    if self.physics.position.distance_to(last_position) > 0.35:
                        last_progress = time.monotonic()
                        last_position = self.physics.position
                    elif self._cliff_ahead:
                        # Wiki：下落超过 3 格会摔伤，走到悬崖边就停，换条路
                        return ActionResult(False, "前方落差太大，停下了")
                    elif time.monotonic() - last_progress > 4.0:
                        return ActionResult(False, "被卡住，无法继续前进")
                await asyncio.sleep(TICK)
            return ActionResult(False, f"移动超时（目标 {target.as_tuple()}）")
        finally:
            self._waypoint = None
            self._sprint_walking = False
            self._sneak_walking = False
            if sprint:
                self._request_sprint(False)
            if sneak:
                self._request_sneak(False)

    async def goto(self, target: BlockPos | Vec3, *, radius: float = 1.0, timeout: float = 90.0) -> ActionResult:
        """寻路走到目标（会绕开障碍、跳跃、避让危险方块）。"""
        if not self.joined:
            return ActionResult(False, "尚未进入游戏")
        target_pos = target if isinstance(target, BlockPos) else target.floor()
        goal = GoalNear(target_pos, radius)
        for attempt in range(3):
            path = self.pathfinder.find_path(self.physics.position, goal)
            if path is None:
                return ActionResult(False, f"找不到通往 {target_pos.as_tuple()} 的路径")
            result = await self._follow_path(path, timeout=timeout / 3)
            if result.ok:
                return result
            if attempt == 2:
                return result
        return ActionResult(False, "无法到达目标")

    async def _follow_path(self, path: Path, *, timeout: float = 30.0) -> ActionResult:
        started = time.monotonic()
        nodes = path.nodes[1:]
        for index, node in enumerate(nodes):
            if time.monotonic() - started > timeout:
                return ActionResult(False, "沿路径行走超时")
            target = Vec3(node.x + 0.5, node.y, node.z + 0.5)
            remaining = len(nodes) - index
            result = await self.walk_to(target, radius=0.55, timeout=8.0, sprint=remaining > 4)
            if not result.ok and result.message != "被卡住，无法继续前进":
                return result
            if not result.ok:
                return ActionResult(False, "路径上有障碍")
        return ActionResult(True, "已到达目标附近")

    async def look_at_entity(self, entity: Entity | int, *, humanize: bool = True) -> ActionResult:
        entity_obj = entity if isinstance(entity, Entity) else self.client.entities.get(entity)
        if entity_obj is None:
            return ActionResult(False, "目标实体不存在")
        # 末影人：直视眼睛会被激怒（Wiki「Enderman」），所以瞄身体下半段。
        if knowledge.is_gaze_provoked_mob(entity_obj.short_name):
            aim_height = knowledge.enderman_safe_aim_height()
        else:
            aim_height = 1.4
        position = entity_obj.position.replace(y=entity_obj.position.y + aim_height)
        return await self.look_at(position, humanize=humanize)

    async def look_at(self, target: Vec3, *, humanize: bool = True, timeout: float = 3.0) -> ActionResult:
        rotation = look_at(self.physics.eye_position(), target)
        if humanize and self.running:
            self._aim_plan = self.humanizer.plan_aim(Rotation(self.physics.yaw, self.physics.pitch), rotation)
            started = time.monotonic()
            while self._aim_plan is not None:
                if time.monotonic() - started > timeout:
                    self._aim_plan = None
                    break
                await asyncio.sleep(TICK)
        self._aim_plan = None
        self.physics.set_rotation(rotation.yaw, rotation.pitch)
        self.client.send_rotation(self.physics.yaw, self.physics.pitch, self.physics.on_ground)
        return ActionResult(True, "已转向目标")

    # ------------------------------------------------------------------ 采集
    def _hotbar_items(self) -> list[tuple[int, ItemStack]]:
        items = []
        # Native 776 keeps vanilla window-0 slot numbers; legacy backend
        # historically exposes hotbar as 0..8.
        offset = 36 if self.client.protocol_version == 776 else 0
        for slot in range(9):
            item = self.client.inventory.get(offset + slot)
            if item is not None and not item.is_empty:
                items.append((slot, item))
        return items

    def _best_tool(self, block: BlockInfo) -> tuple[int, ItemStack] | None:
        best: tuple[int, ItemStack] | None = None
        best_score = -1.0
        for slot, item in self._hotbar_items():
            info = self.registry.item(item.item_id)
            if info is None:
                continue
            speed = self._tool_speed(info, block)
            score = speed
            if info.tool_class and info.tool_class == block.tool_class:
                score += 10.0
            if score > best_score:
                best_score = score
                best = (slot, item)
        return best

    def _tool_speed(self, item_info, block: BlockInfo) -> float:
        """工具对目标方块的挖掘速度倍率（数值来自 Minecraft Wiki「Breaking」）。"""
        if not item_info.tool_class or item_info.tool_class != block.tool_class:
            return 1.0
        tier = item_info.short_name.split("_", 1)[0]
        return knowledge.TOOL_SPEED_BY_TIER.get(tier, 1.0)

    def break_ticks(self, position: BlockPos) -> int | None:
        """按 Wiki 公式估算破坏该方块需要的 tick 数；``None`` 表示无法破坏。

        会算上"头在水里 ×5"和"脚不沾地 ×5"这两种原版惩罚，否则在水里/悬空
        挖方块时会等太短，服务器根本没破坏，表现就是"一直挖不掉"。
        """
        block = self.client.world.block_at(position)
        if self.registry.is_air(block.state_id):
            return 0
        if self.client.game_mode == 1:
            return 0
        hardness = block.hardness
        if hardness < 0:
            return None
        tool = self._best_tool(block)
        speed = 1.0
        can_harvest = not block.requires_tool
        if tool is not None:
            info = self.registry.item(tool[1].item_id)
            if info is not None:
                speed = self._tool_speed(info, block)
                if info.tool_class == block.tool_class and info.tool_level >= block.min_tool_level:
                    can_harvest = True
        return knowledge.mining_ticks(
            hardness,
            speed,
            can_harvest=can_harvest,
            submerged=self.physics.head_in_water,
            on_ground=self.physics.on_ground,
        )

    async def mine(self, position: BlockPos, *, timeout: float = 12.0) -> ActionResult:
        """挖掉一个方块（自动换工具、瞄准、按破坏时间等待）。"""
        async with self._action_lock:
            if not self.joined:
                return ActionResult(False, "尚未进入游戏")
            block = self.client.world.block_at(position)
            if self.registry.is_air(block.state_id):
                return ActionResult(True, "该位置已经是空气", {"block": block.name})
            if self.physics.eye_position().distance_to(position.center()) > 4.8:
                return ActionResult(False, f"{block.short_name} 太远了，需要先靠近")
            ticks = self.break_ticks(position)
            if ticks is None:
                return ActionResult(False, f"{block.short_name} 无法被破坏")
            tool = self._best_tool(block)
            if tool is not None and tool[0] != self.client.held_slot:
                self.client.set_held_slot(tool[0])
                await asyncio.sleep(0.15)
            await self.look_at(position.center())
            await asyncio.sleep(self.humanizer.reaction_delay(2.0))
            self._waypoint = None
            for attempt in range(3):
                self.client.dig(0, position)
                await asyncio.sleep(max(0.05, ticks * TICK + 0.12 * attempt))
                self.client.dig(2, position)
                self.client.swing_arm()
                started = time.monotonic()
                while time.monotonic() - started < 1.5:
                    if self.registry.is_air(self.client.world.get_state(position.x, position.y, position.z)):
                        return ActionResult(True, f"已破坏 {block.short_name}", {"block": block.name})
                    await asyncio.sleep(TICK)
            return ActionResult(False, f"挖掘 {block.short_name} 失败（可能缺少合适工具）")

    def can_harvest(self, position: BlockPos) -> bool:
        """按 Wiki「Breaking」：这个方块现在挖掉会不会真的掉落资源。"""
        block = self.client.world.block_at(position)
        if not block.requires_tool:
            return True
        tool = self._best_tool(block)
        if tool is None:
            return False
        info = self.registry.item(tool[1].item_id)
        if info is None:
            return False
        return info.tool_class == block.tool_class and info.tool_level >= block.min_tool_level

    async def mine_nearest(self, names: set[str] | frozenset[str], *, radius: int = 32, max_blocks: int = 1) -> ActionResult:
        """在附近寻找并挖掘指定方块，返回挖到的数量。

        只挑"挖了会有收获"的方块：徒手挖石头既慢又没有掉落，纯属浪费时间。
        """
        full = frozenset(name if ":" in name else f"minecraft:{name}" for name in names)
        mined = 0
        for _ in range(max_blocks):
            candidates = self.client.world.find_blocks(full, self.physics.position, radius=radius, limit=16)
            target = next((pos for pos in candidates if self.can_harvest(pos)), None)
            if target is None:
                if mined == 0 and candidates:
                    return ActionResult(False, "附近的目标方块需要更好的工具", {"mined": 0})
                break
            approach = await self.goto(target, radius=3.0, timeout=90.0)
            if not approach.ok:
                return ActionResult(mined > 0, f"靠近失败: {approach.message}", {"mined": mined})
            if self.physics.eye_position().distance_to(target.center()) > 4.5:
                closer = await self.goto(target, radius=1.2, timeout=30.0)
                if not closer.ok:
                    return ActionResult(mined > 0, f"靠得太远，挖不到: {closer.message}", {"mined": mined})
            result = await self.mine(target)
            if not result.ok and "太远" in result.message:
                await self.goto(target, radius=1.0, timeout=30.0)
                result = await self.mine(target)
            if not result.ok:
                return ActionResult(mined > 0, result.message, {"mined": mined})
            mined += 1
            await asyncio.sleep(0.3)
        return ActionResult(mined > 0, f"挖掘完成，共 {mined} 个", {"mined": mined})

    # ------------------------------------------------------------------ 伐木
    def _within_reach(self, position: BlockPos, reach: float = 4.3) -> bool:
        """眼睛到方块中心的距离是否够得着（原版约 4.5 方块）。"""
        return self.physics.eye_position().distance_to(position.center()) <= reach

    async def _reach_tree(self, tree) -> bool:
        """走到能勾到树干的落脚点；已经够得着就直接返回 True。"""
        from .woodcutting import approach_points

        if any(self._within_reach(log) for log in tree.logs):
            return True
        for point in approach_points(self.client.world, tree, 4.2)[:6]:
            result = await self.goto(point, radius=0.8, timeout=15.0)
            if result.ok and any(self._within_reach(log) for log in tree.logs):
                return True
        return False

    async def _walk_within_reach(self, position: BlockPos) -> bool:
        """走到某个方块周围能勾到它的位置（找不到就返回 False）。"""
        candidates: list[tuple[int, BlockPos]] = []
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            for dy in (0, 1, -1):
                x, y, z = position.x + dx, position.y + dy, position.z + dz
                if not self._can_stand_cell(x, y, z):
                    continue
                candidates.append((abs(dx) + abs(dz) + abs(dy), BlockPos(x, y, z)))
        candidates.sort(key=lambda item: item[0])
        for _cost, point in candidates[:4]:
            if not await self.goto(point, radius=0.7, timeout=10.0):
                continue
            if self._within_reach(position):
                return True
        return False

    async def _clear_leaf_step(self, tree) -> bool:
        """挖掉一片"挡在树和我们之间"的树叶，一步步挖出接近通道。

        厚树冠 / 大树的树干可能离任何落脚点都超过交互距离，只能像真人一样
        先扒开面前的树叶再往里挪。每次只挖一片，挖完由调用方重新判断。
        """
        from .woodcutting import is_leaves

        world = self.client.world
        base = tree.base
        origin = self.physics.eye_position()
        # 朝树干方向的水平单位向量：优先挖"正前方、更靠近树干"的树叶，
        # 而不是绕着树冠外围一颗颗乱挖。
        direction_x = base.x + 0.5 - origin.x
        direction_z = base.z + 0.5 - origin.z
        length = math.hypot(direction_x, direction_z)
        if length < 1e-6:
            direction_x, direction_z, length = 1.0, 0.0, 1.0
        direction_x /= length
        direction_z /= length
        candidates: list[tuple[float, BlockPos, BlockPos | None]] = []

        def score(leaf: BlockPos) -> float:
            """越近、越正前方、越接近腰/头高度的树叶越优先。

            这样才会像真人一样"扒开眼前的叶子往里走"，而不是绕到树冠另一边。
            """
            offset_x = leaf.x + 0.5 - origin.x
            offset_z = leaf.z + 0.5 - origin.z
            lateral = abs(offset_x * direction_z - offset_z * direction_x)
            distance = math.hypot(offset_x, offset_z)
            height_penalty = abs((leaf.y + 0.5) - (self.physics.position.y + 1.0))
            return -distance - 0.8 * lateral - 0.5 * height_penalty

        for dx in range(-7, 8):
            for dy in range(-2, 7):
                for dz in range(-7, 8):
                    leaf = base.offset(dx, dy, dz)
                    state = world.get_state(leaf.x, leaf.y, leaf.z)
                    if not state or not is_leaves(state):
                        continue
                    value = score(leaf)
                    if self._within_reach(leaf):
                        candidates.append((value, leaf, None))
                        continue
                    for ox, oy, oz in ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1), (0, 1, 0), (0, -1, 0)):
                        x, y, z = leaf.x + ox, leaf.y + oy, leaf.z + oz
                        if not self._can_stand_cell(x, y, z):
                            continue
                        eye = Vec3(x + 0.5, y + 1.62, z + 0.5)
                        if eye.distance_to(leaf.center()) > 4.3:
                            continue
                        candidates.append((value, leaf, BlockPos(x, y, z)))
                        break
        candidates.sort(key=lambda item: -item[0])
        for _value, leaf, stand in candidates[:8]:
            if not self._within_reach(leaf):
                if stand is None:
                    continue
                if not await self.goto(stand, radius=0.7, timeout=10.0):
                    continue
                if not self._within_reach(leaf):
                    continue
            if (await self.mine(leaf)).ok:
                return True
        return False

    async def _chop_single_tree(self, tree, *, max_logs: int, deadline: float) -> int:
        """砍一棵树：清挡路树叶 → 靠近 → 从下往上砍。返回砍到的原木数。"""
        world = self.client.world
        arrived = await self._reach_tree(tree)
        cleared = 0
        while not arrived and cleared < 16 and time.monotonic() < deadline:
            if not await self._clear_leaf_step(tree):
                break
            cleared += 1
            arrived = await self._reach_tree(tree)
        if not arrived:
            return 0
        # 从最低的原木开始砍：真人也是先砍树干底部
        mined = 0
        for log in sorted(tree.logs, key=lambda pos: (pos.y, pos.x, pos.z)):
            if mined >= max_logs or time.monotonic() > deadline:
                break
            if self.registry.is_air(world.get_state(log.x, log.y, log.z)):
                continue
            for _ in range(3):
                if self._within_reach(log):
                    break
                if not await self._walk_within_reach(log):
                    break
            result = await self.mine(log)
            if result.ok:
                mined += 1
                await asyncio.sleep(0.25)
            else:
                # 再高就够不到了，先收工
                break
        return mined

    async def chop_tree(
        self,
        center: BlockPos | Vec3 | None = None,
        *,
        radius: int = 40,
        max_logs: int = 8,
        timeout: float = 95.0,
        exclude: set[BlockPos] | None = None,
    ) -> ActionResult:
        """砍一整棵树（支持被树叶包住的树），并顺手捡起掉落物。"""
        if not self.joined:
            return ActionResult(False, "尚未进入游戏")
        from .woodcutting import find_trees

        origin = center if center is not None else self.physics.position
        trees = find_trees(self.client.world, origin, radius=radius, limit=8)
        if exclude:
            trees = [tree for tree in trees if tree.base not in exclude]
        if not trees:
            return ActionResult(False, "附近没有找到可砍的树", {"mined": 0, "trees": []})
        deadline = time.monotonic() + timeout
        mined = 0
        tried: list[tuple[int, int, int]] = []
        for tree in trees:
            if mined >= max_logs or time.monotonic() > deadline:
                break
            tried.append(tree.base.as_tuple())
            try:
                count = await self._chop_single_tree(tree, max_logs=max_logs - mined, deadline=deadline)
            except Exception as exc:  # 一棵树失败不该影响换下一棵
                self.log(f"砍树失败 {tree.base.as_tuple()}: {exc!r}")
                count = 0
            if count > 0:
                mined += count
                await self.collect_items(radius=7.0, timeout=6.0)
        message = f"砍到 {mined} 个原木" if mined else "这附近的树都砍不到（可能被方块围住）"
        return ActionResult(mined > 0, message, {"mined": mined, "trees": tried})

    # ------------------------------------------------------------------ 放置
    def _find_block_item(self, block_name: str) -> tuple[int, ItemStack] | None:
        item_id = self.registry.item_id(block_name)
        if item_id is None:
            return None
        for slot, item in self._hotbar_items():
            if item.item_id == item_id:
                return slot, item
        return None

    async def place_block(self, position: BlockPos, block_name: str) -> ActionResult:
        """在指定位置放一个方块（需要主手/快捷栏里有该方块）。"""
        async with self._action_lock:
            if not self.joined:
                return ActionResult(False, "尚未进入游戏")
            state = self.client.world.get_state(position.x, position.y, position.z)
            if not self.registry.is_replaceable(state):
                return ActionResult(False, "目标位置已被占用")
            found = self._find_block_item(block_name)
            if found is None:
                return ActionResult(False, f"快捷栏里没有 {block_name}")
            slot, _item = found
            if slot != self.client.held_slot:
                self.client.set_held_slot(slot)
                await asyncio.sleep(0.1)
            # 找一个相邻的实心方块作为放置面：(相邻方块, 点击面, 点击点局部坐标)
            candidates = (
                (position.below(), 1, (0.5, 1.0, 0.5)),
                (position.above(), 0, (0.5, 0.0, 0.5)),
                (position.offset(0, 0, -1), 3, (0.5, 0.5, 1.0)),
                (position.offset(0, 0, 1), 2, (0.5, 0.5, 0.0)),
                (position.offset(-1, 0, 0), 5, (1.0, 0.5, 0.5)),
                (position.offset(1, 0, 0), 4, (0.0, 0.5, 0.5)),
            )
            for neighbor, face, cursor in candidates:
                neighbor_state = self.client.world.get_state(neighbor.x, neighbor.y, neighbor.z)
                if not self.registry.is_solid(neighbor_state):
                    continue
                hit_point = Vec3(neighbor.x + cursor[0], neighbor.y + cursor[1], neighbor.z + cursor[2])
                await self.look_at(hit_point)
                await asyncio.sleep(self.humanizer.reaction_delay(2.5))
                self.client.place_block(neighbor, face, cursor)
                self.client.swing_arm()
                started = time.monotonic()
                while time.monotonic() - started < 1.5:
                    if not self.registry.is_air(self.client.world.get_state(position.x, position.y, position.z)):
                        return ActionResult(True, f"已放置 {block_name}")
                    await asyncio.sleep(TICK)
            return ActionResult(False, "找不到可用的放置面")

    async def open_block(self, position: BlockPos, *, timeout: float = 2.5) -> bool:
        """右键目标方块尝试打开容器/工作台，等服务器回 open_screen。"""
        await self.look_at(position.center())
        await asyncio.sleep(0.08)
        # use_item_on：手上没拿可放置方块时就是"使用方块"（开箱子/合成台）
        self.client.place_block(position, 1, (0.5, 1.0, 0.5))
        self.client.swing_arm()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self.client, "crafting_window", None) is not None:
                return True
            await asyncio.sleep(TICK)
        return False

    async def move_to_hotbar(self, window: int, slot: int) -> bool:
        """用 SWAP 点击把背包里的物品换到快捷栏（合成结果常在主背包）。"""
        hotbar = list(range(36, 45)) if window == 0 else list(range(37, 46))
        items = self.client.window_items.get(window, {})
        for index, hotbar_slot in enumerate(hotbar):
            stack = items.get(hotbar_slot)
            if stack is not None and not stack.is_empty:
                continue
            self.client.click_slot(slot, button=index, click="swap", window=window)
            await asyncio.sleep(0.25)
            return True
        return False

    # ------------------------------------------------------------------ 战斗
    def weapon_short_name(self) -> str:
        """当前手持物品的短名（空手返回空串），用于算攻击速度。"""
        item = self.client.selected_item()
        if item is None or item.is_empty:
            return ""
        info = self.registry.item(item.item_id)
        return info.short_name if info is not None else ""

    async def attack(self, entity_id: int, *, max_seconds: float = 8.0, crit: bool = False) -> ActionResult:
        """攻击实体直到它消失或超时。

        攻击间隔按 Wiki「Damage#Attack cooldown」：T = 20 / 攻击速度，
        等满再点才有全额伤害（剑 0.625 秒、斧约 1.1 秒、空手 0.25 秒）。
        暴击（``crit``）要跳起来、在**下落过程中**命中，伤害 ×1.5。
        """
        async with self._action_lock:
            entity = self.client.entities.get(entity_id)
            if entity is None:
                return ActionResult(False, "目标实体不存在")
            weapon = self.weapon_short_name()
            cooldown = knowledge.attack_cooldown_seconds(weapon)
            started = time.monotonic()
            hits = 0
            while time.monotonic() - started < max_seconds:
                entity = self.client.entities.get(entity_id)
                if entity is None:
                    return ActionResult(True, f"目标已被击杀（命中 {hits} 次）", {"hits": hits})
                distance = self.physics.position.distance_to(entity.position)
                if distance > 3.2:
                    await self.walk_to(entity.position, radius=2.2, timeout=2.5, sprint=True)
                await self.look_at_entity(entity)
                if crit:
                    if self.physics.on_ground:
                        self.physics.jump()
                        await asyncio.sleep(TICK * 2)  # 起跳后再等开始下落
                        continue
                    if self.physics.velocity.y > -0.02:
                        await asyncio.sleep(TICK)
                        continue
                self.client.attack(entity_id)
                self.client.swing_arm()
                hits += 1
                # 等攻击条回满，再带一点点人类化的晚点；空手不代表能连点更快
                await asyncio.sleep(cooldown * self.humanizer.rng.uniform(1.02, 1.18))
            return ActionResult(False, f"攻击超时（命中 {hits} 次）", {"hits": hits})

    # ------------------------------------------------------------------ 进食 / 拾取
    def _find_food_slot(self) -> int | None:
        for slot, item in self._hotbar_items():
            if self.registry.food(item.item_id) is not None:
                return slot
        return None

    def find_food_slot(self) -> int | None:
        """快捷栏里第一个食物的槽位（没有则 ``None``）。"""
        return self._find_food_slot()

    async def eat(self) -> ActionResult:
        slot = self._find_food_slot()
        if slot is None:
            return ActionResult(False, "快捷栏里没有食物")
        if self.client.food >= 20:
            return ActionResult(False, "现在不饿")
        if slot != self.client.held_slot:
            self.client.set_held_slot(slot)
            await asyncio.sleep(0.15)
        before = self.client.food
        for _ in range(4):
            self.client.use_item(0)
            await asyncio.sleep(0.5)
            if self.client.food > before:
                return ActionResult(True, f"吃完了，饥饿值 {before} → {self.client.food}")
        return ActionResult(False, "进食失败")

    async def _auto_eat_once(self) -> None:
        if self.client.food > 16 or not self.joined:
            return
        async with self._action_lock:
            await self.eat()

    async def collect_items(self, *, radius: float = 12.0, timeout: float = 20.0) -> ActionResult:
        """把附近的掉落物捡起来。"""
        started = time.monotonic()
        collected = 0
        while time.monotonic() - started < timeout:
            items = [
                entity
                for entity in self.client.entities.values()
                if entity.type_name == "minecraft:item"
                and self.physics.position.distance_to(entity.position) <= radius
            ]
            if not items:
                break
            items.sort(key=lambda entity: self.physics.position.distance_to(entity.position))
            target = items[0]
            await self.walk_to(target.position, radius=0.9, timeout=6.0)
            await asyncio.sleep(0.3)
            collected += 1
        return ActionResult(True, f"拾取流程结束（接近 {collected} 个掉落物）", {"collected": collected})

    # ------------------------------------------------------------------ 背包/装备
    def _item_short_name(self, stack: ItemStack | None) -> str:
        if stack is None or stack.is_empty:
            return ""
        info = self.registry.item(stack.item_id)
        return info.short_name if info is not None else ""

    def find_item_slot(self, name: str) -> int | None:
        """按短名（支持子串）在背包里找物品，返回窗口 0 的槽位号。"""
        wanted = name.strip().lower()
        if not wanted:
            return None
        candidates: list[tuple[int, str]] = []
        for slot, stack in sorted(self.client.inventory.items()):
            short = self._item_short_name(stack)
            if not short:
                continue
            if short == wanted:
                return slot
            if wanted in short:
                candidates.append((slot, short))
        return candidates[0][0] if candidates else None

    async def equip_item(self, name: str) -> ActionResult:
        """把背包里的物品换到快捷栏并拿在手上。"""
        slot = self.find_item_slot(name)
        if slot is None:
            return ActionResult(False, f"背包里没有 {name}")
        hotbar_offset = 36 if self.client.protocol_version == 776 else 0
        if hotbar_offset <= slot < hotbar_offset + 9:
            self.client.set_held_slot(slot - hotbar_offset)
            return ActionResult(True, f"已手持 {self._item_short_name(self.client.inventory.get(slot))}")
        if hasattr(self.client, "click_slot"):
            moved = await self.move_to_hotbar(0, slot)
            if not moved:
                return ActionResult(False, "快捷栏已满，换不过去")
            # 换好后再找一次拿在手上
            slot = self.find_item_slot(name)
            if slot is None:
                return ActionResult(False, f"没找到换过去的 {name}")
            if hotbar_offset <= slot < hotbar_offset + 9:
                self.client.set_held_slot(slot - hotbar_offset)
            return ActionResult(True, f"已把 {name} 放到手上")
        return ActionResult(False, "旧版后端不支持自动换栏")

    async def place_torch(self) -> ActionResult:
        """在脚下附近点一支火把（需要背包里有火把）。"""
        if self.find_item_slot("torch") is None:
            return ActionResult(False, "背包里没有火把")
        equipped = await self.equip_item("torch")
        if not equipped.ok:
            return equipped
        position = self.physics.position
        # 火把插在脚所在格（下面就是地面），插高一格会找不到附着面
        spot = BlockPos(int(math.floor(position.x)), int(math.floor(position.y)), int(math.floor(position.z)))
        return await self.place_block(spot, "torch")

    async def drop_item(self, name: str) -> ActionResult:
        """丢掉手上的（或背包里的）某个物品。"""
        slot = self.find_item_slot(name)
        if slot is None:
            return ActionResult(False, f"背包里没有 {name}")
        hotbar_offset = 36 if self.client.protocol_version == 776 else 0
        if not (hotbar_offset <= slot < hotbar_offset + 9):
            equipped = await self.equip_item(name)
            if not equipped.ok:
                return equipped
        self.client.drop_item(all_items=True)
        return ActionResult(True, f"丢掉了 {name}")

    # ------------------------------------------------------------------ 挖矿 / 建造
    def _build_block_name(self) -> str | None:
        """挑一个能放下去的方块（木板/圆石/泥土…）。"""
        for keyword in ("planks", "cobblestone", "dirt", "stone", "log", "sand", "netherrack"):
            slot = self.find_item_slot(keyword)
            if slot is not None:
                return self._item_short_name(self.client.inventory.get(slot))
        return None

    async def dig_down(self, steps: int = 3) -> ActionResult:
        """朝面朝方向挖阶梯往下（像真人挖矿入门）。"""
        steps = max(1, min(int(steps), 32))
        mined = 0
        yaw = math.radians(self.physics.yaw)
        dx, dz = -math.sin(yaw), math.cos(yaw)
        for _ in range(steps):
            position = self.physics.position
            front = BlockPos(
                int(math.floor(position.x + dx)),
                int(math.floor(position.y)),
                int(math.floor(position.z + dz)),
            )
            targets = [front, front.below()]
            for block in targets:
                if self.registry.is_air(self.client.world.get_state(block.x, block.y, block.z)):
                    continue
                result = await self.mine(block)
                if not result.ok and "太远" in result.message:
                    await self.walk_to(block.center(), radius=1.0, timeout=5.0)
                    result = await self.mine(block)
                if result.ok:
                    mined += 1
            move = await self.goto(front, radius=0.7, timeout=10.0)
            if not move.ok:
                break
        return ActionResult(mined > 0, f"向下挖了 {mined} 格，现在 y={self.physics.position.y:.1f}", {"mined": mined})

    async def pillar_up(self, count: int = 3) -> ActionResult:
        """跳起来往脚下垫方块，爬高（需要方块）。"""
        block_name = self._build_block_name()
        if block_name is None:
            return ActionResult(False, "背包里没有可以垫脚的方块")
        placed = 0
        for _ in range(max(1, min(int(count), 16))):
            if not self.physics.on_ground:
                await asyncio.sleep(TICK * 4)
            start = self.physics.position
            self.physics.jump()
            rose = False
            for _ in range(12):
                await asyncio.sleep(TICK)
                if self.physics.position.y > start.y + 0.7:
                    rose = True
                    break
            if not rose:
                break
            base = BlockPos(int(math.floor(start.x)), int(math.floor(start.y)) - 1, int(math.floor(start.z)))
            result = await self.place_block(base, block_name)
            if not result.ok:
                break
            placed += 1
            await asyncio.sleep(0.15)
        return ActionResult(placed > 0, f"垫高了 {placed} 格", {"placed": placed})

    async def bridge(self, degrees: float = 0.0, length: int = 3) -> ActionResult:
        """朝指定方向铺路/搭桥（需要方块）。"""
        block_name = self._build_block_name()
        if block_name is None:
            return ActionResult(False, "背包里没有可以铺的方块")
        radians = math.radians(float(degrees))
        dx, dz = -math.sin(radians), math.cos(radians)
        placed = 0
        for _ in range(max(1, min(int(length), 32))):
            position = self.physics.position
            front = BlockPos(
                int(math.floor(position.x + dx)),
                int(math.floor(position.y)) - 1,
                int(math.floor(position.z + dz)),
            )
            state = self.client.world.get_state(front.x, front.y, front.z)
            if self.registry.is_air(state) or self.registry.is_replaceable(state):
                result = await self.place_block(front, block_name)
                if not result.ok:
                    break
                placed += 1
            step = await self.walk_to(front.center().replace(y=position.y), radius=0.5, timeout=5.0)
            if not step.ok:
                break
        return ActionResult(placed > 0, f"铺了 {placed} 格路", {"placed": placed})

    async def dig_shelter(self, depth: int = 2) -> ActionResult:
        """挖进旁边的土坡/山体，做一个躲藏的小洞。"""
        position = self.physics.position
        candidates = [
            (1, 0, "东"), (-1, 0, "西"), (0, 1, "南"), (0, -1, "北"),
        ]
        for dx, dz, label in candidates:
            wall = BlockPos(
                int(math.floor(position.x)) + dx,
                int(math.floor(position.y)),
                int(math.floor(position.z)) + dz,
            )
            if not self.registry.is_solid(self.client.world.get_state(wall.x, wall.y, wall.z)):
                continue
            mined = 0
            for layer in range(1, max(1, int(depth)) + 1):
                feet = BlockPos(
                    int(math.floor(position.x)) + dx * layer,
                    int(math.floor(position.y)),
                    int(math.floor(position.z)) + dz * layer,
                )
                for block in (feet, feet.above()):
                    if self.registry.is_air(self.client.world.get_state(block.x, block.y, block.z)):
                        continue
                    result = await self.mine(block)
                    if result.ok:
                        mined += 1
            move = await self.goto(
                BlockPos(int(math.floor(position.x)) + dx, int(math.floor(position.y)), int(math.floor(position.z)) + dz),
                radius=0.6,
                timeout=10.0,
            )
            if mined and move.ok:
                return ActionResult(True, f"往{label}挖了 {mined} 格的藏身处")
        return ActionResult(False, "附近没有可以挖进去的土坡")

    # ------------------------------------------------------------------ 观察
    def nearby_summary(self, radius: float = 10.0) -> dict[str, Any]:
        """附近有什么：生物、玩家、树/水/岩浆等。"""
        position = self.physics.position
        entities: dict[str, int] = {}
        for entity in self.client.entities.values():
            if position.distance_to(entity.position) > radius:
                continue
            entities[entity.short_name] = entities.get(entity.short_name, 0) + 1
        players = []
        for info in self.client.players.values():
            if info.uuid == getattr(self.client.session, "uuid", None):
                continue
            players.append(info.name)
        world = self.client.world
        center = position.floor()
        interesting: dict[str, int] = {}
        for name in ("water", "lava", "oak_log", "birch_log", "stone", "coal_ore", "iron_ore", "chest", "crafting_table"):
            try:
                state = self.registry.state_id(name)
            except KeyError:
                continue
            found = world.find_blocks({name}, center, radius=int(radius), limit=1)
            if found:
                interesting[name] = len(found)
        return {
            "entities": entities,
            "players": players,
            "blocks": interesting,
            "dark_here": self.client.is_dark_here() if hasattr(self.client, "is_dark_here") else None,
        }

    # ------------------------------------------------------------------ 家
    def sethome(self) -> ActionResult:
        """把当前位置设为"家"。"""
        self.home = self.physics.position
        return ActionResult(True, f"家设在 ({self.home.x:.1f}, {self.home.y:.1f}, {self.home.z:.1f})")

    async def go_home(self, *, timeout: float = 90.0) -> ActionResult:
        """走回家（没设过家就先设当前点为家）。"""
        if self.home is None:
            self.sethome()
            return ActionResult(True, "还没设过家，就把这里当家了")
        return await self.goto(self.home.floor(), radius=2.0, timeout=timeout)

    async def follow_player(self, name: str, *, distance: float = 3.0, timeout: float = 30.0) -> ActionResult:
        """跟随某个玩家（按名字匹配玩家列表）。"""
        if not name.strip():
            return ActionResult(False, "要跟随谁？")
        wanted = name.strip().lower()
        identity = None
        for info in self.client.players.values():
            if info.name.lower() == wanted:
                identity = info.uuid
                break
        if identity is None:
            return ActionResult(False, f"附近没有叫 {name} 的玩家")
        entity = next(
            (item for item in self.client.entities.values() if item.type_name == "minecraft:player" and item.uuid == identity),
            None,
        )
        if entity is None:
            return ActionResult(False, f"看不到 {name} 的位置")
        return await self.follow(entity.entity_id, distance=distance, timeout=timeout)

    # ------------------------------------------------------------------ 其他
    async def say(self, text: str) -> ActionResult:
        if not text.strip():
            return ActionResult(False, "消息为空")
        self.client.send_chat(text[:250])
        return ActionResult(True, "已发送")

    async def follow(self, entity_id: int, *, distance: float = 3.0, timeout: float = 30.0) -> ActionResult:
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            entity = self.client.entities.get(entity_id)
            if entity is None:
                return ActionResult(False, "目标已消失")
            if self.physics.position.distance_to(entity.position) > distance:
                result = await self.walk_to(entity.position, radius=1.5, timeout=5.0, sprint=True)
                if not result.ok and not self.joined:
                    return result
            else:
                await self.look_at_entity(entity, humanize=True)
                await asyncio.sleep(0.2)
        return ActionResult(False, "跟随超时")

    async def respawn(self) -> ActionResult:
        if not self.client.is_dead:
            return ActionResult(False, "当前没有死亡")
        await asyncio.sleep(self.humanizer.reaction_delay(1.5))
        self.client.respawn()
        return ActionResult(True, "已请求重生")

    def stop_moving(self) -> None:
        self._waypoint = None
        self._shore_target = None
        self._aim_plan = None
        self.physics.velocity = Vec3()

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        position = self.physics.position
        held = self.client.selected_item()
        return {
            "connected": self.client.connected,
            "joined": self.joined,
            "health": round(self.client.health, 1),
            "food": self.client.food,
            "position": [round(position.x, 2), round(position.y, 2), round(position.z, 2)],
            "yaw": round(self.physics.yaw, 1),
            "pitch": round(self.physics.pitch, 1),
            "on_ground": self.physics.on_ground,
            "in_water": self.physics.in_water,
            "head_in_water": self.physics.head_in_water,
            "submerged_seconds": round(self.submerged_seconds, 1),
            "dark_here": self.client.is_dark_here() if hasattr(self.client, "is_dark_here") else None,
            "dimension": self.client.dimension,
            "time_of_day": self.client.world_time_of_day(),
            "held_item": self.client.item_name(held),
            "entities": len(self.client.entities),
            "players": len(self.client.players),
            "chunks": len(self.client.world.columns),
            "chunk_packets": self.client.chunk_packets_received,
            "chunk_errors": self.client.chunk_decode_errors,
            "last_chunk_error": self.client.last_chunk_error,
            "last_chunk_hex": self.client.last_chunk_hex,
            "packet_age": round(self.client.last_packet_age, 1),
            "server_brand": self.client.server_brand,
            "ping_ms": round(self.client.ping_ms, 1),
        }

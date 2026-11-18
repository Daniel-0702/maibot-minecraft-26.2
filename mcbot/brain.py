"""自治行为：让麦麦在没有指令时也像玩家一样自己找事做。

优先级大致是「保命 → 吃饭 → 打怪 → 采集 → 闲逛」，另外负责：

* 根据状态挑目标（血量、饥饿、附近怪物、背包、时间）
* 在游戏里回应其他玩家的聊天（可选接 LLM 生成，失败则用规则回复）
* 偶尔自言自语，制造"真人感"
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import knowledge
from .client import ChatMessage, Entity, MinecraftClient
from .config import BotConfig
from .humanize import Humanizer
from .player import PlayerController
from .types import BlockPos, Vec3

__all__ = ["Brain", "HOSTILE_MOBS"]

HOSTILE_MOBS = frozenset(
    {
        "zombie",
        "zombie_villager",
        "husk",
        "drowned",
        "skeleton",
        "stray",
        "bogged",
        "creeper",
        "spider",
        "cave_spider",
        "witch",
        "slime",
        "magma_cube",
        "blaze",
        "wither_skeleton",
        "piglin",
        "piglin_brute",
        "hoglin",
        "zoglin",
        "enderman",
        "endermite",
        "silverfish",
        "guardian",
        "elder_guardian",
        "shulker",
        "vex",
        "evoker",
        "vindicator",
        "pillager",
        "ravager",
        "phantom",
        "warden",
        "breeze",
    }
)

WOOD_BLOCKS = frozenset(
    {
        "minecraft:oak_log",
        "minecraft:birch_log",
        "minecraft:spruce_log",
        "minecraft:jungle_log",
        "minecraft:acacia_log",
        "minecraft:dark_oak_log",
        "minecraft:mangrove_log",
        "minecraft:cherry_log",
        "minecraft:pale_oak_log",
        "minecraft:crimson_stem",
        "minecraft:warped_stem",
    }
)

STONE_BLOCKS = frozenset(
    {
        "minecraft:stone",
        "minecraft:cobblestone",
        "minecraft:deepslate",
        "minecraft:andesite",
        "minecraft:diorite",
        "minecraft:granite",
        "minecraft:tuff",
    }
)


@dataclass(slots=True)
class BrainStatus:
    activity: str
    paused: bool
    last_reply_at: float
    goals_done: int


class Brain:
    """自治大脑。"""

    def __init__(
        self,
        client: MinecraftClient,
        player: PlayerController,
        config: BotConfig,
        humanizer: Humanizer,
        *,
        logger: Callable[[str], None] = print,
        llm_call: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self.client = client
        self.player = player
        self.config = config
        self.humanizer = humanizer
        self.log = logger
        self.llm_call = llm_call
        self.rng = humanizer.rng

        self.running = False
        self.paused = False
        self._loop_task: asyncio.Task | None = None
        self._activity_task: asyncio.Task | None = None
        self._activity = "idle"
        self._last_reply_at = 0.0
        self._last_llm_goal = 0.0
        self._goals_done = 0
        self._session_started = time.monotonic()
        #: 第一次安全落脚的位置当作"家"，天黑就往回走
        self._home: Vec3 | None = None
        #: 探索时的大致朝向（度），避免原地随机打转
        self._heading = self.rng.uniform(0, 360)
        #: 砍/挖失败的坐标，短时间内不再尝试
        self._bad_targets: dict[BlockPos, float] = {}
        #: 合成失败后的冷静期（避免反复尝试同一步卡死）
        self._craft_cooldown_until = 0.0

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        self.cancel_activity()
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    def pause(self) -> None:
        self.paused = True
        self.cancel_activity()

    def resume(self) -> None:
        self.paused = False

    def cancel_activity(self) -> None:
        if self._activity_task is not None:
            self._activity_task.cancel()
            self._activity_task = None
        self.player.stop_moving()

    def status(self) -> BrainStatus:
        return BrainStatus(self._activity, self.paused, self._last_reply_at, self._goals_done)

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        while self.running:
            try:
                if self.paused or not self.player.joined or self.client.is_dead:
                    await asyncio.sleep(0.5)
                    continue
                if self._activity_task is not None and not self._activity_task.done():
                    await asyncio.sleep(0.4)
                    continue
                activity = await self._choose_activity()
                self._activity = activity.__name__
                self._activity_task = asyncio.create_task(self._run_activity(activity))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f"行为循环异常: {exc!r}")
            await asyncio.sleep(0.5)

    async def _run_activity(self, activity: Callable[[], Awaitable[None]]) -> None:
        try:
            # 砍树/挖矿这类活要给它足够时间做完，不要半路被换掉（那看起来就像在乱晃）
            await asyncio.wait_for(activity(), timeout=150.0)
            self._goals_done += 1
        except asyncio.TimeoutError:
            self.log("当前活动超时，换一个目标")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"活动 {activity.__name__} 失败: {exc!r}")

    # ------------------------------------------------------------------ 决策
    async def _choose_activity(self) -> Callable[[], Awaitable[None]]:
        self._remember_home()
        health = self.client.health
        food = self.client.food
        # 0. 溺水保命：头已经在水里接近氧气耗尽（Wiki：约 15 秒），先上浮
        if self.player.physics.head_in_water and self.player.submerged_seconds > 8.0:
            return self._swim_out
        # 1. 保命：掉进水里先游出来（比砍树、打怪都优先，不然会淹死）
        if self.player.physics.in_water:
            return self._swim_out
        # 2. 爬行者贴脸：它 1.5 秒就会爆（Wiki「Creeper」），立刻往后撤
        creeper = self._nearest_mob("creeper", knowledge.CREEPER_BLAST_RADIUS + 1.5)
        if creeper is not None:
            return self._flee
        if health <= 6.0:
            return self._flee
        # Wiki「Healing」：饥饿 ≥ 18 才会自然回血。受伤且没到 18 就先吃点东西。
        hunger = self.client.food
        if (
            health < 20.0
            and hunger < knowledge.HEALTH_REGEN_MIN_HUNGER
            and hunger > 0
            and self.player.find_food_slot() is not None
        ):
            return self._eat
        if food <= 13 and self.player.find_food_slot() is not None:
            return self._eat
        hostile = self._nearest_hostile(10.0)
        if hostile is not None:
            if self._should_fight(hostile):
                return self._fight
            return self._flee
        if self.humanizer.should_take_break(time.monotonic() - self._session_started):
            return self._take_break
        if self.config.llm_enabled and self.llm_call is not None and time.monotonic() - self._last_llm_goal > 300:
            goal = await self._ask_llm_goal()
            if goal is not None:
                return goal
        # 3. 天黑又没灯：回家待着，不要摸黑乱走（真人也不会半夜满山跑）
        if self._is_night() and not self._has_light_source():
            return self._stay_home
        # 4. 白天按需求排队干活，而不是随机乱晃
        task = self._next_task()
        if task == "wood":
            return self._gather_wood
        if task == "craft":
            return self._craft_gear
        if task == "stone":
            return self._gather_stone
        return self._explore

    # ------------------------------------------------------------------ 生物判断
    def _is_threat(self, entity: Entity) -> bool:
        """按 Wiki 的生物行为判断它现在是否真的会来打我们。

        * 蜘蛛：亮度足够（白天、地面上）时中立 —— 不用躲；
        * 末影人：不盯着眼睛、不攻击它时中立 —— 不要主动招惹；
        * 僵尸/骷髅：白天会着火，但还活着的时候照样会追人，仍算威胁。
        """
        profile = knowledge.mob_profile(entity.short_name)
        if profile is None:
            return entity.short_name in HOSTILE_MOBS
        if not profile.hostile:
            return False
        if profile.daylight_neutral:
            # 蜘蛛：Wiki 说亮度 ≥ 11 时中立、≤ 10 敌对。优先用真实光照数据判断，
            # 拿不到光照时退回到"白天 + 地表"的近似。
            light = None
            if hasattr(self.client, "light_at"):
                position = entity.position
                block = self.client.light_at(position.x, position.y + 1, position.z, "block")
                sky = self.client.light_at(position.x, position.y + 1, position.z, "sky")
                if block is not None or sky is not None:
                    light = max(block or 0, sky or 0)
            if light is not None:
                return light <= 10
            return self._is_night() or self.player.physics.position.y <= 50.0
        return True

    def _should_fight(self, entity: Entity) -> bool:
        """该不该打：有武器才打，没武器只敢欺负不会自爆的；末影人不惹。"""
        if entity.short_name in {"creeper", "warden"}:
            return False
        profile = knowledge.mob_profile(entity.short_name)
        if profile is not None and profile.gaze_provoked:
            return False  # 末影人：打了就被追，收益太低
        weapon = self.player.weapon_short_name()
        if weapon and knowledge.attack_cooldown_seconds(weapon) <= 0.75:
            return self.client.health > 10.0
        # 空手/慢武器：血量要够高，且不主动打远程怪
        if profile is not None and profile.ranged:
            return False
        return self.client.health > 14.0

    def _nearest_mob(self, short_name: str, radius: float) -> Entity | None:
        position = self.player.physics.position
        best: Entity | None = None
        best_distance = radius
        for entity in self.client.entities.values():
            if entity.short_name != short_name:
                continue
            distance = position.distance_to(entity.position)
            if distance < best_distance:
                best = entity
                best_distance = distance
        return best

    def _nearest_hostile_entity(self, radius: float) -> Entity | None:
        best: Entity | None = None
        best_distance = radius
        position = self.player.physics.position
        for entity in self.client.entities.values():
            if entity.short_name not in HOSTILE_MOBS or not self._is_threat(entity):
                continue
            distance = position.distance_to(entity.position)
            if distance < best_distance:
                best = entity
                best_distance = distance
        return best

    # ------------------------------------------------------------------ 目标
    def _remember_home(self) -> None:
        """第一次落到安全地面时记下"家"，之后天黑往回走。"""
        if self._home is not None or not self.player.joined:
            return
        position = self.player.physics.position
        if self.client.world.is_loaded(int(position.x), int(position.z)):
            self._home = position

    def _is_night(self) -> bool:
        try:
            return int(self.client.world_time_of_day()) >= 13000
        except Exception:
            return False

    def _has_light_source(self) -> bool:
        return any(self.count_items(name) > 0 for name in ("torch", "lantern", "glowstone", "campfire"))

    def _wood_count(self) -> int:
        return (
            self.count_items("log")
            + self.count_items("stem")
            + self.count_items("planks")
            + self.count_items("_wood")
            + self.count_items("hyphae")
        )

    def _stone_count(self) -> int:
        # 注意别用 "stone" 做关键词：会误匹配红石(redstone)等物品
        return (
            self.count_items("cobblestone")
            + self.count_items("deepslate")
            + self.count_items("andesite")
            + self.count_items("diorite")
            + self.count_items("granite")
            + self.count_items("tuff")
        )

    def _next_task(self) -> str:
        """按需求排优先级：凑木头 → 做工具 → 凑石头 → 探索。"""
        if self._wood_count() < 8:
            return "wood"
        if self._needs_crafting():
            return "craft"
        if self._stone_count() < 24 and self._has_tool("pickaxe"):
            return "stone"
        return "explore"

    def _inventory_counts(self) -> dict[str, int]:
        """背包"物品短名→数量"（用 recipes 模块的统计，含全部槽位）。"""
        from . import recipes as recipe_lib

        return recipe_lib.inventory_counts(self.client.inventory, self.client.registry)

    def _needs_crafting(self) -> bool:
        """缺工具/缺火把时才去合成，失败后冷静 3 分钟，避免死循环。"""
        if time.monotonic() < self._craft_cooldown_until:
            return False
        counts = self._inventory_counts()
        if not any(name.endswith("_pickaxe") for name in counts):
            return True
        if not any(name.endswith("_sword") for name in counts):
            return True
        coal = counts.get("coal", 0) + counts.get("charcoal", 0)
        if coal > 0 and counts.get("torch", 0) == 0:
            return True
        return False

    def _mark_bad(self, position: BlockPos) -> None:
        """记下砍不动 / 到不了的坐标，10 分钟内不再选它。"""
        now = time.monotonic()
        self._bad_targets[position] = now + 600.0
        for key, expiry in list(self._bad_targets.items()):
            if expiry < now:
                del self._bad_targets[key]

    def _bad_set(self) -> set[BlockPos]:
        now = time.monotonic()
        for key, expiry in list(self._bad_targets.items()):
            if expiry < now:
                del self._bad_targets[key]
        return set(self._bad_targets)

    # ------------------------------------------------------------------ 水中自救
    async def _swim_out(self) -> None:
        """掉水里：持续上浮 + 游向岸边（对应玩家一直按住空格往上游）。"""
        result = await self.player.swim_to_land(timeout=25.0)
        if result.ok:
            self.log("已经从水里上岸")
        else:
            self.log(f"还在水里：{result.message}")

    def _nearest_hostile(self, radius: float) -> Entity | None:
        return self._nearest_hostile_entity(radius)

    # ------------------------------------------------------------------ 背包判断
    def count_items(self, keyword: str) -> int:
        total = 0
        for item in self.client.inventory.values():
            if item is None or item.is_empty:
                continue
            info = self.client.registry.item(item.item_id)
            if info is not None and keyword in info.short_name:
                total += item.count
        return total

    def _has_tool(self, kind: str) -> bool:
        """背包里有没有某类工具（镐/斧/剑/锹…），全槽位查找。"""
        for item in self.client.inventory.values():
            if item is None or item.is_empty:
                continue
            info = self.client.registry.item(item.item_id)
            if info is not None and info.tool_class == kind:
                return True
        return False

    def _has_wood(self) -> bool:
        return self._wood_count() > 0

    # ------------------------------------------------------------------ 活动
    async def _idle(self) -> None:
        action = self.humanizer.idle_action()
        hold = self.humanizer.idle_hold_seconds(action)
        if action == "look_around":
            position = self.client.position
            target = position + Vec3(self.rng.uniform(-6, 6), self.rng.uniform(-2, 3), self.rng.uniform(-6, 6))
            await self.player.look_at(target)
        elif action in {"strafe_left", "strafe_right", "spin"}:
            angle = self.rng.uniform(-math.pi, math.pi)
            offset = Vec3(math.cos(angle), 0, math.sin(angle)) * self.rng.uniform(0.8, 2.5)
            await self.player.walk_to(self.player.physics.position + offset, radius=0.4, timeout=2.0)
        elif action == "jump":
            if self.player.physics.on_ground:
                self.player.physics.jump()
        elif action == "sneak":
            self.player.client.set_sneaking(True)
            await asyncio.sleep(hold)
            self.player.client.set_sneaking(False)
            return
        elif action == "swing_arm":
            self.player.client.swing_arm()
        await asyncio.sleep(max(0.2, hold))

    async def _explore(self) -> None:
        """朝一个固定方向走一段路（"去那边看看"），路上顺手干活，而不是原地乱转。"""
        for attempt in range(3):
            position = self.player.physics.position
            # 每次只微调方向，保持"一直在往某个方向走"的连贯感
            self._heading = (self._heading + self.rng.uniform(-55.0, 55.0)) % 360.0
            distance = self.rng.uniform(18.0, 30.0)
            angle = math.radians(self._heading)
            target_x = position.x + math.sin(angle) * distance
            target_z = position.z + math.cos(angle) * distance
            surface = self.client.world.surface_y(int(target_x), int(target_z))
            if surface is None:
                continue
            goal = BlockPos(int(target_x), int(surface), int(target_z))
            result = await self.player.goto(goal, radius=2.0, timeout=45.0)
            if not result.ok:
                continue
            await self.player.look_at(
                self.client.position + Vec3(self.rng.uniform(-6, 6), 1, self.rng.uniform(-6, 6))
            )
            # 到了新地方：附近有树就砍一棵，有石头就挖两块，让"探索"有产出
            if self.client.world.find_blocks(WOOD_BLOCKS, self.player.physics.position, radius=22, limit=1):
                await self.player.chop_tree(radius=24, max_logs=4, exclude=self._bad_set())
                await self.player.collect_items(radius=5.0, timeout=5.0)
            elif self.client.world.find_blocks(STONE_BLOCKS, self.player.physics.position, radius=14, limit=1):
                await self.player.mine_nearest(STONE_BLOCKS, radius=14, max_blocks=2)
            return
        await self._idle()

    async def _gather_wood(self) -> None:
        """砍够一批木头：优先整棵树从下往上砍，砍完捡掉落物再找下一棵。"""
        target = self._wood_count() + 6
        for attempt in range(3):
            result = await self.player.chop_tree(
                radius=40,
                max_logs=min(6, max(2, target - self._wood_count())),
                timeout=45.0,
                exclude=self._bad_set(),
            )
            mined = int(result.data.get("mined", 0))
            for base in result.data.get("trees", []):
                if mined == 0:
                    self._mark_bad(BlockPos(*base))
            if mined > 0:
                await self.player.collect_items(radius=7.0, timeout=8.0)
                if self._wood_count() >= target or attempt >= 2:
                    await self.maybe_chat("砍到木头啦，先攒点材料~")
                    return
                continue
            # 这附近砍不到树：换个方向找
            self.log(f"附近没砍到树：{result.message}")
            await self._explore()
            return

    async def _gather_stone(self) -> None:
        """挖一批石头（会自动换镐子、靠近、捡掉落物）。"""
        for attempt in range(3):
            result = await self.player.mine_nearest(STONE_BLOCKS, radius=16, max_blocks=4)
            if int(result.data.get("mined", 0)) > 0:
                await self.player.collect_items(radius=5.0, timeout=6.0)
                if self._stone_count() >= 24 or attempt >= 2:
                    if self.rng.random() < 0.2:
                        await self.maybe_chat("挖了点石头，回头盖房子用")
                    return
                continue
            self.log(f"附近没挖到石头：{result.message}")
            await self._explore()
            return

    async def _stay_home(self) -> None:
        """天黑：回家，有火把就点上（Wiki：光照足够就不会在旁边刷怪）。"""
        home = self._home
        if home is not None and self.player.physics.position.distance_to(home) > 6.0:
            goal = BlockPos(int(math.floor(home.x)), int(math.floor(home.y)), int(math.floor(home.z)))
            result = await self.player.goto(goal, radius=2.0, timeout=60.0)
            if not result.ok:
                self.log(f"回家失败：{result.message}")
                return
        await self._place_torch()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if not self._is_night():
                return
            if self.rng.random() < 0.6:
                position = self.player.physics.position
                await self.player.look_at(
                    position + Vec3(self.rng.uniform(-8, 8), self.rng.uniform(-2, 2), self.rng.uniform(-8, 8))
                )
            await asyncio.sleep(self.rng.uniform(2.0, 4.0))

    async def _place_torch(self) -> bool:
        """在家附近放一支火把（需要已经在背包里）。"""
        if self.count_items("torch") <= 0:
            return False
        result = await self.player.place_torch()
        if result.ok:
            self.log("在家附近点了一支火把")
        return result.ok

    async def _fight(self) -> None:
        hostile = self._nearest_hostile(12.0)
        if hostile is None:
            return
        if self.client.health < 8.0:
            await self._flee()
            return
        await self.player.attack(hostile.entity_id, max_seconds=6.0)
        if self.client.health < 6.0:
            await self._flee()

    async def _flee(self) -> None:
        hostile = self._nearest_hostile(14.0)
        if hostile is None:
            if self.client.food < 18:
                await self._eat()
            else:
                await self._idle()
            return
        direction = (self.player.physics.position - hostile.position).normalized()
        for distance in (8.0, 12.0, 6.0):
            target = self.player.physics.position + direction * distance
            surface = self.client.world.surface_y(int(target.x), int(target.z))
            if surface is None:
                continue
            goal = BlockPos(int(target.x), int(surface), int(target.z))
            result = await self.player.goto(goal, radius=2.0, timeout=12.0)
            if result.ok:
                break
        if self.client.food < 16 and self.player.find_food_slot() is not None:
            await self.player.eat()

    async def _eat(self) -> None:
        """吃到能自然回血的饥饿值（Wiki：≥18）为止。"""
        ate = False
        for _ in range(6):
            if self.client.food >= knowledge.HEALTH_REGEN_MIN_HUNGER:
                break
            result = await self.player.eat()
            if not result.ok:
                break
            ate = True
        if ate:
            await self.maybe_chat("饿了，先吃点东西")

    # ------------------------------------------------------------------ 合成
    def _craft_supported(self) -> bool:
        """只有 26.2 原生客户端实现了容器点击协议，旧版后端/关闭配置时跳过合成。"""
        if not getattr(self.config, "crafting", True):
            return False
        return hasattr(self.client, "click_slot") and hasattr(self.player, "open_block")

    async def _craft_gear(self) -> None:
        """木板→木棍→合成台→木镐/木剑/木斧；有煤再点火把。"""
        if not self._craft_supported():
            self._craft_cooldown_until = time.monotonic() + 3600.0
            return
        from . import recipes as recipe_lib
        from .crafting import CraftingController

        crafter = CraftingController(self.player, logger=self.log)
        registry = self.client.registry
        crafted = 0
        targets = ["wooden_pickaxe", "wooden_sword", "wooden_axe"]
        for target in targets:
            counts = self._inventory_counts()
            tool_class = target.split("_", 1)[1]  # pickaxe / sword / axe
            if self._has_tool(tool_class):
                continue
            steps = recipe_lib.plan(registry, counts, target, times=1)
            if not steps:
                continue
            ok = True
            for step in steps:
                if not await crafter.craft(step):
                    ok = False
                    break
            if ok:
                crafted += 1
                await self._equip_crafted(tool_class)
        # 有煤/木炭就做火把，夜里能照亮家
        counts = self._inventory_counts()
        coal = counts.get("coal", 0) + counts.get("charcoal", 0)
        if coal > 0 and counts.get("torch", 0) == 0:
            steps = recipe_lib.plan(registry, counts, "torch", times=1)
            if steps:
                for step in steps:
                    if await crafter.craft(step):
                        crafted += 1
        if crafted:
            self.log(f"合成完成（{crafted} 步）")
            await self.maybe_chat("做了点工具~", probability=0.5)
            self._craft_cooldown_until = 0.0
        else:
            self._craft_cooldown_until = time.monotonic() + 180.0
            self.log("这次没合成成功，先去做别的")

    async def _equip_crafted(self, tool_class: str) -> None:
        """把刚做好的工具放进快捷栏，不然选不中它。"""
        window = 0
        items = getattr(self.client, "window_items", {}).get(window, self.client.inventory)
        for slot, stack in sorted(items.items()):
            if stack is None or getattr(stack, "is_empty", False):
                continue
            info = self.client.registry.item(stack.item_id)
            if info is None or info.tool_class != tool_class:
                continue
            try:
                await self.player.move_to_hotbar(window, slot)
            except Exception as exc:  # 换栏失败不影响后续
                self.log(f"工具放进快捷栏失败: {exc!r}")
            return

    async def _take_break(self) -> None:
        """连续玩太久就像真人一样"挂机"一会儿：站着、偶尔看看四周。"""
        duration = self.humanizer.break_duration()
        self.log(f"连续游玩较久，休息 {duration:.0f} 秒")
        end = time.monotonic() + duration
        while time.monotonic() < end:
            if self.rng.random() < 0.5:
                position = self.player.physics.position
                target = position + Vec3(self.rng.uniform(-8, 8), self.rng.uniform(-2, 3), self.rng.uniform(-8, 8))
                await self.player.look_at(target)
            await asyncio.sleep(min(3.0, max(0.5, end - time.monotonic())))
        self._session_started = time.monotonic()

    # ------------------------------------------------------------------ LLM
    async def _ask_llm_goal(self) -> Callable[[], Awaitable[None]] | None:
        self._last_llm_goal = time.monotonic()
        if self.llm_call is None:
            return None
        prompt = (
            "你在玩 Minecraft。根据当前状态选一个接下来要做的事，只输出 JSON："
            '{"action": "explore|gather_wood|gather_stone|fight|idle", "reason": "一句话"}\n'
            f"状态：{json.dumps(self.player.status(), ensure_ascii=False)}"
        )
        try:
            answer = await asyncio.wait_for(self.llm_call(prompt), timeout=20.0)
        except Exception:
            return None
        if not answer:
            return None
        try:
            start = answer.find("{")
            end = answer.rfind("}")
            data = json.loads(answer[start : end + 1]) if start >= 0 and end > start else {}
        except Exception:
            return None
        mapping = {
            "explore": self._explore,
            "gather_wood": self._gather_wood,
            "gather_stone": self._gather_stone,
            "fight": self._fight,
            "idle": self._idle,
        }
        return mapping.get(str(data.get("action", "")).strip())

    # ------------------------------------------------------------------ 聊天
    async def handle_chat(self, message: ChatMessage) -> None:
        """处理一条游戏内聊天，决定是否/如何回复。"""
        if not self.config.chat_reply or message.kind != "chat":
            return
        if message.sender == self.client.username or not message.text.strip():
            return
        now = time.monotonic()
        if now - self._last_reply_at < self.config.chat_reply_cooldown:
            return
        text = message.text.strip()
        mentioned = self.client.username.lower() in text.lower() or "麦麦" in text
        if not mentioned and self.rng.random() > self.config.chat_reply_probability:
            return
        reply = await self._generate_reply(text, message.sender)
        if not reply:
            return
        self._last_reply_at = now
        delay = self.humanizer.plan_typing(reply).duration + self.humanizer.reaction_delay()
        await asyncio.sleep(min(7.0, max(0.6, delay)))
        if self.player.joined:
            self.client.send_chat(reply[:200])
            self.log(f"回复 {message.sender}: {reply}")

    async def _generate_reply(self, text: str, sender: str) -> str | None:
        if self.llm_call is not None and self.config.llm_enabled:
            prompt = (
                f"{self.config.persona}\n"
                f"当前状态：{json.dumps(self.player.status(), ensure_ascii=False)}\n"
                f"玩家 {sender} 说：{text}\n"
                "用一句话回复（不超过 40 字）。"
            )
            try:
                answer = await asyncio.wait_for(self.llm_call(prompt), timeout=15.0)
                if answer:
                    answer = answer.strip().splitlines()[0][:100]
                    return answer or None
            except Exception:
                pass
        return self._rule_reply(text)

    def _rule_reply(self, text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in ("你好", "hi", "hello", "在吗", "在么")):
            return self.rng.choice(["在的在的~", "嗨！我在挖矿呢", "来了来了"])
        if any(word in lowered for word in ("干嘛", "做什么", "在做什么")):
            status = self.player.status()
            activity = {
                "idle": "发呆",
                "explore": "到处逛",
                "gather_wood": "砍树",
                "gather_stone": "挖石头",
                "fight": "打怪",
                "flee": "跑路",
                "_swim_out": "往岸上游",
                "_stay_home": "回家待着",
                "_take_break": "歇一会儿",
            }.get(self._activity, "瞎逛")
            return f"我在{activity}呀，坐标 {status['position'][0]:.0f} {status['position'][2]:.0f}"
        if any(word in lowered for word in ("过来", "来一下", "这里")):
            return "马上来，等我一下"
        if any(word in lowered for word in ("挖矿", "钻石", "铁矿")):
            return "地下黑漆漆的，我慢慢挖"
        if any(word in lowered for word in ("吃", "饿")):
            return "饿了就啃点面包"
        return self.rng.choice(["嗯嗯", "好耶", "这就去", "哈哈", "你也在玩吗", "等我一下"])

    async def maybe_chat(self, text: str, probability: float = 0.25) -> None:
        """在合适的时候自言自语，增加真人感。"""
        if not self.player.joined or self.rng.random() > probability:
            return
        if time.monotonic() - self._last_reply_at < self.config.chat_reply_cooldown:
            return
        await asyncio.sleep(self.humanizer.reaction_delay(0.8))
        self.client.send_chat(text)
        self._last_reply_at = time.monotonic()

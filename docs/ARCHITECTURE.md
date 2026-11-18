# 架构设计

这份文档面向想改代码的人：说明每一层负责什么、任务怎么跑、状态从哪来到哪去，
以及为什么这样分层。

## 设计目标

1. **真的能玩**：不是发指令等服务器执行，而是自己算物理、自己寻路、自己瞄准。
2. **零依赖**：MaiBot 用户大多只想 `pip install` 一个 SDK；协议、NBT、AES、A\* 全部内置。
3. **可测试**：协议和物理不能靠"开个服务器试试"，所以内置假服务器与 214 个用例。
4. **可替换**：LLM 是可选增强，关掉也能靠规则跑；协议层与行为层解耦，方便加新版本。

## 分层

```
┌────────────────────────────────────────────────────────────┐
│ plugin.py      MaiBot 适配：配置模型 / Tool / Command / 上报 │
├────────────────────────────────────────────────────────────┤
│ bridge.py      生命周期：连接、重连、死亡重生、动作分发       │
├──────────────────────────┬─────────────────────────────────┤
│ player.py   动作层        │ brain.py   决策层                │
│ 走/挖/放/打/吃/捡/跟随    │ 需求优先级、活动选择、聊天回复    │
├──────────────────────────┴─────────────────────────────────┤
│ physics.py 物理 │ pathfinding.py 寻路 │ humanize.py 人类化   │
├────────────────────────────────────────────────────────────┤
│ client.py      协议状态机、收包分发、世界/实体/背包状态       │
├────────────────────────────────────────────────────────────┤
│ world.py 体素世界 │ chunk.py 区块解码 │ registry.py 数据表   │
├────────────────────────────────────────────────────────────┤
│ buffer.py 读写 │ packets.py 包表 │ nbt.py │ crypto.py │ auth.py│
└────────────────────────────────────────────────────────────┘
```

依赖方向自上而下，底层不 import 上层。`player.py` 只依赖 `client/world/physics/pathfinding/humanize`，
`brain.py` 只依赖 `player/client/config`，`bridge.py` 把三者组装起来。

## 并发模型

全部跑在 MaiBot Runner 的同一个 asyncio 事件循环里，没有线程：

| 任务 | 频率 | 职责 |
| --- | --- | --- |
| `client._read_loop` | 事件驱动 | 收包、解密、解压、分发、更新状态 |
| `player._tick_loop` | 20Hz | 计算输入 → 物理 → 发包 → 自动进食检查 |
| `brain._loop` | 0.5s 轮询 | 选活动、跑活动（每个活动有 90s 超时） |
| `bridge._monitor_loop` | 1Hz | 断线检测、重连、死亡重生、看门狗 |
| `plugin._pump_events` | 事件驱动 | 把游戏聊天/死亡转发到 QQ |
| `device code` | 一次性 | 微软登录等待 |

互斥：

* `player._action_lock`：同一时刻只允许一个"物理动作"（挖矿/放置/攻击/进食），
  避免两个协程同时发 `block_dig`。
* `bridge._action_lock`：同一时刻只执行一个高层指令；执行前 `brain.pause()`，
  执行后 `brain.resume()`（`stop` 除外），保证"人指挥"优先于"自己玩"。
* 取消：`brain.cancel_activity()`、`player.stop_moving()` 用于 `/mc stop` 和工具超时。

## 数据流

```
TCP bytes
  → client._read_frame()      解密 → 解压 → 拆包
  → client._dispatch()        按包 ID 更新 client / world / entities / inventory
  → 事件（chat/death/join/position/...）
       ├→ bridge.events       → plugin 转 QQ
       ├→ brain.handle_chat   → 是否回复、LLM、打字延迟
       └→ player 订阅 position/respawn/join，校正物理状态
```

`client.world` 是唯一的方块真源；`player` 每个 tick 从 `world` 取碰撞，
`brain` 用 `world.find_blocks` 找目标，`plugin` 通过 `bridge.status()` 读快照。

## 世界模型

* `World.columns: dict[(cx, cz), ChunkColumn]`，未加载区域视为空气，`is_loaded()` 区分。
* `ChunkColumn` 每个 16×16×16 区段要么是单个整数（全空气/全石头），要么是
  `array('H')` / `list[int]` 的 4096 个全局状态 ID；解码时用"位累加器"跨 long 取索引。
* `BlockRegistry` 把状态 ID 映射到方块属性：硬度、工具类别/等级、透明、液体、可攀爬、
  碰撞箱（0–1 局部坐标，支持台阶/楼梯/栅栏），以及物品、食物、实体、掉落表。
  数据在 `mcbot/data/mc_1_20_1.json`（约 600KB），启动时一次性载入并缓存碰撞箱。
* `World.collision_boxes(aabb)` 按包围盒覆盖的方块逐个取碰撞箱；物理层用"扫掠 AABB"
  （起点 ∪ 终点）查询，避免高速移动穿模。

## 物理

`PlayerPhysics` 近似原版但更稳：

* 水平：目标速度（走 4.317 / 跑 5.612 / 潜行 1.295 m/s），按地面/空中/水中加速度逼近；
  无输入时按摩擦衰减。
* 垂直：重力 0.08/tick，空气阻力 0.98，跳跃初速 0.42（冲刺额外 0.2 前向冲量），
  水中/岩浆中减速并可上浮。
* 碰撞：Y → X → Z 分轴解算，撞墙只丢该轴速度，所以能贴墙滑行；
  水平被挡时尝试抬高 0.6（台阶/半砖）再走。
* 玩家 AABB：宽 0.6，高 1.8（潜行 1.5），眼高 1.62。

## 寻路

`Pathfinder` 在"脚所在方块"上做 A\*：

* 节点移动：8 方向走/斜走（斜走要求两个正交邻格都可通行，防止切角）、上跳 1 格、
  下落 1–3 格；门/栅栏门视为阻挡（交给上层开）。
* 代价：直走 1.0、斜走 1.414、上跳 1.6、下落 1.2+0.3/格、水中 ×2.5、
  危险方块相邻 +4、粗糙地面 +2。
* 启发式：octile 距离 + 1.6×垂直差，不放大，保证可采纳。
* 安全：未加载区块直接当墙，避免走进虚空；岩浆/火/仙人掌不进节点。
* 预算：默认 8000 节点 / 1.5 秒，超时返回 `None`，由 `player.goto` 重试或报"到不了"。

## 动作层

| 动作 | 关键实现 |
| --- | --- |
| `goto` | 寻路 → 逐节点 `walk_to`（朝向目标限速转身、卡住起跳、超时重算，最多 3 次） |
| `mine` | 距离检查（≤4.8）→ 选最佳工具并切快捷栏 → 瞄准 → `block_dig` start → 等待破坏时间 → finish → 轮询方块变化（最多 3 次） |
| `place` | 检查目标可替换 → 找相邻实心面 → 瞄准命中点 → `use_item_on` → 轮询方块变化 |
| `attack` | 追到 3.2 格内 → 瞄准实体胸口 → 攻击 + 挥臂，间隔 0.55–0.75s |
| `eat` | 找快捷栏食物 → 选中 → 连续 `use_item` 直到饥饿值上升 |
| `collect` | 反复找最近的 `item` 实体并走近 |

破坏时间用原版公式近似：

```
damage_per_tick = speed / hardness / (30 if 能收获 else 100)
ticks = ceil(1 / damage_per_tick)
```

`speed`：空手 1；木/金 2/12；石 4；铁 6；钻石 8；下界合金 9（只有工具类别匹配才生效）。

## 人类化

`humanize.py` 不碰网络，只产出"下一个视角/下一段文字/下一个摸鱼动作"：

* `AimPlan`：缓出三次曲线 + 衰减高斯手抖 + 过冲修正，末步精确命中。
* `TypingPlan`：逐字延迟、词间停顿、QWERTY 邻近键打错后回退重打。
* `Humanizer.reaction_delay()`：对数正态反应时间。
* `idle_action()` / `scan_pattern()` / `should_take_break()`：摸鱼、张望、休息。

`player.tick` 每帧从 `AimPlan` 取一个视角；`brain` 用 `TypingPlan.duration` 决定
发聊天前的"打字时间"。

## 决策层

`Brain` 是一个带优先级的活动选择器（见 README 表格）。每个活动是一个协程，
90 秒超时；失败只记日志，不影响连接。活动之间通过 `player` 的方法串行执行。

LLM 只用于两件事：

1. 每 5 分钟从固定选项里挑下一个目标（`explore/gather_wood/gather_stone/fight/idle`）；
2. 回复游戏内聊天（失败回退到规则回复）。

这样即使模型胡言乱语，也不会让角色做出协议层无法执行的动作。

## 桥接与插件

* `MinecraftBridge.execute(action, **kwargs)` 是唯一入口，返回
  `{"success", "content", "message"/data}`。动作表见 `bridge._dispatch`。
* `execute` 负责暂停/恢复自治、串行化、异常兜底，插件层不需要关心 asyncio 细节。
* `plugin.py` 把 `ctx` 的能力包成两个回调：`send.text`（上报）与 `llm.generate`（生成），
  其余全部交给 bridge。
* 配置热更新：比较连接相关字段，变化才重启连接；只改人设/概率则原地生效。
* 版本兼容：manifest 声明 Host `1.3.15 – 1.4.99`、SDK `2.16.0 – 2.23.99`。1.3.x
  没有命令级权限，`/mc` 由 `[security].admin_users` 在插件内校验；1.4.x 另有
  `permission="operator"` 由 Host 鉴权。`llm.generate` 的 `task_name` 参数做了
  `TypeError` 降级，老 SDK 只传 `model`。

## 错误处理

* 单个包解析失败只记日志，不断连接（`client._read_loop` 内层 try）。
* 动作异常 → `ActionResult(False, ...)`，工具层返回给 LLM 可读原因。
* 连接断开 → `bridge` 看门狗：35 秒无包强制重连；`auto_connect` 时按次数/间隔重试。
* 死亡 → 监控循环按反应时间发 `client_command(perform_respawn)`。
* 所有循环任务在 `stop()`/`on_unload()` 里 cancel 并 await，避免 Runner 卸载插件后
  还残留协程。

## 扩展点

| 想加什么 | 改哪里 |
| --- | --- |
| 新动作（如"放火把"） | `player.py` 加方法，`bridge._dispatch` 加分支，`plugin.py` 加 Tool |
| 新自治活动 | `brain.py` 加 `_activity` 方法并在 `_choose_activity` 排优先级 |
| 新协议版本 | 新增 `packets_xxx.py` + 数据表 + `client` 版本分支，见 `docs/PROTOCOL.md` |
| 新登录方式 | `auth.py` 加 `AuthSession` 来源，`client._handle_encryption_request` 复用 |
| WebUI 状态卡片 | `plugin.py` 声明 `home_cards`，读 `bridge.status()` |

## 测试策略

1. **纯函数单测**：buffer / nbt / chunk / registry / physics / pathfinding / humanize。
2. **组件测试**：用 `tests/stubs/maibot_sdk` 替身验证工具注册、参数、命令路由、生命周期。
3. **端到端**：`tests/fake_server.py` 真正握手/登录/发区块/收包；验证行走、挖矿、
   聊天、KeepAlive、压缩通道。

端到端测试是这一层最值钱的东西：它同时验证了包 ID、字段顺序、压缩、区块格式、
物理与寻路的集成，而且不依赖任何外部服务。

# Minecraft 陪玩 · MaiBot 插件

让麦麦真的走进《我的世界》——不是查 Wiki、不是发截图，而是作为一个玩家上线：
走路、跳台阶、砍树、挖矿、放方块、打怪、吃饭、捡掉落物，还会在公屏上跟人聊天。

* 纯 Python 实现，**零运行时依赖**（标准库即可；正版服建议加装 `cryptography`）
* 新版目标：**Minecraft Java 26.2（原生协议 776，无需 ViaProxy）**；保留 1.20.1 旧后端
* 在插件服务器配置中设置 `minecraft_version = "26.2"`，填写真实服务器地址和端口；安装与验证范围见 [26.2 说明](docs/NATIVE_26_2.md)
* 插件形态：MaiBot 新插件系统（`maibot-plugin-sdk`），14 个 LLM 工具 + 1 个管理员命令
* **MaiBot 兼容：1.3.15 及以上**（1.4.x 同样可加载），要求插件 SDK `>= 2.16.0`
* 自带 242 个测试用例，包括官方 26.2 编码器样本和本地 TCP 集成测试；不代表已在你的服务器实测

---

## 目录

- [它到底能做什么](#它到底能做什么)
- [安装](#安装)
- [配置](#配置)
- [使用](#使用)
- [她是怎么"像人"的](#她是怎么像人的)
- [自主行为](#自主行为)
- [架构](#架构)
- [协议支持与已知限制](#协议支持与已知限制)
- [测试](#测试)
- [目录结构](#目录结构)
- [数据来源与许可](#数据来源与许可)
- [免责声明](#免责声明)
- [Roadmap](#roadmap)

---

## 它到底能做什么

| 能力 | 说明 |
| --- | --- |
| 进服 / 重连 | 离线模式直接进；正版服走微软设备码登录；掉线自动重连 |
| 移动 | 20Hz 物理、重力/跳跃/冲刺/潜行/游泳/自动上台阶，A\* 寻路绕障碍、避岩浆；掉水里会持续上浮并游向最近干岸；**悬崖边会停住（摔落 >3 格掉血）** |
| 采集 | 挖掘时间按 Wiki 公式（硬度、工具等级、水下 ×5、悬空 ×5）、自动换镐子、捡掉落物；砍树从树干底部往上砍，被树叶包住时先扒开树叶开通道；徒手挖不到掉落的方块会跳过 |
| 合成 | 26.2 原生后端会像真人一样点合成格：木板→木棍→合成台→木/石工具、火把；结果自动放进快捷栏，失败会把材料收回 |
| 建造 | 在指定位置放方块（需要快捷栏里有对应方块），自动找放置面；天黑回家有火把会点上 |
| 战斗 | 按武器真实攻击冷却出手（剑 0.625s / 斧约 1.1s）、暴击在下落中命中；爬行者贴脸就撤，末影人不惹，蜘蛛白天/亮处不打 |
| 生存 | 饿了自动吃、受伤吃到饥饿 ≥18 以触发回血、死亡自动重生、落水自救（溺水 15 秒计时）；能读到方块光/天空光判断周围亮不亮 |
| 社交 | 读游戏内聊天，被点名或用 LLM 判断时回复；会偶尔自言自语 |
| 进服验证 | 自动读取加载页面（标题/动作栏/聊天）的验证码并发到 QQ 群，支持自定义模板与 `/mc verify` 重发 |
| 自治 | 按需求排队：凑木头 → 凑石头 → 有方向地探索；天黑回家，不再无目标乱晃 |
| 远程指挥 | QQ 里 `/mc ...`，或让 MaiBot 的 LLM 直接调用工具 |

## 安装

> 适用 MaiBot **1.3.15 及以上**（1.4.x 也可以直接加载）。MaiBot 1.3.15 自带
> `maibot-plugin-sdk>=2.16.0` 依赖；如果你的环境里没有，按下面第 2 步安装。

1. 把整个 `maibot-minecraft` 目录放进 MaiBot 的 `plugins/` 下（目录名随意）。
2. 确认插件 SDK 已安装（MaiBot 新插件系统的唯一硬依赖）：

   ```bash
   pip install "maibot-plugin-sdk>=2.16.0"
   ```

3. （可选，但**正版服务器强烈建议**）安装加密加速库，否则登录阶段的 AES 会用纯 Python 跑，速度较慢：

   ```bash
   pip install cryptography
   ```

4. 重启 MaiBot，进 WebUI 的插件页确认「Minecraft 陪玩」已被加载，然后到插件配置里填服务器地址。
   加载日志里会打印识别到的 MaiBot 版本，方便确认兼容性。

> 发布到插件市场前，请把 `_manifest.json` 里的 `urls.repository` / `urls.documentation`
> 换成你自己的仓库地址（当前是占位链接）。

## 配置

配置由 `plugin.py` 里的 `config_model` 声明，WebUI 会自动生成表单。主要字段：

### `[plugin]` 插件（不要删除）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 宿主启停开关；由 Runner 维护，插件配置模型不声明它（避免 1.3.x 开关/重载循环） |
| `config_version` | `1.0.0` | 配置结构版本，MaiBot Runner 强制要求 |

> MaiBot 1.3.x 的 Runner 会读取 `config_data["plugin"]["config_version"]`，
> 缺少这个节会报「配置校验失败，工具和调度已暂停」。v1.0.2 已包含，升级时不要手工删掉。

### `[server]` 服务器

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 服务器域名或 IP |
| `port` | `25565` | 端口 |
| `username` | `MaiBot` | 游戏角色名（离线模式下即昵称） |
| `auth` | `offline` | `offline` 或 `microsoft` |
| `microsoft_client_id` | `00000000402b5328` | 微软设备码登录的 Azure 应用 ID |
| `auth_cache` | 空 | 正版登录令牌缓存，留空用 `~/.maibot-mc/auth.json` |
| `view_distance` | `8` | 视距（2–32） |
| `auto_connect` | `false` | 插件加载后自动进服 |
| `reconnect_delay` | `8.0` | 断线重连间隔（秒） |
| `max_reconnect` | `5` | 连续重连上限 |

### `[behavior]` 行为

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `autonomous` | `true` | 没人指挥时自己找事做 |
| `auto_eat` | `true` | 饥饿自动进食 |
| `humanize` | `true` | 反应延迟 / 鼠标平滑 / 打字节奏 |
| `chat_reply` | `true` | 回应游戏内聊天 |
| `chat_reply_cooldown` | `8.0` | 两次回复的最短间隔 |
| `chat_reply_probability` | `0.75` | 没被点名时的回复概率 |
| `reaction_base` | `0.2` | 平均反应时间（秒） |
| `typing_cps` | `5.5` | 打字速度（字符/秒） |

### `[llm]` 模型

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `llm_enabled` | `true` | 用 MaiBot 的模型生成游戏内回复和自主目标 |
| `llm_model` / `llm_task` | 空 | 指定模型 / 任务名，留空用默认 |
| `persona` | 内置 | 游戏内人设提示词 |

### `[report]` 消息上报

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `report_stream_id` | 空 | 上报到哪个聊天流；留空用最近一次对话的流 |
| `report_chat` | `true` | 把游戏内聊天转发到 QQ |
| `report_death` | `true` | 死亡提醒 |
| `report_prefix` | `[MC]` | 消息前缀 |

### `[verify]` 进服验证

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `verify_enabled` | `true` | 自动识别加载界面的验证码并转发 |
| `verify_stream_id` | 空 | 验证码发到哪个聊天流（通常是服务器验证群）；留空复用上报聊天流 |
| `verify_pattern` | 空 | 自定义提取正则（第一个捕获组为验证码）；留空自动识别 |
| `verify_template` | `[MC] 进服验证码：{code}` | 发送模板，可用 `{code}` `{username}` `{source}` |
| `verify_window` | `180.0` | 进服后监听多少秒，`0` 表示一直监听 |
| `verify_forward_source` | `false` | 是否把原始提示文本也一起发出 |

### `[security]` 权限

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `admin_users` | 空 | 允许使用 `/mc` 的用户，逗号分隔，如 `qq:123456789,telegram:987654321`；也可以只写用户 ID |

MaiBot **1.4+** 会按命令的 `permission="operator"` 走内置操作员鉴权；**1.3.15**
还没有插件命令级权限，所以插件自己读 `admin_users` 做校验。留空表示不额外限制
（此时 1.3.x 上任何能触发命令的人都能用 `/mc`，建议填上）。

### 配置报错排查

**「配置校验失败，工具和调度已暂停 / 缺少 `[plugin]` 配置节」**

* v1.0.2 之前没有声明 `[plugin]` 节，MaiBot 1.3.x 的 Runner 会直接判定配置不合法；
  升级到 **v1.0.2** 即可。
* 如果升级后 TOML 编辑区仍是空的：删掉 `plugins/<插件目录>/config.toml`，重新加载插件
  （或重启 MaiBot），Runner 会用默认值生成；也可以把 `config.example.toml` 的内容
  粘进去保存。
* 保存后页面右上角状态应从「配置错误，业务暂停」恢复到正常，工具与自主行为随之启动。

### `/mc connect` 没反应 / 进不去

1. 先发 `/mc diag`，看 `client_state`、`joined` 和「最近错误」。
2. `bridge 未初始化`：WebUI 插件页确认插件处于启用状态，改完配置点保存。
3. `last_error` 含 `连接失败`：地址只要域名就行，端口填 `0`（自动查 SRV，再退回 25565）；
   服务器不在默认端口时写成 `域名:端口`。原生 26.2 不再连接旧 ViaProxy 端口。
   详细规则见 [只有地址没有端口](docs/NATIVE_26_2.md#只有地址没有端口)。
4. `last_error` 含 `Incompatible client` 或版本号：设置 `minecraft_version = "26.2"` 并确认是 Java 正式版 26.2。
   见 [原生 26.2 说明](docs/NATIVE_26_2.md)。
5. `last_error` 含 `正版验证`：服务器是 online-mode，需要配置微软登录；不要为了插件关闭公共服务器认证。
6. 仍无头绪：把 `/mc diag` 的输出和插件日志里带 `[mc]` 的行发出来。

### 掉水里不出来 / 砍不到树 / 到处乱晃（2.0.2 已修）

- **掉水里**：现在会在水里每 tick 都按跳跃（等于一直按住空格上浮），并自动朝最近干岸游。
  日志/活动里会显示「往岸上游」。如果四周全是深水没有岸边，会先浮在水面保证不淹死。
- **砍不到树**：现在先找能勾到原木的落脚点；树干被树叶包住时，会朝树干方向逐片扒开树叶
  挖出通道，再从最低的原木往上砍，砍完自动捡掉落物。特别高的树只会砍够得着的那几截。
- **乱晃**：自主行为改成按需求排队（木头 → 石头 → 有方向探索 → 天黑回家），失败目标会拉黑
  10 分钟不再重复。想让它专注干活就先发 `/mc stop`，手动下指令。

### WebUI 报 `Minified React error #185`

这是 **MaiBot WebUI（React 前端）的已知问题**（MaiBot issue #2292 / #1478），不是插件
Python 侧崩溃。典型触发：聊天/「麦麦观察」页历史里出现重复 `message_id`，或旧版配置
编辑器输入时；控制台会写 `Maximum update depth exceeded`。

* 先 `Ctrl + F5` 强制刷新；还报错就重启 MaiBot；
* 在 WebUI 里删除当前会话 / 清空出问题的聊天历史（新会话通常正常）；
* 或者用自带脚本清理重复 `message_id`（**先停 MaiBot**，脚本默认只检查、会先备份）：

  ```powershell
  python tools/fix_maibot_chat_db.py --db "C:\MaiBot\data\MaiBot.db"
  python tools/fix_maibot_chat_db.py --db "C:\MaiBot\data\MaiBot.db" --apply
  ```

  只想清掉某个会话：加 `--session <session_id>`（配合 `--apply`）。
* 升级到 MaiBot 1.4.x（新版已修复）；
* 插件本身仍在运行：终端日志里能看到 `[mc]` 输出。页面卡死时可以直接改
  `config.toml`（文件模式），不必依赖可视化配置页。

## 使用

### QQ 命令（管理员）

> 1.4+ 由 MaiBot 操作员权限控制；1.3.15 请先在 `[security].admin_users` 里填上你的
> `平台:ID`，否则任何能触发命令的人都能使用 `/mc`。

| 命令 | 作用 |
| --- | --- |
| `/mc` / `/mc status` | 在线状态：坐标、血量、饥饿、手持、附近实体、区块数 |
| `/mc help [分类]` | 列出所有动作（按移动/采集/合成/背包/建造/战斗/信息分类），支持中文 |
| `/mc connect [地址:端口]` | 进服；不写地址就用配置里的服务器 |
| `/mc disconnect` | 让麦麦退出 Minecraft 服务器（MaiBot 本身仍在线） |
| `/mc stop` / `/mc resume` | 停下并暂停自主行为 / 恢复 |
| `/mc say 我回来了` | 在游戏里说一句话 |
| `/mc come` / `/mc goto 100 64 -20` / `/mc goto 小明` | 走到说话的人 / 坐标 / 玩家 |
| `/mc follow 小明` | 跟着某个玩家 |
| `/mc explore 24` | 朝一个方向探索（会顺手砍树挖矿） |
| `/mc home` / `/mc sethome` | 回家 / 把当前位置设为家 |
| `/mc mine 木头 5` | 挖 5 个木头（支持中文别名） |
| `/mc chop 6` | 砍 6 个原木（整棵树、会清挡路树叶） |
| `/mc dig 4` | 朝前下方挖 4 层阶梯（下矿） |
| `/mc craft wooden_pickaxe` | 合成（自动做依赖：木板→木棍→合成台→工具） |
| `/mc recipes` / `/mc recipes torch` | 现在能做什么 / 火把要什么材料 |
| `/mc equip torch` / `/mc drop stone` | 把火把拿到手上 / 丢掉石头 |
| `/mc place oak_planks 10 64 -3` | 在指定坐标放方块 |
| `/mc pillar 3` / `/mc bridge 6` / `/mc shelter` | 垫高 / 铺路 / 挖避难所 |
| `/mc torch` | 在脚下点火把（背包里要有） |
| `/mc swim` / `/mc flee` / `/mc eat` | 游上岸 / 逃跑 / 吃东西 |
| `/mc nearby 12` | 看看周围有什么（生物、玩家、树/水/矿） |
| `/mc where` / `/mc time` / `/mc light` / `/mc players` | 坐标 / 时间 / 光照 / 在线玩家 |
| `/mc verify` | 重新发送最近一次捕获的进服验证码 |
| `/mc diag` | 诊断：插件版本、目标地址、连接状态、最近错误、验证码配置 |

完整清单（含全部别名）见 [指令表](docs/COMMANDS.md)，由 `mcbot/actions.py` 自动生成。

### 首次进服验证码

有些服务器第一次进服时会把验证码显示在加载页面（标题、动作栏或聊天），要求玩家把
验证码发到 QQ 群完成绑定。插件默认开启自动转发：

* 进服后 `verify_window` 秒内监听 **标题、副标题、动作栏、聊天、Boss 栏、Tab 列表头**；
* 用关键词（验证码 / verification / code…）+ 4–10 位字母数字识别验证码，并做去重；
* 发送到 `verify_stream_id`（留空则用消息上报的聊天流），模板默认
  `[MC] 进服验证码：{code}`，可加 `{username}`、`{source}`；
* 手动重发最近一次验证码：`/mc verify`；
* 自定义格式：`verify_pattern = "(?i)code[:：]\\s*(\\w{6})"`。

> 如果服务器的验证机器人要求"必须由玩家本人的 QQ 发送"，机器人账号代发可能不通过。
> 这时把 `verify_stream_id` 设成你的私聊，收到验证码后自己发到验证群即可。

### LLM 工具

麦麦的模型可以自己决定调用这些工具（工具描述已按 LLM 可读性写好）：

`mc_status`、`mc_connect`、`mc_disconnect`、`mc_goto`、`mc_mine`、`mc_place`、
`mc_attack`、`mc_follow`、`mc_say`、`mc_eat`、`mc_collect`、`mc_inventory`、
`mc_stop`、`mc_task`（自然语言任务），以及 2.0.5 新增的：

`mc_command`（任意动作的通用入口，例如 `subcommand=craft args=wooden_pickaxe`）、
`mc_chop`、`mc_craft`、`mc_recipes`、`mc_nearby`、`mc_home`、`mc_equip`、
`mc_build`（pillar/bridge/shelter）、`mc_survive`（swim/flee/eat/torch）。

其中 `mc_status` / `mc_stop` 是核心工具（每轮可见），其余在 deferred 池里按需检索。
`mc_command` 能覆盖所有 40 个动作，所以以后加动作不必再加工具。

### 自然语言示例

对麦麦说：

* “去游戏里挖五个木头” → `mc_task` → `mine oak_log count=5`
* “跟着小明” → `mc_follow`
* “在游戏里喊一句我回来了” → `mc_say`
* “去 100 64 100 看看” → `mc_goto`

没配 LLM 时也有规则解析（中文方块别名、坐标、玩家名）。

## 她是怎么"像人"的

`mcbot/humanize.py` 专门负责"不像机器人"：

* **反应时间**：对数正态分布（默认均值 0.2s），紧急情况缩短，闲时更慢。
* **鼠标移动**：不是瞬间转头，而是缓出曲线 + 衰减手抖 + 约 70% 处过冲再修正，
  每 tick 有最大转速；最后一定精确对准目标，避免"永远差一点"。
* **打字**：逐字随机延迟、词间停顿、按错键后回退重打（QWERTY 邻近键），
  所以公屏消息的节奏和真人接近。
* **摸鱼**：没人管时会左右看、走两步、跳一下、潜行、空挥手臂、发呆。
* **作息**：连续玩 8–25 分钟后会"休息" 15–120 秒（不操作），再继续。
* **失误**：寻路会绕路，挖矿偶尔需要多敲一次，打怪有 0.55–0.75s 的点击间隔。

## 自主行为

`mcbot/brain.py` 的优先级（2.0.2 起改为"按需求排队"，不再随机滚点）：

```
浸水快 15 秒 / 掉进水里   → 上浮 + 游向岸边
爬行者 3.5 格内           → 立刻撤（它 1.5 秒就会爆）
血量 ≤ 6                  → 逃跑 / 吃东西
受伤且饥饿 < 18 且有食物   → 先吃（Wiki：≥18 才会自然回血）
敌对环境生物                → 有武器才打；蜘蛛白天中立不打，末影人不惹
天黑且没光源               → 回家附近待着
木头不足 16 / 石头不足 24  → 有目标地砍树、挖石头
其余                      → 朝一个方向探索 18–30 格，路上顺手干活
```

这些阈值和判断依据来自 Minecraft Wiki（Java 版），集中写在
[`mcbot/knowledge.py`](mcbot/knowledge.py)，采用的规则清单见
[`docs/WIKI_KNOWLEDGE.md`](docs/WIKI_KNOWLEDGE.md)。

每 5 分钟（配置了 LLM 时）可以让模型从固定选项里挑一个目标，避免行为过于机械。
游戏内聊天会走 LLM 生成回复（失败则规则回复），并做冷却与频率限制。

## 架构

```
MaiBot Runner
   └── plugin.py            MaiBotPlugin：配置模型、14 个 Tool、/mc 命令、事件上报
         └── mcbot/bridge.py    连接/重连/死亡重生/事件队列/动作分发（execute）
               ├── mcbot/client.py    协议客户端：登录、压缩、加密、收包分发、世界与实体状态
               │     ├── mcbot/packets.py  1.20.1 包 ID 与上行包构造
               │     ├── mcbot/buffer.py   VarInt / 字符串 / UUID / 坐标读写
               │     ├── mcbot/nbt.py      网络 NBT 读写
               │     ├── mcbot/chunk.py    区块调色板解码（方块状态 + 生物群系）
               │     ├── mcbot/world.py    体素世界、碰撞查询、方块搜索
               │     ├── mcbot/registry.py 方块/物品/实体/掉落表（内置 1.20.1 数据）
               │     ├── mcbot/crypto.py   AES-CFB8 / RSA / 会话哈希
               │     └── mcbot/auth.py     离线 UUID、微软设备码、session join
               ├── mcbot/player.py    20Hz 物理循环 + 行走/挖掘/放置/攻击/进食/拾取
               │     ├── mcbot/physics.py      AABB 碰撞、重力、跳跃、游泳、上台阶
               │     ├── mcbot/pathfinding.py  A* 寻路（跳跃/下落/斜走/避危险）
               │     └── mcbot/humanize.py     反应、瞄准、打字、摸鱼、休息
               └── mcbot/brain.py     自治决策 + 游戏内聊天回复
```

详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 与 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

## 协议支持与已知限制

**支持**

* Minecraft Java **1.20.1（协议 763）**，离线模式与正版（微软账号）模式
* TCP 压缩（`Set Compression`）、AES/CFB8 加密、KeepAlive、传送确认
* 区块（1.18+ 调色板格式）、方块更新、实体生成/移动/移除、背包、血量/饥饿、聊天、时间、死亡/重生

**MaiBot 版本兼容**

* `_manifest.json` 声明 `host_application` 为 **1.3.15 – 1.4.99**，`sdk` 为 **2.16.0 – 2.23.99**
  （MaiBot 1.3.15 的 `pyproject.toml` 要求 `maibot-plugin-sdk>=2.16.0`）
* 插件只使用各版本都提供的 `Tool` / `Command` / `PluginConfigBase` / `ctx.send.text` /
  `ctx.llm.generate` 接口；`llm.generate` 的 `task_name` 参数做了降级处理，
  老 SDK 不接受时会自动退回只传 `model`
* 1.3.x 没有命令级权限：请在 `[security].admin_users` 中授权，否则 `/mc` 对所有人开放
* 1.4.x 起由 MaiBot 的 `permission="operator"` 接管，`admin_users` 仍可叠加使用

**26.2（协议 776）原生后端**

直接连接服务器，不再把 26.2 转成 1.20.1。`minecraft_version = "auto"` 根据状态查询选择
763 或 776；查询不可用时默认 776。可设置 `"26.2"` 强制使用新版。
26.1、26.3、基岩版及模组协议不属于本包支持范围。见 [26.2 说明](docs/NATIVE_26_2.md)。
旧 ViaProxy 启动工具仅为历史保留，不是新版的安装步骤。

**限制（重要）**

* **版本绑定**：新版仅针对 Java 26.2 / 776；不将协议号相近当作兼容。旧 763 后端保留用于回归，不代表新版功能已在旧服实测。
* **在线模式的纯 Python 加密很慢**：CFB8 每字节一次 AES 分组。装 `cryptography`
  后走 C 实现；不装也能连，但登录时加载区块会明显变慢。离线服完全不受影响。
* **不做容器/合成/交易/红石**：没有开箱子、工作台合成、村民交易、钓鱼、床睡觉。
  快捷栏里没有的工具不会自动从背包倒腾（拾取物会优先进快捷栏）。
* **不做复杂建造**：能放单个方块，但不会自动搭桥、盖房子、搭楼梯。
* **反作弊**：移动是"接近原版"而不是逐字节复刻，高频发包/瞬移类检查严格的服务器
  可能回弹。请只在允许机器人的服务器使用。
* **不处理资源包/聊天签名**：正版服 `enforce-secure-profile` 下消息可能被标记为未签名。
* 服务器若使用自定义方块/协议（模组服、代理转发特殊配置）不保证可用。

## 测试

```bash
# 全部 214 个用例（含端到端，约 35 秒）
python tests/run_all.py

# 单独跑
python tests/test_core.py        # 协议基础、注册表、世界、AES
python tests/test_nbt.py         # NBT 读写
python tests/test_chunk.py       # 区块调色板解码
python tests/test_physics.py     # 物理与碰撞
python tests/test_pathfinding.py # A* 寻路
python tests/test_humanize.py    # 人类化
python tests/test_plugin.py      # 插件组件/命令/工具（用 SDK 替身）
python tests/test_e2e.py         # 假服务器上的完整登录/行走/挖矿/聊天
```

端到端测试里的 `tests/fake_server.py` 是一个能真正握手、登录、发区块、处理挖掘与
KeepAlive 的最小服务器；不装 Minecraft 也能跑。

附带的 `examples/offline_demo.py` 会用假服务器演示一遍「进服 → 看状态 → 走过去 →
挖方块 → 说话」，适合快速确认环境。

## 目录结构

```
maibot-minecraft/
├── _manifest.json          # MaiBot 插件清单
├── plugin.py               # 插件入口（配置模型 / Tool / Command）
├── config.example.toml     # 配置示例（Runner 会自动生成 config.toml）
├── requirements.txt
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── LICENSE
├── mcbot/                  # 纯 Python 客户端与行为层
│   ├── data/mc_1_20_1.json # 内置方块/物品/实体/掉落数据（约 600KB）
│   └── ...
├── tools/                  # 数据生成脚本（开发用）
├── tests/                  # 214 个 unittest 用例 + 假服务器
├── examples/offline_demo.py
└── docs/
    ├── ARCHITECTURE.md
    ├── MODERN_VERSIONS.md
    └── PROTOCOL.md
```

## 数据来源与许可

* 方块 / 物品 / 实体 / 掉落 / 协议 ID 数据来自
  [PrismarineJS/minecraft-data](https://github.com/PrismarineJS/minecraft-data)（MIT），
  由 `tools/fetch_minecraft_data.py` + `tools/gen_data.py` 压缩成 `mcbot/data/mc_1_20_1.json`。
* 本项目代码以 MIT 许可证发布，见 [LICENSE](LICENSE)。

## 免责声明

* 请只在**你有权限**的服务器上使用，并遵守服务器规则；很多服务器禁止自动化客户端。
* 使用正版账号登录时，令牌保存在本机缓存文件里，请勿分享；因使用本插件导致的封号、
  数据丢失等风险由使用者自行承担。
* 本项目与 Mojang / Microsoft / MaiBot 官方无隶属关系。

## Roadmap

- [ ] 1.20.4 / 1.21.x 协议表与注册表（配置切换版本）
- [ ] 容器交互：开箱子、工作台合成、熔炉
- [ ] 简易建造：搭桥、楼梯、火柴盒避难所
- [ ] 睡觉跳过夜晚、床与重生点
- [ ] 更聪明的战斗：走位、盾牌、弓箭
- [ ] WebUI 实时状态卡片（在线人数、背包、截图）

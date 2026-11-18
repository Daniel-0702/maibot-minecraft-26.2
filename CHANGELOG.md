# 更新日志

## 2.0.5 (2026-11-16)

重写**底层指令层**：动作从硬编码 if/elif 变成一张可扩展的注册表，`/mc` 子命令和
LLM 工具共用同一份清单；同时把"像真人玩 MC"常用的动作补到 40 个。

### 新增

- `mcbot/actions.py`：动作注册表（40 个动作、中英别名、分类、用法、帮助生成）+ 参数解析
  （坐标/数字/中文数字/地址）。`resolve()` 支持大小写、`/mc` 前缀、中文别名。
- `docs/COMMANDS.md`：由注册表自动生成的完整指令表（改表就能改文档）。
- 新动作：
  - 移动：`explore` 探索、`swim` 游上岸、`home`/`sethome` 回家与设家、`jump`
  - 采集：`chop` 砍树、`dig` 阶梯下挖
  - 建造：`pillar` 垫高、`bridge` 铺路、`shelter` 挖避难所
  - 背包：`equip` 拿到手上、`drop` 丢弃
  - 合成：`craft`、`recipes`（查现在能做什么/某物品配方）、`torch`
  - 信息：`nearby`（附近生物/玩家/方块）、`where`、`time`、`light`、`players`
  - 战斗：`attack`、`flee`
- `/mc help [分类]`：按分类列出所有动作与别名；未知动作会给出帮助而不是沉默。
- 新 LLM 工具：`mc_command`（任意动作的通用入口）、`mc_chop`、`mc_craft`、
  `mc_recipes`、`mc_nearby`、`mc_home`、`mc_equip`、`mc_build`、`mc_survive`。

### 修复

- `/mc eat` 之前解析会失败（要求必填参数），现在可以不带参数吃背包里第一份食物。
- 火把放置位置改为脚所在格（上一版放在头顶格，找不到附着面会失败）。

### 测试

- 新增 87 个用例（动作注册表 57、指令路由 18、玩家动作 12），总计 **499 个**。

## 2.0.4 (2026-11-16)

让麦麦真正会合成，并能读到光照（26.2 原生后端）。详见
[合成与光照](docs/CRAFTING_AND_LIGHT.md)。

### 新增

- `mcbot/crafting.py`：通过 26.2 的容器点击协议（`ServerboundContainerClickPacket` +
  HashedStack + stateId）在玩家 2x2 合成格与合成台 3x3 上真的合成；出错会把材料放回背包。
- `mcbot/recipes.py`：27 条配方（木板/木棍/合成台/火把、木与石的工具、熔炉、箱子）
  + 依赖展开规划 `plan()`。
- `mcbot/modern_client.py`：跟踪窗口 stateId/槽位内容、`click_slot()`、`open_screen`
  与 `container_close`，以及区块包光照解析和 `light_at()`。
- `mcbot/light.py`：逐层解析方块光/天空光（掩码 + nibble），`is_dark_enough_for_mobs()`。
- 大脑：没有工具时自动合成木镐/木剑/木斧并换到快捷栏；有煤时做火把，天黑回家点燃；
  合成失败冷静 3 分钟。
- `/mc craft <物品>` 手动合成；`/mc diag` 增加光照错误、已打开容器与是否偏暗。
- 配置 `[behavior].crafting`（默认开）可关闭自动合成。

### 修复

- `_has_tool()` 之前只查槽位 0–8（26.2 里那是合成格和盔甲位），导致 26.2 后端永远
  判断"没有镐子"；改为全背包查找。
- 区块包解析补上方块实体列表与光照数据的读取（之前靠"后面字节不管"歪打正着）。

### 测试

- 新增 90 个用例（配方 41、光照 30、光照接入 3、点击编码 10、合成流程 6），总计 **412 个**。

## 2.0.3 (2026-11-16)

按 Minecraft Wiki（Java 版）核对机制，让行为有依据：

### 新增 `mcbot/knowledge.py`

把 Wiki 上的规则整理成可测试的常量与函数，注释里标注出处：

- **挖掘**（[Breaking](https://minecraft.wiki/w/Breaking)）：硬度 ×1.5/×5 的基础时间、
  工具速度表（补上铜 5）、效率附魔、水下 ×5、悬空 ×5、瞬间破坏。
- **伤害**（[Damage](https://minecraft.wiki/w/Damage)）：摔落 = 格数 − 3、溺水 15 秒后每秒 2 HP、
  攻击冷却 T = 20/攻速 与伤害倍率公式、暴击 ×1.5。
- **饥饿**（[Hunger](https://minecraft.wiki/w/Hunger)/[Healing](https://minecraft.wiki/w/Healing)）：疾跑需 >6、
  自然回血需 ≥18。
- **生物**（[Creeper](https://minecraft.wiki/w/Creeper)/[Enderman](https://minecraft.wiki/w/Enderman)/
  [Spider](https://minecraft.wiki/w/Spider)）：爬行者 1.5 秒引信与 3–5 格爆炸、末影人盯眼激怒、
  蜘蛛白天中立、僵尸骷髅白天着火。

### 行为改动

- 挖掘计时算上"头在水里 ×5 / 脚不沾地 ×5"，不再出现"在水里一直挖不掉"。
- 新增悬崖保护：往前一步会掉超过 3 格就停住，`walk_to` 报"前方落差太大"。
- 战斗按手持武器使用真实攻击冷却（剑 0.625s / 斧约 1.1s / 空手 0.25s），暴击改为下落中命中。
- 不打末影人，瞄准时只瞄躯干/脚避免被激怒；白天不把蜘蛛当威胁；爬行者 3.5 格内直接撤。
- 受伤且饥饿 <18 时先吃到 18（Wiki：≥18 才自然回血）；溺水计时到 8 秒优先上浮。
- `mine_nearest` 只挖"挖了有掉落"的方块（徒手挖石头不再白费时间）。
- 新增 39 个知识/接入测试，总计 322 个。

### 文档

- 新增 [`docs/WIKI_KNOWLEDGE.md`](docs/WIKI_KNOWLEDGE.md)：列清采用了哪些 Wiki 规则、改了哪些行为、
  以及哪些没采用（合成、光照刷怪、Boss 等）及原因。

## 2.0.2 (2026-11-16)

修复三条实际游玩体验问题（用户在真实服务器观察到的行为）：

### 1. 掉进水里只会跳一下就沉下去

- 物理层新增 `head_in_water`；水里每 tick 都按跳跃（= 原版"一直按住空格上浮"），
  只按一次仍然只会浮一点点然后被水压回去。
- 站在浅水底、头还没进水时给普通起跳初速，能正常跳上岸。
- 新增 `PlayerController.swim_to_land()`：持续上浮 + 扫描最近干岸并游过去；
  空闲时也会自动寻岸，避免原地淹死。
- 大脑新增最高优先级的落水自救活动（在打怪、砍树之前）。

### 2. 砍树砍不到被树叶包住的树

- 新增 `mcbot/woodcutting.py`：识别/聚类原木成"一棵树"，找出树干底部、
  可站立的接近点、以及贴着树干的树叶。
- 新增 `PlayerController.chop_tree()`：先按距离找能勾到原木的落脚点；够不到时
  朝树干方向逐片扒开树叶挖出一条通道，再**从最低的原木往上砍**，砍完顺手捡掉落物。
- 大脑砍树改为"按需求砍够一批"（默认 16 个木头），而不是只砍一个方块就走。

### 3. 无目标乱晃

- 决策从"随机滚点"改成按需求排队：保命/吃饭/打怪 → 落水自救 → 天黑回家 →
  凑木头 → 凑石头 → 有方向地探索（朝一个大致方向走 18-30 格，到了顺手砍树/挖石）。
- 记住出生点当作"家"，夜里（且没有光源）回家附近待着，不再摸黑满山跑。
- 探索有连续朝向而不是每轮随机瞬移式换向；砍/挖失败的坐标拉黑 10 分钟，避免反复撞同一棵树。
- 服务器例行位置核对不再清零速度（小幅偏差只贴位置），减少"走一步顿一下"的抖动；
  寻路对水的代价从 2.5 提到 6，有干路时尽量不下水。
- 新增 25 个测试（水中物理、上岸、厚树叶里砍树、伐木几何、大脑落水优先级），总计 283 个。

## 2.0.1 (2026-11-16)

- 服务器只填地址也能连：端口填 0/留空时按原版规则自动处理。
- 新增 Java 版 SRV 探测（`_minecraft._tcp.<域名>`，纯标准库 DNS，Windows 注册表 /
  `/etc/resolv.conf` 取 DNS），查到就跟随目标与端口，查不到退回 25565。
- 地址支持 `域名`、`域名:端口`、`[IPv6]:端口`、裸 IPv6、`minecraft://` 前缀与尾随斜杠。
- 握手包始终发送用户填写的原始域名，SRV 只影响 TCP 目标，不影响虚拟主机路由。
- `port` 默认值改为 0（自动）；旧配置已有显式端口时行为不变。
- `/mc diag` 与状态新增实际连接目标、是否使用 SRV；`/mc connect 域名` 后即走自动端口。
- 新增 19 个测试（地址解析、SRV 报文、本地 DNS 服务器、握手主机名），总计 261 个。

## 2.0.0 (2026-11-15)

- 新增 Java 26.2 原生协议 776 后端，直接连接真实服务器，无需 ViaProxy。
- 新增登录确认、configuration、动态注册表、known-packs 协商、cookie 空响应、区块批次确认与 player-loaded。
- 新版区块解码独立实现：维度决定 section 数、block/fluid count、固定长度且不跨 long 的调色板；使用官方方块状态和碰撞数据。
- 适配新版传送、实体、动作、网络 NBT 文本和快捷栏索引；保留验证码文本转发链路。
- 新增 server.minecraft_version = auto / 26.2 / 1.20.1；诊断显示实际协议。
- 旧版区块代码存在自身格式问题，历史日志中将故障直接归因于 ViaProxy 的结论不充分，本版不依赖该归因。
- 验证范围与限制以 docs/NATIVE_26_2.md 为准，不将本地测试等同于用户服务器实测。

## 1.1.8 (2026-11-15)

区块解码失败时输出原始字节，便于判断 ViaProxy 发来的是 1.20.1 还是 26.x 区块布局。

### 新增

- 区块解析失败时记录 `/mc diag` 展示的 `前 48 字节` 十六进制
- 用于确认 ViaProxy/ViaBackwards 是否真的把 26.2 区块翻译成了 1.20.1 格式
  （1.20.1 每个 section 开头是 i16 非空气方块数；26.x 多一个 u16 fluidCount）

## 1.1.7 (2026-11-15)

新增 `/mc disconnect`：让麦麦退出 Minecraft 服务器。

### 新增

- `/mc disconnect`（别名 `/mc leave`、`/mc 退出`、`/mc 下线`）：
  断开游戏连接并停止自主行为，但 MaiBot 本身继续在线
- `/mc stop` 仍然只是暂停动作，不会断开连接
- 新增 1 个命令测试，总计 **214** 个用例

## 1.1.6 (2026-11-15)

增加区块诊断信息，用于定位「进服后区块数一直是 0」。

### 新增

- 客户端统计：`chunk_packets_received`、`chunk_decode_errors`、`last_chunk_error`
- `/mc status` 与 `/mc diag` 会显示：
  - 已加载区块数
  - 收到多少个区块包
  - 解析失败多少个、最后一次失败原因
  - 最近一次收到服务器数据距今多少秒
- 这样可以区分三种情况：服务器没发区块 / 区块包被丢弃 / 区块解码失败

## 1.1.5 (2026-11-15)

修复连接真实服务器 / ViaProxy 时登录包解析越界的问题。

### 修复

- `Login Start` 的 UUID 是 `option<UUID>`（前面必须有 1 字节「存在」bool）。
  之前直接写 16 字节 UUID，真实服务器/ViaProxy 会把 UUID 第一个字节当成 bool，
  然后读取 UUID 的 long 时越界：
  `readerIndex(22) + length(8) exceeds writerIndex(29)`，
  ViaProxy 随即以「An unhandled error occurred in your connection」断开。
- 假服务器之前不校验这个 bool，所以端到端测试没发现；现在假服务器按 1.20.1
  格式解析，并在单元测试里断言存在标记
- 版本号 1.1.5

## 1.1.4 (2026-11-15)

修复 `/mc` 在 MaiBot 1.4.x 上被插件自身权限误拒的问题。

### 修复

- `security.admin_users` 匹配更宽容：
  - 配置 `qq:123456789` 或只写 `123456789` 都能匹配；
  - 消息结构里没有 `platform` 字段、或平台名不是 `qq`（如 `snowluma`）时，
    按纯用户 ID 匹配，不再误判；
  - 同时支持 `message` 是 dict 或对象（pydantic 消息模型）
- 权限拒绝时会写日志：`命令权限拒绝：candidates=... allowed=...`，方便对照
- 新增 2 个权限匹配测试，总计 **213** 个用例

### 提示

- 如果不想折腾 ID，把 `[security].admin_users` 留空即可，命令权限交给
  MaiBot 1.4 的操作员列表
- `[verify].verify_stream_id` 要填 **MaiBot 聊天流 ID**（WebUI 聊天流里的 stream_id），
  不是 QQ 群号；留空则使用最近一次对话的聊天流

## 1.1.3 (2026-11-15)

给 `/chat` 页 React #185 增加一个可执行的修复工具。

### 新增

- `tools/fix_maibot_chat_db.py`：清理 MaiBot `mai_messages` 表中重复的
  `(platform, session_id, message_id)` 记录（WebUI `/chat` 页 React #185 的已知诱因，
  MaiBot issue #2292）：
  - 默认 dry-run，只列出重复组合与额外副本数；
  - `--apply` 前自动备份数据库（含 `-wal`/`-shm`）；
  - 每个组合只保留 id 最小的一条；`--session <id>` 可清空指定会话；
  - `--vacuum` 可选
- README 的 WebUI 排错章节补充脚本用法
- 3 个内存 SQLite 测试，总计 **211** 个用例

## 1.1.2 (2026-11-15)

针对 WebUI 报 `Minified React error #185` 的排查与插件侧加固。

### 修复

- `[plugin]` 配置节**只声明 `config_version`**，不再声明 `enabled`：
  MaiBot 1.3.x 的 SDK 归一化会丢掉模型未声明的键，插件声明 `enabled` 反而会让
  启停开关/重载出现状态循环（MaiBot issue #2080，修复见 #2103）。`enabled`
  仍保留在 `config.toml` 里，由 Runner 维护
- 回归测试改为断言模型 **不含** `plugin.enabled`、但包含 `plugin.config_version`

### 文档

- README 新增 `React error #185` 说明：这是 WebUI 前端已知问题（issue #2292 /
  #1478，重复 `message_id` 或旧配置编辑器触发），给出强刷、重启、清理会话历史、
  升级 1.4.x 的恢复步骤；插件 Python 侧仍在运行，可直接改 `config.toml`

## 1.1.1 (2026-11-15)

修复 `/mc connect` 看起来"没反应"的问题，并补上连接诊断。

### 修复

- **命令结果显式发回聊天流**：之前只返回 `(success, response, weight)`，在部分
  MaiBot 版本上不会自动把 response 发到群里，导致 `/mc connect` 失败时没有任何提示；
  现在 `handle_mc` 会主动 `ctx.send.text(response)`（成功/失败都会回一条）
- 连接加锁：自动重连和手动 `/mc connect` 不会同时建立两个客户端
- 半开连接（连着但没真正进服）再执行 `/mc connect` 会先清理旧连接再重连，
  不再只回一句「已经连接」
- 连接失败原因写入 `bridge.last_error`，并记录到日志

### 新增

- `/mc diag`（或 `/mc 诊断`）：显示插件版本、目标地址、client_state、是否进服、
  最近错误、验证码转发配置
- 2 个命令回复/诊断测试，总计 **208** 个用例

## 1.1.0 (2026-11-15)

新增「首次进服验证码自动转发」：服务器把验证码显示在加载页面、要求发到 QQ 群时，
麦麦可以自己读出来并发到群里。

### 新增

- 解析加载界面的多种文本来源：标题（`set_title_text`）、副标题、动作栏、系统聊天、
  Boss 栏、Tab 列表头
- `mcbot/verification.py`：
  - `CodeExtractor`：默认按「验证码 / verification / code…」关键词 + 4–10 位字母数字
    识别；支持 `verify_pattern` 自定义正则
  - `VerificationTracker`：同一验证码 60 秒内只处理一次，避免标题和聊天重复触发
- 桥接层 `[verify]` 配置：
  - `verify_enabled`、`verify_stream_id`、`verify_pattern`、`verify_template`
    （`{code}` / `{username}` / `{source}`）、`verify_window`、`verify_forward_source`
- 插件层：验证码事件转发到指定聊天流；`/mc verify` 可重发最近一次验证码
- 12 个新测试（提取、去重、标题/副标题/Boss 栏解析、桥接捕获、插件转发与重发），
  总计 **206** 个用例

### 注意

- 如果服务器的验证机器人要求「必须由玩家本人的 QQ 发送」，机器人账号代发可能不通过；
  可把 `verify_stream_id` 设为你的私聊，收到验证码后自己发到验证群

## 1.0.5 (2026-11-15)

处理 `Unable to access jarfile ViaProxy.jar` 的反馈。

### 修复

- `tools/run_viaproxy.py`：如果目录里已有 **带版本号的** `ViaProxy-3.4.14.jar`
  这类文件，会直接复用，不再强求文件名必须叫 `ViaProxy.jar`
- `docs/MODERN_VERSIONS.md`：明确说明从 Releases 下载的文件名带版本号，
  `Unable to access jarfile ViaProxy.jar` 是因为**当前目录没有这个确切文件名**；
  给出 `dir *.jar`、改文件名、带空格路径加引号等排查步骤
- 新增 2 个 `find_existing_jar` 测试，总计 **194** 个用例

## 1.0.4 (2026-11-15)

解决"ViaProxy.jar 打不开"的问题，并给出一键启动脚本。

### 新增

- `tools/run_viaproxy.py`：自动寻找 Java（PATH / JAVA_HOME / Windows 常见安装目录），
  没有则打开 Adoptium 下载页；有则从 GitHub 下载最新 ViaProxy（Java 8 环境自动选
  `+java8` 构建）并启动图形界面，同时打印该填的 Target Address / Target Version /
  Bind Address
- `docs/MODERN_VERSIONS.md` 增加「先装 Java」章节：Java 21 安装、`java -version`
  验证、双击被解压软件关联、`Unsupported class file major version` 等常见报错的解法
- README 增加「打不开 ViaProxy.jar？」提示
- 6 个脚本纯函数测试（Java 版本解析、jar 构建选择），总计 **192** 个用例

### 说明

- `.jar` 需要 Java 运行，不是压缩包；这是此前打不开的唯一原因

## 1.0.3 (2026-11-15)

支持通过协议代理进入 Minecraft **26.2**（协议 776）等新版本服务器。

### 变更

- 连接前不再因为服务器协议号不是 763 就直接拒绝：代理（ViaProxy / 服务端
  ViaVersion）可能原样转发目标版本号，现在只记警告并继续尝试登录
- 登录失败且服务器版本不是 1.20.1 时，错误信息直接给出 ViaProxy 方案与文档入口
- 成功经代理进入新版本时，日志提示「已通过协议转换代理进入 … 服务器」
- 新增 `docs/MODERN_VERSIONS.md`：ViaProxy 图形/命令行步骤、服务端 ViaVersion
  方案、新方块/物品与正版验证等注意事项
- 假服务器现在会像真实服务器一样拒绝版本不匹配的客户端，测试覆盖 26.2(776)
  的失败提示

### 说明

- 插件原生协议仍是 **1.20.1（763）**；26.2 靠代理翻译，26.2 新增内容在 1.20.1
  客户端里可能不可见或映射为旧方块

## 1.0.2 (2026-11-15)

修复 MaiBot 1.3.x 上「配置校验失败，工具和调度已暂停」的问题。

### 修复

- 配置模型补上 Runner 必需的 `[plugin]` 节：
  - `[plugin].enabled`（宿主启停开关）
  - `[plugin].config_version`（配置结构版本）
  
  MaiBot 1.3.18 的 `extract_plugin_config_version()` 会强制读取
  `config_data["plugin"]["config_version"]`，缺失时抛
  `PluginConfigError`，导致插件加载后配置校验失败、工具与调度暂停。
  之前的模型只有 `server/behavior/llm/report/security`，没有 `plugin` 节。
- `config.example.toml` 与 WebUI 配置分组同步补上 `[plugin]`

### 测试

- 新增回归用例 `test_config_has_host_required_plugin_section`，确保默认配置里始终有
  `plugin.config_version`；总计 **186** 个用例

## 1.0.1 (2026-11-15)

针对 MaiBot **1.3.15** 的兼容性更新。

### 兼容性

- `_manifest.json`：Host 兼容区间改为 **1.3.15 – 1.4.99**，SDK 改为 **2.16.0 – 2.23.99**
  （与 MaiBot 1.3.15 的 `maibot-plugin-sdk>=2.16.0` 依赖对齐）
- 1.3.x 没有插件命令级权限，新增 `[security].admin_users`，由插件自己校验 `/mc`
  的使用者；1.4.x 上仍叠加 MaiBot 的 `permission="operator"` 鉴权
- `ctx.llm.generate` 的 `task_name` 参数按 SDK 版本降级：老 SDK 不接受时只传 `model`，
  避免 `TypeError`
- 工具结果同时返回 `content` 与 `message`，兼容旧版 Host 的结果展示
- 插件加载日志会打印识别到的 MaiBot 版本，便于排查兼容问题

### 其他

- 新增 3 个权限/兼容性测试，总计 **185** 个用例

## 1.0.0 (2026-11-15)

首个版本：把麦麦送进 Minecraft Java 1.20.1 服务器，并让她像普通玩家一样行动。

### 协议客户端

- 纯 Python 实现 1.20.1（协议 763）：握手、状态查询、登录、压缩、AES/CFB8 加密
- 离线模式 UUID 与微软设备码登录（Xbox Live → XSTS → Minecraft → session join）
- 区块调色板解码（1.18+ 格式，含跨 long 的位解包）、方块更新、实体跟踪
- 背包、血量/饥饿、聊天、时间、死亡/重生、KeepAlive、传送确认
- 内置 1.20.1 方块 / 物品 / 实体 / 掉落数据表（来自 PrismarineJS minecraft-data）

### 玩法

- 20Hz 物理：重力、跳跃、冲刺、潜行、游泳、自动上台阶、分轴碰撞
- A\* 寻路：斜走防切角、跳跃/下落、避岩浆与危险方块、未加载区块视为墙
- 动作：走到坐标/玩家、挖方块（按硬度与工具算时间、自动换工具）、放方块、
  攻击实体、进食、捡掉落物、跟随玩家、游戏内聊天
- 自主行为：保命 → 吃饭 → 打怪 → 砍树/挖石头 → 探索/发呆，可接 LLM 选目标
- 游戏内聊天回复：LLM 生成 + 规则兜底，带冷却、概率与打字延迟

### 人类化

- 对数正态反应时间、缓出 + 手抖 + 过冲的鼠标移动、逐字打字与回退重打
- 摸鱼动作（张望、走两步、跳、潜行、空挥）、长时间游玩后的休息

### MaiBot 集成

- `maibot-plugin-sdk` 插件：4 组配置、14 个 LLM 工具、`/mc` 操作员命令
- 游戏聊天与死亡上报到 QQ（可选聊天流）
- 断线重连、35 秒无包看门狗、死亡自动重生

### 测试

- 185 个 unittest 用例：协议基础、NBT、区块、注册表、物理、寻路、人类化、
  插件组件/命令/生命周期
- 内置最小假服务器，端到端验证登录 → 区块 → 行走 → 挖掘 → 聊天 → 压缩通道

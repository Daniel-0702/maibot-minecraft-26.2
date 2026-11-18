# 协议实现说明（Minecraft Java 1.20.1 / 协议 763）

本项目没有使用 `mineflayer` / `mcproto` / `quarry`，而是在标准库上实现了一个
够用的 1.20.1 客户端。这篇文档记录实现依据、用到的包、格式细节，以及加新版本时
需要动哪些地方。

## 为什么锁定 1.20.1

* 方块状态 ID、包 ID、区块格式都是**版本绑定**的，客户端必须与服务器完全一致。
* 1.20.1 是 1.20 系列里最常见、且**还没有** 1.20.2 的 configuration 阶段和
  1.20.5 的 registry data / 物品组件改动的版本，协议结构相对简洁。
* 包 ID 与字段定义来自 PrismarineJS `minecraft-data` 的 `pc/1.20/protocol.json`，
  数据表由 `tools/` 脚本生成，不是凭记忆写的。

连接其他版本时，客户端会在登录时被服务器以"版本不匹配"踢出；`/mc connect` 会显示
服务器返回的原因。`mcbot.cli status` 可以提前看到服务器协议号。

## 连接状态机

```
TCP
 └─ handshaking ──(next_state=1)→ status ──→ 读 JSON ──→ 关闭
                └─(next_state=2)→ login ──(Login Success)→ play
```

### 握手与状态查询

| 方向 | ID | 包 |
| --- | --- | --- |
| C→S | 0x00 | Handshake：协议号 varint、host string、port u16、next_state varint |
| C→S | 0x00 | Status Request（空） |
| S→C | 0x00 | Status Response：JSON 字符串 |
| C→S | 0x01 | Ping：i64 |
| S→C | 0x01 | Pong：i64 |

`ping_server()` 用这套流程读版本、在线人数、MOTD 和延迟。

### 登录

```
C→S 0x00 Login Start        username + UUID(16B)
S→C 0x03 Set Compression    threshold varint（可选，之后所有包走压缩格式）
S→C 0x01 Encryption Request serverId + 公钥 + verifyToken（仅 online-mode）
C→S 0x01 Encryption Response 加密后的 secret + token
S→C 0x02 Login Success      UUID + username + properties
→ play
```

* **压缩**：`threshold` 之后，每个包体变成 `VarInt 未压缩长度 + 数据`；
  长度为 0 表示未压缩，否则 zlib 压缩。
* **加密**：密钥 = 共享密钥（16 字节随机），IV 也等于它，算法 `AES/CFB8/NoPadding`。
  加密从 `Encryption Response` 发出后的**下一个字节**开始，双向生效。
  会话哈希：

  ```python
  sha1(serverId.encode() + shared_secret + public_key_der)
  # 取有符号大整数，转十六进制（负数带 '-'），即 Java BigInteger.toString(16)
  ```

  在线模式在发响应前还要调用
  `POST https://sessionserver.mojang.com/session/minecraft/join`
  把 `serverId`、`selectedProfile`、`accessToken` 绑到会话上，服务器之后才能
  `hasJoined` 成功。这一步在 `auth.join_server()`。

## play 阶段用到的包

### 服务端 → 客户端

| ID | 名称 | 用途 |
| --- | --- | --- |
| 0x28 | Login (Join Game) | 实体 ID、维度、视距、死亡点 |
| 0x3C | Synchronize Player Position | 传送/校正，回 `teleport_confirm` |
| 0x24 | Chunk Data and Update Light | 区块（只解析方块与生物群系） |
| 0x0A | Block Update | 单方块变化 |
| 0x43 | Section Blocks Update | 批量方块变化 |
| 0x1E | Unload Chunk | 卸载区块 |
| 0x23 | Keep Alive | 回同 ID 的 KeepAlive |
| 0x32 | Ping | 回 `pong` |
| 0x57 | Set Health | 血量/饥饿/饱和度 |
| 0x56 | Set Experience | 经验 |
| 0x41 | Respawn | 维度切换/重生 |
| 0x1A | Disconnect | 被踢原因（JSON 文本） |
| 0x64 | System Chat | 系统消息 / 动作栏 |
| 0x35 | Player Chat | 玩家聊天（未签名） |
| 0x1B | Disguised Chat | 无档案聊天 |
| 0x46 | Action Bar | 动作栏 |
| 0x12 / 0x14 / 0x4D | Window Items / Set Slot / Held Item | 背包 |
| 0x01 / 0x03 / 0x68 / 0x2B-0x2D / 0x3E | 实体生成/传送/移动/移除 | 实体跟踪 |
| 0x67 | Collect Item | 捡起掉落物 |
| 0x5E | Update Time | 世界时间 |
| 0x1F | Game Event | 重生屏开关等 |
| 0x37 | Player Info Update | 玩家名单/延迟/皮肤 |
| 0x17 | Plugin Message | 服务器品牌 |

### 客户端 → 服务端

| ID | 名称 | 用途 |
| --- | --- | --- |
| 0x00 | Confirm Teleportation | 回应位置同步 |
| 0x05 | Chat Message | 公屏发言（发送时不带签名） |
| 0x04 | Chat Command | 执行 `/命令` |
| 0x08 | Client Information | 语言、视距、皮肤部件 |
| 0x12 | Keep Alive | 回应心跳 |
| 0x14 / 0x15 / 0x16 / 0x17 | 位置 / 位置+视角 / 视角 / 落地 | 移动 |
| 0x1D | Player Action | 开始/结束挖掘、丢弃、交换副手 |
| 0x31 | Use Item On | 放方块/交互 |
| 0x32 | Use Item | 吃/用物品 |
| 0x10 | Interact | 攻击/交互实体 |
| 0x1E | Player Command | 潜行/冲刺状态 |
| 0x28 | Set Held Item | 切快捷栏 |
| 0x2F | Swing Arm | 挥手动画 |
| 0x07 | Client Command | 请求重生 |
| 0x20 | Pong | 回应 Ping |

所有带 `sequence` 的包（挖掘/放置/使用物品）用一个自增计数器，服务器用它做
"acknowledge" 匹配；本项目只发不收，够用。

## 区块格式（1.18+）

`Chunk Data` 的 `chunkData` 字段（VarInt 长度 + 字节）：

```
VarInt section_count（1.20.1 主世界 24）
每个区段：
  i16 non_air_block_count
  PalettedContainer 方块状态
  PalettedContainer 生物群系
```

PalettedContainer：

```
u8 bits_per_entry
if bits == 0:
    VarInt palette_len(=1), VarInt value          # 全区段同一方块
elif bits <= 8:
    VarInt palette_len, palette_len × VarInt
VarInt data_len（i64 个数）, data_len × i64
```

解包：条目按 LSB 连续打包，**允许跨 long**：

```
bit = index * bits
long_index, shift = divmod(bit, 64)
value = (longs[long_index] >> shift) & mask
if shift + bits > 64:
    value |= longs[long_index + 1] << (64 - shift)
```

索引顺序 `index = (y << 8) | (z << 4) | x`。生物群系容器在 1.20.1 是 4×4×4=64 项，
解码器会根据 `data_len` 自动识别 64 / 4096 两种体积。

方块状态 ID 是**全局状态 ID**，与内置 `mcbot/data/mc_1_20_1.json` 的
`min_state_id..max_state_id` 对应；例如 `stone = 1`、`grass_block = 9`。
这些 ID 来自 `minecraft-data`，不要手改。

## 方块交互

| 操作 | 包序列 |
| --- | --- |
| 挖 | `Player Action(status=0, pos, face, seq)` → 等待破坏时间 → `Player Action(status=2, ...)` → `Swing Arm` |
| 放 | `Use Item On(hand, pos, face, cursor, inside, seq)` → `Swing Arm` |
| 吃 | `Use Item(hand, seq)` 连续几次 |
| 攻击 | `Interact(entity, action=1)` + `Swing Arm` |

破坏时间用原版公式近似（见 `docs/ARCHITECTURE.md`）。服务器如果认为时间不够，
不会发 `Block Update`，`player.mine` 会重试并最终返回失败原因。

## 聊天与文本

1.20.1 的文本组件在协议里是 **JSON 字符串**（1.20.3 才改成 NBT）。客户端只做"压平"：
递归取 `text` / `translate` / `extra`，忽略 `hoverEvent`、`clickEvent` 等交互字段。
发送聊天用 `Chat Message`：`message + timestamp + salt + 无签名 + offset + 3 字节 ack`。
正版服如果强制安全档案，未签名消息可能显示异常，本项目不做签名链。

## 内置数据表

`mcbot/data/mc_1_20_1.json` 由 `tools/gen_data.py` 生成，包含：

* `blocks`：`[name, min_state, max_state, hardness×1000, flags, tool_class, min_tool_level, default_state, shape_spec]`
* `shapes`：碰撞箱（0–1 局部坐标，六元组）
* `items` / `foods` / `entities` / `loot`

重新生成：

```bash
python tools/fetch_minecraft_data.py     # 下载 PrismarineJS 原始 JSON
python tools/gen_data.py                 # 压缩成内置表
```

生成脚本会把工具等级从 `harvestTools` 反推出来（木/金 0、石 1、铁 2、钻石 3、下界合金 4），
把掉落表压成 `[item, chance, min, max, silk, no_silk]`。

## 扩展到其他版本

1. 复制 `mcbot/packets.py` 为 `packets_1_20_4.py`，按目标版本改包 ID（建议用
   `minecraft-data` 的 `protocol.json` + `tools/dump_packet_ids` 思路重新生成）。
2. 生成对应版本的数据表（`tools/gen_data.py --version 1.20.4`），改 `registry.py` 的默认路径。
3. 处理版本差异：
   * 1.20.2+：login 之后多一个 **configuration** 阶段（registry data / finish configuration）。
   * 1.20.5+：物品组件、registry data、`Login Start` 不再带 UUID 等。
4. 在 `client.py` 里按 `protocol_version` 选择包表与解析分支，并在
   `mcbot/__init__.py` 声明支持列表。
5. 端到端测试最省事：把 `tests/fake_server.py` 的包表换掉，跑通"登录 → 区块 → 走 → 挖"。

## 已知的协议层取舍

* 不解析实体 metadata / 装备 / 药水效果（只跟踪位置、类型、UUID、速度）。
* 不解析光照、生物群系客户端渲染相关字段（服务端照发，客户端跳过）。
* 不实现资源包下载（服务器若强制资源包会卡在提示，需在服务器关闭强制）。
* 不实现聊天签名链与消息确认（`message_acknowledgement`），正版服的安全档案提示可能不消失。
* 不做服务端命令补全、配方书、统计、进度。

# 合成与光照（2.0.4）

这一版给麦麦加了两个之前没有的"感官/能力"：**会真的合成**、**能读到区块光照**。

## 一、合成：像真人一样点合成格

### 协议事实（官方 26.2 jar，javap 核实）

- 点击背包用的是 `ServerboundContainerClickPacket`：
  `containerId(VarInt) stateId(VarInt) slot(i16) button(i8) input(VarInt) changedSlots(map) carriedItem(HashedStack)`
- 26.2 的物品在点击包里是 **HashedStack**：`可选标记 + 物品 id(VarInt) + 数量(VarInt) + 组件补丁哈希(两个计数)`。
  普通物品（没改过组件）可以直接编码；**带自定义组件的物品不能用来点击**（会明确报错而不是发错包）。
- 窗口布局（`CraftingMenu`）：输出槽 0、合成格 1–9、背包 10–36、快捷栏 37–45；
  玩家自带 2x2 是输出 0、合成格 1–4。合成台菜单 id = **12**。
- 打开界面由 `ClientboundOpenScreenPacket`（containerId、菜单类型、标题）通知；
  每个容器操作后服务器回 `container_set_content`（含新的 stateId），点击必须带最新 stateId。

实现见 `mcbot/modern_client.py`（`click_slot` / `open_block`）和 `mcbot/crafting.py`。

### 合成流程

`CraftingController.craft(recipe)`：

1. 2x2 配方直接用窗口 0；3x3 配方先在附近找合成台（没有就放一个背包里的），右键打开。
2. 对每个合成格：`左键拿一叠材料 → 右键放进合成格 1 个 → 左键把剩下的放回背包`。
3. 点输出槽拿走成品，再点背包空位放下。
4. 任何一步对不上（输出槽空、光标不空、stateId 没更新）都会把合成格材料收回背包并中止，
   不会继续瞎点。
5. 3x3 用完会关掉窗口。

### 配方与规划

`mcbot/recipes.py` 收录 27 条配方（数据来自 [Minecraft Wiki 合成](https://minecraft.wiki/w/Crafting)）：

- 11 种原木/菌柄 → 对应木板 ×4
- 2x2：木棍（2 木板）、合成台（4 木板）、火把（煤/木炭 + 木棍）
- 3x3：木/石 剑、镐、斧、锹（木板或圆石 + 木棍）、熔炉（8 圆石）、箱子（8 木板）

`plan(registry, counts, target)` 会自动展开依赖：只有原木也能规划出
「原木→木板→木棍→（缺合成台时先做合成台）→木镐」的步骤链；材料不够返回 `None`。

### 自主使用

`Brain._craft_gear()` 在"木头够了但没有工具"时触发：

- 没有镐 → 做木镐；没有剑 → 做木剑；没有斧 → 做木斧（做完自动换到快捷栏）。
- 背包里有煤/木炭且没有火把 → 做 4 支火把；天黑回家时会点燃。
- 失败一次后冷静 3 分钟，避免反复卡在同一步。

也可以手动试：`/mc craft wooden_pickaxe`。
配置里可以用 `[behavior].crafting = false` 完全关掉自动合成（26.2 原生后端才支持）。

## 二、光照：知道自己在明处还是暗处

### 协议事实

区块包 `ClientboundLevelChunkWithLightPacket` 的顺序是：
坐标 → 高度图 → 区块数据 → 方块实体列表 → 光照。
光照 = `skyYMask / blockYMask / emptySkyYMask / emptyBlockYMask` 四个 BitSet，
然后按 sky、block 顺序各读 2048 字节 nibble 数据；掩码位 i 对应
`sectionY = (世界最低 section y − 1) + i`。
（`mcbot/light.py`，30 个测试覆盖编码/截断/越界。）

### 用在哪

- `client.light_at(x, y, z, "block"/"sky")` 查光照，`client.is_dark_here()` 判断可能刷怪。
- **蜘蛛**：Wiki 说亮度 ≥ 11 中立、≤ 10 敌对。现在优先用真实光照判断
  （之前只能"白天 + 地表 ≥ 50 格"近似）；拿不到光照才退回近似。
- `/mc diag` 会显示"光照解析失败 N 次"和当前位置是否偏暗（`dark_here`）。

## 三、验证范围

- 412 个测试全部通过，其中新增：
  - 配方/规划 41 个、光照解析 30 个、光照接入 3 个、点击包编码 10 个、合成流程 6 个。
  - 合成流程用"本地模拟容器"跑通完整点击序列（拾取/右键放 1/取成品/清格），
    并覆盖 2x2 木板、木棍、合成台、火把与 3x3 木镐。
- **没有在真实服务器上验证合成点击**：真实服务器的容器校验更严（stateId、HashedStack
  哈希、反作弊插件），可能出现"点不动"或"服务器回滚"。所以提供了配置开关和
  `/mc craft` 手动测试入口；如果失败，`/mc diag` 会显示打开中的窗口和背包错误。
- 光照只在客户端用，未用于寻路或建造；如果服务器不发光照（个别代理/插件），
  会自动退回近似判断，不会崩。

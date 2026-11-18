# 哪些 MC 知识被写进了插件（来源：Minecraft Wiki）

2.0.3 起，插件里的"游戏规则"不再凭感觉写，而是集中到
[`mcbot/knowledge.py`](../mcbot/knowledge.py)，并在代码注释里标注 Wiki 出处。
下面列出**实际采用**的规则、对应的 Wiki 页面，以及它改变了插件什么行为。

> 只收录插件能用上、且能从 Wiki 确认的规则；没有把握的数值不写进代码。
> 这些都是 **Java 版** 规则。Wiki 会随版本更新，若数值有调整以 Wiki 为准。

## 1. 挖掘时间与工具

来源：[Breaking](https://minecraft.wiki/w/Breaking)

| 规则 | 采用情况 |
| --- | --- |
| 基础时间 = 硬度 × 1.5（可收获）或 × 5（不可收获） | `knowledge.mining_ticks()` |
| 工具速度：空手 1、木 2、石 4、**铜 5**、铁 6、钻石 8、下界合金 9、金 12 | 补上了原先漏掉的铜 |
| 效率附魔：速度 += 等级² + 1 | `efficiency_level` 参数 |
| 头在水里（无水下速掘）：速度 ÷5 | 接入 `break_ticks()`：水里挖方块会等更久，避免"挖不掉" |
| 脚不沾地：速度再 ÷5 | 同上 |
| 伤害 ≥ 1 时瞬间破坏 | `mining_ticks()` 返回 0 |
| 石头/金属需要镐子，越硬的矿需要越高等级镐 | `can_harvest()`：徒手挖石头不会白挖，`mine_nearest` 会跳过它 |

## 2. 伤害：摔落、溺水、攻击节奏

来源：[Damage](https://minecraft.wiki/w/Damage)、[Healing](https://minecraft.wiki/w/Healing)

| 规则 | 采用情况 |
| --- | --- |
| 摔落伤害 = max(0, 下落格数 − 3)，3 格以内不掉血 | 新增悬崖保护：走一步会掉超过 3 格就刹住，`walk_to` 报"前方落差太大" |
| 溺水：氧气约 300 tick（15 秒）耗尽，之后每秒 2 HP | `submerged_seconds` 计时；大脑在浸水 8 秒时优先上浮 |
| 攻击冷却 T = 20 / 攻击速度，伤害倍率 = 0.2 + ((t+0.5)/T)² × 0.8 | `attack()` 按手持武器决定点击间隔：剑 0.625 s、斧约 1.1 s、空手 0.25 s |
| 暴击 = 伤害 ×1.5，需要在下落中命中 | `attack(crit=True)` 改为"起跳 → 等下落 → 命中" |
| 饥饿 ≥ 18 才自然回血；20 且有饱和度回得更快 | 受伤且饥饿 < 18 时先吃到 18，再继续干活 |

## 3. 生物行为

来源：[Creeper](https://minecraft.wiki/w/Creeper)、[Enderman](https://minecraft.wiki/w/Enderman)、
[Spider](https://minecraft.wiki/w/Spider)、[Zombie](https://minecraft.wiki/w/Zombie)、
[Skeleton](https://minecraft.wiki/w/Skeleton)、[Mob](https://minecraft.wiki/w/Mob)

| 规则 | 采用情况 |
| --- | --- |
| 爬行者靠近约 1 格后 1.5 秒爆炸，爆炸波及 3–5 格 | 3.5 格内立刻逃跑（优先于战斗、砍树） |
| 末影人：直视眼睛 5 tick 会被激怒；本身中立 | 不打末影人；`look_at_entity` 对末影人只瞄躯干/脚，避免盯眼 |
| 蜘蛛：亮度足够（白天、地表）时中立，不会主动攻击 | 白天且在地表时不把蜘蛛当威胁 |
| 僵尸/骷髅：白天会着火，但活着时仍会追人 | 算威胁，但不会因为它们就放弃所有计划 |
| 大多数敌对生物发现距离约 16 格（僵尸 40 格） | `DEFAULT_FOLLOW_RANGE` / `ZOMBIE_FOLLOW_RANGE`，用于威胁判断的参考半径 |

## 4. 疾跑与饥饿

来源：[Sprinting](https://minecraft.wiki/w/Sprinting)、[Hunger](https://minecraft.wiki/w/Hunger)

| 规则 | 采用情况 |
| --- | --- |
| 饥饿 ≤ 6 不能疾跑；疾跑消耗饱和度 | `knowledge.sprint_allowed()` 统一门槛（原来写死的 `> 6` 改为引用知识表） |

## 5. 合成与刷怪光照（2.0.4 起）

来源：[Crafting](https://minecraft.wiki/w/Crafting)、[Light](https://minecraft.wiki/w/Light)、
[Mob spawning](https://minecraft.wiki/w/Mob_spawning)

| 规则 | 采用情况 |
| --- | --- |
| 木板/木棍/合成台/火把与工具、熔炉、箱子的配方形状 | `mcbot/recipes.py`（27 条），3x3 配方需要合成台 |
| 一个原木 → 4 木板；2 木板 → 4 木棍 | 规划 `plan()` 自动换算数量 |
| 敌对生物在方块光 0、且夜晚或天空光 ≤7 时可能刷新 | `mcbot/light.py` 的保守判断（用于 `dark_here`、蜘蛛中立判断） |
| 蜘蛛亮度 ≥11 中立、≤10 敌对 | 有光照数据时按真实亮度判断，否则退回"白天+地表"近似 |

## 没有采用（以及为什么）

- **合成 / 容器点击 / 附魔**：需要完整的容器 GUI 协议（点击、拖拽、状态同步），
  现在 26.2 原生客户端还没实现，勉强做会错位并可能被服务器踢，所以不做。
- **光照等级驱动的刷怪判断**：插件没有解析区块光照数据，只能用"白天/夜晚 + 地表高度"近似。
- **凋灵/末影龙/袭击等 Boss 流程**：属于长线玩法，当前自治目标是采集与生存。
- **村民交易、钓鱼、睡觉**：同样依赖 GUI 或床的交互，未实现。

## 如何复核

```powershell
# 知识表与接入逻辑的 39 个测试
python -m unittest discover -s tests -p test_knowledge.py -v

# 全量回归（322 个用例）
python tests/run_all.py
```

其中挖掘时间、摔落伤害、溺水、攻击冷却、生物档案这些数值都在
`tests/test_knowledge.py` 里与 Wiki 公式逐条对照。

# 麦麦能做的事（动作注册表）

这份表由 mcbot/actions.py 生成，是 /mc 子命令 与 LLM 工具共用的唯一清单。
新增动作只要在注册表里加一条，帮助、别名和工具都会自动跟上。

## 移动

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| goto | /mc goto <x> <y> <z> | 走到指定坐标或玩家身边 | go / 去 / 走到 / 前往 / 导航 |
| come | /mc come | 走到正在说话的玩家身边 | 来 / 过来 |
| follow | /mc follow [玩家] | 跟在指定玩家身后 | 跟随 / 跟着 |
| look | /mc look <x> <y> <z> | 转头看向指定坐标或玩家 | lookat / 看向 / 看 |
| explore | /mc explore [半径] | 在附近探索，顺路找资源 | 探索 / 逛逛 |
| swim | /mc swim | 游向岸边并爬上岸 | 上岸 / 游上岸 |
| home | /mc home | 走回之前设置的家 | 回家 |
| sethome | /mc sethome | 把当前位置设为家 | 设家 / 设置家 |
| jump | /mc jump | 原地跳一下 | 跳 / 跳一下 |
| stop | /mc stop | 立即停下当前动作 | 停 / 停下 |
| resume | /mc resume | 继续刚才被打断的动作 | 继续 / 恢复 |

## 采集

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| mine | /mc mine <方块> [数量] | 挖掉指定方块，可指定数量 | 挖 / 挖掘 |
| chop | /mc chop [数量] | 砍树 | 砍树 / 砍木 |
| dig | /mc dig [数量] | 向下挖竖井找矿 | 下挖 / 挖矿 |
| collect | /mc collect [半径] | 捡起附近掉落的物品 | 捡 / 捡东西 / 拾取 |
| eat | /mc eat [食物] | 吃东西补充饥饿值 | 吃 / 吃东西 |

## 合成

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| craft | /mc craft <物品> [数量] | 按配方合成物品 | 合成 / 制作 |
| recipes | /mc recipes [物品] | 查看能做什么或某个物品的配方 | 配方 / 能做什么 |
| torch | /mc torch | 合成或放置火把照明 | 火把 / 点火把 |

## 背包

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| inventory | /mc inventory | 查看背包物品 | 背包 / 物品栏 |
| equip | /mc equip <物品> | 把物品拿在主手 | 装备 / 拿在手上 / 手持 |
| drop | /mc drop [物品] [数量] | 丢弃背包里的物品 | 丢弃 / 扔掉 |

## 建造

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| place | /mc place <方块> [x y z] | 在指定位置放置方块 | 放置 / 放方块 |
| pillar | /mc pillar [高度] | 边跳边垫方块升高 | 垫高 / 起柱 |
| bridge | /mc bridge [长度] | 向前搭桥或铺路 | 搭桥 / 铺路 |
| shelter | /mc shelter | 就地挖一个避难所 | 避难所 / 躲起来 |

## 战斗

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| attack | /mc attack [目标] [次数] | 攻击指定目标 | 攻击 / 打 |
| flee | /mc flee | 从危险中逃跑 | 逃跑 / 快跑 |

## 信息

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| status | /mc status | 查看连接与角色状态 | 状态 |
| say | /mc say 文字 | 在游戏里说一句话 | 说 / 聊天 / 说话 / 发言 |
| diag | /mc diag | 输出诊断信息，排查连接问题 | 诊断 |
| nearby | /mc nearby [半径] | 列出附近的东西 | 附近 / 周围 |
| players | /mc players | 列出服务器在线玩家 | 玩家 / 在线玩家 |
| where | /mc where | 报告当前坐标与维度 | 坐标 / 位置 |
| time | /mc time | 查看游戏内时间 | 时间 |
| light | /mc light | 查看当前光照亮度 | 光照 / 亮度 |
| help | /mc help [分类] | 显示动作帮助 | 帮助 / 命令 |

## 系统

| 动作 | 用法 | 说明 | 别名 |
| --- | --- | --- | --- |
| connect | /mc connect <地址[:端口]> | 连接到 Minecraft 服务器 | 连接 / 进服 |
| disconnect | /mc disconnect | 断开当前服务器连接 | 断开 / 退服 / 下线 |
| verify | /mc verify | 重新发送进服验证码 | 验证码 |


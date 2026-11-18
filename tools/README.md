# 开发工具

这些脚本只在开发/更新内置数据时使用，插件运行时不依赖它们。

| 脚本 | 用途 |
| --- | --- |
| `fetch_minecraft_data.py` | 从 PrismarineJS/minecraft-data 下载指定版本的 blocks / items / entities / loot / protocol 等原始 JSON，默认存到 `tools/mcdata/` |
| `gen_data.py` | 把原始 JSON 压缩成插件内置的 `mcbot/data/mc_1_20_1.json`（约 600KB） |
| `run_viaproxy.py` | 自动找 Java、下载并启动 ViaProxy，把 26.2 等新版本服务器转成插件支持的 1.20.1 |
| `fix_maibot_chat_db.py` | 清理 MaiBot 数据库里重复的 `message_id`（WebUI `/chat` 页 React #185 的已知诱因），默认 dry-run + 自动备份 |

## 重新生成数据

```bash
python tools/fetch_minecraft_data.py --version 1.20.1
python tools/gen_data.py
```

数据来源：<https://github.com/PrismarineJS/minecraft-data>（MIT）。

## 校验

```bash
python -m json.tool mcbot/data/mc_1_20_1.json > /dev/null
```

生成后建议跑一遍 `python tests/run_all.py`，确认 `registry` / `chunk` / 端到端测试仍然通过。

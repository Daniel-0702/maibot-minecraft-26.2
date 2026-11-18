"""下载 PrismarineJS minecraft-data 原始数据（开发用，不随插件运行）。

用法：
    python tools/fetch_minecraft_data.py [--version 1.20.1]

数据来源：https://github.com/PrismarineJS/minecraft-data
协议版本：1.20.1 -> 763
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

ROOT = "https://raw.githubusercontent.com/PrismarineJS/minecraft-data/master/data"
DATAPATHS = f"{ROOT}/dataPaths.json"

# 1.20.1 的部分数据按主版本共享目录存放，这里按 dataPaths.json 的映射解析。
DEFAULT_VERSION = "1.20.1"


def resolve_paths(version: str) -> dict[str, str]:
    with urllib.request.urlopen(DATAPATHS, timeout=60) as resp:
        paths = json.load(resp)["pc"][version]
    return {name: f"{ROOT}/{path}/{name}.json" for name, path in paths.items()}


def download(url: str, target: str, force: bool = False) -> None:
    if os.path.exists(target) and not force:
        print(f"cached  {target} ({os.path.getsize(target)} bytes)")
        return
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read()
    with open(target, "wb") as fh:
        fh.write(data)
    print(f"fetched {target} ({len(data)} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "mcdata"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    wanted = (
        "version",
        "blocks",
        "blockCollisionShapes",
        "items",
        "foods",
        "entities",
        "blockLoot",
        "protocol",
        "materials",
    )
    paths = resolve_paths(args.version)
    for name in wanted:
        if name not in paths:
            print(f"skip    {name} (not available for {args.version})")
            continue
        download(paths[name], os.path.join(args.out, f"{name}.json"), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

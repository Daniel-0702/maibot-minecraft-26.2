"""把 minecraft-data 原始 JSON 压缩成插件内置的小体积数据表。

产物：mcbot/data/mc_1_20_1.json

结构::

    {
      "version": "1.20.1",
      "protocol": 763,
      "min_y": -64,
      "height": 384,
      "blocks": [
          [name, min_state, max_state, hardness_milli, flags, tool_class,
           min_tool_level, default_state, shape_spec], ...
      ],
      "shapes": [[[x1, y1, z1, x2, y2, z2], ...], ...],
      "items":   {"<net_id>": [name, stack_size]},
      "foods":   {"<net_id>": [food_points, saturation]},
      "entities":{"<type_id>": name},
      "loot":    {"<block>": [[item, chance, min, max, silk, no_silk], ...]}
    }

flags 位：1=可挖掘 2=透明 4=液体 8=可攀爬
"""

from __future__ import annotations

import argparse
import json
import os

TOOL_TIER = {
    "wooden": 0,
    "golden": 0,
    "stone": 1,
    "iron": 2,
    "diamond": 3,
    "netherite": 4,
}
TOOL_KINDS = ("pickaxe", "axe", "shovel", "hoe", "shears", "sword")

FLAG_DIGGABLE = 1
FLAG_TRANSPARENT = 2
FLAG_LIQUID = 4
FLAG_CLIMBABLE = 8

LIQUIDS = {"water", "lava", "flowing_water", "flowing_lava"}
CLIMBABLE = {
    "ladder",
    "vine",
    "scaffolding",
    "twisting_vines",
    "twisting_vines_plant",
    "weeping_vines",
    "weeping_vines_plant",
    "cave_vines",
    "cave_vines_plant",
}


def tool_kind(material: str) -> str:
    if material.startswith("mineable/"):
        kind = material.split("/", 1)[1]
        if kind in TOOL_KINDS:
            return kind
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", default=os.path.join(os.path.dirname(__file__), "mcdata"))
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "..", "mcbot", "data", "mc_1_20_1.json"),
    )
    args = parser.parse_args()

    def load(name: str):
        with open(os.path.join(args.src, f"{name}.json"), encoding="utf-8") as fh:
            return json.load(fh)

    version = load("version")
    blocks = load("blocks")
    shapes_raw = load("blockCollisionShapes")
    items = load("items")
    foods = load("foods")
    entities = load("entities")
    loot = load("blockLoot")
    materials = load("materials")

    item_by_id = {entry["id"]: entry for entry in items}

    def tool_level(harvest: dict | None) -> int | None:
        """返回该方块要求的最低工具等级；None 表示不需要工具。"""
        if not harvest:
            return None
        levels = []
        for raw_id in harvest:
            item = item_by_id.get(int(raw_id))
            if not item:
                continue
            name = item["name"]
            prefix = name.split("_", 1)[0]
            if prefix in TOOL_TIER:
                levels.append(TOOL_TIER[prefix])
        return min(levels) if levels else 0

    # 收集实际被用到的碰撞箱，重新编号，减小体积。
    used_shape_ids: dict[int, int] = {}
    shape_payload: list[list[list[float]]] = []

    def intern_shape(shape_id: int) -> int:
        if shape_id in used_shape_ids:
            return used_shape_ids[shape_id]
        boxes = shapes_raw["shapes"].get(str(shape_id), [])
        new_id = len(shape_payload)
        used_shape_ids[shape_id] = new_id
        shape_payload.append([[round(float(v), 4) for v in box] for box in boxes])
        return new_id

    block_rows = []
    for block in sorted(blocks, key=lambda b: b["minStateId"]):
        name = block["name"]
        flags = 0
        if block.get("diggable"):
            flags |= FLAG_DIGGABLE
        if block.get("transparent"):
            flags |= FLAG_TRANSPARENT
        if name in LIQUIDS:
            flags |= FLAG_LIQUID
        if name in CLIMBABLE:
            flags |= FLAG_CLIMBABLE

        kind = tool_kind(block.get("material", ""))
        min_level = tool_level(block.get("harvestTools"))
        tool = kind
        level = min_level if min_level is not None else -1
        if not kind and min_level is not None:
            # 剪刀 / 剑这类特殊工具
            for raw_id in block.get("harvestTools") or {}:
                item = item_by_id.get(int(raw_id))
                if not item:
                    continue
                for candidate in TOOL_KINDS:
                    if item["name"].endswith("_" + candidate):
                        tool = candidate
                        break
                if tool:
                    break

        raw_shapes = shapes_raw["blocks"].get(name)
        if isinstance(raw_shapes, list):
            shape_spec = [intern_shape(sid) for sid in raw_shapes]
        elif isinstance(raw_shapes, int):
            shape_spec = intern_shape(raw_shapes)
        else:
            shape_spec = 0

        block_rows.append(
            [
                name,
                block["minStateId"],
                block["maxStateId"],
                int(round(float(block.get("hardness", 0.0)) * 1000)),
                flags,
                tool,
                level,
                block.get("defaultState", block["minStateId"]),
                shape_spec,
            ]
        )

    # 物品：网络 id -> [名称, 堆叠上限]
    items_out = {
        str(entry["id"]): [entry["name"], int(entry.get("stackSize", 64))]
        for entry in items
        if str(entry.get("name", "")).strip()
    }
    foods_out = {
        str(entry["id"]): [float(entry.get("foodPoints", 0.0)), float(entry.get("saturation", 0.0))]
        for entry in foods
    }
    entities_out = {str(entry["id"]): entry["name"] for entry in entities}

    loot_out: dict[str, list] = {}
    for entry in loot:
        drops = []
        for drop in entry.get("drops", []):
            stack_range = drop.get("stackSizeRange") or [1, 1]
            low = stack_range[0] if stack_range[0] is not None else 1
            high = stack_range[1] if stack_range[1] is not None else low
            drops.append(
                [
                    drop.get("item", ""),
                    float(drop.get("dropChance", 1.0)),
                    int(low),
                    int(high),
                    bool(drop.get("silkTouch")),
                    bool(drop.get("noSilkTouch")),
                ]
            )
        if drops:
            loot_out[entry["block"]] = drops

    payload = {
        "version": version["minecraftVersion"],
        "protocol": version["version"],
        "min_y": -64,
        "height": 384,
        "blocks": block_rows,
        "shapes": shape_payload,
        "items": items_out,
        "foods": foods_out,
        "entities": entities_out,
        "loot": loot_out,
        "materials": materials,
    }

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(out_path)
    print(f"wrote {out_path} ({size / 1024:.1f} KiB)")
    print(f"blocks={len(block_rows)} shapes={len(shape_payload)} items={len(items_out)} loot={len(loot_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

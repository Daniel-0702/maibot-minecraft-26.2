"""Generate BlockRegistry's 26.2 table from official reports and server classes.

python tools/gen_modern_data.py --research ../research-26.2
Requires the official extracted bundle, reports and JDK25 under that directory.
Never starts a server or accepts an EULA. No third-party Minecraft data is used.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

JAVA_SOURCE = r'''import com.google.gson.*;
import java.nio.file.*;
import net.minecraft.SharedConstants;
import net.minecraft.server.Bootstrap;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.EmptyBlockGetter;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
public class ExtractModern {
 public static void main(String[] args) throws Exception {
  SharedConstants.tryDetectVersion(); Bootstrap.bootStrap();
  var lookup=net.minecraft.data.registries.VanillaRegistries.createLookup();
  for(var pending:BuiltInRegistries.DATA_COMPONENT_INITIALIZERS.build(lookup)) pending.apply();
  JsonObject root=new JsonObject(); JsonArray states=new JsonArray();
  for(BlockState s:Block.BLOCK_STATE_REGISTRY){
   JsonObject row=new JsonObject(); row.addProperty("id",Block.getId(s));
   row.addProperty("name",BuiltInRegistries.BLOCK.getKey(s.getBlock()).toString());
   row.addProperty("hardness",s.getDestroySpeed(EmptyBlockGetter.INSTANCE,BlockPos.ZERO));
   row.addProperty("solid",s.isSolidRender());
   row.addProperty("requires_tool",s.requiresCorrectToolForDrops());
   JsonArray boxes=new JsonArray();
   for(var b:s.getCollisionShape(EmptyBlockGetter.INSTANCE,BlockPos.ZERO).toAabbs()){
    JsonArray a=new JsonArray(); for(double v:new double[]{b.minX,b.minY,b.minZ,b.maxX,b.maxY,b.maxZ})a.add(v); boxes.add(a);
   }
   row.add("boxes",boxes); states.add(row);
  }
  root.add("states",states); JsonObject items=new JsonObject(); JsonObject foods=new JsonObject();
  for(var i:BuiltInRegistries.ITEM){String id=""+BuiltInRegistries.ITEM.getId(i); JsonArray a=new JsonArray();a.add(BuiltInRegistries.ITEM.getKey(i).toString());a.add(i.getDefaultMaxStackSize());items.add(id,a);
   var f=i.components().get(DataComponents.FOOD);if(f!=null){JsonArray food=new JsonArray();food.add(f.nutrition());food.add(f.saturation());foods.add(id,food);}
  }
  root.add("items",items);root.add("foods",foods);JsonObject entities=new JsonObject();for(var e:BuiltInRegistries.ENTITY_TYPE)entities.addProperty(""+BuiltInRegistries.ENTITY_TYPE.getId(e),BuiltInRegistries.ENTITY_TYPE.getKey(e).toString());root.add("entities",entities);
  Files.writeString(Path.of(args[0]),new Gson().toJson(root));
 }
}
'''


def generate(research: Path, output: Path) -> dict:
    bundle = research / "bundle"
    jar = bundle / "META-INF/versions/26.2/server-26.2.jar"
    reports = research / "reports/reports"
    blocks = json.loads((reports / "blocks.json").read_text(encoding="utf-8"))
    version = json.loads((bundle / "version.json").read_text(encoding="utf-8"))
    if version["id"] != "26.2" or version["protocol_version"] != 776:
        raise ValueError("expected official 26.2/protocol 776 bundle")
    work = research / "world-work"
    work.mkdir(parents=True, exist_ok=True)
    source = work / "ExtractModern.java"
    source.write_text(JAVA_SOURCE, encoding="utf-8")
    classpath = os.pathsep.join(str(p.resolve()) for p in sorted(bundle.rglob("*.jar")))
    executable = lambda name: str((research / "jdk-25.0.2/bin" / (name + ".exe")).resolve())
    subprocess.run([executable("javac"), "-cp", classpath, str(source.resolve())], check=True)
    extracted = work / "extracted.json"
    subprocess.run([executable("java"), "-cp", str(work.resolve()) + os.pathsep + classpath,
                    "ExtractModern", str(extracted.resolve())], check=True)
    raw = json.loads(extracted.read_text(encoding="utf-8"))
    states = sorted(raw["states"], key=lambda s: s["id"])
    if [s["id"] for s in states] != list(range(len(states))):
        raise ValueError("official state registry is not contiguous")
    with zipfile.ZipFile(jar) as archive:
        def tag(name, seen=frozenset()):
            if name in seen:
                raise ValueError("cyclic block tag")
            path = "data/minecraft/tags/block/" + name + ".json"
            values = json.loads(archive.read(path))["values"]
            result = set()
            for value in values:
                if isinstance(value, dict):
                    value = value["id"]
                if value.startswith("#minecraft:"):
                    result.update(tag(value[11:], seen | {name}))
                else:
                    result.add(value)
            return result
        climbable = tag("climbable")
        tools = {kind: tag("mineable/" + kind) for kind in ("pickaxe", "axe", "shovel", "hoe")}
        tiers = [(level, tag("needs_" + tier + "_tool")) for level, tier in ((1, "stone"), (2, "iron"), (3, "diamond"))]
    shapes, shape_ids = [], {}
    def intern(boxes):
        key = tuple(tuple(box) for box in boxes)
        if key not in shape_ids:
            shape_ids[key] = len(shapes)
            shapes.append(boxes)
        return shape_ids[key]
    rows = []
    cursor = 0
    for name, definition in sorted(blocks.items(), key=lambda item: min(s["id"] for s in item[1]["states"])):
        official = sorted(definition["states"], key=lambda s: s["id"])
        ids = [s["id"] for s in official]
        if ids != list(range(cursor, cursor + len(ids))):
            raise ValueError("noncontiguous block range: " + name)
        subset = states[cursor:cursor + len(ids)]
        if any(s["name"] != name for s in subset):
            raise ValueError("report and runtime state ID disagreement: " + name)
        default_id = next(s["id"] for s in official if s.get("default"))
        default = states[default_id]
        flags = (1 if default["hardness"] >= 0 else 0) | (0 if default["solid"] else 2)
        if name in {"minecraft:water", "minecraft:lava"}: flags |= 4
        if name in climbable: flags |= 8
        tool = next((kind for kind, names in tools.items() if name in names), "")
        level = max((tier for tier, names in tiers if name in names), default=0) if default["requires_tool"] else -1
        spec = [intern(s["boxes"]) for s in subset]
        rows.append([name.removeprefix("minecraft:"), cursor, ids[-1], round(default["hardness"] * 1000),
                     flags, tool, level, default_id, spec[0] if len(set(spec)) == 1 else spec])
        cursor += len(ids)
    if cursor != len(states): raise ValueError("unreported runtime states")
    payload = {"version": "26.2", "protocol": 776, "min_y": -64, "height": 384,
               "blocks": rows, "shapes": shapes,
               "items": {k: [v[0].removeprefix("minecraft:"), v[1]] for k, v in raw["items"].items()},
               "foods": raw["foods"], "entities": {k: v.removeprefix("minecraft:") for k, v in raw["entities"].items()},
               "loot": {}, "source": {"server_sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
               "collision_context": "EmptyBlockGetter.INSTANCE, BlockPos.ZERO, empty CollisionContext",
               "limitations": ["Shapes are context-free, not entity/world-sensitive collision queries.",
                "Hardness and flags are default-state block properties; transparent means not isSolidRender.",
                "Tool class/tier uses vanilla block tags; special tool behavior is not modeled.",
                "Loot omitted: dynamic loot tables are not approximated."]}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=Path(__file__).resolve().parents[2] / "research-26.2")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "mcbot/data/mc_26_2.json")
    args = parser.parse_args()
    result = generate(args.research, args.out)
    print(f"blocks={len(result['blocks'])} shapes={len(result['shapes'])} items={len(result['items'])} entities={len(result['entities'])}")

if __name__ == "__main__":
    main()

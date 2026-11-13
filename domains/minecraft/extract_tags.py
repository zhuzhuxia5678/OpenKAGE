"""从 Minecraft 服务端 jar 抽出 item tag（物品标签）表，供原语播报「归纳配方」用。

# 为什么需要这个

原版配方的材料位大量使用**标签**而非具体物品，例如 `soul_campfire`：

    "key": {"S": "minecraft:stick",
            "#": "#minecraft:soul_fire_base_blocks",   # 灵魂沙 或 灵魂土
            "L": "#minecraft:logs"}                    # 任意原木/木头/菌柄/菌核，含去皮

而 `minecraft-data` / `prismarine-recipe` **不支持标签**，会把它**笛卡尔展开**成一条条具体
配方，标签语义彻底丢失：播报只能挑一条（可能与 bot 实际手持物品不符）或全量枚举（啰嗦），
且库数据版本滞后还会静默漏收新物品（如 1.21 新增的苍白橡木木材）。

用 jar 里的标签做归纳同时解决两者：播报引用标签而非枚举具体物品，库数据滞后也不会漏。
jar 里共 178 个 item tag 文件，是游戏自带的权威分组。

# 用法

    python3 -m domains.minecraft.extract_tags            # 用默认 jar，写到 mc_server/lib/mc_tags.json
    python3 -m domains.minecraft.extract_tags --jar ... --out ...

产物 `mc_tags.json` 形如：

    {"__version__": "1.21.11",
     "logs": ["oak_log", "oak_wood", "stripped_oak_log", ..., "warped_hyphae"],
     "planks": ["oak_planks", ..., "cherry_planks"],
     ...}

标签值里的嵌套引用（`#minecraft:logs_that_burn`）已**递归解开**，只留具体物品名（去掉
`minecraft:` 前缀）。
"""
from __future__ import annotations

import argparse
import json
import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: 默认 jar：worker 模板目录（experiments/run.py 的 --mc-dir 默认值）下 fabric 解出的原版服务端。
#: 注意**不是** fabric-server-launch.jar（那是 loader），也不是最外层的 bundler jar。
DEFAULT_JAR = "/path/to/mc_probe/server121/.fabric/server/1.21.11-server.jar"
DEFAULT_OUT = Path(__file__).with_name("mc_server") / "lib" / "mc_tags.json"

#: jar 内 item tag 的路径前缀（1.21 起是单数 `tags/item/`；老版本是 `tags/items/`，两个都试）
_TAG_DIRS = ("data/minecraft/tags/item/", "data/minecraft/tags/items/")


def _open_inner_jar(jar_path: str) -> zipfile.ZipFile:
    """打开真正含 data/ 的 jar。

    1.21 的官方服务端 jar 是 **bundler**：外层只有 `net/minecraft/bundler/*.class` 和
    `META-INF/versions/<ver>/server-<ver>.jar`，data/ 在**内层**。这里自动下钻一层。
    """
    zf = zipfile.ZipFile(jar_path)
    names = zf.namelist()
    if any(n.startswith(d) for d in _TAG_DIRS for n in names):
        return zf  # 已经是内层 jar
    inner = [n for n in names
             if n.startswith("META-INF/versions/") and n.endswith(".jar")]
    if not inner:
        raise RuntimeError(
            f"{jar_path} 里既没有 data/minecraft/tags/item/，也没有 META-INF/versions/*.jar —— "
            "确认传的是原版服务端 jar（不是 fabric-server-launch.jar）"
        )
    import io
    data = zf.read(inner[0])
    zf.close()
    return zipfile.ZipFile(io.BytesIO(data))


def extract_item_tags(jar_path: str = DEFAULT_JAR) -> dict[str, list[str]]:
    """读 jar，返回 {tag名: [具体物品名, ...]}，嵌套引用已递归解开。"""
    zf = _open_inner_jar(jar_path)
    try:
        raw: dict[str, list] = {}
        prefix = None
        for name in zf.namelist():
            for d in _TAG_DIRS:
                if name.startswith(d) and name.endswith(".json"):
                    prefix = d
                    tag = name[len(d):-len(".json")]
                    try:
                        raw[tag] = json.loads(zf.read(name)).get("values", [])
                    except Exception:  # pylint: disable=broad-except
                        logger.warning("[tags] 解析失败，跳过：%s", name)
                    break
        if not raw:
            raise RuntimeError(f"{jar_path} 里没找到任何 item tag（试过 {_TAG_DIRS}）")

        version = ""
        try:
            version = json.loads(zf.read("version.json")).get("name", "")
        except Exception:  # pylint: disable=broad-except
            pass
    finally:
        zf.close()

    def resolve(tag: str, seen: frozenset[str] = frozenset()) -> list[str]:
        """递归展开一个 tag。seen 防自引用/环（原版没有，但别让脏数据把这里打成死循环）。"""
        if tag in seen or tag not in raw:
            return []
        seen = seen | {tag}
        out: list[str] = []
        for v in raw[tag]:
            # 值可能是字符串，也可能是 {"id": ..., "required": false}（可选成员）
            if isinstance(v, dict):
                v = v.get("id", "")
            if not isinstance(v, str) or not v:
                continue
            if v.startswith("#"):
                out.extend(resolve(v.split(":")[-1], seen))
            else:
                out.append(v.split(":")[-1])
        # 去重保序
        return list(dict.fromkeys(out))

    resolved = {t: resolve(t) for t in sorted(raw)}
    resolved = {t: v for t, v in resolved.items() if v}  # 丢掉展开后为空的
    resolved["__version__"] = version  # type: ignore[assignment]
    return resolved


def extract_recipes(jar_path: str = DEFAULT_JAR) -> dict[str, list[dict]]:
    """读 jar 的合成配方，**保留材料位的 tag 引用**（这是 prismarine 丢掉的信息）。

    返回 {产物名: [ {"needs": [{"count": n, "tag": "logs"} | {"count": n, "item": "stick"}],
                     "requiresTable": bool}, ... ]}

    收三种工作台合成类型：
      · `crafting_shaped`     —— 有 pattern/key
      · `crafting_shapeless`  —— 有 ingredients
      · `crafting_transmute`  —— schema 是 `input` + `material`（无 pattern），
        语义为「input + material → 同形态但换色的产物」（各色 shulker_box / bundle 的染色配方）。
    熔炼/切石/锻造/篝火（smelting/stonecutting/smithing_*/campfire_cooking 等）不收 —— 它们不走
    craftItem，有各自的原语与失败提示。
    `crafting_special_*`（地图复制、烟花、盔甲染色、修复等）是**动态配方**，材料由代码
    运行时决定，静态数据里没有材料表，本就无法表达 —— 这部分 craftHelper 会自动降级到枚举
    prismarine 变体。

    `requiresTable` 按 3×3 判定：shaped 的 pattern 超过 2×2、或 shapeless 材料数 > 4；
    transmute 是 2 个材料，恒 False（2×2 够放）。
    """
    zf = _open_inner_jar(jar_path)
    out: dict[str, list[dict]] = {}
    try:
        for name in zf.namelist():
            if not (name.startswith("data/minecraft/recipe/") and name.endswith(".json")):
                continue
            try:
                r = json.loads(zf.read(name))
            except Exception:  # pylint: disable=broad-except
                continue
            rtype = str(r.get("type", "")).split(":")[-1]
            if rtype not in ("crafting_shaped", "crafting_shapeless",
                             "crafting_transmute"):
                continue
            result = r.get("result") or {}
            product = str(result.get("id", "")).split(":")[-1]
            if not product:
                continue

            # 统计每个「材料标识」需要几个。
            # 一个材料位可能是**候选列表**（如 torch 的 `"X": ["coal","charcoal"]`，即
            #   「煤炭或木炭均可」）。这里把整个列表规范化成一个可哈希的标识
            #   `coal|charcoal`，**不能只取第一个** —— 那会静默丢掉一种可行材料。
            def _ident(spec) -> str | None:
                """把配方里的材料声明规范化成标识：`stick` / `#logs` / `coal|charcoal`。"""
                if isinstance(spec, dict):
                    spec = spec.get("item") or spec.get("tag")
                if isinstance(spec, list):
                    parts = [p for p in (_ident(s) for s in spec) if p]
                    return "|".join(dict.fromkeys(parts)) or None
                if isinstance(spec, str) and spec:
                    return spec if spec.startswith("#") else spec.split(":")[-1]
                return None

            counts: dict[str, int] = {}
            if rtype == "crafting_shaped":
                key = r.get("key") or {}
                pattern = r.get("pattern") or []
                for row in pattern:
                    for ch in row:
                        if ch == " ":
                            continue
                        ident = _ident(key.get(ch))
                        if not ident:
                            continue
                        counts[ident] = counts.get(ident, 0) + 1
                width = max((len(row) for row in pattern), default=0)
                requires_table = width > 2 or len(pattern) > 2
            elif rtype == "crafting_transmute":
                # schema：{"input": <被染色的物品/tag>, "material": <染料>}，没有 pattern。
                # 语义是「input + material → 同形态但换色的产物」（潜影盒/收纳袋染色）。
                for field in ("input", "material"):
                    ident = _ident(r.get(field))
                    if ident:
                        counts[ident] = counts.get(ident, 0) + 1
                requires_table = False  # 只 2 个材料，2×2 放得下
            else:
                for ing in r.get("ingredients") or []:
                    ident = _ident(ing)
                    if not ident:
                        continue
                    counts[ident] = counts.get(ident, 0) + 1
                requires_table = sum(counts.values()) > 4

            needs = []
            for ident, cnt in counts.items():
                if ident.startswith("#"):
                    needs.append({"count": cnt, "tag": ident.split(":")[-1]})
                elif "|" in ident:
                    # 候选列表：任一即可，展开成具体物品名数组
                    needs.append({"count": cnt, "anyOf": ident.split("|")})
                else:
                    needs.append({"count": cnt, "item": ident})
            if not needs:
                continue
            out.setdefault(product, []).append(
                {"needs": needs, "requiresTable": requires_table,
                 "makes": int(result.get("count", 1) or 1)})
    finally:
        zf.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--jar", default=DEFAULT_JAR, help=f"原版服务端 jar（默认 {DEFAULT_JAR}）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出 JSON（默认 {DEFAULT_OUT}）")
    args = ap.parse_args()

    tags = extract_item_tags(args.jar)
    recipes = extract_recipes(args.jar)
    payload = {"__version__": tags.pop("__version__", ""), "tags": tags, "recipes": recipes}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tags] {args.jar}")
    print(f"[tags] 版本={payload['__version__']!r} → {out}")
    print(f"[tags] item tag {len(tags)} 个 | 工作台配方 {len(recipes)} 种产物")
    for probe in ("logs", "planks", "soul_fire_base_blocks", "stone_tool_materials"):
        if probe in tags:
            print(f"[tags]   #{probe}: {len(tags[probe])} 种")
    for probe in ("soul_campfire", "stone_pickaxe", "crafting_table"):
        if probe in recipes:
            print(f"[tags]   {probe}: {recipes[probe]}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()

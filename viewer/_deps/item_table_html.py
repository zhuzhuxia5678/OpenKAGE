#!/usr/bin/env python3
"""物品清单交互式 HTML 表格：1 实验组(有图谱) vs N 对照组(无图谱)，5 臂 × 物品/成就。

行两层：第一层 = model group (w/ shared graph / 各无图谱对照)，第二层 = arm (alice..erin)。
列 = 物品（按类别分组：原木/矿物/锭/工具/武器防具/食物/红石/杂项），表头冻结 +
横向滚动 + 列排序 + 分组筛选。✓ = 该臂获得过该物品。

支持任意 N 组（1 实验组 + 1~3 对照组）：实验组蓝实线，各对照组不同暖色虚线。
用法（4 组示例）：
  python experiments/item_table_html.py \
      --exp results/minecraft_20260902_153908/minecraft_20260902_153908.json \
      --ctl results/minecraft_20260903_103939/minecraft_20260903_103939.json \
           results/minecraft_20260904_133129/minecraft_20260904_133129.json \
           results/minecraft_20260904_133128/minecraft_20260904_133128.json \
      [--ctl-label DSV4-Flash,GLM-5.3,HY3]
"""
from __future__ import annotations

import argparse
import html
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_ROOT))
from daily_log_loader import daily_by_arm_rows, load_daily_rows  # noqa: E402

ARMS = ["alice", "bob", "carol", "dave", "erin"]

# ── 配色：实验组(有图谱)蓝实线；各对照组(无图谱)不同暖色虚线 ──
# Ours 固定蓝；CTL 颜色按对照组出现顺序循环，全部虚线以与实验组区分。
OURS_COLOR = "#1f6feb"
# 对照组调色板：橙/品红/青绿，色相分散、与蓝拉开对比，且互不撞色
_CTL_PALETTE = ["#d4761a", "#b0396a", "#1f9e7a"]
# 实验组统一用蓝；所有对照组统一虚线（dashed）。图例/表头按 palette 着色。
# linestyle: 实验组 solid "-"；对照组 dashed "--"。


@dataclass
class Group:
    """一个 run 的展示元数据 + 已抽取数据。"""
    label: str            # 表头/图例显示名，如 "有共享图谱" 或 "DSV4-Flash"
    sub: str              # 角标小字，如 run_ts 或 "w/o graph"
    is_exp: bool          # True=实验组(蓝实线)；False=对照组(虚线)
    color: str            # 该组主色
    linestyle: str        # "-" 实验组 / "--" 对照组
    run_ts: str           # run 时间戳
    items_by_arm: dict[str, set[str]] = field(default_factory=dict)
    advs_by_arm: dict[str, set[str]] = field(default_factory=dict)
    res: dict | None = None  # 原始 result JSON，取元数据
    res_path: Path | None = None  # result JSON 路径（逐夜指标从同目录 checkpoint 取）

    @property
    def css_cls(self) -> str:
        """表头分组标签的 CSS 类：Ours 用蓝底；Baseline 系列按色着色。"""
        return "grp-Ours" if self.is_exp else "grp-Baseline"

# ── 物品分类（精确物品集合，避免子串误归，如 iron_chestplate 不归 Logs）──
# 顺序即列分组顺序。改用精确集合 + 显式列出每个物品。
_CAT_ITEMS: list[tuple[str, tuple[str, ...]]] = [
    ("原木与木材", (
        "oak_log", "birch_log", "oak_planks", "birch_planks", "oak_fence",
        "birch_fence", "oak_stairs", "birch_stairs", "oak_door", "oak_sapling",
        "leaf_litter", "stick", "ladder", "crafting_table", "chest", "furnace", "torch")),
    ("矿石与原料", (
        "raw_iron", "raw_copper", "raw_gold", "coal", "charcoal", "lapis_lazuli",
        "redstone", "emerald", "diamond", "flint", "clay_ball", "sand", "gravel",
        "dirt", "tuff", "granite", "diorite", "andesite", "cobblestone", "stone",
        "cobbled_deepslate", "sandstone", "glow_berries", "copper_ingot",
        "iron_ingot", "iron_nugget", "gold_ingot")),
    ("工具", (
        "wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "copper_pickaxe",
        "diamond_pickaxe", "wooden_axe", "stone_axe", "iron_axe", "wooden_hoe",
        "stone_hoe", "fishing_rod", "name_tag", "lead", "filled_map", "paper",
        "shield", "bow", "bowl")),
    ("武器与防具", (
        "wooden_sword", "stone_sword", "iron_sword", "arrow", "iron_helmet",
        "iron_chestplate", "iron_leggings", "iron_boots", "copper_leggings",
        "diamond_nautilus_armor", "enchanted_book", "experience_bottle",
        "potion", "ominous_bottle", "white_banner")),
    ("食物", (
        "apple", "beef", "chicken", "cod", "salmon", "pufferfish", "porkchop",
        "mutton", "cooked_beef", "cooked_chicken", "cooked_mutton",
        "cooked_porkchop", "bread", "egg", "wheat", "melon_seeds",
        "beetroot_seeds", "wheat_seeds", "rotten_flesh")),
    ("红石与铁轨", (
        "rail", "activator_rail", "detector_rail", "powered_rail")),
    ("羊毛与掉落物", (
        "white_wool", "black_wool", "brown_wool", "gray_wool", "light_gray_wool",
        "string", "bone", "feather", "leather", "white_bed", "dandelion")),
]
# 物品 → 类别 反查
_ITEM_TO_CAT: dict[str, str] = {it: cat for cat, its in _CAT_ITEMS for it in its}

# ── 中文译名（Minecraft 官方简体中文译名）──
_CN_NAMES: dict[str, str] = {
    # 原木与木材
    "oak_log": "橡木原木", "birch_log": "白桦原木", "oak_planks": "橡木木板",
    "birch_planks": "白桦木板", "oak_fence": "橡木栅栏", "birch_fence": "白桦栅栏",
    "oak_stairs": "橡木楼梯", "birch_stairs": "白桦楼梯", "oak_door": "橡木门",
    "oak_sapling": "橡树树苗", "leaf_litter": "落叶", "stick": "木棍",
    "ladder": "梯子", "crafting_table": "工作台", "chest": "箱子",
    "furnace": "熔炉", "torch": "火把",
    # 矿石与原料
    "raw_iron": "粗铁", "raw_copper": "粗铜", "raw_gold": "粗金",
    "coal": "煤炭", "charcoal": "木炭", "lapis_lazuli": "青金石",
    "redstone": "红石粉", "emerald": "绿宝石", "diamond": "钻石",
    "flint": "燧石", "clay_ball": "黏土球", "sand": "沙子", "gravel": "沙砾",
    "dirt": "泥土", "tuff": "凝灰岩", "granite": "花岗岩", "diorite": "闪长岩",
    "andesite": "安山岩", "cobblestone": "圆石", "stone": "石头",
    "cobbled_deepslate": "深板岩圆石", "sandstone": "砂岩",
    "glow_berries": "发光浆果", "copper_ingot": "铜锭", "iron_ingot": "铁锭",
    "iron_nugget": "铁粒", "gold_ingot": "金锭",
    # 工具
    "wooden_pickaxe": "木镐", "stone_pickaxe": "石镐", "iron_pickaxe": "铁镐",
    "copper_pickaxe": "铜镐", "diamond_pickaxe": "钻石镐", "wooden_axe": "木斧",
    "stone_axe": "石斧", "iron_axe": "铁斧", "wooden_hoe": "木锄",
    "stone_hoe": "石锄", "fishing_rod": "钓鱼竿", "name_tag": "命名牌",
    "lead": "拴绳", "filled_map": "地图", "paper": "纸", "shield": "盾牌",
    "bow": "弓", "bowl": "碗",
    # 武器与防具
    "wooden_sword": "木剑", "stone_sword": "石剑", "iron_sword": "铁剑",
    "arrow": "箭", "iron_helmet": "铁头盔", "iron_chestplate": "铁胸甲",
    "iron_leggings": "铁护腿", "iron_boots": "铁靴子", "copper_leggings": "铜护腿",
    "diamond_nautilus_armor": "钻石鹦鹉螺盔甲", "enchanted_book": "附魔书",
    "experience_bottle": "附魔之瓶", "potion": "药水",
    "ominous_bottle": "不祥之瓶", "white_banner": "白色旗帜",
    # 食物
    "apple": "苹果", "beef": "生牛肉", "chicken": "生鸡肉", "cod": "生鳕鱼",
    "salmon": "生鲑鱼", "pufferfish": "河豚", "porkchop": "生猪排",
    "mutton": "生羊肉", "cooked_beef": "牛排", "cooked_chicken": "熟鸡肉",
    "cooked_mutton": "熟羊肉", "cooked_porkchop": "熟猪排", "bread": "面包",
    "egg": "鸡蛋", "wheat": "小麦", "melon_seeds": "西瓜种子",
    "beetroot_seeds": "甜菜根种子", "wheat_seeds": "小麦种子",
    "rotten_flesh": "腐肉",
    # 红石与铁轨
    "rail": "铁轨", "activator_rail": "激活铁轨", "detector_rail": "探测铁轨",
    "powered_rail": "充能铁轨",
    # 羊毛与掉落物
    "white_wool": "白色羊毛", "black_wool": "黑色羊毛", "brown_wool": "棕色羊毛",
    "gray_wool": "灰色羊毛", "light_gray_wool": "淡灰色羊毛", "string": "线",
    "bone": "骨头", "feather": "羽毛", "leather": "皮革",
    "white_bed": "白色床", "dandelion": "蒲公英",
}


def _category_of(item: str) -> str:
    return _ITEM_TO_CAT.get(item, "其他")


def _items_by_arm(res: dict) -> dict[str, set[str]]:
    pa = (res.get("train_curve") or [{}])[0].get("per_arm_detail") or {}
    return {a: set(pa.get(a, {}).get("items_seen") or []) for a in ARMS}


def _daily_by_arm(res_path: Path) -> dict[str, list[dict]]:
    """{arm: [daily rows 升序]}，供时序图用。数据源 = run 目录 checkpoint 的 night_reflections。"""
    return daily_by_arm_rows(load_daily_rows(res_path.parent))


def _cn_name(item: str) -> str:
    return _CN_NAMES.get(item, item.replace("_", " "))


def _icon_data_uri(item: str, icon_dir: Path) -> str | None:
    """物品贴图 → base64 data URI（HTML 自包含）。无图返回 None。"""
    p = icon_dir / f"{item}.png"
    if not p.exists() or p.stat().st_size < 100:
        return None
    import base64
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


# ═══════════════════════════════════════════════════════════════════════
# 成就（Advancement）数据
# ═══════════════════════════════════════════════════════════════════════

#: 进度显示名(游戏聊天播报的英文标题,带方括号) -> (中文译名, 图标物品 snake_case, 所属 tab)。
#: 由 tools/gen_advancement_display_table.py 从 Advancements.txt + mcData 1.21.11 lang
#: 自动生成，勿手改。中文译名见该脚本 CN_OVERRIDES（未覆盖者暂用英文兜底，补译后重跑）。
#: 全 125 项 = Minecraft16/Nether23/TheEnd9/Adventure47/Husbandry31（lang 缺 ['husbandry.uh_oh']，已跳过）。
_ADV_INFO: dict[str, tuple[str, str, str]] = {
    # Minecraft (16 个)
    '[Minecraft]': ('Minecraft', 'grass_block', 'Minecraft'),
    '[Stone Age]': ('石器时代', 'cobblestone', 'Minecraft'),
    '[Getting an Upgrade]': ('获得升级', 'stone_pickaxe', 'Minecraft'),
    '[Acquire Hardware]': ('获得硬件', 'iron_ingot', 'Minecraft'),
    "[Isn't It Iron Pick]": ('非铁镐莫属', 'iron_pickaxe', 'Minecraft'),
    '[Hot Stuff]': ('热腾腾', 'lava_bucket', 'Minecraft'),
    '[Suit Up]': ('穿上装备', 'iron_chestplate', 'Minecraft'),
    '[Diamonds!]': ('钻石！', 'diamond', 'Minecraft'),
    '[Ice Bucket Challenge]': ('冰桶挑战', 'obsidian', 'Minecraft'),
    '[Not Today, Thank You]': ('今日免矣', 'shield', 'Minecraft'),
    '[Enchanter]': ('附魔师', 'enchanted_book', 'Minecraft'),
    '[Cover Me with Diamonds]': ('钻石护体', 'diamond', 'Minecraft'),
    '[We Need to Go Deeper]': ('我们得再深入点', 'flint_and_steel', 'Minecraft'),
    '[Zombie Doctor]': ('僵尸医生', 'golden_apple', 'Minecraft'),
    '[Eye Spy]': ('间谍之眼', 'eye_of_ender', 'Minecraft'),
    '[The End?]': ('末地？', 'end_stone', 'Minecraft'),
    # Nether (23 个)
    '[Nether]': ('Nether', 'red_nether_bricks', 'Nether'),
    '[Oh Shiny]': ('Oh Shiny', 'gold_ingot', 'Nether'),
    '[Subspace Bubble]': ('Subspace Bubble', 'empty_map', 'Nether'),
    '[Those Were the Days]': ('Those Were the Days', 'polished_blackstone_bricks', 'Nether'),
    '[A Terrible Fortress]': ('A Terrible Fortress', 'nether_bricks', 'Nether'),
    '[Hidden in the Depths]': ('Hidden in the Depths', 'ancient_debris', 'Nether'),
    '[Who is Cutting Onions?]': ('Who is Cutting Onions?', 'crying_obsidian', 'Nether'),
    '[Return to Sender]': ('Return to Sender', 'fire_charge', 'Nether'),
    '[This Boat Has Legs]': ('This Boat Has Legs', 'warped_fungus_on_a_stick', 'Nether'),
    '[War Pigs]': ('War Pigs', 'chest', 'Nether'),
    '[Spooky Scary Skeleton]': ('Spooky Scary Skeleton', 'wither_skeleton_skull', 'Nether'),
    '[Into Fire]': ('Into Fire', 'blaze_rod', 'Nether'),
    '[Cover Me in Debris]': ('Cover Me in Debris', 'netherite_chestplate', 'Nether'),
    '[Not Quite "Nine" Lives]': ('Not Quite "Nine" Lives', 'respawn_anchor', 'Nether'),
    '[Uneasy Alliance]': ('Uneasy Alliance', 'ghast_tear', 'Nether'),
    '[Hot Tourist Destinations]': ('Hot Tourist Destinations', 'netherite_boots', 'Nether'),
    '[Feels Like Home]': ('Feels Like Home', 'warped_fungus_on_a_stick', 'Nether'),
    '[Withering Heights]': ('Withering Heights', 'nether_star', 'Nether'),
    '[Local Brewery]': ('Local Brewery', 'water_bottle', 'Nether'),
    '[Bring Home the Beacon]': ('Bring Home the Beacon', 'beacon', 'Nether'),
    '[A Furious Cocktail]': ('A Furious Cocktail', 'milk_bucket', 'Nether'),
    '[Beaconator]': ('Beaconator', 'beacon', 'Nether'),
    '[How Did We Get Here?]': ('How Did We Get Here?', 'bucket', 'Nether'),
    # The End (9 个)
    '[The End]': ('The End', 'end_stone', 'The End'),
    '[Free the End]': ('Free the End', 'dragon_head', 'The End'),
    '[You Need a Mint]': ('You Need a Mint', 'dragons_breath', 'The End'),
    '[The Next Generation]': ('The Next Generation', 'dragon_egg', 'The End'),
    '[Remote Getaway]': ('Remote Getaway', 'ender_pearl', 'The End'),
    '[The End... Again...]': ('The End... Again...', 'end_crystal', 'The End'),
    '[The City at the End of the Game]': ('The City at the End of the Game', 'purpur_block', 'The End'),
    "[Sky's the Limit]": ("Sky's the Limit", 'elytra', 'The End'),
    '[Great View From Up Here]': ('Great View From Up Here', 'shulker_shell', 'The End'),
    # Adventure (47 个)
    '[Adventure]': ('冒险', 'empty_map', 'Adventure'),
    '[Sneak 100]': ('Sneak 100', 'sculk_sensor', 'Adventure'),
    '[Crafters Crafting Crafters]': ('Crafters Crafting Crafters', 'crafter', 'Adventure'),
    '[Caves & Cliffs]': ('Caves & Cliffs', 'water_bucket', 'Adventure'),
    '[Heart Transplanter]': ('Heart Transplanter', 'creaking_heart', 'Adventure'),
    '[Sticky Situation]': ('Sticky Situation', 'honey_block', 'Adventure'),
    '[Monster Hunter]': ('怪物猎人', 'bone', 'Adventure'),
    '[Surge Protector]': ('Surge Protector', 'lightning_rod', 'Adventure'),
    '[Minecraft: Trial(s) Edition]': ('Minecraft: Trial(s) Edition', 'chiseled_tuff', 'Adventure'),
    "[Ol' Betsy]": ("Ol' Betsy", 'crossbow', 'Adventure'),
    '[The Power of Books]': ('The Power of Books', 'chiseled_bookshelf', 'Adventure'),
    "[Isn't It Scute?]": ("Isn't It Scute?", 'armadillo_scute', 'Adventure'),
    '[Respecting the Remnants]': ('Respecting the Remnants', 'brush', 'Adventure'),
    '[Sweet Dreams]': ('美梦成真', 'white_bed', 'Adventure'),
    '[Is It a Bird?]': ('Is It a Bird?', 'spyglass', 'Adventure'),
    '[What a Deal!]': ('成交！', 'emerald', 'Adventure'),
    '[Crafting a New Look]': ('Crafting a New Look', 'dune_armor_trim', 'Adventure'),
    '[Voluntary Exile]': ('自愿流放', 'white_banner', 'Adventure'),
    '[Country Lode, Take Me Home]': ('Country Lode, Take Me Home', 'lodestone', 'Adventure'),
    '[Monsters Hunted]': ('Monsters Hunted', 'diamond_sword', 'Adventure'),
    '[It Spreads]': ('It Spreads', 'sculk_catalyst', 'Adventure'),
    '[Take Aim]': ('瞄准目标', 'bow', 'Adventure'),
    '[A Throwaway Joke]': ('A Throwaway Joke', 'trident', 'Adventure'),
    '[Postmortal]': ('Postmortal', 'totem_of_undying', 'Adventure'),
    '[Mob Kabob]': ('Mob Kabob', 'iron_spear', 'Adventure'),
    '[Blowback]': ('Blowback', 'wind_charge', 'Adventure'),
    '[Lighten Up]': ('Lighten Up', 'copper_bulb', 'Adventure'),
    '[Over-Overkill]': ('Over-Overkill', 'mace', 'Adventure'),
    '[Under Lock and Key]': ('Under Lock and Key', 'trial_key', 'Adventure'),
    '[Who Needs Rockets?]': ('Who Needs Rockets?', 'wind_charge', 'Adventure'),
    '[Arbalistic]': ('Arbalistic', 'crossbow', 'Adventure'),
    '[Two Birds, One Arrow]': ('Two Birds, One Arrow', 'crossbow', 'Adventure'),
    "[Who's the Pillager Now?]": ("Who's the Pillager Now?", 'crossbow', 'Adventure'),
    '[Careful Restoration]': ('Careful Restoration', 'decorated_pot', 'Adventure'),
    '[Adventuring Time]': ('Adventuring Time', 'diamond_boots', 'Adventure'),
    '[Sound of Music]': ('Sound of Music', 'jukebox', 'Adventure'),
    '[Light as a Rabbit]': ('Light as a Rabbit', 'leather_boots', 'Adventure'),
    '[Is It a Balloon?]': ('Is It a Balloon?', 'spyglass', 'Adventure'),
    '[Hired Help]': ('Hired Help', 'carved_pumpkin', 'Adventure'),
    '[Star Trader]': ('Star Trader', 'emerald', 'Adventure'),
    '[Smithing with Style]': ('Smithing with Style', 'silence_armor_trim', 'Adventure'),
    '[Hero of the Village]': ('Hero of the Village', 'ominous_banner', 'Adventure'),
    '[Bullseye]': ('Bullseye', 'target', 'Adventure'),
    '[Sniper Duel]': ('狙击手对决', 'arrow', 'Adventure'),
    '[Very Very Frightening]': ('Very Very Frightening', 'trident', 'Adventure'),
    '[Is It a Plane?]': ('Is It a Plane?', 'spyglass', 'Adventure'),
    '[Revaulting]': ('Revaulting', 'ominous_trial_key', 'Adventure'),
    # Husbandry (30 个)
    '[Husbandry]': ('农牧业', 'hay_bale', 'Husbandry'),
    "[You've Got a Friend in Me]": ("You've Got a Friend in Me", 'cookie', 'Husbandry'),
    '[The Parrots and the Bats]': ('The Parrots and the Bats', 'wheat', 'Husbandry'),
    '[Fishy Business]': ('钓鱼大业', 'cod', 'Husbandry'),
    '[Glow and Behold!]': ('Glow and Behold!', 'glow_ink_sac', 'Husbandry'),
    '[Smells Interesting]': ('Smells Interesting', 'sniffer_egg', 'Husbandry'),
    '[Stay Hydrated!]': ('Stay Hydrated!', 'dried_ghast', 'Husbandry'),
    '[A Seedy Place]': ('播种之地', 'wheat_seeds', 'Husbandry'),
    '[Whatever Floats Your Goat!]': ('Whatever Floats Your Goat!', 'oak_boat', 'Husbandry'),
    '[Bee Our Guest]': ('Bee Our Guest', 'honey_bottle', 'Husbandry'),
    '[Total Beelocation]': ('Total Beelocation', 'bee_nest', 'Husbandry'),
    '[Bukkit Bukkit]': ('Bukkit Bukkit', 'bucket_of_tadpole', 'Husbandry'),
    '[Best Friends Forever]': ('Best Friends Forever', 'lead', 'Husbandry'),
    '[Birthday Song]': ('Birthday Song', 'note_block', 'Husbandry'),
    '[Two by Two]': ('Two by Two', 'golden_carrot', 'Husbandry'),
    '[Tactical Fishing]': ('Tactical Fishing', 'bucket_of_pufferfish', 'Husbandry'),
    '[Little Sniffs]': ('Little Sniffs', 'torchflower_seeds', 'Husbandry'),
    '[A Balanced Diet]': ('A Balanced Diet', 'apple', 'Husbandry'),
    '[Serious Dedication]': ('Serious Dedication', 'netherite_hoe', 'Husbandry'),
    '[Wax On]': ('Wax On', 'honeycomb', 'Husbandry'),
    '[When the Squad Hops into Town]': ('When the Squad Hops into Town', 'lead', 'Husbandry'),
    '[Good as New]': ('Good as New', 'wolf_armor', 'Husbandry'),
    '[Shear Brilliance]': ('Shear Brilliance', 'shears', 'Husbandry'),
    '[A Complete Catalogue]': ('A Complete Catalogue', 'raw_cod', 'Husbandry'),
    '[The Whole Pack]': ('The Whole Pack', 'bone', 'Husbandry'),
    '[The Cutest Predator]': ('The Cutest Predator', 'bucket_of_axolotl', 'Husbandry'),
    '[Planting the Past]': ('Planting the Past', 'pitcher_pod', 'Husbandry'),
    '[Wax Off]': ('Wax Off', 'stone_axe', 'Husbandry'),
    '[With Our Powers Combined!]': ('With Our Powers Combined!', 'verdant_froglight', 'Husbandry'),
    '[The Healing Power of Friendship!]': ('The Healing Power of Friendship!', 'bucket_of_tropical_fish', 'Husbandry'),
}


# 5 个 tab 的全量进度数（wiki 官方），用于显示覆盖比
_ADV_TAB_TOTAL = {"Minecraft": 16, "Nether": 23, "The End": 9, "Adventure": 47, "Husbandry": 31}
_ADV_TAB_ORDER = ["Minecraft", "Nether", "The End", "Adventure", "Husbandry"]


def _advs_by_arm(res: dict) -> dict[str, set[str]]:
    """{arm: set(成就名)}。"""
    pa = (res.get("train_curve") or [{}])[0].get("per_arm_detail") or {}
    return {a: set(pa.get(a, {}).get("advancements") or []) for a in ARMS}


# ═══════════════════════════════════════════════════════════════════════
# 时序指标（死亡 / XP / 装备）—— matplotlib 生成 PNG → base64 内联
# ═══════════════════════════════════════════════════════════════════════

# 装备材质分：leather=1, gold/chain/copper=2, iron=3, diamond=4, netherite=5
_MAT_SCORE = {"leather": 1, "golden": 2, "gold": 2, "chainmail": 2, "copper": 2,
              "iron": 3, "turtle": 3, "diamond": 4, "netherite": 5}


def _equip_score(eq) -> float:
    """4 槽（头/胸/腿/脚）材质分之和，满分 4×4=16（全钻石）。None/空=0。"""
    if not eq:
        return 0.0
    sc = 0.0
    for slot in ("head", "torso", "legs", "feet"):
        v = eq.get(slot)
        if v and isinstance(v, dict) and v.get("name"):
            for mat, val in _MAT_SCORE.items():
                if v["name"].startswith(mat):
                    sc += val
                    break
    return sc


def _series_mean_std(by_arm: dict[str, list[dict]], key: str, transform=None):
    """逐天对 5 臂取 key 字段（可 transform）的 mean/std。返回 (days, mean, std)。"""
    per_day: dict[int, list[float]] = {}
    for arm in ARMS:
        for row in by_arm.get(arm, []):
            v = row.get(key)
            if v is None:
                continue
            if transform is not None:
                v = transform(v)
            if v is None:
                continue
            per_day.setdefault(row["day_idx"], []).append(float(v))
    days = sorted(per_day)
    mean = [float(sum(per_day[d]) / len(per_day[d])) for d in days]
    std = [float(__import__("statistics").pstdev(per_day[d])) if len(per_day[d]) > 1 else 0.0
           for d in days]
    return days, mean, std


def _png_b64(fig) -> str:
    """matplotlib fig → base64 PNG data URI。"""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    import base64
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def build_timeseries_html(groups: list[Group]) -> str:
    """死亡 / XP / 装备 三联时序图（5 臂 mean±std，N 组对比）→ 内联 PNG。

    实验组蓝实线在上层；各对照组按各自配色虚线在下层（先画后画，后画的盖上层）。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm

    # 中文字体（显式注册 OTF）
    for _p in ("/usr/share/fonts/noto-cjk/NotoSansSC-Regular.otf",):
        if Path(_p).exists():
            fm.fontManager.addfont(_p)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=_p).get_name(), "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    panels = [
        ("n_deaths_so_far", None, "(a) 死亡次数（累计）", "死亡数"),
        ("xp_total", None, "(b) XP（累计）", "XP"),
        ("equipment", _equip_score, "(c) 装备层级（0-16，满=全钻石）", "装备分"),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.6), sharex=True)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.80, bottom=0.13, wspace=0.28)

    for ax, (key, tf, title, ylabel) in zip(axes, panels):
        # 对照组先画（下层），实验组最后画（上层最显眼）
        for g in groups:
            days, mean, std = _series_mean_std(_daily_by_arm(g.res_path), key, tf)
            if not days:
                continue
            ax.fill_between(days, [m - s for m, s in zip(mean, std)],
                            [m + s for m, s in zip(mean, std)], color=g.color,
                            alpha=0.10 if not g.is_exp else 0.13, linewidth=0)
            lw = 2.4 if g.is_exp else 2.0
            ax.plot(days, mean, color=g.color, linestyle=g.linestyle,
                    linewidth=lw, label=g.label)
        ax.set_title(title, fontsize=11, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=10)
        ax.set_xlabel("天", fontsize=10)
        ax.set_xlim(0.5, 30.5)
        ax.set_xticks([1, 5, 10, 15, 20, 25, 30])
        ax.tick_params(labelsize=9)
        ax.grid(True, axis="x", alpha=0.25)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    legend_elems = [
        plt.Line2D([0], [0], color=g.color, linestyle=g.linestyle,
                   linewidth=2.4 if g.is_exp else 2.0,
                   label=f"{g.label}（5 臂 mean ± std）")
        for g in groups
    ]
    fig.legend(handles=legend_elems, loc="upper center", ncol=len(groups),
               bbox_to_anchor=(0.5, 0.995), fontsize=10, frameon=False,
               handlelength=2.4, columnspacing=2.2)

    uri = _png_b64(fig)
    plt.close(fig)

    return (f'<h2 style="margin:24px 18px 6px;font-size:16px;">生存与装备时序 '
            f'<span style="font-weight:normal;color:#666;font-size:12px;">'
            f'5 臂 mean ± std，横轴=天</span></h2>'
            f'<div style="margin:0 18px 18px;border:1px solid #ddd;background:#fff;padding:8px;">'
            f'<img src="{uri}" style="width:100%;max-width:1200px;display:block;margin:0 auto;">'
            f'</div>')


# ═══════════════════════════════════════════════════════════════════════
# 科技树时代时序（tier mean±std）—— 内联 PNG
# ═══════════════════════════════════════════════════════════════════════
# tier 名 → rank：none=0 wooden=1 stone=2 iron=3 diamond=4（旧 4 级口径，老 run 用）
_TIER_RANK = {"none": 0, "wooden": 1, "stone": 2, "iron": 3, "diamond": 4}
_TIER_LABEL = {1: "木", 2: "石", 3: "铁", 4: "钻"}


def _tier_series_mean_std(by_arm: dict[str, list[dict]]):
    """逐天对 5 臂的 tier rank(0-4) 取 mean/std。返回 (days, mean, std)。"""
    import statistics
    per_day: dict[int, list[float]] = {}
    for arm in ARMS:
        for row in by_arm.get(arm, []):
            r = _TIER_RANK.get(row.get("tier"), 0)
            per_day.setdefault(row["day_idx"], []).append(float(r))
    days = sorted(per_day)
    mean = [sum(per_day[d]) / len(per_day[d]) for d in days]
    std = [statistics.pstdev(per_day[d]) if len(per_day[d]) > 1 else 0.0 for d in days]
    return days, mean, std


def _arm_first_diamond(by_arm: dict[str, list[dict]]) -> dict[str, int | None]:
    """{arm: 首次到 diamond 的 day}，用累积 max 推（抵御 tier 回退）。未达成 None。"""
    out = {}
    for arm in ARMS:
        cur = 0
        first = None
        for row in by_arm.get(arm, []):
            r = _TIER_RANK.get(row.get("tier"), 0)
            if r > cur:
                cur = r
                if r == 4:
                    first = row["day_idx"]
        out[arm] = first
    return out


def build_techtree_html(groups: list[Group]) -> str:
    """科技树时代时序图：4 组各一条 tier rank(1-4) 的 5 臂 mean±std 曲线 → 内联 PNG。

    纵轴 木/石/铁/钻，横轴天。实验组蓝实线、各对照组虚线。图例标各组钻石达成率。
    ★ 老口径 tier 按瞬时库存判定会因采样时机回落（非真实退化），故用累积 max 的
    首达钻石统计图例，更公允；曲线本身仍画 daily 当前值以反映波动。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    from matplotlib.lines import Line2D

    for _p in ("/usr/share/fonts/noto-cjk/NotoSansSC-Regular.otf",):
        if Path(_p).exists():
            fm.fontManager.addfont(_p)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=_p).get_name(), "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(12, 5.6))
    fig.subplots_adjust(left=0.08, right=0.97, top=0.84, bottom=0.13)

    for g in groups:
        daily = _daily_by_arm(g.res_path)
        days, mean, std = _tier_series_mean_std(daily)
        if not days:
            continue
        ax.fill_between(days, [m - s for m, s in zip(mean, std)],
                        [m + s for m, s in zip(mean, std)], color=g.color,
                        alpha=0.11 if not g.is_exp else 0.14, linewidth=0)
        ax.plot(days, mean, color=g.color, linestyle=g.linestyle,
                linewidth=2.6 if g.is_exp else 2.1, label=g.label,
                zorder=4 if g.is_exp else 3)

    # 钻石达成率（累积 max，不受回落干扰）→ 图例
    n_dia: dict[str, int] = {}
    for g in groups:
        firsts = _arm_first_diamond(_daily_by_arm(g.res_path))
        n_dia[g.label] = sum(1 for v in firsts.values() if v is not None)

    ax.set_yticks([1, 2, 3, 4])
    ax.set_yticklabels([_TIER_LABEL[i] for i in [1, 2, 3, 4]])
    ax.set_ylim(0.7, 4.45)
    ax.set_xlim(0.5, 30.5)
    ax.set_xticks([1, 5, 10, 15, 20, 25, 30])
    ax.set_ylabel("科技树时代", fontsize=12)
    ax.set_xlabel("天", fontsize=11)
    ax.tick_params(labelsize=10)
    ax.grid(True, alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    # 时代分隔带色背景
    for lo, hi, c in [(0.7, 1.5, "#f5e6c8"), (1.5, 2.5, "#d9d9d9"),
                      (2.5, 3.5, "#cfe2f3"), (3.5, 4.45, "#e6d9f0")]:
        ax.axhspan(lo, hi, color=c, alpha=0.22, zorder=0)
    ax.set_title("科技树时代演进 —— 有共享图谱 vs 无图谱对照（5 臂 mean ± std）",
                 fontsize=13, fontweight="bold", pad=12)

    legend_elems = [
        Line2D([0], [0], color=g.color, linestyle=g.linestyle,
               linewidth=2.6 if g.is_exp else 2.1,
               label=f"{g.label}（{n_dia[g.label]}/5 臂达钻）")
        for g in groups
    ]
    fig.legend(handles=legend_elems, loc="upper center", ncol=len(groups),
               bbox_to_anchor=(0.5, 0.995), fontsize=10, frameon=False,
               handlelength=2.4, columnspacing=1.8)

    uri = _png_b64(fig)
    plt.close(fig)

    note = ("★ 老口径 tier 按瞬时库存判定，会因采样时机（放箱子/空手）回落，非真实退化；"
            "图例的钻石达成率用累积最高值算，不受回落干扰。")
    return (f'<h2 style="margin:24px 18px 6px;font-size:16px;">科技树时代演进 '
            f'<span style="font-weight:normal;color:#666;font-size:12px;">'
            f'5 臂 mean ± std，纵轴=木/石/铁/钻</span></h2>'
            f'<p style="margin:0 18px 6px;font-size:11px;color:#888;">{html.escape(note)}</p>'
            f'<div style="margin:0 18px 18px;border:1px solid #ddd;background:#fff;padding:8px;">'
            f'<img src="{uri}" style="width:100%;max-width:1200px;display:block;margin:0 auto;">'
            f'</div>')


def _draw_combined_ts_fig(groups: list[Group]):
    """画生存(死亡/XP/装备)+科技树(tier) 四联时序图 → 返回 matplotlib fig（调用方负责 close）。

    2×2 子图：(a)死亡 (b)XP (c)装备 (d)科技树。横轴统一=天，顶部一个共享图例
    （4 组各一条，仅组名）。各子图带 fill_between 的 mean±std 阴影。
    供 build_combined_ts_html（内联 PNG）与 experiments/plot_combined_ts.py（独立 PNG 文件）共用。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    from matplotlib.lines import Line2D

    for _p in ("/usr/share/fonts/noto-cjk/NotoSansSC-Regular.otf",):
        if Path(_p).exists():
            fm.fontManager.addfont(_p)
            plt.rcParams["font.sans-serif"] = [fm.FontProperties(fname=_p).get_name(), "DejaVu Sans"]
            break
    plt.rcParams["axes.unicode_minus"] = False

    # (key, transform, title, ylabel, is_tier)
    # 标题末尾箭头标优劣方向：↑=越高越好，↓=越低越好
    panels = [
        ("n_deaths_so_far", None, "(a) 死亡次数（累计）↓", "死亡数", False),
        ("xp_total", None, "(b) XP（累计）↑", "XP", False),
        ("equipment", _equip_score, "(c) 装备层级（0-16，满=全钻石）↑", "装备分", False),
        (None, None, "(d) 科技树时代↑", "时代", True),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), sharex=True)
    fig.subplots_adjust(left=0.07, right=0.97, top=0.88, bottom=0.08, wspace=0.22, hspace=0.20)

    for ax, (key, tf, title, ylabel, is_tier) in zip(axes.flat, panels):
        for g in groups:
            daily = _daily_by_arm(g.res_path)
            if is_tier:
                days, mean, std = _tier_series_mean_std(daily)
            else:
                days, mean, std = _series_mean_std(daily, key, tf)
            if not days:
                continue
            ax.fill_between(days, [m - s for m, s in zip(mean, std)],
                            [m + s for m, s in zip(mean, std)], color=g.color,
                            alpha=0.10 if not g.is_exp else 0.13, linewidth=0)
            ax.plot(days, mean, color=g.color, linestyle=g.linestyle,
                    linewidth=2.4 if g.is_exp else 2.0)
        ax.set_title(title, fontsize=11.5, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=10)
        ax.set_xlabel("天", fontsize=10)
        ax.set_xlim(0.5, 30.5)
        ax.set_xticks([1, 5, 10, 15, 20, 25, 30])
        ax.tick_params(labelsize=9)
        ax.grid(True, axis="x", alpha=0.25)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        # 科技树子图：纵轴改 木/石/铁/钻 + 时代带色背景
        if is_tier:
            ax.set_yticks([1, 2, 3, 4])
            ax.set_yticklabels([_TIER_LABEL[i] for i in [1, 2, 3, 4]])
            ax.set_ylim(0.7, 4.45)
            for lo, hi, c in [(0.7, 1.5, "#f5e6c8"), (1.5, 2.5, "#d9d9d9"),
                              (2.5, 3.5, "#cfe2f3"), (3.5, 4.45, "#e6d9f0")]:
                ax.axhspan(lo, hi, color=c, alpha=0.22, zorder=0)

    # 顶部共享图例（4 组各一条，仅组名）
    legend_elems = [
        Line2D([0], [0], color=g.color, linestyle=g.linestyle,
               linewidth=2.4 if g.is_exp else 2.0, label=g.label)
        for g in groups
    ]
    fig.legend(handles=legend_elems, loc="upper center", ncol=len(groups),
               bbox_to_anchor=(0.5, 0.995), fontsize=10.5, frameon=False,
               handlelength=2.4, columnspacing=2.0)
    return fig


def build_combined_ts_html(groups: list[Group]) -> str:
    """四联时序图 → 内联 PNG，嵌 HTML。画图逻辑见 _draw_combined_ts_fig。"""
    import matplotlib.pyplot as plt
    fig = _draw_combined_ts_fig(groups)
    uri = _png_b64(fig)
    plt.close(fig)

    note = ("★ (d) 科技树：老口径 tier 按瞬时库存判定，会因采样时机（放箱子/空手）回落，"
            "非真实退化；比较各组的钻石达成宜看曾到达的最高时代，而非末态。")
    return (f'<h2 style="margin:24px 18px 6px;font-size:16px;">生存与科技树时序 '
            f'<span style="font-weight:normal;color:#666;font-size:12px;">'
            f'4 组对照，横轴=天</span></h2>'
            f'<p style="margin:0 18px 6px;font-size:11px;color:#888;">{html.escape(note)}</p>'
            f'<div style="margin:0 18px 18px;border:1px solid #ddd;background:#fff;padding:8px;">'
            f'<img src="{uri}" style="width:100%;max-width:1200px;display:block;margin:0 auto;">'
            f'</div>')


def build_adv_html(groups: list[Group], icon_dir: Path) -> str:
    """成就表：按 Minecraft 5 个 tab 分组，显示覆盖比（达成/总数）。N 组对比。"""
    all_advs = sorted(set().union(*[set().union(*g.advs_by_arm.values()) for g in groups]))

    # 按 tab 分组成就（用 _ADV_INFO 的 tab；未收录的归"其他"）
    by_tab: dict[str, list[str]] = {t: [] for t in _ADV_TAB_ORDER}
    by_tab.setdefault("其他", [])
    for adv in all_advs:
        tab = _ADV_INFO.get(adv, ("", "", "其他"))[2]
        by_tab.setdefault(tab, []).append(adv)
    # 保留有成就的 tab + 未触及但全量>0 的 tab（显示 0/N）
    tab_groups = []
    for t in _ADV_TAB_ORDER:
        advs_t = by_tab.get(t, [])
        if advs_t or _ADV_TAB_TOTAL.get(t, 0) > 0:
            tab_groups.append((t, advs_t, _ADV_TAB_TOTAL.get(t, 0)))
    if by_tab.get("其他"):
        tab_groups.append(("其他", by_tab["其他"], len(by_tab["其他"])))

    # ── 表头两行：第1行 tab 分组带（带覆盖比），第2行图标+中文名 ──
    head_rows = []
    r1 = ['<th class="col1" rowspan="2">组别</th>',
          '<th class="grp-th" rowspan="2">角色</th>',
          '<th class="grp-th" rowspan="2">成就数</th>']
    for tab, advs_t, total in tab_groups:
        n = len(advs_t)
        label = f"{tab} ({n}/{total})" if total else f"{tab} ({n})"
        r1.append(f'<th class="grp-th" colspan="{max(len(advs_t),1)}" title="{html.escape(tab)}">'
                  f'<span>{html.escape(label)}</span></th>')
    head_rows.append("<tr>" + "".join(r1) + "</tr>")
    r2 = []
    for tab, advs_t, total in tab_groups:
        if advs_t:
            for adv in advs_t:
                cn, icon_item, _ = _ADV_INFO.get(adv, (adv.strip("[]"), "stick", tab))
                uri = _icon_data_uri(icon_item, icon_dir)
                img = (f'<img src="{uri}" alt="{html.escape(adv)}">' if uri else "")
                r2.append(f'<th class="sortable" title="{html.escape(adv)}" '
                          f'data-item="{html.escape(adv)}" data-cat="{html.escape(tab)}">{img}'
                          f'<span class="cn">{html.escape(cn)}</span></th>')
        else:
            # 未触及的 tab：占位列头标 "—"
            r2.append(f'<th class="sortable" data-cat="{html.escape(tab)}" '
                      f'style="background:#f0f0f0;color:#aaa;">未触及</th>')
    head_rows.append("<tr>" + "".join(r2) + "</tr>")

    # ── 表体：每段 = 一个 group（5 臂），首臂带组标签（按 group.color 着色）──
    body_rows = []
    for gi, g in enumerate(groups):
        for ai, arm in enumerate(ARMS):
            advs = g.advs_by_arm[arm]
            cells = []
            if ai == 0:
                cells.append(f'<th class="row1" rowspan="5"><span class="{g.css_cls}" '
                             f'style="display:inline-block;padding:3px 8px;border-radius:3px;'
                             f'background:{g.color};">{html.escape(g.label)}</span></th>')
            cells.append(f'<th class="row2">{html.escape(arm)}</th>')
            cells.append(f'<td class="count">{len(advs)}</td>')
            for tab, advs_t, total in tab_groups:
                if advs_t:
                    for adv in advs_t:
                        has = adv in advs
                        if has:
                            cells.append(f'<td class="has" data-has="1" '
                                         f'data-item="{html.escape(adv)}" '
                                         f'style="background:{g.color};">✓</td>')
                        else:
                            cells.append(f'<td class="no" data-has="0" '
                                         f'data-item="{html.escape(adv)}"></td>')
                else:
                    cells.append('<td class="no" style="background:#f7f7f7;"></td>')
            cls_extra = " grp-start" if (gi > 0 and ai == 0) else ""
            body_rows.append(f'<tr class="{cls_extra.strip()}">' + "".join(cells) + "</tr>")

    table_html = ('<table id="adv-tbl"><thead>' + "".join(head_rows) + '</thead><tbody>'
                  + "".join(body_rows) + '</tbody></table>')

    n_reached = len(all_advs)
    n_total = sum(_ADV_TAB_TOTAL.values())  # 126
    return (f'<h2 style="margin:24px 18px 6px;font-size:16px;">进度（Advancement）达成清单 '
            f'<span style="font-weight:normal;color:#666;font-size:12px;">'
            f'覆盖 {n_reached}/{n_total} = {n_reached/n_total:.0%}，按 Minecraft 5 个 tab 分组</span></h2>'
            f'<p style="margin:0 18px 8px;font-size:11px;color:#888;">'
            f'Java 版共 126 项进度（Minecraft 16 / Nether 23 / The End 9 / Adventure 47 / Husbandry 31）。'
            f'agent 触及 Minecraft 与 Adventure 两 tab 的早期进度，下界/末地 tab 零覆盖。</p>'
            f'<div class="wrap">{table_html}</div>')


def build_html(groups: list[Group], icon_dir: Path) -> str:
    all_items = sorted(set().union(*[set().union(*g.items_by_arm.values()) for g in groups]))
    # 按类别分组（用 _CAT_ITEMS 的精确集合，保留其顺序），组内按中文名排序
    by_cat: dict[str, list[str]] = {cat: [] for cat, _ in _CAT_ITEMS}
    by_cat.setdefault("其他", [])
    for it in all_items:
        by_cat.setdefault(_category_of(it), []).append(it)
    cat_order = [c for c, _ in _CAT_ITEMS if by_cat.get(c)] + (
        ["其他"] if by_cat.get("其他") else [])
    # 组内按中文名排序
    grouped = [(c, sorted(by_cat[c], key=lambda it: (_cn_name(it), it))) for c in cat_order]

    # ── CSS ──
    css = """
    <style>
      body { font-family: -apple-system, "Segoe UI", Roboto, "Noto Sans CJK SC", sans-serif;
             margin: 0; background: #f5f6f8; color: #222; }
      h1 { font-size: 18px; margin: 14px 18px 6px; }
      .meta { font-size: 12px; color: #666; margin: 0 18px 14px; }
      .toolbar { margin: 0 18px 12px; font-size: 12px; }
      .toolbar button { margin-right: 6px; padding: 4px 10px; border: 1px solid #bbb;
        border-radius: 4px; background: #fff; cursor: pointer; font-size: 12px; }
      .toolbar button.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
      .wrap { overflow: auto; max-height: 80vh; border: 1px solid #ddd; margin: 0 18px 18px;
              background: #fff; }
      table { border-collapse: collapse; font-size: 12px; }
      th, td { border: 1px solid #e3e3e3; padding: 4px 6px; white-space: nowrap; }
      thead th { position: sticky; top: 0; z-index: 3; background: #eef1f5; font-weight: 600; }
      .grp-th { position: sticky; top: 0; background: #dfe6ef; border-bottom: 2px solid #9bb; }
      .grp-th span { display: inline-block; width: 100%; text-align: center; font-size: 11px; }
      tbody th.row1 { position: sticky; left: 0; z-index: 2; background: #f7f9fc;
                      font-weight: 600; text-align: left; min-width: 96px; }
      tbody th.row2 { position: sticky; left: 0; z-index: 1; background: #fff;
                      text-align: left; font-weight: normal; color: #555; }
      thead th.col1 { position: sticky; left: 0; top: 0; z-index: 4; background: #dfe6ef; }
      td.has { color: #fff; text-align: center; font-weight: 600; }
      td.no { background: #f3f4f6; color: #d0d3d8; text-align: center; }
      /* 组间分隔：每组第一臂（非首组）的上边框加粗 */
      tbody tr.grp-start td, tbody tr.grp-start th { border-top: 2px solid #888; }
      td.count { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }
      th.sortable { cursor: pointer; user-select: none; text-align: center;
                    vertical-align: bottom; padding: 4px 2px; }
      th.sortable:hover { background: #d0d8e2; }
      th.sortable img { width: 28px; height: 28px; display: block; margin: 0 auto 2px;
                        image-rendering: pixelated; }
      th.sortable .cn { font-size: 11px; line-height: 1.1; max-width: 56px;
                        overflow: hidden; text-overflow: ellipsis; display: block; }
      .cat-band { height: 3px; }
    </style>
    """

    # ── 表头两行：第1行类别带，第2行图标+中文名 ──
    # 表体每行前3格 = [组标签(rowspan5)] [角色名] [物品数]，表头须3个 rowspan=2 格一一对应
    head_rows = []
    # 第1行：类别分组带（colspan）
    r1 = ['<th class="col1" rowspan="2">组别</th>',
          '<th class="grp-th" rowspan="2">角色</th>',
          '<th class="grp-th" rowspan="2">物品数</th>']
    for cat, items in grouped:
        r1.append(f'<th class="grp-th" colspan="{len(items)}" title="{html.escape(cat)}">'
                  f'<span>{html.escape(cat)}</span></th>')
    head_rows.append("<tr>" + "".join(r1) + "</tr>")
    # 第2行：图标 + 中文名（可排序）
    r2 = []
    for cat, items in grouped:
        for it in items:
            cn = html.escape(_cn_name(it))
            uri = _icon_data_uri(it, icon_dir)
            img = (f'<img src="{uri}" alt="{html.escape(it)}" '
                   f'title="{html.escape(it)}">' if uri else "")
            r2.append(f'<th class="sortable" title="{html.escape(it)}&#10;{html.escape(it)}" '
                      f'data-item="{html.escape(it)}" data-cat="{html.escape(cat)}">'
                      f'{img}<span class="cn">{cn}</span></th>')
    head_rows.append("<tr>" + "".join(r2) + "</tr>")

    # ── 表体：每个 group 一段（5 臂），首臂带组标签（按 group.color 着色）──
    # ✓ 格用 inline background:{g.color}，故 N 组各自配色无需预定义 CSS 类。
    body_rows = []
    for gi, g in enumerate(groups):
        for ai, arm in enumerate(ARMS):
            items = g.items_by_arm[arm]
            cells = []
            if ai == 0:
                cells.append(f'<th class="row1" rowspan="5"><span class="{g.css_cls}" '
                             f'style="display:inline-block;padding:3px 8px;border-radius:3px;'
                             f'background:{g.color};">{html.escape(g.label)}</span></th>')
            cells.append(f'<th class="row2">{html.escape(arm)}</th>')
            cells.append(f'<td class="count">{len(items)}</td>')
            for cat, items_list in grouped:
                for it in items_list:
                    has = it in items
                    if has:
                        cells.append(f'<td class="has" data-has="1" '
                                     f'data-item="{html.escape(it)}" '
                                     f'style="background:{g.color};">✓</td>')
                    else:
                        cells.append(f'<td class="no" data-has="0" '
                                     f'data-item="{html.escape(it)}"></td>')
            cls_extra = " grp-start" if (gi > 0 and ai == 0) else ""
            body_rows.append(f'<tr class="{cls_extra.strip()}">' + "".join(cells) + "</tr>")

    # ── 工具栏：分组筛选按钮 + 排序说明 ──
    filter_btns = ['<button class="active" data-filter="all">全部</button>']
    for cat, _ in grouped:
        filter_btns.append(f'<button data-filter="{html.escape(cat)}">{html.escape(cat)}</button>')

    toolbar = ('<div class="toolbar">'
               '按类别筛选：' + "".join(filter_btns) +
               '<span style="margin-left:16px;color:#888;">点击列头可排序（按获得该物品的臂数）。</span></div>')

    table_html = (
        '<table id="tbl"><thead>' + "".join(head_rows) + '</thead><tbody>'
        + "".join(body_rows) + '</tbody></table>'
    )

    # ── JS：列排序（按"获得该物品的臂数"降序）+ 分组筛选 ──
    js = """
    <script>
    const tbl = document.getElementById('tbl');
    const head2 = document.querySelector('#tbl thead tr:nth-child(2)');
    const sortHeads = () => Array.from(head2.querySelectorAll('th.sortable'));
    const dataRows = () => Array.from(tbl.querySelectorAll('tbody tr'));

    // 计算"获得该物品的总臂数"
    function itemCount(item) {
      let n = 0;
      dataRows().forEach(r => {
        const c = r.querySelector('td[data-item="'+CSS.escape(item)+'"]');
        if (c && c.dataset.has === '1') n++;
      });
      return n;
    }

    // 重排列：按 order（物品名数组）重排表头 + 每行数据格
    function reorderCols(order) {
      // 表头：保留非 sortable（Group/Arm/Items），追加 sortable 按 order
      const sticky = Array.from(head2.children).filter(h => !h.classList.contains('sortable'));
      const byItem = {};
      sortHeads().forEach(h => byItem[h.dataset.item] = h);
      head2.innerHTML = '';
      sticky.forEach(h => head2.appendChild(h));
      order.forEach(it => head2.appendChild(byItem[it]));
      // 每行：保留非 data-item 格，追加 data-item 格按 order
      dataRows().forEach(r => {
        const lead = Array.from(r.children).filter(c => !c.hasAttribute('data-item'));
        const byItem = {};
        r.querySelectorAll('td[data-item]').forEach(c => byItem[c.dataset.item] = c);
        r.innerHTML = '';
        lead.forEach(c => r.appendChild(c));
        order.forEach(it => r.appendChild(byItem[it]));
      });
    }

    sortHeads().forEach(th => {
      th.addEventListener('click', () => {
        const asc = th.dataset.dir === 'asc';
        th.dataset.dir = asc ? 'desc' : 'asc';
        const items = sortHeads().map(h => h.dataset.item);
        const cnt = {}; items.forEach(it => cnt[it] = itemCount(it));
        const order = items.sort((a, b) =>
          (asc ? cnt[a]-cnt[b] : cnt[b]-cnt[a]) || a.localeCompare(b));
        reorderCols(order);
        // 标记排序方向：用独立 mark 元素，不碰 textContent（否则图标丢失）
        document.querySelectorAll('th.sortable .sortmark').forEach(m => m.remove());
        const mk = document.createElement('span');
        mk.className = 'sortmark';
        mk.textContent = asc ? ' ↑' : ' ↓';
        mk.style.color = '#1f6feb';
        th.appendChild(mk);
      });
    });

    // 分组筛选：按类别显示/隐藏列（用 data-cat on th + data-item 关联）
    document.querySelectorAll('.toolbar button').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.toolbar button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        // 物品→类别 映射（从表头取）
        const catOf = {};
        sortHeads().forEach(h => catOf[h.dataset.item] = h.dataset.cat);
        sortHeads().forEach(h => {
          const show = (f === 'all') || (h.dataset.cat === f);
          h.style.display = show ? '' : 'none';
        });
        dataRows().forEach(r => {
          r.querySelectorAll('td[data-item]').forEach(c => {
            const show = (f === 'all') || (catOf[c.dataset.item] === f);
            c.style.display = show ? '' : 'none';
          });
        });
        // 类别分组带（第1行 colspan）：整组隐藏则隐藏带
        document.querySelectorAll('#tbl thead tr:first-child .grp-th[colspan]').forEach(g => {
          const cat = g.textContent.trim();
          const anyVisible = sortHeads().some(h => h.dataset.cat === cat && h.style.display !== 'none');
          g.style.display = (f === 'all' || anyVisible) ? '' : 'none';
        });
      });
    });
    </script>
    """

    n_groups = len(groups)
    title = "物品获取与成就达成清单 —— 有共享图谱 vs 无图谱对照"
    n_roles = n_groups * len(ARMS)
    runs_desc = " · ".join(f"{g.label} run {g.run_ts}" for g in groups)
    meta = (f"{n_roles} 个角色（{n_groups} 组 × {len(ARMS)} 臂）。"
            f"✓ = 该角色至少获得过该物品 / 达成该成就，颜色区分组别。"
            f"{runs_desc}。"
            f"物品表可横向滚动、按类别筛选、点击列头排序。")

    # 成就表 + 合并时序图(生存+科技树)：只要任意组带数据就渲染
    adv_section = build_adv_html(groups, icon_dir)
    ts_section = build_combined_ts_html(groups)

    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(title)}</title>{css}</head><body>"
            f"<h1>{html.escape(title)}</h1>"
            f"<p class='meta'>{html.escape(meta)}</p>"
            f"{toolbar}<div class='wrap'>{table_html}</div>"
            f"{ts_section}{adv_section}{js}</body></html>")


def _model_label(res: dict) -> str:
    """从 config.model.task 抽取简短模型名，用作对照组成员标签。
    DeepSeek-V4-Flash → DSV4-Flash；glm-5.3 → GLM-5.3；hy3 → HY3。"""
    m = str(((res.get("config") or {}).get("model") or {}).get("task") or "").lower()
    if "deepseek" in m and "v4" in m:
        return "DSV4-Flash"
    if "glm" in m:
        # glm-5.3 → GLM-5.3：取 glm 之后的部分，去首尾连字符
        ver = m.split("glm", 1)[-1].split("-", 1)[0].strip("-") or m.split("glm", 1)[-1].strip("-")
        return f"GLM-{ver}" if ver else "GLM"
    if "hy3" in m:
        return "HY3"
    return m or "unknown"


def _make_group(label: str, sub: str, is_exp: bool, color: str, linestyle: str,
                res: dict, res_path: Path = None) -> Group:
    return Group(
        label=label, sub=sub, is_exp=is_exp, color=color, linestyle=linestyle,
        run_ts=str(res.get("run_ts") or "unknown"),
        items_by_arm=_items_by_arm(res),
        advs_by_arm=_advs_by_arm(res),
        res=res,
        res_path=res_path,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="物品清单交互式 HTML 表格（1 实验组 + N 对照组）")
    ap.add_argument("--exp", required=True, help="实验组(有图谱) result JSON")
    ap.add_argument("--ctl", required=True, nargs="+",
                    help="对照组(无图谱) result JSON，可多个")
    ap.add_argument("--ctl-label", default=None,
                    help="对照组显示名，逗号分隔，按 --ctl 顺序；缺省自动从 config.model 推断")
    ap.add_argument("--out", default=None, help="输出 HTML 路径；缺省 results/item_table.html")
    ap.add_argument("--icon-dir", default=None,
                    help="物品贴图目录；缺省 results/item_icons（无贴图则不显示图标）")
    args = ap.parse_args()

    exp_path = Path(args.exp)
    if not exp_path.exists():
        sys.exit(f"实验组 JSON 不存在: {exp_path}")
    ctl_paths = [Path(p) for p in args.ctl]
    for p in ctl_paths:
        if not p.exists():
            sys.exit(f"对照组 JSON 不存在: {p}")
    if len(ctl_paths) > len(_CTL_PALETTE):
        sys.exit(f"对照组最多 {len(_CTL_PALETTE)} 个，当前传了 {len(ctl_paths)} 个")

    icon_dir = Path(args.icon_dir) if args.icon_dir else exp_path.parent.parent / "item_icons"
    out = Path(args.out) if args.out else exp_path.parent.parent / "item_table.html"

    exp_res = json.loads(exp_path.read_text(encoding="utf-8"))
    ctl_res_list = [json.loads(p.read_text(encoding="utf-8")) for p in ctl_paths]

    # ── 组装 groups：实验组蓝实线 + 各对照组按 palette 虚线 ──
    # label = 模型名 + 图谱条件，让同模型有/无图谱的对照一目了然
    groups: list[Group] = [_make_group(f"{_model_label(exp_res)} w/ 共享图谱",
                                       "w/ graph", True, OURS_COLOR, "-", exp_res,
                                       exp_path)]
    ctl_labels = (args.ctl_label.split(",") if args.ctl_label
                  else [_model_label(r) for r in ctl_res_list])
    if len(ctl_labels) != len(ctl_res_list):
        sys.exit(f"--ctl-label 数({len(ctl_labels)})与 --ctl 数({len(ctl_res_list)})不符")
    for i, (label, res, res_p) in enumerate(zip(ctl_labels, ctl_res_list, ctl_paths)):
        groups.append(_make_group(f"{label} w/o 共享图谱",
                                  "w/o graph", False, _CTL_PALETTE[i], "--", res,
                                  res_p))

    html_doc = build_html(groups, icon_dir)
    out.write_text(html_doc, encoding="utf-8")

    # 控制台摘要
    all_items = sorted(set().union(*[set().union(*g.items_by_arm.values()) for g in groups]))
    n_icons = sum(1 for it in all_items if (icon_dir / f"{it}.png").exists())
    all_advs = sorted(set().union(*[set().union(*g.advs_by_arm.values()) for g in groups]))
    print(f"\n✓ 物品+成就清单表已生成: {out}")
    print(f"  物品: {len(all_items)} × {len(groups)*len(ARMS)} 臂，{n_icons} 个有贴图")
    print(f"  成就: {len(all_advs)} × {len(groups)*len(ARMS)} 臂")
    for g in groups:
        per = [f"{a}={len(g.items_by_arm[a])}物/{len(g.advs_by_arm[a])}成" for a in ARMS]
        tag = "实验组" if g.is_exp else "对照组"
        print(f"  [{tag}] {g.label}(run {g.run_ts}): " + "  ".join(per))


if __name__ == "__main__":
    main()

"""adapted Voyager 的六个 prompt（课程 / 代码生成 / 函数描述 / 自验证）。

来源：arXiv:2305.16291 附录 A（Prompt 1/4/5/6；Prompt 2/3 是省略的 Wiki QA 模块）。
原文英文照抄进 SYSTEM_* 常量——**不意译**：这是外部基线，prompt 是它的「方法本体」，
改写等于改方法。适配只发生在三处，全部在拼接层（voyager_agent.py）而非措辞层：
  ① 控制原语 API 段 → 本仓裸 mineflayer API 速查表（0827 全表统一口径，见 run_voyager.py
     适配清单；原文那 16 个 control primitives 是它的「预封装脚手架」，与本仓全表口径冲突，
     用户定 2026-10-01：裸接口，表内归因最干净）；
  ② 状态段 → state_adapter.assemble_state_block（warm-up 课表门控）；
  ③ 温度：课程 0.1、其余 0（原文 §3.1，per-call config 透传，见 voyager_agent.py）。

关于语言：动作代码生成用英文 prompt（与原文一致，产出 JS 代码）；课程的响应格式
（Reasoning/Task）与自验证的 JSON 格式按原文。**不**像本仓 OpenKAGE 域那样写中文 prompt
——那是我们的方法设计，不该传染给基线。
"""

from __future__ import annotations

# ── Prompt 1：自动课程（原文 A.3.4，temperature=0.1）──────────────────────────
# 逐字照抄，包括「Do not ask me to build or dig shelter even if it's at night」这类
# 看似可优化的措辞——它是 Voyager 课程行为的一部分，改了就不是 Voyager。
CURRICULUM_SYSTEM = """You are a helpful assistant that tells me the next immediate task to do in Minecraft. My ultimate goal is to discover as many diverse things as possible, accomplish as many diverse tasks as possible and become the best Minecraft player in the world.

I will give you the following information:
Biome: ...
Time: ...
Nearby blocks: ...
Other blocks that are recently seen: ...
Nearby entities (nearest to farthest): ...
Health: Higher than 15 means I'm healthy.
Hunger: Higher than 15 means I'm not hungry.
Position: ...
Equipment: If I have better armor in my inventory, you should ask me to equip it.
Inventory (xx/36): ...
Chests: You can ask me to deposit or take items from these chests. There also might be some unknown chest, you should ask me to open and check items inside the unknown chest.
Completed tasks so far: ...
Failed tasks that are too hard: ...

You must follow the following criteria:
1) You should act as a mentor and guide me to the next task based on my current learning progress.
2) Please be very specific about what resources I need to collect, what I need to craft, or what mobs I need to kill.
3) The next task should follow a concise format, such as "Mine [quantity] [block]", "Craft [quantity] [item]", "Smelt [quantity] [item]", "Kill [quantity] [mob]", "Cook [quantity] [food]", "Equip [item]" etc. It should be a single phrase. Do not propose multiple tasks at the same time. Do not mention anything else.
4) The next task should not be too hard since I may not have the necessary resources or have learned enough skills to complete it yet.
5) The next task should be novel and interesting. I should look for rare resources, upgrade my equipment and tools using better materials, and discover new things. I should not be doing the same thing over and over again.
6) I may sometimes need to repeat some tasks if I need to collect more resources to complete more difficult tasks. Only repeat tasks if necessary.
7) Do not ask me to build or dig shelter even if it's at night. I want to explore the world and discover new things. I don't want to stay in one place.
8) Tasks that require information beyond the player's status to verify should be avoided. For instance, "Placing 4 torches" and "Dig a 2x1x2 hole" are not ideal since they require visual confirmation from the screen. All the placing, building, planting, and trading tasks should be avoided. Do not propose task starting with these keywords.

You should only respond in the format as described below:
RESPONSE FORMAT:
Reasoning: Based on the information I listed above, do reasoning about what the next task should be.
Task: The next task.

Here's an example response:
Reasoning: The inventory is empty now, chop down a tree to get some wood.
Task: Obtain a wood log."""


#: 2026-10-03 公平性对齐：30夜开放探索协议下，任务选择层的长期目标必须与 OpenKAGE
#: 完全同语义。这里直接使用 react.yaml 的中文原文，避免翻译漂移；同时移除原版课程中
#: “不要 build/dig shelter”的偏向（它与协议目标中的“建造”冲突）。响应格式与具体性、
#: 可验证性等课程约束仍保留 Voyager 机制。论文附录需列为 adapted Voyager 的协议对齐项。
PROTOCOL_MISSION_ZH = """你在这个世界里围绕**生存 / 探索 / 建造**推进三个长期目标：

**生存**：维持生命、储备资源、规避危险、延长存活
**探索**：探索未知、勇于冒险、开拓视野、持续升级
**建造**：营建据点、立台设炉、安营扎寨、成就家园"""

CURRICULUM_SYSTEM_ALIGNED = PROTOCOL_MISSION_ZH + """

You are the curriculum module of an adapted Voyager agent. Propose the next immediate Minecraft task that serves the protocol mission above.

I will give you the following information:
Biome: ...
Time: ...
Nearby blocks: ...
Nearby entities (nearest to farthest): ...
Health: Higher than 15 means I'm healthy.
Hunger: Higher than 15 means I'm not hungry.
Position: ...
Equipment: If I have better armor in my inventory, you should ask me to equip it.
Inventory (xx/36): ...
Chests: You can ask me to deposit or take items from these chests. There also might be some unknown chest, you should ask me to open and check items inside the unknown chest.
Completed tasks so far: ...
Failed tasks that are too hard: ...

You must follow the following criteria:
1) Act as a mentor and guide me to the next task based on my current learning progress and the protocol mission.
2) Be very specific about what resources I need to collect, craft, build, or smelt, or which mobs I need to handle.
3) The next task should follow a concise format, such as "Mine [quantity] [block]", "Craft [quantity] [item]", "Smelt [quantity] [item]", "Build [quantity] [block/item]", "Kill [quantity] [mob]", "Cook [quantity] [food]", or "Equip [item]". It should be a single English phrase. Do not propose multiple tasks at the same time.
4) The next task should not be too hard since I may not have the necessary resources or have learned enough skills to complete it yet.
5) Prefer novel and useful progression: rare resources, better equipment and tools, safer logistics, or settlement construction. Do not repeat old work unnecessarily.
6) I may sometimes need to repeat tasks if I need to collect more resources to complete more difficult tasks.
7) Prefer tasks whose success can be verified from inventory, nearby blocks, status, or an advancement. Avoid tasks that require information beyond the player's available observation to verify.

You should only respond in the format as described below:
RESPONSE FORMAT:
Reasoning: Based on the information I listed above, do reasoning about the next task.
Task: The next task in English.

Here's an example response:
Reasoning: The inventory is empty now; obtaining wood supports both survival and construction.
Task: Obtain a wood log."""

# ── Prompt 4：代码生成（原文 A.4.2，temperature=0）───────────────────────────
# {programs_doc} 是适配点①：原文此处是 16 个 control primitives 的源码+签名，本仓换成
# OpenKAGE react.yaml 的 API/Goal 速查表原样提取。
# 不做 Voyager 风格改写、不精简、不翻译——事实性 API 文档必须与 OpenKAGE 完全同版。
ACTION_SYSTEM_TEMPLATE = """You are a helpful assistant that writes Mineflayer javascript code to complete any Minecraft task specified by me.

Here are all the APIs I can use in the Mineflayer javascript package:
{programs_doc}

At each round of conversation, I will give you
Code from the last round: ...
Execution error: ...
Chat log: ...
Biome: ...
Time: ...
Nearby blocks: ...
Nearby entities (nearest to farthest): ...
Health: ...
Hunger: ...
Position: ...
Equipment: ...
Inventory (xx/36): ...
Chests: ...
Task: ...
Context: ...
Critique: ...

You should then respond to me with
Explain (if applicable): Are there any steps missing in your plan? Why does the code not complete the task? What does the chat log and execution error imply?
Plan: How to complete the task step by step. You should pay attention to Inventory since it tells what you have. The task completeness check is also based on your final inventory.
Code:
1) Write an async function taking the bot as the only argument.
2) Reuse the above useful programs as much as possible.
3) Your function will be reused for building more complex functions. Therefore, you should make it generic and reusable. You should not make strong assumption about the inventory (as it may be changed at a later time), and therefore you should always check whether you have the required items before using them. If not, you should first collect the required items and reuse the above useful programs.
4) Functions in the "Code from the last round" section will not be saved or executed. Do not reuse functions listed there.
5) Anything defined outside a function will be ignored, define all your variables inside your functions.
6) Call `bot.chat` to show the intermediate progress.
7) When you cannot find something nearby (a block or a mob), first walk some distance in a random direction and search again — repeat with different directions instead of staying in one place.
8) `maxDistance` should always be 32 for `bot.findBlocks` and `bot.findBlock`. Do not cheat.
9) Do not write infinite loops or recursive functions.
10) Do not use `bot.on` or `bot.once` to register event listeners. You definitely do not need them.
11) Name your function in a meaningful way (can infer the task from the name).

You should only respond in the format as described below:
RESPONSE FORMAT:
Explain: ...
Plan:
1) ...
2) ...
3) ...
...
Code:
```javascript
// helper functions (only if needed, try to avoid them)
...

// main function after the helper functions
async function yourMainFunctionName(bot) {
  // ...
}
```"""

# ── Prompt 5：函数描述（原文 A.4.2 尾部，技能入库时的 key 生成，temperature=0）──
SKILL_DESC_SYSTEM = """You are a helpful assistant that writes a description of the given function written in Mineflayer javascript code.
1) Do not mention the function name.
2) Do not mention anything about `bot.chat` or helper functions.
3) There might be some helper functions before the main function, but you only need to describe the main function.
4) Try to summarize the function in no more than 6 sentences.
5) Your response should be a single line of text.

For example, if the function is:

async function mineCobblestone(bot) {
  ...
}

The main function is `mineCobblestone`.

Then you would write:
The function is about mining 8 cobblestones using a wooden pickaxe. First check if a wooden pickaxe is in the inventory. If not, craft one. If the wooden pickaxe is available, equip the wooden pickaxe in the hand. Next, explore the environment until finding a stone block. Once a stone block is found, mine a total of 8 cobblestone blocks using the wooden pickaxe."""

# ── Prompt 6：自验证（原文 A.5.2，temperature=0）─────────────────────────────
CRITIC_SYSTEM = """You are an assistant that assesses my progress of playing Minecraft and provides useful guidance.
You are required to evaluate if I have met the task requirements.
Exceeding the task requirements is also considered a success while failing to meet them requires you to provide critique to help me improve.
I will give you the following information:
Biome: The biome after the task execution.
Time: The current time.
Nearby blocks: The surrounding blocks. These blocks are not collected yet. However, this is useful for some placing or planting tasks.
Health: My current health.
Hunger: My current hunger level. For eating task, if my hunger level is 20.0, then I successfully ate the food.
Position: My current position.
Equipment: My final equipment. For crafting tasks, I sometimes equip the crafted item.
Inventory (xx/36): My final inventory. For mining and smelting tasks, you only need to check inventory.
Chests: If the task requires me to place items in a chest, you can find chest information here.
Task: The objective I need to accomplish.
Context: The context of the task.
You should only respond in JSON format as described below:
{{
"reasoning": "reasoning",
"success": boolean,
"critique": "critique",
}}
Ensure the response can be parsed by Python 'json.loads', e.g.: no trailing commas, no single quotes, etc.
Here are some examples:
INPUT:
Inventory (2/36): {{'oak_log':2, 'spruce_log':2}}
Task: Mine 3 wood logs

RESPONSE:
{{
    "reasoning": "You need to mine 3 wood logs. You have 2 oak logs and 2 spruce logs, which add up to 4 wood logs.",
    "success": true,
    "critique": ""
}}
INPUT:
Inventory (3/36): {{'crafting_table': 1, 'spruce_planks': 6, 'stick': 4}}
Task: Craft a wooden pickaxe

RESPONSE:
{{
    "reasoning": "You have enough materials to craft a wooden pickaxe, but you didn't craft it.",
    "success": false,
    "critique": "Craft a wooden pickaxe with a crafting table using 3 spruce planks and 2 sticks."
}}
INPUT:
Inventory (2/36): {{'raw_iron': 5, 'stone_pickaxe': 1}}
Task: Mine 5 iron_ore

RESPONSE:
{{
    "reasoning": "Mining iron_ore in Minecraft will get raw_iron. You have 5 raw_iron in your inventory.",
    "success": true,
    "critique": ""
}}
INPUT:
Biome: plains
Nearby blocks: stone, dirt, grass_block, grass, farmland, wheat
Inventory (26/36): ...
Task: Plant 1 wheat seed.

RESPONSE:
{{
    "reasoning": "For planting tasks, inventory information is useless. In nearby blocks, there is farmland and wheat, which means you succeed to plant the wheat seed.",
    "success": true,
    "critique": ""
}}
INPUT:
Inventory (11/36): {{..., 'rotten_flesh': 1}}
Task: Kill 1 zombie

Context: ...

RESPONSE:
{{
    "reasoning": "You have rotten flesh in your inventory, which means you successfully killed one zombie.",
    "success": true,
    "critique": ""
}}
INPUT:
Hunger: 20.0/20.0

Inventory (11/36): ...

Task: Eat 1 ...

Context: ...

RESPONSE
{{
    "reasoning": "For all eating task, if the player's hunger is 20.0, then the player successfully ate the food.",
    "success": true,
    "critique": ""
}}"""


def programs_doc_for_bare_interface() -> str:
    """直接提取 OpenKAGE react.yaml 的 API/Goal 速查表，保证外部基线与本方法同版。

    只取「下面这些已经在代码作用域里」到「## 4. 你每步能看到什么」前的事实性 API 区，
    不带 OpenKAGE 的记忆工具/计划工具/人设，也不接入任何可选增强原语。
    """
    from functools import lru_cache

    @lru_cache(maxsize=1)
    def _load() -> str:
        import re
        from pathlib import Path
        fp = (Path(__file__).resolve().parents[1]
              / "domains" / "minecraft" / "prompts" / "react.yaml")
        text = fp.read_text(encoding="utf-8")
        m = re.search(
            r"^(  下面这些\*\*已经在你的代码作用域里、直接用\*\*，不需要 `require`：.*?)"
            r"^(?=  ---\n\n  ## 4\. 你每步能看到什么)",
            text, re.DOTALL | re.MULTILINE)
        if not m:
            raise ValueError("无法从 react.yaml 提取 OpenKAGE API 速查表（标记漂移）")
        block = m.group(1)
        # react.yaml system 块整体有两格缩进；还原为 prompt 片段。
        block = "\n".join(ln[2:] if ln.startswith("  ") else ln
                          for ln in block.splitlines())
        block = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "", block)
        if "{" in block and re.search(r"\{[A-Za-z_][\w]*\}", block):
            raise ValueError("OpenKAGE API 速查表提取后仍有未渲染占位符")
        return block.strip()

    return _load()

#: ACTION prompt 的占位符填充（str.format 会把 JS 注释里的 `{matching}` 当格式字段，
#: 故用显式 replace；未填占位符抛错——与 prompt_store 同防线）。
_ACTION_PLACEHOLDER = "{programs_doc}"


def render_action_system() -> str:
    """ACTION_SYSTEM_TEMPLATE + 裸 API 速查表（适配点①）。未替换占位符留在结果里 = bug。"""
    rendered = ACTION_SYSTEM_TEMPLATE.replace(_ACTION_PLACEHOLDER,
                                              programs_doc_for_bare_interface())
    if _ACTION_PLACEHOLDER in rendered:
        raise ValueError("ACTION_SYSTEM_TEMPLATE 渲染后仍有未替换占位符")
    return rendered

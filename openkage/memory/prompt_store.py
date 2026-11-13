"""Agent 提示词的**公共装载层**：一个类，每个 Agent 一份 yaml。

    from openkage.memory.prompt_store import REFLECT, AGGREGATE
    REFLECT.system                       # 渲染好的 system prompt（词表已注入）
    REFLECT.layers                       # 该 Agent 能写哪些层（create_concept 的 layer enum）
    REFLECT.kind_names                   # kind 词表 —— **2026-08-09 起为空**（见下）
    REFLECT.relation_probes              # 关系 probe（供 relation_semantics 算均值向量）
    REFLECT.tool_desc("add_facet")       # 工具文案
    REFLECT.result("slug_empty")          # 工具**返回给模型**的文案（缺则落到 default.yaml）
    REFLECT.render_user_input(name, vals)  # 每次调用拼给它的那段话

## ⚠️ kind / relation 词表当前是**空的**（2026-08-09）

两份 yaml 的 `vocab` 只剩 `layers:` 一项 —— kind 与 relation 已放开为**自由命名**
（本次实验要看图谱自发长出什么范畴，再反推词表；理由与两处能力回退见那两份 yaml 顶部）。
所以本模块的 `kinds` / `kind_names` / `kind_layers` / `relations` / `relation_probes`
现在**全是空容器**，`_render_kinds_table` / `_render_relations_list` / `relations_block`
**暂时没有消费者**。

**这些都是有意保留的**：反推出词表后填回 yaml、把 system 那两节改回 `{{vocab.xxx}}`，
整条链路自动恢复 —— 删了再加回来是白工。别当死代码清掉。

## 这一层做什么

框架里有两个写图谱的 ReActAgent —— **反思**（写私有图谱）与**聚合**（写公共图谱）。
它们结构完全同构：都是 `system prompt + 受控词表 + 10 个窄工具 + 每次调用的 user_input`。
**装载/校验/渲染的代码只有一份**（本模块），**内容按 Agent 物理隔离**（各自一份 yaml），
两个范畴（私有记忆 / 公共经验）不共用任何文件。

## 任务域的 ReAct Agent 也用这个类

同一个类服务两类 Agent（写图谱的反思/聚合，与无词表的任务域 ReAct），靠的是两条通用规则：

1. **「有没有词表」由 yaml 自己声明** —— 有 `vocab:` 那一节就是写图谱的 Agent，没有就不是。
2. **`{{占位符}}` 是对 yaml 自身的路径引用**（如 `{{vocab.relations}}`），不是一张代码里
   枚举的名单 —— 加一份新词表零代码改动。

`base_dir` / `nested` 让域私有 yaml 放在 `domains/<域>/prompts/` 下（框架不认识插件，
但插件可以复用框架的装载器 —— 依赖方向仍是单向的）。

## 为什么是 YAML 而不是 JSON

正文含 markdown 表格与代码块。YAML 的 `|` 块标量原样保留它们（换行、`#`、`:`、`|` 都不转义）；
JSON 会压成一行带 `\\n` 的字符串，审阅和改措辞都很痛苦 —— 而这些文案是要反复迭代的实验条件。

⚠️ **要保留块末尾空行必须用 `|+`（keep）**，普通 `|` 是 clip 模式会吃掉它，导致内容与后面
的指令黏在一起。

## 一份 agent yaml 的结构

    system:       str            # 人设+规则+SOP。两种占位符，分界是**值在哪**：
                                 #   {{vocab.relations}} = 引用本文件的路径，加载期自动填
                                 #   {default_sec}       = 值在调用方手里，render_system() 填
                                 #                         （不用另列清单，从正文扫出来）
    vocab:                       # **可选**：有这一节 = 写图谱的 Agent
      layers:     [memory, …]    #   **必填**：该 Agent 能写哪些层 = layer enum 的唯一来源
      memory_kinds:     [...]    #   ⚠️ 以下 kind/relation 三项 **2026-08-09 起都不写了**
      experience_kinds: [...]    #      （放开为自由命名，见模块 docstring）。装载器仍认它们，
      kinds:            [...]    #      填回来即恢复。
      relations:  [{name, group, means, probes:[…]}, …]
      任意新键:    [...]          #   随便加 —— {{vocab.任意新键}} 就能引用，无需改代码
    tools:        {工具名: {desc, 参数名: 说明}, _kind_field: str}
    user_inputs:  {模板名: [{text, when?}, …]}
    tool_results: {键: 文案}      # **可选**：本 Agent 独有的工具返回文案。缺的键落到
                                 #   `prompts/default.yaml`（框架层工具的措辞在那儿，
                                 #   reflect/aggregate 因此一个字都不用写）
    # 子类用 SECTIONS 声明自己**必需**的顶层键
    # ⚠️ 未知顶层键一律报错 —— 拼错的键不会静默变成"那一节不存在"

渲染方式由**路径末段**决定（`_RENDERERS`）：末段以 `kinds` 结尾走 md 表格、以 `relations`
结尾走分组列表、其余 `str()`。这是本模块唯一的领域知识，只有两条。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

#: 框架层工具返回给模型的文案（**所有 Agent 共用的兜底一份**），见 `prompts/default.yaml`。
#: `AgentPrompts.result(key)` 先查本 Agent 自己的 `tool_results`，缺了才落到这里 ——
#: 于是 reflect/aggregate 的 yaml **一个字都不用加**就已经在用这份文案。
#:
#: 为什么是隐式 fallback，而不是让各 yaml 写一行 `include: [default]`：
#: 「这条文案在哪」已经由**它写在哪个文件**表达了，再加一份名单就是同一个事实存两份，
#: 还得配一条"两份不一致就报错"的校验 —— 正是 2026-08-07 被纠的那种形态
#: （见 config_vs_derivable_fact 那条判据）。
_DEFAULT_TOOL_RESULTS: "dict[str, Any] | None" = None


def _load_default_tool_results() -> dict:
    """读 `prompts/default.yaml`（模块内只读一次，结果缓存）。

    ★ 校验故意很紧：**顶层只允许 `tool_results` 一个键**。这份文件的唯一职责就是那一节，
    多出来的键必然是没人读的（同基类那条「未知顶层键一律报错」）。
    """
    global _DEFAULT_TOOL_RESULTS  # noqa: PLW0603
    if _DEFAULT_TOOL_RESULTS is not None:
        return _DEFAULT_TOOL_RESULTS
    import yaml

    path = _PROMPTS_DIR / "default.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    extra = sorted(set(raw) - {"tool_results"})
    if extra:
        raise ValueError(
            f"{path}：只应该有 `tool_results:` 一个顶层键，多出来的 {extra} 没人会读"
        )
    texts = raw.get("tool_results")
    if not texts:
        raise ValueError(f"{path}：`tool_results:` 不能为空 —— 这份文件的全部内容就是它")
    bad = [k for k in texts if not str(k).strip()]
    if bad:
        raise ValueError(f"{path}：tool_results 里有空键名 —— 取不到，也没法排查")
    _DEFAULT_TOOL_RESULTS = texts
    return texts


def _render_kinds_table(kinds: list[dict]) -> str:
    """把一份 kind 列表渲染成给模型看的 md 表格。"""
    lines = ["| kind | 它回答什么 | 装什么 | **不要**装什么 |", "|---|---|---|---|"]
    for k in kinds:
        lines.append(f"| `{k['name']}` | {k['answers']} | {k['holds']} | ❌ {k['avoid']} |")
    return "\n".join(lines)


def _render_relations_list(relations: list[dict]) -> str:
    """把 vocab.relations 按 group 分组渲染成列表。保持 yaml 里的书写顺序（那是分组顺序）。"""
    out: list[str] = []
    seen_group = None
    for r in relations:
        g = r.get("group", "")
        if g and g != seen_group:
            if out:
                out.append("")
            out.append(f"**{g}**")
            seen_group = g
        out.append(f"- `{r['name']}`: {r['means']}")
    return "\n".join(out)


#: 一份 kind/relation 列表**怎么渲染成给模型看的文本**。
#: 这是本模块唯一需要"认识"的领域知识 —— 占位符名字不再由代码枚举，见 `_resolve_path`。
_RENDERERS: dict[str, Any] = {
    "kinds": _render_kinds_table,        # 列表 → md 表格
    "relations": _render_relations_list,  # 列表 → 按 group 分组的「- `名`: 含义」
}


def _resolve_path(data: Any, path: str) -> Any:
    """按 `a.b.c` 取值，取不到抛 KeyError（**不返回 None**，那会静默渲染成 "None"）。"""
    cur = data
    for i, seg in enumerate(path.split(".")):
        if not isinstance(cur, dict) or seg not in cur:
            reached = ".".join(path.split(".")[:i]) or "(顶层)"
            avail = sorted(cur) if isinstance(cur, dict) else f"（{type(cur).__name__}，不是字典）"
            raise KeyError(f"`{path}` 走不通：{reached} 下面没有 {seg!r}。可选：{avail}")
        cur = cur[seg]
    return cur


def render_blocks(blocks: list[dict], values: dict, *, where: str = "") -> str:
    """极小的条件拼接器（**不是**模板引擎，故意不引 jinja2 —— 本机也没装）。

    - 逐个 block 拼接（block 的 `text` 用 YAML `|` 块标量，换行/缩进原样保留）；
    - `when: <键名>` → 仅当 `values[<键名>]` 为真才输出这个 block；
    - `{name}` → 用 `values[name]` 字面替换（**不走 str.format**：文案里有 md 表格的 `|`
      和代码示例的 `{}`，format 会当成字段名直接抛错）。

    ★ **未提供的键直接抛错**，不留空：静默拼出个空洞（"今天是第  天"）比报错难查得多，
      而这正是 prompt 类 bug 最典型的形态 —— 跑得通、只是模型收到一句残缺的话。
    """
    out: list[str] = []
    for i, blk in enumerate(blocks):
        cond = blk.get("when")
        if cond is not None and not values.get(cond):
            continue
        text = str(blk.get("text", ""))
        for name in re.findall(r"\{(\w+)\}", text):
            if name not in values:
                raise KeyError(
                    f"{where} 第 {i + 1} 个 block 用到 {{{name}}}，但调用方没传这个值。"
                    f"已传：{sorted(values)}"
                )
            text = text.replace("{" + name + "}", str(values[name]))
        out.append(text)
    # block 的 text 各自以 \n 结尾（YAML `|` 的语义），直接拼即可。
    # 末尾统一 rstrip：**不要补 "\n"** —— prompt 的字节级一致性是重构的验收标准。
    return "".join(out).rstrip() if out else ""


class AgentPrompts:
    """**一个 Agent 的全部提示词** = 一份 yaml 的内存视图 + 校验。

    ## 一条规则贯穿全类：**yaml 自己声明它有什么，代码不预设**

    没有 `HAS_VOCAB` 之类的开关，也没有一张"支持哪些占位符"的名单。判据只有一条：

    - **有 `vocab:` 这一节 → 就是写图谱的 Agent**（反思/聚合），那一节的 `layers` 给出
      `create_concept` 的 layer enum；`kind_names` / `relation_probes` 等在词表放开后为空
      （见模块 docstring）；
    - **没有 → 就是无词表的 Agent**（任务域的 ReAct），那些属性是空容器。

    「有没有词表」本来就写在 yaml 里了（那一节在不在），再加一个类属性就是**同一个事实存两份**，
    还得配一条"两份不一致就报错"的校验 —— 那正是本模块要消灭的那种重复。
    拼错 `vocab:` 为 `vocba:` 也不会静默关掉校验：那个键会被 ⓪ 的"多余顶层键"检查当场拦下 ——
    **未知的键一律报错**，比一个布尔开关管得更宽。

    ## system 里的占位符 = **对 yaml 自身的路径引用**

        {{vocab.relations}}          → 取 vocab.relations，按 _RENDERERS 渲染成关系列表
        {{vocab.memory_kinds}}       → 取那一份 kind 列表，渲染成 md 表格
        {{vocab.kinds + vocab.…}}    ← 不支持表达式；要合并就在 yaml 里另起一个键

    路径存在就能引用，**加一个词表不需要动代码**。渲染方式由**路径末段的名字**决定
    （`_RENDERERS`：末段叫 `*kinds` 走表格、叫 `*relations` 走分组列表），
    末段不认识就直接 `str()`。这是本模块唯一保留的领域知识 —— 只有两条。

    值不在 yaml 里的（时限、别的 Agent 的词表）用**单**花括号，由调用方在
    `render_system(**values)` 传。两种括号的分工是「**值在文件里** vs **值在调用方手里**」，
    而不是"词表 vs 其它" —— 前者能自解释，后者只是巧合。

    ## 顶层键

    `system` / `tools` / `user_inputs` 必需；`vocab` 与 `tool_results` 可选
    （前者有=写图谱、其 `layers` 必填；后者缺的键落到 `prompts/default.yaml`，见 `result()`）。
    子类用 `SECTIONS` 声明自己**必需**的键（如 ReAct 的 `night_reflection`）。
    **未在上述范围内的顶层键一律报错** —— 那要么是拼错，要么是写了段没人读的内容。

    子类可覆写 `_extra_validate()` 加该 Agent 特有的检查。
    """

    #: 本 Agent 额外需要的顶层键（子类声明）。基类的 4 个不用重复写。
    SECTIONS: tuple[str, ...] = ()

    #: 基类认识的顶层键。`vocab` 可选，其余必需。
    _BASE_SECTIONS = ("system", "tools", "user_inputs")
    #: `tool_results` 可选：不写就全部走 `prompts/default.yaml`（reflect/aggregate 就是
    #: 这样，一个字都不用加）；写了的键**遮蔽**同名默认值，装载期会 warning 列出来。
    _OPTIONAL_SECTIONS = ("vocab", "tool_results", "ablation_overrides")

    def __init__(self, agent: str, *, base_dir: "Path | None" = None,
                 nested: bool = True) -> None:
        """`nested=True`（框架默认）→ `<base_dir>/<agent>/<agent>.yaml`：框架级 `prompts/` 下
        每个 Agent 一个子目录（两份 yaml 各占一个）。
        `nested=False` → `<base_dir>/<agent>.yaml`：域私有目录里就一份 yaml，
        再套一层同名目录只是多一级路径。
        """
        import yaml

        self.agent = agent
        base = base_dir if base_dir is not None else _PROMPTS_DIR
        self.path = (base / agent / f"{agent}.yaml") if nested else (base / f"{agent}.yaml")
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        self.raw: dict[str, Any] = raw

        required = self._BASE_SECTIONS + tuple(self.SECTIONS)
        for key in required:
            if key not in raw:
                raise ValueError(f"{self.path}：缺少顶层键 `{key}:`")
        # ⓪ 未知顶层键一律报错 —— 这同时兜住了「`vocab:` 拼成 `vocba:`」那类手滑
        #    （那种键会落到这里被点名，而不是让词表静默变空）。
        known = set(required) | set(self._OPTIONAL_SECTIONS)
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"{self.path}：有没人读的顶层键 {unknown}（认识的是 {sorted(known)}）。"
                f"要么是键名拼错了，要么写了一段代码不会读的内容 —— 两种都得改，"
                f"别留一份看着生效其实没生效的配置"
            )

        from openkage.memory.schema import LAYER_EXPERIENCE, LAYER_MEMORY

        #: **有没有 `vocab:` 这一节就是唯一判据**（见类 docstring）。
        vocab = raw.get("vocab") or {}
        self.has_vocab: bool = "vocab" in raw
        #: 本 Agent 能写哪些层 —— **直接读 `vocab.layers`**（2026-08-09 起）。
        #: 此前它是从 kind 列表反推的（`[层 for 层, ks in kinds_by_layer if ks]`），
        #: 而那两份 kind 列表已随词表放开一起删掉 → 层没有第二个来源了，yaml 是唯一真相。
        self.layers: list[str] = [str(x) for x in (vocab.get("layers") or [])]
        #: 记忆层概念的类别词表（2026-09-12）—— `create_concept` 在 layer=memory 时的
        #: category enum 的唯一来源。缺省（没写 `vocab.categories`）= 本 Agent 不用分类
        #: （聚合 Agent 单层 experience；skill_maintenance 也没有概念分类需求）。
        from openkage.memory.schema import CATEGORIES
        _cats_raw = [str(x) for x in (vocab.get("categories") or [])]
        self.categories: list[str] = _cats_raw or list(CATEGORIES)
        if _cats_raw and set(_cats_raw) != set(CATEGORIES):
            raise ValueError(
                f"{self.path}：`vocab.categories` 必须恰好是 {list(CATEGORIES)}（多了少了都会"
                f"造成「prompt 教的分类工具却不认」或反之。当前值：{_cats_raw}"
            )
        #: {层: 该层的 kind 列表} / kind 相关的一切 —— **词表放开后一律为空容器**。
        #: 保留这些属性与下面的渲染器：反推出词表后填回 yaml 即自动恢复，不是死代码
        #: （见两份 yaml 顶部的「回归路径」）。
        self.kinds_by_layer: dict[str, list[dict]] = {}
        if vocab.get("memory_kinds") or vocab.get("experience_kinds"):
            self.kinds_by_layer = {
                LAYER_MEMORY: list(vocab.get("memory_kinds") or []),
                LAYER_EXPERIENCE: list(vocab.get("experience_kinds") or []),
            }
        elif vocab.get("kinds"):
            # 单份词表：整个 Agent 只写一层（该层取 layers 的第一个）
            self.kinds_by_layer = {
                (self.layers[0] if self.layers else LAYER_EXPERIENCE): list(vocab["kinds"])
            }
        self.kinds: list[dict] = [k for ks in self.kinds_by_layer.values() for k in ks]
        self.relations: list[dict] = list(vocab.get("relations") or [])
        if self.has_vocab and not self.layers:
            raise ValueError(
                f"{self.path}：写了 `vocab:` 就必须给 `layers:` —— 它是 create_concept 的 "
                f"layer enum 的唯一来源（kind 词表已放开，反推不出层了）"
            )

        self.kind_names: list[str] = [k["name"] for k in self.kinds]
        self.relation_names: list[str] = [r["name"] for r in self.relations]
        self.relation_probes: dict[str, list[str]] = {
            r["name"]: [str(p) for p in (r.get("probes") or []) if str(p).strip()]
            for r in self.relations
        }
        #: {kind 名: 它所属的层}。**这不是"层由 kind 推出"** —— 层由 Agent 在 create_concept
        #: 时显式声明（见 tools.CreateConceptTool），这张表只用于**一致性校验**。
        #: ⚠️ **词表放开后它是空的** → 那道校验已随之删除（见 tools.py 那处的注释）。
        self.kind_layers: dict[str, str] = {
            k["name"]: lay for lay, ks in self.kinds_by_layer.items() for k in ks
        }

        #: system 里等调用方填的 `{单花括号}` —— **从正文扫出来，不用另写一份清单**
        #: （清单与正文是同一个事实的两份拷贝，迟早对不上：已经踩过一次「sub 算了、
        #: 正文的占位符却被删」的静默失效）。
        self.system_subs: list[str] = sorted(set(
            re.findall(r"(?<!\{)\{(\w+)\}(?!\})", str(raw["system"]))))

        self._validate()

        #: `{{路径}}` 在**加载期**填掉 —— 值就在这份 yaml 里，与调用方无关。
        self.system: str = self._render_self_refs(str(raw["system"]).strip())

        self.tools: dict[str, Any] = raw["tools"]
        self.user_inputs: dict[str, list] = raw["user_inputs"]

        #: 本 Agent **自己的**工具返回文案（可选）。取值时缺的键落到 `default.yaml`，见 `result()`。
        self.tool_results: dict[str, Any] = raw.get("tool_results") or {}
        #: ★ **遮蔽要出声**：本 yaml 写了与 default 同名的键 → 默认那条永不生效。
        #: 不禁止（某个域确实可能想改一句框架文案的措辞），但要让「我改了 default.yaml
        #: 却没生效」在日志里有解释 —— 否则那是最难查的一类。
        shadowed = sorted(set(self.tool_results) & set(_load_default_tool_results()))
        if shadowed:
            logger.warning(
                "%s：tool_results 的 %s 遮蔽了 prompts/default.yaml 里的同名文案"
                "（改 default.yaml 对本 Agent 不生效）", self.path, shadowed)

    def _render_self_refs(self, text: str) -> str:
        """把 `{{a.b}}` 换成本 yaml 里 `a.b` 的渲染结果。路径走不通 → 抛错。"""
        def sub(m: "re.Match[str]") -> str:
            path = m.group(1).strip()
            try:
                val = _resolve_path(self.raw, path)
            except KeyError as e:
                raise ValueError(f"{self.path}：system 里的 {{{{{path}}}}} 引用不到 —— {e}") from None
            renderer = _RENDERERS.get(path.rsplit(".", 1)[-1].rsplit("_", 1)[-1])
            return renderer(val) if renderer else str(val)

        out = re.sub(r"\{\{([^{}]+)\}\}", sub, text)
        if "{{" in out:
            raise ValueError(f"{self.path}：system 里还有没配对的 `{{{{` —— 检查括号")
        return out

    def render_system(self, **values: Any) -> str:
        """填上 system 里的 `{单花括号}` 占位符（值在调用方手里的那些），返回最终 prompt。

        正文里没有单花括号占位符的 Agent（反思/聚合）不必调 —— 直接用 `self.system`。

        **字面替换，不用 str.format**：正文含 JS/JSON 代码块（`const { Vec3 } = …`、
        `{oak_log: 5}`），format 会把那些花括号当字段名直接抛 IndexError。
        字面 replace 只动 `self.system_subs` 里的键（那是从正文扫出来的 `{\\w+}`），
        代码块里的 `{name: count}` 不匹配 `\\w+` 形状，不受影响。

        两个方向都校验（这两类漂移都真实发生过，且都**不报错、只让模型收到残缺的话**）：
        - 传了正文里没有的键 → 抛错（键名拼错，或那个占位符已被删）；
        - 正文里有却没传 → 抛错（否则留一个字面 `{default_sec}` 给模型看）。
        """
        unknown = [k for k in values if k not in self.system_subs]
        if unknown:
            raise KeyError(
                f"{self.path}：render_system 收到 {unknown}，但 system 正文里没有这些占位符"
                f"（正文里的是 {self.system_subs}）—— 要么键名拼错，要么占位符已被删"
            )
        missing = [k for k in self.system_subs if k not in values]
        if missing:
            raise KeyError(
                f"{self.path}：render_system 缺 {missing}（system 正文里有这些占位符）。"
                f"不填会让 prompt 里留下字面 `{{{missing[0]}}}` 给模型看"
            )
        text = self.system
        for name, val in values.items():
            text = text.replace("{" + name + "}", str(val))
        return text

    # ---- 校验：把"静默失效"类错误全部前移到加载期 ----
    def _validate(self) -> None:
        p = self.path
        # ⓪ 层名单：非空由 __init__ 保证，这里查重复（enum 里出现两个一样的值）
        if len(set(self.layers)) != len(self.layers):
            raise ValueError(f"{p}：vocab.layers 里有重复的层名 {self.layers}")
        # ① 名字唯一 —— 重名会让 enum 里出现两个一样的词
        for label, names in (("kinds", self.kind_names), ("relations", self.relation_names)):
            dup = {n for n in names if names.count(n) > 1}
            if dup:
                raise ValueError(f"{p}：vocab.{label} 里有重复名字 {sorted(dup)}")
        # ② probe **可选**（2026-08-09 起）：关系词表放开后，绝大多数关系是 Agent 自造的、
        #    根本不在 yaml 里，谈不上"有没有配 probe"。缺 probe 的关系在检索打分时
        #    **用关系名本身当权重文本**（见 relation_semantics 的 fallback），不再被静默忽略。
        #    仍打一条 info：yaml 里显式写了关系却漏了 probe，多半是手滑，而两者行为确实不同
        #    （probe 是几条语气句取均值，名字只有一个短词，区分度更弱）。
        no_probe = [n for n, ps in self.relation_probes.items() if not ps]
        if no_probe:
            logger.info(
                "%s：关系 %s 没有 probes —— 检索打分将改用**关系名本身**作为权重文本。"
                "这是合法的（自造关系全都走这条路），但显式写在 yaml 里的关系建议配几条"
                "「会想沿这条边走」的 query 语气句，区分度更好", p, no_probe)
        # ③ kind 表字段齐全 —— 缺了会渲染出空单元格
        for k in self.kinds:
            missing = [f for f in ("name", "answers", "holds", "avoid") if not k.get(f)]
            if missing:
                raise ValueError(f"{p}：vocab.kinds 的 {k.get('name', '?')!r} 缺字段 {missing}")
        for r in self.relations:
            if not r.get("means"):
                raise ValueError(f"{p}：vocab.relations 的 {r.get('name', '?')!r} 缺 `means`")
        # ④ 两层的 kind 名**不能重叠** —— 否则一致性校验无法判断某个 kind 属于哪层
        if len(self.kinds_by_layer) > 1:
            seen: dict[str, str] = {}
            for lay, ks in self.kinds_by_layer.items():
                for k in ks:
                    if k["name"] in seen:
                        raise ValueError(
                            f"{p}：kind {k['name']!r} 同时出现在 {seen[k['name']]!r} 与 {lay!r} 两层。"
                            f"两层的 kind 名必须互斥 —— 否则「声明的 layer 与 kind 是否矛盾」无从判断"
                        )
                    seen[k["name"]] = lay
        # ⑤ kind 词表在时，它声明的层必须都在 layers 里（否则那批 kind 挂不到任何层上）
        stray = sorted(set(self.kinds_by_layer) - set(self.layers))
        if stray:
            raise ValueError(
                f"{p}：kind 词表声明了层 {stray}，但 vocab.layers 里没有它们（layers={self.layers}）"
            )
        self._extra_validate()

    def _extra_validate(self) -> None:
        """子类挂钩：该 Agent 特有的检查。基类无操作。"""

    # ---- 渲染好的词表片段（给任务域的 system prompt 用）----
    @property
    def relations_block(self) -> str:
        """「- `名字`: 含义」列表（按 group 分组）。任务域要告诉 Agent 合法关系词时用。"""
        return _render_relations_list(self.relations)

    @property
    def kinds_table(self) -> str:
        return _render_kinds_table(self.kinds)

    # ---- 工具文案 ----
    def tool_text(self, tool: str, field: str) -> str:
        """取某个工具的一段文案。**缺 key 直接抛错**，不静默退化成空字符串 ——
        空 description 会让模型完全不知道这个工具干什么，而这种错读日志也看不出来。"""
        try:
            return str(self.tools[tool][field]).strip()
        except (KeyError, TypeError):
            raise KeyError(
                f"{self.path}：tools.{tool}.{field} 不存在。加工具/加参数时要同步在这里补文案"
            ) from None

    def tool_desc(self, tool: str) -> str:
        return self.tool_text(tool, "desc")

    @property
    def kind_field_desc(self) -> str:
        """facet kind 参数的说明 —— 五个工具共用一份，避免五处漂移。"""
        try:
            return str(self.tools["_kind_field"]).strip()
        except (KeyError, TypeError):
            raise KeyError(f"{self.path}：缺少 tools._kind_field") from None

    @property
    def source_steps_field_desc(self) -> str:
        """来源步参数的说明 —— create_concept / add_facet / update_facet 共用一份（三者都会
        落下一条 facet 的内容）。

        **只有多层 Agent（反思）会读它** —— 聚合的 yaml 里本就没有这个键，也不该有：
        它读的是脱离具体轨迹的通名结论，报不出步号（见 tools.py 里 _MULTI_LAYER 那段）。
        """
        try:
            return str(self.tools["_source_steps_field"]).strip()
        except (KeyError, TypeError):
            raise KeyError(f"{self.path}：缺少 tools._source_steps_field") from None

    # ---- 工具返回给模型的文案 ----
    def result(self, key: str, **values: Any) -> str:
        """取一条 tool result 文案，`{name}` 按字面替换。

        **查找顺序**：本 Agent 的 `tool_results` → `prompts/default.yaml` → 抛错。
        于是框架层工具（10 个 CRUD、retrieve/expand）的措辞只写一份，而某个域想改其中
        一句时，在自己的 yaml 里写同名键即可（会有遮蔽 warning，见 `__init__`）。

        不用 str.format：文案里有 `(slug=…)`、`bot.look` 这类内容，遇到花括号 format 会
        当成字段名直接抛错。模板里声明了却没传的占位符**直接抛错**：静默留一个字面
        `{n_public}` 在发给模型的文本里，跑得通、只是模型读到一句残缺的话 —— 正是最难查的
        那类 prompt bug。

        注意残留花括号的检查边界：
        旧实现是**替换完再扫「整段结果」**有没有残留 `{xxx}`，命中就抛 KeyError。本意是抓
        上面那类残缺文案，但它把**模型自己产出的花括号**也当成了占位符——实案是 agent 在经验
        正文里写了伪代码 `while(stick<N){有棍配方就搓棍否则搓木板}`，存进记忆后被 retrieve 取
        出填进 `{ctx}`，替换完那段花括号还在 ⇒ KeyError ⇒ registry 打成「工具执行失败」，更外层
        `_unified_inject` 的 except 把**整段经验注入丢掉**（日志「经验检索失败（不影响主流程）」）。
        即：模型写个花括号 = 该步的记忆上下文静默报废，且是**非设计内的实验条件扰动**（消融组横比
        的噪声）。
        修法：**只校验模板自身声明的占位符**（模板=框架/域作者写的，可信），替换后**不再扫描**
        （`ctx`/`label` 是模型产出，不可信，里面出现花括号是正常的 JS/伪代码）。
        代价：只还抓得住「模板里的占位符名写错」，抓不到替换后残留——但模板有本文件 __init__
        的 yaml 全量校验兜底，远比「模型写个花括号就整段经验报废」划算。
        """
        text = self.tool_results.get(key)
        if text is None:
            text = _load_default_tool_results().get(key)
        if text is None:
            raise KeyError(
                f"{self.path}：tool_results.{key} 不存在。"
                f"本 Agent 自己的：{sorted(self.tool_results)}；"
                f"prompts/default.yaml 的：{sorted(_load_default_tool_results())}"
            )
        text = str(text)
        # ★ 先按**模板原文**算需要哪些占位符，校验 values 是否都传了；替换后不再扫描
        need = re.findall(r"\{(\w+)\}", text)
        missing = [n for n in need if n not in values]
        if missing:
            raise KeyError(
                f"{self.path}：tool_results.{key} 还有未填的占位符 {missing}，"
                f"已传：{sorted(values)}"
            )
        for name, val in values.items():
            text = text.replace("{" + name + "}", str(val))
        return text

    # ---- user_input 模板 ----
    def render_user_input(self, template: str, values: dict) -> str:
        try:
            blocks = self.user_inputs[template]
        except KeyError:
            raise KeyError(
                f"{self.path}：user_inputs.{template} 不存在，可用：{sorted(self.user_inputs)}"
            ) from None
        return render_blocks(blocks, values, where=f"{self.path}:user_inputs.{template}")

    # ---- 消融层指令覆盖（2026-09-18 leave-one-out） ----
    def raw_ablation_override(self, rung: str) -> "str | None":
        """取 `ablation_overrides.<rung>.section_1_replacement` 的原文（无则 None）。

        只被 ArmGraph._reflect_system_prompt 用（no_memory_layer/no_exp_text 时替换
        system 的「## 1. 两层」节）。直接读 `self.raw`：这段文本不进 render 管线
        （没有占位符、不参与 when 条件），渲染反而引入一套只有这一个调用方用的路径。
        其它 yaml 没写 `ablation_overrides` 节 → `.get` 链自然返 None，无侵入。
        """
        node = (self.raw.get("ablation_overrides") or {}).get(rung) or {}
        text = node.get("section_1_replacement")
        return str(text) if text else None

    def raw_ablation_append_line(self, rung: str) -> "str | None":
        """取 `ablation_overrides.<rung>.append_line` 的原文（无则 None）。

        ★ 2026-09-26 用户定：**消融提示词改动以「只加一行」为准**，能不整段替换就不替换。
        −M/−E/no_exp_layer 改的是「收集哪一层」= §1 节本身的语义，走 section_1_replacement；
        而 −K（no_exp_skills）**两层指令全都要保留**（它只拆技能），故用 append_line 在原版
        prompt 末尾**追加一行**声明，不碰任何既有节。
        """
        node = (self.raw.get("ablation_overrides") or {}).get(rung) or {}
        text = node.get("append_line")
        return str(text).strip() if text else None


class _ReflectPrompts(AgentPrompts):
    """反思 Agent：**同时写两层**，故 `vocab.layers` 必须两层都在。"""

    def __init__(self) -> None:
        super().__init__("reflect")

    def _extra_validate(self) -> None:
        from openkage.memory.schema import LAYER_EXPERIENCE, LAYER_MEMORY

        # 两层都得声明 —— 少了任一层，工具的 layer enum 就少一个值，反思 Agent 写不出那层的
        # 内容，而「聚合只拿经验层」会导致整个聚合恒为空（静默、极难查）。
        # ⚠️ 判据从「两层的 kind 列表都非空」改成「两层都在 layers 里」（2026-08-09）：
        #    kind 词表已放开，没有列表可查了，能查的只有层名单本身。
        missing = [lay for lay in (LAYER_MEMORY, LAYER_EXPERIENCE) if lay not in self.layers]
        if missing:
            raise ValueError(
                f"{self.path}：vocab.layers 缺 {missing}（现在是 {self.layers}）。"
                f"反思 Agent 要同时写两层 —— 缺 {LAYER_EXPERIENCE!r} 会让聚合恒定拿不到东西"
                f"（静默失败），缺 {LAYER_MEMORY!r} 则明天的自己丢掉营地坐标与没做完的事"
            )


#: 两个单例（模块加载时各读一次 yaml + 全量校验）
REFLECT = _ReflectPrompts()
AGGREGATE = AgentPrompts("aggregate")
#: 技能维护 Agent（反思阶段2，2026-08-29）：独立 system prompt + 仅 5 个技能工具。
#: 不写 concept → 无 vocab 段（has_vocab=False，跳过层校验）。tools 段空占位
#: （技能工具的描述在 reflect.yaml 的 tools 段——一套类两个 Agent 共用，那边是唯一真相）。
SKILL_MAINTENANCE = AgentPrompts("skill_maintenance")

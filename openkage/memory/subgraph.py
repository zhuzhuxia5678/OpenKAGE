"""检索结果的统一载体：`MemorySubgraph`。

两个检索入口（`retrieve()` / `expand_concept()`）都返回同一种对象：一小片图
（节点 + 边 + 属性），渲染只有一处，Agent 能看到节点之间怎么连、谁是枢纽、还能往哪走展开——
不是一份丢了图结构的扁平列表。

文本形式（节点段 + 独立边清单）：

```
【相关子图】11 个概念，8 条内部关系
★ = 直接匹配你这次查询的入口概念

★ 「夜间掩体建造规范」(slug=minecraft_shelter_building)
    [working_method] 掩体设计五要素：…
  「火把合成与照明系统」(slug=minecraft_torch_crafting)
    [working_definition] 火把制作：…

── 这些概念之间怎么连 ──
minecraft_torch_crafting --requires--> minecraft_shelter_building
…
── 还能往外走（用 expand 展开）──
night_defense_minimal_shelter --similar_to--> night_bunker_digging_tactic
```
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MemorySubgraph:
    """一次检索取回的子图：节点（concept + 其 facet）+ 边。

    concepts: {slug: {slug, name, summary, layer, strength, …}}  —— 节点属性（强度见 schema.py）
    facets:   [{concept_slug, concept_name, kind, content}, …] —— 节点内容
    inner_edges: [(from_slug, relation, to_slug)] 两端都在子图内的边
    outer_edges: [(from_slug, relation, to_slug)] 一端在子图外 = 可 expand 的方向
    entry_slugs: 向量锚定命中的入口节点（渲染时标 ★）
    """

    concepts: dict[str, dict] = field(default_factory=dict)
    facets: list[dict] = field(default_factory=list)
    inner_edges: list[tuple[str, str, str, int]] = field(default_factory=list)
    outer_edges: list[tuple[str, str, str, int]] = field(default_factory=list)
    entry_slugs: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """没有任何 facet 就算空 —— 判据是 facet 而非 concept：一个只有 name/summary、
        身上没有任何经验的概念，对 Agent 没有可用信息（渲染出来只是噪音）。"""
        return not self.facets

    def render(self, *, extra_note: str = "", header: str = "") -> str | None:
        """渲染成喂给模型的文本。空子图返回 None（调用方据此决定不挂记忆块）。

        ⚠️ **措辞必须域中性**：本类在框架层，minecraft / math 都走它，不能出现
        "过去做同类**题**的经验"这类只适用于某个域的表述。

        `header`：覆盖默认的「相关子图」标题。全量注入私有记忆时走这个 —— 那不是
        "检索到的相关子图"，而是"你的全部记忆"，沿用默认标题会让模型以为这只是一次检索
        结果的片段（从而以为还有别的没查到）。
        """
        if self.empty:
            return None
        # 按 concept 分组，保持 facet 传入顺序（调用方已按 created_at, kind 排好序）
        grouped: dict[str, list[dict]] = {}
        for f in self.facets:
            grouped.setdefault(f["concept_slug"], []).append(f)

        entries = set(self.entry_slugs)
        blocks = []
        for slug, items in grouped.items():
            name = items[0].get("concept_name") or slug
            mark = "★ " if slug in entries else "  "
            lines = [f"{mark}「{name}」(slug={slug})"]
            lines.extend(f"    [{f['kind']}] {f['content']}" for f in items)
            blocks.append("\n".join(lines))

        head = header or f"【相关子图】{len(grouped)} 个概念"
        if not header:
            if self.inner_edges:
                head += f"，{len(self.inner_edges)} 条内部关系"
        else:
            head += f"（{len(grouped)} 个条目"
            head += f"，{len(self.inner_edges)} 条关系）" if self.inner_edges else "）"
        parts = [head]
        if extra_note:
            parts.append(extra_note)
        parts.append("\n".join(blocks))

        # 边清单：三元组形式，`a --rel--> b` 对模型最好解析。边的可信度由落点概念的强度
        # 间接表达（见 graph._expand），这里不重复标注。
        def _fmt(edges: list[tuple[str, str, str]]) -> str:
            return "\n".join(f"{f} --{rel}--> {t}" for f, rel, t in edges)

        if self.inner_edges:
            parts.append("── 内部关系 ──\n" + _fmt(self.inner_edges))
        if self.outer_edges:
            parts.append("── 可展开 ──\n" + _fmt(self.outer_edges))
        return "\n\n".join(parts)

    def without_slugs(self, slugs) -> "MemorySubgraph":
        """返回去掉指定概念后的**新子图**（原对象不动）。

        供检索注入侧做「增量去重」：调用方扫上文已出现过的概念 slug，把它们的
        内容从检索结果里滤掉、只注入增量 —— 旧消息一字不改（前缀缓存友好），
        全重复时 empty=True，调用方据此走「无新增」分支。

        edges 从 inner+outer 合并重导：至少一端保留的边留下，两端都在**保留概念集**里
        → inner，否则 outer。⚠️ 判据是「端点在不在保留集」而非「在不在 drop」——原始
        outer 边（一端本来就在子图外）两端都不在 drop，按 drop 判断会把它错归成 inner。
        边元组用索引取端点（`e[0]`/`e[2]`），**不假设元数** ——
        dataclass 注解写的是 4 元组（含 strength），实际构造处传的是 3 元组，
        以索引取值两边都兼容。
        """
        drop = set(slugs or ())
        if not drop:
            return self
        if not self.concepts:
            return MemorySubgraph()
        kept_concepts = {s: c for s, c in self.concepts.items() if s not in drop}
        kept_facets = [f for f in self.facets if f["concept_slug"] not in drop]
        new = MemorySubgraph(
            concepts=kept_concepts,
            facets=kept_facets,
            entry_slugs=[s for s in self.entry_slugs if s not in drop],
        )
        for edge in list(self.inner_edges) + list(self.outer_edges):
            if edge[0] not in kept_concepts and edge[2] not in kept_concepts:
                continue  # 两端都不在保留集 → 边随之消失
            (new.inner_edges if (edge[0] in kept_concepts and edge[2] in kept_concepts)
             else new.outer_edges).append(edge)
        return new

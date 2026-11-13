"""Voyager 四模块：自动课程 / 代码生成（迭代提示）/ 自验证 / 技能库。

每个智能体一个 `VoyagerAgent` 实例。与原文（arXiv:2305.16291 §2、Pseudocode 1）的映射：

    curriculum.propose_next_task()      ← 原文 CurriculumAgent（GPT-4，temp=0.1）
    action.generate_code()              ← 原文 ActionAgent（GPT-4，temp=0；环境反馈/执行
                                          错误/critique 回灌迭代，最多 4 轮）
    critic.check_task_success()         ← 原文 CriticAgent（GPT-4，temp=0，JSON 判定）
    skill_manager（SkillStore 只读复用）← 原文 SkillManager（描述嵌入 top-5 检索 + 入库）

适配（与原文的差异全部要在论文附录列明，见 run_voyager.py 的适配清单）：
  · LLM：GPT-4/GPT-3.5 → 统一骨干（get_llm，课程/生成/验证同模型）；温度照原文
    （课程 0.1 / 其余 0），per-call config 显式传（llm._ensure_temperature 显式优先）。
  · Wiki QA 附加上下文（原文 A.3.2，自标 optional）省略；GPT-3.5 承担的自问自答与
    函数描述（原文用 GPT-3.5 省预算）统一用同一骨干。
  · 动作接口：16 个 control primitives → 裸 mineflayer API + waitUntil（全表统一口径）。
  · 技能库：ChromaDB → SkillStore（同构：intent 嵌入→js_code；KaLM 嵌入器；零 openkage 写依赖，
    create_skill 是技能库自身接口而非图谱写入）。
  · 昼夜节律：任务循环嵌在 env.run_exploration 的「天」里——天亮（worldDay 滚动）时
    若任务未完，进度（当前任务+已完成轮数）进 checkpoint、次日续跑（resume 语义）。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from openkage.llm import get_llm

from . import prompts
from .state_adapter import assemble_state_block

logger = logging.getLogger("voyager")

#: 原文 §2.3：一个任务最多 4 轮代码生成，仍未过自验证就放弃、让课程出下一个任务。
MAX_CODE_ROUNDS = 4
#: 原文温度（§3.1）：课程 0.1（鼓励任务多样性），其余 0。
TEMP_CURRICULUM = 0.1
TEMP_DEFAULT = 0.0

#: 首任务（原码 curriculum.py:241-244：progress==0 时固定任务+固定 context，不走 LLM）。
FIRST_TASK = ("Mine 1 wood log",
              "You can mine one of oak, birch, spruce, jungle, acacia, "
              "dark oak, or mangrove logs.")
#: ★ 首任务连续失败上限（2026-10-04 修死循环）：达到后放开硬编码分支、交课程 LLM。
#:   原版同结构无此风险（GPT-4 + 手写原语下首任务近乎必成）；统一骨干/裸接口下首任务
#:   反复失败 → completed_tasks 恒空 → 硬编码分支永远命中 → 30 夜死循环在同一个任务上
#:   （smoke 实抓：首任务失败一次后又重出同任务）。★ 上限取 5（用户 2026-10-04 拍板
#:   「抬高重试次数」）：官方语义是无限重试（progress==0 恒命中，只被 160 迭代上限
#:   截断），护栏只是防整条 run 烧死——5 次 × 4 轮代码生成 ≈ 20 步，一夜 30+ 步的
#:   预算内还能给课程 LLM 留一半时间。护栏记进 run_voyager.py 适配清单。
MAX_FIRST_TASK_FAILS = 5


def _summarize_chatlog(events: "list[tuple[str, str]] | None") -> str:
    """从 chat log 抽「I also need …」需求句（原码 action.py:258-280 逐字移植）。

    原版模式匹配的是它 control primitives 的反馈文案（"I cannot make X because I
    need: …" / "…because there is no crafting table nearby" / "I need at least a X
    to mine Y!"）。裸接口下这些文案不会出现（原语没了），但保留同样的模式——
    LLM 代码自己 bot.chat 的同款句式仍能命中；不命中返回空串（原版行为）。
    """
    def filter_item(message: str) -> str:
        craft_pattern = r"I cannot make \w+ because I need: (.*)"
        craft_pattern2 = r"I cannot make \w+ because there is no crafting table nearby"
        mine_pattern = r"I need at least a (.*) to mine \w+!"
        if re.match(craft_pattern, message):
            return re.match(craft_pattern, message).groups()[0]  # pylint: disable=E1101
        elif re.match(craft_pattern2, message):
            return "a nearby crafting table"
        elif re.match(mine_pattern, message):
            return re.match(mine_pattern, message).groups()[0]  # pylint: disable=E1101
        return ""

    chatlog = set()
    for kind, text in events or []:
        if kind != "onError":
            item = filter_item(text or "")
            if item:
                chatlog.add(item)
    return "I also need " + ", ".join(chatlog) + "." if chatlog else ""


def _extract_code(resp_text: str) -> str | None:
    """从代码生成响应里抠 ```javascript 块的代码（原文响应格式）。纯函数（单测覆盖）。

    优先 fenced 块（```javascript ... ``` 或 ``` ... ```）；没有 fence 时退而求其次找
    `async function` 起（适配：骨干偶发漏 fence，原文靠 GPT-4 格式纪律我们不赌）。
    """
    text = resp_text or ""
    m = re.search(r"```(?:javascript|js)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip() or None
    m = re.search(r"(async\s+function\s+[A-Za-z_$][\w$]*.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _parse_task(resp_text: str) -> str | None:
    """解析课程响应的 `Task: ...` 行。纯函数（单测覆盖）。"""
    m = re.search(r"^Task:\s*(.+)$", resp_text or "", re.MULTILINE)
    if not m:
        return None
    task = m.group(1).strip().strip('"')
    return task or None


def _parse_critic(resp_text: str) -> tuple[bool, str] | None:
    """解析自验证响应的 JSON {reasoning, success, critique}。纯函数（单测覆盖）。

    返回 (success, critique)；解析不出返回 None（调用方按失败重试/兜底）。
    fence / 前后杂文都容忍（取第一个 { 到最后一个 } 的区间再 json.loads）。
    """
    text = resp_text or ""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "success" not in obj:
        return None
    return bool(obj.get("success")), str(obj.get("critique") or "")


class VoyagerAgent:
    """一个 Voyager 智能体：课程 + 迭代代码生成 + 自验证 + 本地技能库。

    与 env.run_exploration 的对接在 run_voyager.py（react_day 回调形态）——本类只管
    LLM 侧（课程/生成/验证/入库），不碰 world、不碰步循环，纯「无状态服务」：
    所有跨步状态（任务清单/技能库/上下文）由调用方经 checkpoint 持久化后传回。
    这样单测可以用假 LLM 函数跑通全逻辑（tests/test_voyager_agent.py）。
    """

    def __init__(self, name: str, model_name: str | None,
                 skill_store, *, max_code_rounds: int = MAX_CODE_ROUNDS):
        self.name = name
        self.model_name = model_name
        self.skill_store = skill_store          # SkillStore（或单测假库），只读写技能表
        self.max_code_rounds = max_code_rounds
        self.completed_tasks: list[str] = []    # 已验证成功的任务（原文 exploration progress）
        self.failed_tasks: list[str] = []       # 4 轮未过的任务（"Failed tasks that are too hard"）
        #: 课程/验证调用的计数（进 metrics：LLM 调用是公平性指标的一部分）。
        self.n_llm_calls = {"curriculum": 0, "action": 0, "critic": 0, "desc": 0}
        #: 留痕器（可选）：`(call_kind, messages) -> None`，由 run_voyager 接 ContextDumper——
        #: 每个 LLM 调用把真正发出的 messages 落盘（本仓工程纪律：留痕是复现所需的全部信息，
        #: 见 openkage.agent_io）。None=不留痕（单测）。
        self.context_dump: "callable | None" = None

    # ---- 状态持久化（checkpoint 用；技能库由 SkillStore 自己持久化）----
    def state_dict(self) -> dict:
        return {"completed_tasks": list(self.completed_tasks),
                "failed_tasks": list(self.failed_tasks),
                "n_llm_calls": dict(self.n_llm_calls)}

    def load_state(self, d: dict | None) -> None:
        d = d or {}
        self.completed_tasks = list(d.get("completed_tasks") or [])
        self.failed_tasks = list(d.get("failed_tasks") or [])
        # n_llm_calls 不恢复（计数器非行为状态，续跑段从 0 重新计、与 OpenKAGE 的
        # cost_* 同口径——续跑段例外已在 metrics 注释里写明）。

    # ---- LLM 薄封装 ----
    async def _think(self, messages: list[dict], temperature: float, *,
                     kind: str = "") -> str:
        """一次自由文本调用（无工具）。网关错误/空响应由 OpenAICompatLLM 重试层兜。

        kind 进留痕文件名（curriculum/action/critic/desc——复盘时按调用点分辨）。
        """
        if self.context_dump is not None:
            try:
                self.context_dump(kind or "think", messages)
            except Exception:  # pylint: disable=broad-except
                pass  # 留痕绝不影响主流程（ContextDumper 纪律）
        llm = get_llm(self.model_name, role="task")
        resp = await llm.async_think(
            messages=messages, tools=None, tool_choice=None,
            config={"temperature": temperature})
        if resp.finish_reason == "error":
            # 不 raise：迭代提示机制的容错语义是「拿不到就把空当反馈、下一轮重试」，
            # 课程的容错是「重试上次的任务」（由调用方决定）。
            logger.warning("[voyager:%s] LLM 网关错误：%s", self.name,
                           (resp.content or "")[:120])
        return resp.content or ""

    # ── 模块 1：自动课程 ────────────────────────────────────────────────────
    async def propose_next_task(self, obs: dict) -> "tuple[str, str] | None":
        """按当前状态 + 探索进度提下一个任务。返回 (task, context)。

        ★ 原码两个**硬编码分支**（curriculum.py:241-276，论文没写、只有代码里有）：
          ① progress==0 且 auto 模式 → 固定首任务 "Mine 1 wood log" + 固定 context（
             列出七种原木名）——不给 LLM，保证开局任务万无一失；
          ② 库存 ≥33 槽 → 强制存箱任务链（已知箱子→Deposit / 有 chest 物品→Place a chest /
             否则→Craft 1 chest），防满包卡死探索。
        这两个分支是 Voyager 行为的一部分（第 1 个任务从不失败、满包自愈是它的生存能力），
        照抄进适配版；其余走 LLM（Prompt 1，temp=0.1）。
        返回 (task, context) 二元组（context 进代码生成/critic 的 Context: 段——原版
        get_task_context 的简化：无 QA 链时首任务有固定 context、其余空串）。
        """
        # ① 首任务硬编码（原码 propose_next_task 第一分支）。
        #   ★ 死循环护栏（2026-10-04，smoke 实抓）：首任务失败时 completed_tasks 仍为空、
        #   此分支永远命中 → 无限重出同一任务。原版无此风险（GPT-4+手写原语下首任务
        #   近乎必成）。MAX_FIRST_TASK_FAILS 次失败后放开，交课程 LLM 出新任务。
        if not self.completed_tasks:
            n_first_fails = self.failed_tasks.count(FIRST_TASK[0])
            if n_first_fails < MAX_FIRST_TASK_FAILS:
                return FIRST_TASK
            logger.warning("[voyager:%s] 首任务 %d 次未成——放开硬编码，交课程 LLM",
                           self.name, n_first_fails)
        # ② 满包存箱硬编码（原码第二分支）。本域观察有 nearbyChests（16 格内），
        #   原版 chest_memory 是跨步积累的（ActionAgent 维护）——这里用 nearbyChests
        #   即时近似（发现已知箱子就存），差异记适配清单。
        status = obs.get("status", {}) or {}
        inv_used = int(status.get("inventoryUsed", 0) or 0)
        if inv_used >= 33:
            inv = obs.get("inventory", {}) or {}
            if "chest" in inv:
                return ("Place a chest",
                        "You have a chest in inventory, place it around you. "
                        "If chests is not None, or nearby blocks contains chest, this task is success.")
            return ("Craft 1 chest",
                    "Craft 1 chest with 8 planks of any kind of wood.")
        state_block = assemble_state_block(obs, len(self.completed_tasks))
        user = "\n".join(filter(None, [
            state_block,
            f"Completed tasks so far: {', '.join(self.completed_tasks[-30:]) or '(none)'}",
            f"Failed tasks that are too hard: {', '.join(self.failed_tasks[-20:]) or '(none)'}",
        ]))
        self.n_llm_calls["curriculum"] += 1
        resp = await self._think(
            [{"role": "system", "content": prompts.CURRICULUM_SYSTEM_ALIGNED},
             {"role": "user", "content": user}], TEMP_CURRICULUM, kind="curriculum")
        task = _parse_task(resp)
        if task is None and resp.strip():
            # 课程失败兜底：重试上次的任务（原文 Inaccuracies 节的语义——课程可稍后再试）。
            if self.completed_tasks:
                task = self.completed_tasks[-1]
                logger.warning("[voyager:%s] 课程响应解析失败，重试上一任务 %r", self.name, task)
        return (task, "") if task else None

    # ── 模块 2+3：迭代代码生成 + 自验证 ─────────────────────────────────────
    async def generate_code(self, task: str, obs: dict, *, last_code: str | None,
                            events: "list[tuple[str, str]] | None",
                            critique: str = "", context: str = "") -> str | None:
        """一轮代码生成（原文 Prompt 4，temp=0）。last_code/events/critique 是上一轮的
        执行反馈回灌（原文迭代提示机制）；首轮它们为空。返回 JS 代码（解析失败 None）。"""
        err_lines = [t for k, t in (events or []) if k == "onError"]
        chat_lines = [t for k, t in (events or []) if k != "onError"]
        state_block = assemble_state_block(obs, len(self.completed_tasks))
        user = "\n".join(filter(None, [
            f"Code from the last round: {last_code or '(none)'}",
            f"Execution error: {' '.join(err_lines) or '(none)'}",
            f"Chat log: {' | '.join(chat_lines[-20:]) or '(none)'}",
            state_block,
            f"Task: {task}",
            f"Context: {context or '(none)'}",
            f"Critique: {critique or '(none)'}",
        ]))
        self.n_llm_calls["action"] += 1
        resp = await self._think(
            [{"role": "system", "content": prompts.render_action_system()},
             {"role": "user", "content": user}], TEMP_DEFAULT, kind="action")
        return _extract_code(resp)

    async def check_task_success(self, task: str, obs: dict, *,
                                 context: str = "") -> tuple[bool, str] | None:
        """自验证（原文 Prompt 6，temp=0）。返回 (success, critique)；LLM/解析失败 None。"""
        # 自验证的状态不受 warm-up 门控（原文 A.5.1 明确排除两字段，其余照给——判定要全信息）。
        state_block = assemble_state_block(obs, 10**9)
        user = "\n".join(filter(None, [
            state_block, f"Task: {task}", f"Context: {context or '(none)'}"]))
        self.n_llm_calls["critic"] += 1
        resp = await self._think(
            [{"role": "system", "content": prompts.CRITIC_SYSTEM},
             {"role": "user", "content": user}], TEMP_DEFAULT, kind="critic")
        return _parse_critic(resp)

    # ── 模块 4：技能库（SkillStore 只读复用）────────────────────────────────
    def retrieve_skills(self, task: str, *, limit: int = 5,
                        events: "list[tuple[str, str]] | None" = None,
                        context: str = "") -> list[dict]:
        """按任务检索 top-5 相关技能。

        ★ query 构成照原码（voyager.py:245-249）：`context + summarize_chatlog(events)`——
          任务上下文 + 从 chat log 抽出的「I also need …」需求句（缺料/缺工具提示），
          **不是裸任务文本**。summarize_chatlog 的抽取模式照原码 action.py:258-280。
        """
        query = "\n\n".join(p for p in (context, _summarize_chatlog(events)) if p) or task
        try:
            return self.skill_store.retrieve_skills(query, limit=limit)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[voyager:%s] 技能检索失败（按无技能继续）", self.name, exc_info=True)
            return []

    async def add_skill(self, code: str, *, at: int) -> str | None:
        """任务成功后入库（原文 Fig.4 top）：描述（LLM）→ 嵌入 key + 代码 value。

        描述由 SKILL_DESC_SYSTEM 生成（原文用 GPT-3.5，这里同骨干）；slug 从函数名抽
        （与 SkillStore._slugify 同规则，但这里先抽 async function 名保可读）。
        失败只警告不入库（技能库是增量资产，一条失败不该拖垮任务循环）。
        """
        m = re.search(r"async\s+function\s+([A-Za-z_$][\w$]*)", code or "")
        fname = m.group(1) if m else None
        self.n_llm_calls["desc"] += 1
        desc = await self._think(
            [{"role": "system", "content": prompts.SKILL_DESC_SYSTEM},
             {"role": "user", "content": code}], TEMP_DEFAULT, kind="desc")
        desc = (desc or "").strip().splitlines()
        desc = desc[0].strip() if desc else ""
        if not fname or not desc:
            logger.warning("[voyager:%s] 技能入库跳过：函数名/描述缺失（fname=%s desc=%d字）",
                           self.name, fname, len(desc))
            return None
        try:
            from openkage.memory._common import _slugify
            ok, msg = self.skill_store.create_skill(
                _slugify(fname), fname, desc, code,
                signature=f"await {fname}(bot)", preconditions=None, at=at)
            if ok:
                logger.info("[voyager:%s] 技能入库：%s（%d字描述）", self.name, msg, len(desc))
                return msg
            logger.warning("[voyager:%s] 技能入库被拒：%s", self.name, msg)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[voyager:%s] 技能入库异常（跳过）", self.name)
        return None

    def mark_task(self, task: str, success: bool) -> None:
        """课程进度记账（原文 add_completed_task / add_failed_task）。"""
        if success:
            if task not in self.completed_tasks:
                self.completed_tasks.append(task)
        elif task not in self.failed_tasks:
            self.failed_tasks.append(task)

    # ---- 便捷：迭代执行一个任务的完整状态机（react_day 回调里驱动）----
    async def run_task_step(self, task: str, obs: dict,
                            round_state: dict) -> "dict[str, Any] | None":
        """跑「当前任务」的下一轮：生成代码 → 交调用方执行 → 下一轮带回反馈。

        round_state（跨轮持久、随 checkpoint 走）：
            {"round": 已生成的轮数, "last_code": 上一轮代码, "events": 上一轮执行反馈,
             "critique": 上轮 critique, "parse_fail_streak": 连续解析失败计数}
        生成 prompt 读 round_state 里的 events/last_code/critique（上一轮的回灌，原文
        迭代提示机制）；执行后的新 events 由**调用方**写回 round_state（do_step 返回后）。
        返回 {"code": js} 让调用方 do_step 执行；返回 None = 本任务放弃
        （轮数用尽或代码连续解析失败——由调用方 mark_task(False) + 取新任务）。
        本方法不执行代码（执行在 env 侧、带时间栅栏与全部记账），也不判成功
        （判成功在 do_step 返回后调 check_task_success）。
        """
        rnd = int(round_state.get("round", 0))
        if rnd >= self.max_code_rounds:
            return None
        code = await self.generate_code(
            task, obs, last_code=round_state.get("last_code"),
            events=round_state.get("events"), critique=round_state.get("critique", ""),
            context=round_state.get("context", ""),
        )
        round_state["round"] = rnd + 1
        round_state["last_code"] = code
        if code is None:
            # 解析失败也算一轮（原文的执行错误回灌等价物——下一轮模型看到自己上轮没给代码）。
            round_state.setdefault("parse_fail_streak", 0)
            round_state["parse_fail_streak"] += 1
            if round_state["parse_fail_streak"] >= 2:
                return None  # 连续两轮拿不到代码：放弃，别把 4 轮全烧在解析上
        else:
            round_state["parse_fail_streak"] = 0
        return {"code": code} if code else None

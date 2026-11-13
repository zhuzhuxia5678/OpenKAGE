"""技能归属重构（2026-08-27）：技能从公有图谱移出，每臂一个常驻 skill.db + 全员可检索。

# 为什么单独一个模块

原先技能存在两个地方：副脑反思在 ArmGraph 的 skill 表里产出（每窗口 clear_skills 清掉），
聚合 Agent 用 adopt_skill 整段搬进**公有脑 brain.db 的 skill 表**。问题：

1. **聚合后没人维护**：技能一旦 adopt 进公有脑，写它的那个臂的私有副本已被清掉，作者断了
   联系——失败率涨了、过时了，没人更新，只能等聚合 Agent 用 demote 软淘汰（它还看不到代码）。
2. **聚合 Agent 带着一堆 skill 工具**（adopt/merge/demote/search_skill），职责臃肿。
3. 「谁写的谁维护」不成立。

定调（用户拍板）：每臂一个常驻 `skill_{arm_key}.db`（跨 epoch/window 存活，聚合不再清），
公有脑不存 skill，聚合 Agent 的 skill 工具全删；全员共享可检索（执行期检索所有臂，结果标
成功/失败次数 + 来源臂，不去重不合并）；同名冲突从源头防（create_skill 时跨所有臂查函数名，
撞了就拒绝并告诉作者是谁）。

# 本模块两个类

- `SkillStore`：**一个臂**的技能库（独立 sqlite + 向量库，复用 schema.skill 表 DDL）。
  反思期写它（create/update/list），它就是「作者库」。物理隔离 → 多臂并发写各自库无跨臂锁。
- `SkillRegistry`：**跨臂只读**门面。`retrieve_all` 并行查所有臂（不去重、带 from_user + 计数），
  `funcname_taken`/`slug_taken` 跨臂查重供 create_skill 做硬门控。执行期注入/检索走它。

★ 与 BrainGraph 的 skill 方法**签名对齐**（list_verified_skills / retrieve_skills / clear_skills），
便于执行期统一调用。但 SkillStore 是**独立库**，不带 concept/facet/edge——技能与记忆/经验
解耦，跨 epoch 存活不再靠公有脑副本。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path

from openkage.memory.schema import _SCHEMA, _migrate_skill_revision_counts
from openkage.memory._common import _slugify

logger = logging.getLogger("skill_store")

# skill 向量 biz_id 前缀（与 BrainGraph 一致，concept 用无前缀，skill 用 "skill:"）
_SKILL_VEC_PREFIX = "skill:"


def _js_func_name(js_code: str) -> str | None:
    """从 js_code 里抽 `async function NAME` 的 NAME。抽不到返回 None。

    与 openkage.memory.tools._js_func_name 同逻辑（注入 programs 后 Agent 按函数名调用，
    函数名是跨臂命名的真正冲突点 —— 两个臂都写 mineIron 会互相覆盖）。
    """
    m = re.search(r"async\s+function\s+([A-Za-z_$][\w$]*)", js_code or "")
    return m.group(1) if m else None


class SkillStore:
    """一个臂的常驻技能库：独立 sqlite + 向量库，跨 epoch/window 存活。

    与 BrainGraph 同接口的 skill 子集（create/update/list_verified/retrieve/clear），
    但只管 skill 表（不带 concept/facet/edge）。反思期由本臂 Agent 通过工具写它；
    执行期由 SkillRegistry 跨臂读它。

    ★ **一个臂一个文件**（`skill_{arm_key}.db`），不带 epoch/batch 后缀 → 整个 run 复用一份、
      续跑时打开已有文件追加不重建。这是「跨 epoch 存活」的物理保证（取代旧「公有脑副本」）。
    ★ **物理隔离**：各臂独立 conn + lock，多臂并发写各自库无跨臂锁竞争。
    ★ `arm_key` 仅用于日志/来源标注，不进库（一个库就是一个臂）。
    """

    def __init__(
        self,
        db_path: Path,
        vec_db_path: Path | None = None,
        *,
        arm_key: str = "?",
    ) -> None:
        self.arm_key = arm_key
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        # 独立连接（check_same_thread=False：反思/回填可能在多线程/协程下访问）。
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        # 复用整份 _SCHEMA 建表（skill 表 + concept/facet/edge 也会建出来但永远空着——
        # 不碍事、避免另写一份只含 skill 的 DDL 漂移）。skill 是我们唯一用的表。
        self._conn.executescript(_SCHEMA)
        # ★ skill 修订归档三列迁移（2026-08-29）：SkillStore 此前不跑任何迁移，老 skill.db
        #   的 skill 表没有 total_success/total_fail/revise_count 列，CREATE TABLE IF NOT EXISTS
        #   不会给已存在的表加列 → 必须显式迁移。幂等（PRAGMA 查列再 ADD）。
        _migrate_skill_revision_counts(self._conn)
        self._conn.commit()

        from openkage.embedder import get_embedder
        from openkage.memory.vector_store import MemoryVectorStore

        self._vec_store = MemoryVectorStore(
            get_embedder(), vec_db_path or (db_path.parent / f"{db_path.stem}_vec.db")
        )

    # ---- 写：反思期工具调用 ----
    def create_skill(
        self, slug: str, name: str, intent: str, js_code: str, signature: str,
        preconditions: str | None, *, at: int,
    ) -> tuple[bool, str]:
        """新建一个 verified=1 技能。返回 (ok, msg)。

        ★ **不在这里做跨臂查重**——那需要 SkillRegistry（本类只管自己一个库）。
          跨臂守卫（函数名/slug 撞别的臂）由调用方（CreateSkillTool 经 registry）在调本方法前完成。
          本库内的 slug 唯一在这里挡（同臂重复造 → 拒）。
        """
        final_slug = _slugify(slug)
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM skill WHERE slug = ?", (final_slug,)
            ).fetchone():
                return False, f"技能 {final_slug!r} 已存在（本臂已造过）—— 用 update_skill 改进它，或换个 slug"
            self._conn.execute(
                "INSERT INTO skill (slug, name, intent, js_code, signature, preconditions, "
                "verified, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (final_slug, name, intent, js_code, signature,
                 (preconditions or "").strip() or None, at, at),
            )
            self._conn.commit()
        # intent 入向量库（锁外：MemoryVectorStore 自带锁、另一个连接）
        self._vec_store.upsert(_SKILL_VEC_PREFIX + final_slug, intent, {"name": name})
        return True, final_slug

    def update_skill(
        self, slug: str, *, name=None, intent=None, js_code=None, signature=None,
        preconditions=None, at: int,
    ) -> tuple[bool, str]:
        """更新本臂已有技能的可变字段。只传要改的。intent 改了同步重嵌向量。

        ★ **改 js_code 则重置窗口计数**（2026-08-29 两阶段反思；2026-08-30 修订 total_* 语义）：
          旧代码的成败是旧实现的质量，改了代码后给一个干净评价窗口——`success_count`/`fail_count`
          归零，`revise_count` +1。`total_success_count`/`total_fail_count` 由 `add_usage_counts`
          在执行期全程累加（所有版本成败之和，永不清零），revise 时不重复归档。阶段2 据此看
          「改进后窗口效果」+「历史全程可靠性」。只改 intent/name/preconditions（没改代码）
          不重置——那是文档修订、不影响代码质量评价。
        """
        final_slug = _slugify(slug)
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?"); vals.append(name)
        if intent is not None:
            sets.append("intent = ?"); vals.append(intent)
        if js_code is not None:
            sets.append("js_code = ?"); vals.append(js_code)
            # 改代码 → 当前评价窗口计数归零（给改进后代码一个干净窗口），修订次数 +1。
            # ★ total_* 不在这里归档：它已由 add_usage_counts 在执行期全程累加，是「所有版本
            #   成败全程累计」。归档=把当前窗口搬进 total_* 会导致双计。revise 只负责归零窗口。
            #   阶段2反思据此看「改进后窗口(success/fail) vs 历史全程(total_*)」。
            sets.append("success_count = 0")
            sets.append("fail_count = 0")
            sets.append("revise_count = revise_count + 1")
        if signature is not None:
            sets.append("signature = ?"); vals.append(signature)
        if preconditions is not None:
            sets.append("preconditions = ?"); vals.append((preconditions or "").strip() or None)
        if not sets:
            return False, "没有要更新的字段"
        sets.append("updated_at = ?"); vals.append(at)
        vals.append(final_slug)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE skill SET {', '.join(sets)} WHERE slug = ?", tuple(vals))
            if cur.rowcount == 0:
                self._conn.rollback()
                return False, f"技能 {final_slug!r} 不在（本臂没有这个技能）—— 用 create_skill 新建"
            self._conn.commit()
        # intent 改了 → 重嵌向量（锁外，与 create 同理）
        if intent is not None:
            with self._lock:
                row = self._conn.execute(
                    "SELECT name FROM skill WHERE slug=?", (final_slug,)
                ).fetchone()
            nm = row[0] if row else final_slug
            self._vec_store.upsert(_SKILL_VEC_PREFIX + final_slug, intent, {"name": nm})
        return True, final_slug

    # ---- 读：执行期注入 / 跨臂检索 ----
    def list_verified_skills(self) -> list[dict]:
        """列出本臂全部 verified 技能（含 js_code + 计数 + 时间戳）。与 BrainGraph 同签名。

        ★ 带 success/fail + total_success/total_fail + created_at/updated_at（2026-09-01）：
          执行期签名层改为「全局按成功数+时间排序取前 N」（见 `_inject_all_skills`），需要这些
          字段做排序键。一次 SELECT 拿全（不再二次富集），加字段纯增量，调用方按字段名取值不破。
        """
        try:
            rows = self._conn.execute(
                "SELECT slug, name, intent, signature, js_code, preconditions, "
                "success_count, fail_count, total_success_count, total_fail_count, "
                "created_at, updated_at "
                "FROM skill WHERE verified = 1"
            ).fetchall()
        except Exception:  # pylint: disable=broad-except
            logger.warning("[skill_store:%s] list_verified_skills 读 skill 表失败", self.arm_key)
            return []
        return [{"slug": r[0], "name": r[1], "intent": r[2], "signature": r[3],
                 "js_code": r[4], "preconditions": r[5],
                 "success_count": int(r[6] or 0), "fail_count": int(r[7] or 0),
                 "total_success_count": int(r[8] or 0), "total_fail_count": int(r[9] or 0),
                 "created_at": int(r[10] or 0), "updated_at": int(r[11] or 0)} for r in rows]

    def retrieve_skills(self, query: str, limit: int = 5) -> list[dict]:
        """按意图向量检索本臂 verified 技能。与 BrainGraph 同签名/同返回形状。

        返回 [{slug,name,intent,signature,js_code,preconditions,success_count,fail_count,score}]。
        ★ success_count/fail_count（2026-09-04 加）：随结果一次 SELECT 带出，省 retrieve_all 此前
          为补计数而对每臂各跑一次 all_skills_brief 全表扫。下游用 .get(...,0) 兜底（假 store/老路径
          不返回计数时退化为 0，与旧 all_skills_brief 缺数据口径一致）。
        """
        if not (query or "").strip():
            return []
        try:
            hits = self._vec_store.search(query, top_k=limit * 4)
        except Exception:  # pylint: disable=broad-except
            logger.warning("[skill_store:%s] retrieve_skills 向量检索失败 query=%r",
                           self.arm_key, query[:60])
            return []
        # ★ score（余弦相似度 [-1,1]）随结果透传：retrieve_all/find_similar 跨臂查重要用它判
        #   「这技能跟新意图多像」。此前 hits 明明带着 score 却在拼结果时丢了——属于已有数据
        #   白扔。下游不读该键的消费者不受影响（只是多一个字段）。
        score_by_slug = {
            h["biz_id"][len(_SKILL_VEC_PREFIX):]: h["score"]
            for h in hits
            if str(h.get("biz_id", "")).startswith(_SKILL_VEC_PREFIX)
        }
        slugs = list(score_by_slug.keys())
        if not slugs:
            return []
        placeholders = ",".join("?" * len(slugs))
        rows = self._conn.execute(
            f"SELECT slug, name, intent, signature, js_code, preconditions, "
            f"success_count, fail_count "
            f"FROM skill WHERE slug IN ({placeholders}) AND verified = 1",
            tuple(slugs),
        ).fetchall()
        by_slug = {r[0]: r for r in rows}
        out = []
        for s in slugs:
            r = by_slug.get(s)
            if r:
                out.append({"slug": r[0], "name": r[1], "intent": r[2],
                            "signature": r[3], "js_code": r[4], "preconditions": r[5],
                            "success_count": int(r[6] or 0), "fail_count": int(r[7] or 0),
                            "score": round(float(score_by_slug.get(s, 0.0)), 4)})
            if len(out) >= limit:
                break
        return out

    def all_skills_brief(self) -> list[dict]:
        """本臂全部技能的**摘要**（不含 js_code）：供 SkillRegistry 跨臂查重/列示用。

        带 success_count/fail_count/verified——执行期注入要标计数、反思期 search 要看计数。
        ★ 带 total_success/total_fail/revise_count（2026-08-29）：阶段2技能维护要看各技能
          「全员累计成败 + 修订归档」（改进后窗口 vs 历史可靠性）。
        """
        try:
            rows = self._conn.execute(
                "SELECT slug, name, intent, signature, preconditions, "
                "success_count, fail_count, verified, "
                "total_success_count, total_fail_count, revise_count "
                "FROM skill ORDER BY verified DESC, created_at DESC, slug"
            ).fetchall()
        except Exception:  # pylint: disable=broad-except
            return []
        return [{"slug": r[0], "name": r[1], "intent": r[2], "signature": r[3],
                 "preconditions": r[4], "success_count": int(r[5] or 0),
                 "fail_count": int(r[6] or 0), "verified": int(r[7] or 0),
                 "total_success_count": int(r[8] or 0),
                 "total_fail_count": int(r[9] or 0),
                 "revise_count": int(r[10] or 0),
                 "from_user": self.arm_key} for r in rows]

    def add_usage_counts(self, slug: str, ok: int, fail: int, *, at: int) -> None:
        """把一次/一天的成败计数累加进本臂技能行（UPDATE 不命中=非本臂技能，跳过）。

        持本臂 _lock：执行期多臂各自的 night_cb 并发回填，本臂库只被自己写，但仍持锁兜底。

        ★ **同时累加 total_*（2026-08-30 修复）**：`total_success_count`/`total_fail_count` 是
          「所有代码版本的全程累计，永不清零」——执行期每次回填都累加，与 `success_count`/
          `fail_count`（当前版本评价窗口，revise 时归零）互补。此前 total_* 只在 `update_skill`
          改代码时归档当前窗口，从未 revise 的技能恒为 0，无法反映真实历史可靠性。现在 total_*
          在回填时累加 → 永远 ≥ 当前窗口计数，阶段2反思的「历史{tok}/{tfail}」才是真累计。
        """
        if ok == 0 and fail == 0:
            return
        with self._lock:
            cur = self._conn.execute(
                "UPDATE skill SET success_count = success_count + ?, "
                "fail_count = fail_count + ?, "
                "total_success_count = total_success_count + ?, "
                "total_fail_count = total_fail_count + ?, updated_at = ? WHERE slug = ?",
                (ok, fail, ok, fail, at, slug),
            )
            if cur.rowcount > 0:
                logger.info("[skill_store:%s] %s +%d成功 +%d失败（回填作者库）",
                            self.arm_key, slug, ok, fail)
            self._conn.commit()

    def demote(self, slug: str, *, at: int) -> tuple[bool, str]:
        """本臂自己降级自己的技能（verified=0，不再注入/检索命中）。作者自己维护。"""
        final_slug = _slugify(slug)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE skill SET verified = 0, updated_at = ? WHERE slug = ?",
                (at, final_slug))
            if cur.rowcount == 0:
                self._conn.rollback()
                return False, f"技能 {final_slug!r} 不在本臂"
            self._conn.commit()
        try:
            self._vec_store.delete(_SKILL_VEC_PREFIX + final_slug)
        except Exception:  # pylint: disable=broad-except
            pass
        return True, final_slug

    def delete_skill(self, slug: str) -> tuple[bool, str]:
        """本臂硬删除自己的技能：删行 + 删向量，释放 slug/函数名（不可恢复）。

        ★ 与 demote 的差别：demote 软下线（verified=0、行留着、slug/函数名仍占位防
          复活撞名、可 update 恢复）；delete 抹掉整行 → funcname_taken/slug_taken 扫
          全量行时自然查不到 → 名字释放，别人可 create 同名。刚建的废技能没历史，
          槽位不值得保留，用 delete；成熟技能有累计历史要防撞，用 demote。
        """
        final_slug = _slugify(slug)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM skill WHERE slug = ?", (final_slug,))
            if cur.rowcount == 0:
                self._conn.rollback()
                return False, f"技能 {final_slug!r} 不在本臂"
            self._conn.commit()
        try:
            self._vec_store.delete(_SKILL_VEC_PREFIX + final_slug)
        except Exception:  # pylint: disable=broad-except
            pass
        return True, final_slug

    def clear_skills(self) -> dict:
        """清空本臂全部技能（表 + 向量）。★ 聚合不再调（技能跨窗口存活）。

        保留是为了：① 与 BrainGraph.clear_skills 同形状（旧调用点/测试平滑）；
        ② 显式重置场景（如 smoke 重跑）。常驻 run 不应调它。
        """
        with self._lock:
            rows = self._conn.execute("SELECT slug FROM skill").fetchall()
            slugs = [r[0] for r in rows]
            if not slugs:
                return {"skills": 0, "vectors": 0}
            self._conn.execute("DELETE FROM skill")
            self._conn.commit()
        n_vec = 0
        for s in slugs:
            self._vec_store.delete(_SKILL_VEC_PREFIX + s)
            n_vec += 1
        logger.info("[skill_store:%s] clear_skills：删掉 %d 技能 / %d 向量",
                    self.arm_key, len(slugs), n_vec)
        return {"skills": len(slugs), "vectors": n_vec}

    def snapshot_with_vectors(self, path: Path) -> "tuple[Path, Path | None]":
        """快照本臂技能库**连带向量库**，返回 ``(skill 库路径, 向量库路径或 None)``。

        ★ 用途：窗口 checkpoint。skill.db 跨窗口常驻、不随聚合清层（与记忆图谱的
          经验层不同），但续跑/回退重跑必须能把它精确恢复到窗口 W 的态——否则续跑直接
          打开中断时的活 skill.db（含窗口 W 之后产出的"未来技能"），与从 checkpoint{W}/
          private.db 回退的记忆态错配（见 checkpoint.write_arm_graph_checkpoint 的调用）。
          故窗口边界快照一份，续跑时与 private.db 同窗口同源拷回。

        实现照搬 ``BrainGraph.snapshot_with_vectors``（graph.py:1413）：
          · 库用 ``Connection.backup()``（活连接 ``shutil.copy2`` 可能拷到写一半的页）；
          · 向量库先 ``wal_checkpoint(FULL)`` 把 WAL 压回主库再 ``shutil.copy2``
            （sqlite-vec 虚表不保证 backup API 能重建，与 graph.py 同口径）；
          · 向量通道未启用 → 第二返回值 None（快照仍可看结构、只是检索退化）。

        与 ``clear_skills`` 互补：本方法只读快照、不动活库；``clear_skills`` 才删。
        """
        import shutil
        import sqlite3 as _sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            dest = _sqlite3.connect(path)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
        vec_src = getattr(getattr(self, "_vec_store", None), "db_path", None)
        if not vec_src or not getattr(self._vec_store, "enabled", False):
            return path, None
        vec_src = Path(vec_src)
        if not vec_src.exists():
            return path, None
        vec_dst = path.with_name(path.stem + "_vec.db")
        try:
            with self._lock:
                try:
                    self._vec_store._conn.execute(  # noqa: SLF001  与 graph.py 同口径读 vec_store 私有 _conn
                        "PRAGMA wal_checkpoint(FULL)")
                except Exception:  # pylint: disable=broad-except
                    pass  # 没有 WAL 或已 checkpoint —— copy 仍然是完整的
                shutil.copy2(vec_src, vec_dst)
        except Exception:  # pylint: disable=broad-except
            logger.exception("[skill_store:%s] 技能快照向量库复制失败：%s → %s"
                             "（该快照事后无法检索）", self.arm_key, vec_src, vec_dst)
            return path, None
        return path, vec_dst

    def close(self) -> None:
        try:
            self._vec_store.close()
        except Exception:  # pylint: disable=broad-except
            pass
        self._conn.close()


class NullSkillStore:
    """对照组（no_graph）/无图谱臂的空技能库：与 SkillStore 同接口、全 no-op。

    与 NullGraph 同构：不建文件、list_verified/retrieve 恒空、create 是 no-op。
    SkillRegistry 把它当一个空臂处理（检索不命中、查重不占名）。
    """

    arm_key = "?"

    def __init__(self, arm_key: str = "?") -> None:
        self.arm_key = arm_key

    def create_skill(self, *a, **kw) -> tuple[bool, str]:  # noqa: ARG002
        return False, "对照组无技能库"

    def update_skill(self, *a, **kw) -> tuple[bool, str]:  # noqa: ARG002
        return False, "对照组无技能库"

    def list_verified_skills(self) -> list[dict]:
        return []

    def retrieve_skills(self, query: str, limit: int = 5) -> list[dict]:  # noqa: ARG002
        return []

    def all_skills_brief(self) -> list[dict]:
        return []

    def add_usage_counts(self, slug: str, ok: int, fail: int, *, at: int) -> None:  # noqa: ARG002
        return None

    def demote(self, slug: str, *, at: int) -> tuple[bool, str]:  # noqa: ARG002
        return False, "对照组无技能库"

    def delete_skill(self, slug: str) -> tuple[bool, str]:  # noqa: ARG002
        return False, "对照组无技能库"

    def clear_skills(self) -> dict:
        return {"skills": 0, "vectors": 0}

    def snapshot_with_vectors(self, path):  # noqa: ARG002
        return path, None

    def close(self) -> None:
        return None


class SkillRegistry:
    """跨臂只读技能门面：全员检索 + 跨臂查重（执行期 / 反思期共用）。

    构造收 `{arm_key: SkillStore}`。检索对所有臂并行向量检索、**不去重**，每条带 from_user +
    计数。跨臂查重（funcname_taken/slug_taken）供 CreateSkillTool 做硬门控——这是「从源头防
    同名」的落点。

    ★ **只读**：本类不写任何库。写仍由各臂 SkillStore 自己（反思期工具）+ 各臂 add_usage_counts
      （计数回填）。本类只负责「读所有人的 + 防撞名查询」。
    ★ 各臂 SkillStore 自带锁，检索只读、跨臂无锁竞争。
    """

    def __init__(
        self,
        stores: dict[str, "SkillStore | NullSkillStore"],
        *,
        skill_config=None,
    ) -> None:
        # 排除 NullSkillStore（对照组无库），它们既不检索也不占名。
        self._stores: dict[str, "SkillStore | NullSkillStore"] = {
            k: v for k, v in stores.items() if not isinstance(v, NullSkillStore)
        }
        # ★ 技能查重阈值（yml `skill.similar_warn_threshold`），供 find_similar 默认门用。
        #   可选：旧调用/测试不传 → find_similar 的 threshold 走必填参数（不在此兜底默认值，
        #   保持 config「python 侧零默认」纪律）。CreateSkillTool 从这里取 config，没有就跳过警告。
        #   不直接 import SkillConfig 做类型标注，避免 openkage.memory → openkage.config 的导入耦合
        #   （config 是顶层模块，本模块在 memory 子包，反过来依赖会让 config 的测试牵动 memory）。
        self._skill_config = skill_config

    def list_all_verified(self) -> list[dict]:
        """所有臂全部 verified 技能（含 js_code + from_user + 计数 + 时间戳）——执行期全量代码注入用。

        ★ 不去重：两个臂同 slug/同函数名的技能本应已被 create_skill 跨臂守卫挡住；若仍出现
          （老库迁移残留），注入侧兜底取一份并告警。这里如实返回全量。
        ★ 带 success/fail + total_success/total_fail + created_at/updated_at（透传自各臂
          `list_verified_skills`）：执行期签名层「全局按成功数+时间排序取前 N」要用它们做排序键。
        """
        out: list[dict] = []
        for arm_key, store in self._stores.items():
            for sk in store.list_verified_skills():
                sk = dict(sk)
                sk["from_user"] = arm_key
                out.append(sk)
        return out

    def default_topk(self) -> int:
        """签名层默认注入条数（yml `skill.signature_topk`，唯一真相、无 python 兜底）。

        `_make_react_day`/`_inject_all_skills` 不持有 config 对象，从 registry 取——
        构造时已收 `skill_config`（brain.py 传 `SkillConfig.load()`）。
        ★ 无 config 直接报错（保持 config「python 侧零默认」纪律，与 `find_similar` 的 threshold 同）。
          生产路径 brain 总传 config；`_inject_all_skills` 在 registry=None（eval 路径）时已提前返回、
          不会走到这里，故无 config 只可能是测试构造遗漏——报出来比静默兜底一个数更安全。
        """
        if self._skill_config is None:
            raise RuntimeError(
                "SkillRegistry.default_topk() 需要 skill_config（yml skill.signature_topk），"
                "但构造时未传 skill_config——生产路径由 brain.py 传入，测试请显式传 SkillConfig。")
        return self._skill_config.signature_topk

    def retrieve_all(self, query: str, limit: int = 5) -> list[dict]:
        """对所有臂并行向量检索，**不去重**，每条带 from_user + success/fail 计数。

        返回 [{slug,name,intent,signature,js_code,preconditions,success_count,fail_count,from_user}]。
        ★ 带计数：执行期签名块/检索结果要标「来自{arm} · 成功{s}/失败{f}」，让 Agent 知道
          这条是不是自己写的、要不要维护（用户定调）。
        """
        if not (query or "").strip():
            return []
        # 各臂独立检索（自带锁，无跨臂竞争），按臂收集后合并。
        # 各臂各取 limit 个候选（保证小臂的高分技能也进候选池），合并后**全局按 score 降序**
        # 再裁到总 limit——若按臂遍历序裁剪（旧实现），排前面的臂会霸占名额、后位臂的高分技能
        # 被埋没（实测 dave 砍树 score 0.70 被 alice 0.35 挤掉）。全局排序让最相关的浮上来。
        # ★ 计数随 retrieve_skills 一次 SELECT 带回（见 SkillStore.retrieve_skills），不再另跑
        #   all_skills_brief 全表扫补计数。{**sk,...} 直接透传 success_count/fail_count；假 store /
        #   老路径不返回计数时字段缺省，下游用 .get(...,0) 兜底（SearchSkillsTool），与旧口径一致。
        brief_by_slug: dict[tuple[str, str], dict] = {}
        for arm_key, store in self._stores.items():
            for sk in store.retrieve_skills(query, limit=limit):
                key = (arm_key, sk["slug"])
                brief_by_slug[key] = {**sk, "from_user": arm_key}
        # ★ 全局按 score 降序（score 来自各臂 retrieve_skills 的余弦相似度，见 SkillStore.retrieve_skills）。
        #   缺 score（异常/老数据）兜底 0.0 排末尾。排序稳定：同分按 (arm,slug) 字典序。
        ranked = sorted(
            brief_by_slug.values(),
            key=lambda sk: (-float(sk.get("score", 0.0)), sk.get("from_user", ""), sk.get("slug", "")),
        )
        return ranked[:limit]

    def funcname_taken(self, name: str, exclude_arm: str | None = None) -> str | None:
        """某个 JS 函数名是否已被**别的臂**占用。返回占用方 arm_key，未被占返回 None。

        ★ 这是「从源头防同名」的硬门控：两个臂都写 `mineIron` 会注入 programs 互相覆盖。
          create_skill 前调它，撞了就拒（exclude_arm=本臂，允许本臂更新自己已有的同名）。
        ★ 扫各臂全部技能（不只 verified）的 js_code 抽函数名——demote 掉的也占名（避免复活撞名）。
        """
        if not name:
            return None
        for arm_key, store in self._stores.items():
            if arm_key == exclude_arm:
                continue
            try:
                rows = store._conn.execute(  # noqa: SLF001  读 skill 表（store 无公开全量带 js_code 的接口）
                    "SELECT js_code FROM skill"
                ).fetchall()
            except Exception:  # pylint: disable=broad-except
                continue
            for (js,) in rows:
                if _js_func_name(js) == name:
                    return arm_key
        return None

    def slug_taken(self, slug: str, exclude_arm: str | None = None) -> str | None:
        """某个 slug 是否已被别的臂占用。返回占用方 arm_key，未被占返回 None。"""
        if not slug:
            return None
        for arm_key, store in self._stores.items():
            if arm_key == exclude_arm:
                continue
            try:
                if store._conn.execute(  # noqa: SLF001
                    "SELECT 1 FROM skill WHERE slug = ?", (slug,)
                ).fetchone():
                    return arm_key
            except Exception:  # pylint: disable=broad-except
                continue
        return None

    def find_similar(
        self,
        intent: str,
        threshold: float,
        *,
        exclude_arm: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        """跨臂按 intent 检索相似度 ≥ threshold 的已有技能，**带 score**，排除 exclude_arm。

        供 `CreateSkillTool` 在建技能后做语义查重警告用（精确撞名由 funcname_taken/slug_taken 管，
        这里补的是「不同函数名、同一意图」这道语义缺口）。

        返回 `[{slug, name, intent, signature, score, from_user}]`，按 score 降序。

        ★ **threshold 必填**（不在此兜底默认值，保持 config「python 侧零默认」纪律）。
          调用方从 `SkillConfig.similar_warn_threshold` 取；本类持有的 `_skill_config` 只是给
          CreateSkillTool 的便捷来源（见 tools.py CreateSkillTool.run），find_similar 自己不读它。
        ★ **exclude_arm 跳过本臂**：刚 create 的那条会以 score≈1.0 自命中，不排除就永远自报警。
        ★ **只查 verified**：retrieve_skills 底层已 `WHERE verified = 1`——下线的技能不该再作为
          「已有相似技能」劝退新建（它已经被作者判废了）。
        ★ 不补 success/fail 计数：警告只需知道「有谁、多像」，不需要计数（与 retrieve_all 区分）。
        ★ 检索通道挂（embedder 未启用 / 向量库异常）→ 该臂 retrieve_skills 已 broad-except 成 []，
          自然跳过、不抛——警告是旁路，失败不该影响 create_skill 主流程。
        """
        if not (intent or "").strip():
            return []
        out: list[dict] = []
        for arm_key, store in self._stores.items():
            if arm_key == exclude_arm:
                continue
            try:
                hits = store.retrieve_skills(intent, limit=limit)
            except Exception:  # pylint: disable=broad-except
                logger.warning("[skill_registry] find_similar %s 检索失败", arm_key, exc_info=False)
                continue
            for sk in hits:
                score = float(sk.get("score", 0.0))
                if score >= threshold:
                    out.append({"slug": sk["slug"], "name": sk["name"], "intent": sk["intent"],
                                "signature": sk["signature"], "score": round(score, 4),
                                "from_user": arm_key})
        out.sort(key=lambda s: s["score"], reverse=True)
        return out[:limit]

    def store_for(self, arm_key: str) -> "SkillStore | NullSkillStore | None":
        """取某臂的 SkillStore（反思期工具按本臂 key 拿自己的库）。"""
        return self._stores.get(arm_key)

    def arm_only_view(self, arm_key: str) -> "SkillRegistry":
        """返回一个**只读本臂**的 SkillRegistry 视图（消融 A 无共享用）。

        ★ 实现即用本臂单条 store 构造一个新 SkillRegistry 实例——复用全部方法逻辑，零重复。
          视图与原 registry 同一份 `_skill_config`（default_topk/config 同源）。

        ★ 语义自洽性：限本臂下 `_stores` 只含本臂。
          - `list_all_verified` / `retrieve_all` / `all_briefs`：只返本臂技能（`from_user`=本臂）——
            正是「技能只读本臂」要的隔离。
          - `funcname_taken` / `slug_taken` / `find_similar`（CreateSkillTool 调时传 `exclude_arm=本臂`）：
            遍历只含本臂、又被 exclude 跳过 → 恒返 None。这在 A 条件下**正确**——A 里没有「别的臂」，
            本臂自己的同名走 update_skill（被 exclude 跳过是对的），不存在跨臂撞名问题。
          - `store_for(arm_key)`：返本臂 store（与原 registry 一致）。

        无本臂 store（如 arm_key 不存在 / NullSkillStore 已被构造时过滤）→ 视图含空 dict，
        所有检索返 []、查重返 None，等价「该臂无技能」——安全退化。
        """
        store = self._stores.get(arm_key)
        stores = {arm_key: store} if store is not None else {}
        return SkillRegistry(stores, skill_config=self._skill_config)

    def all_briefs(self) -> list[dict]:
        """所有臂全部技能摘要（含计数/from_user/verified）——反思期 search_skills 全量看用。

        与 list_all_verified 的区别：本方法含 verified=0（已降级）的，且不含 js_code。
        """
        out: list[dict] = []
        for arm_key, store in self._stores.items():
            for b in store.all_skills_brief():
                out.append(b)
        return out

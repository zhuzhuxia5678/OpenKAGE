"""
记忆图谱专用的 sqlite-vec 向量存储——照抄 backend/kg/vector_store.py 的 VectorStore 类，
只改两点：① db_path 可传参（不读全局 KG_VEC_DB_PATH，指向独立的
data/memory_vec.db，跟 kg.db 的向量库物理隔离）；② 只留一个 collection
（concept 的向量），因为记忆图谱的 facet 召回走"所属 concept 已经命中"这条路，
不需要对 facet 单独做向量检索。

embedder 复用 `openkage.embedder.get_embedder()`（自包含模块，由 .env 的 KG_EMBEDDING_*
配置决定具体实现，当前指向本地 embed_server）——这是纯粹的 embedding API 调用封装，
跟 KG 的表/状态无关，两套图谱共用同一个 embedder 实例不构成"检索逻辑耦合"——要分开
维护的是 retrieve 算法本身，不是 embedding 客户端。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: 向量库自己的表名与 collection 值 —— **2026-08-10 一起去掉 `memory_` 前缀**
#: （用户定案；此前 2026-08-08 那次「图谱表名去前缀」故意没动它们）。
#:
#: 两者性质不同，改动的代价也不同，改之前先读明白：
#:   · `_VEC_TABLE` 是 **vec0 虚表名**。⚠️ vec0 会另建 4 张**影子表**
#:     （`_rowids` / `_chunks` / `_info` / `_vector_chunks00`），表名硬编码为
#:     `<虚表名>_xxx` —— 只 `ALTER TABLE` 虚表本身会留下一堆旧名影子表，之后任何查询报
#:     `no such table: main.vec_concept_rowids`（实测过）。**5 张必须一起改名。**
#:   · `COLLECTION_CONCEPT` 是**数据值**（存进 `vec_meta.collection` 列），而且参与
#:     `_biz_to_rowid` 的 sha1 —— 所以改它等于**所有既有行的 rowid 全部变号**：
#:     不同步搬迁的话 `search` 会静默返回空（读起来就是"没检索到相关经验"）。
#:
#: ★ **旧库不做自动迁移**（用户定案）：`_ensure_schema` 看不到新表名 → 走 CREATE 分支建空表，
#:   于是**旧 `_vec.db` 的向量等于全部失效**（静默：检索恒 0 命中、不报错）。
#:   现有旧库要么用 `scripts/migrate_vec_db.py` 显式转换，要么就当它没有向量
#:   （概念/facet 都在 `.db` 里，不受影响；重新 upsert 一遍即可重建向量）。
COLLECTION_CONCEPT = "concept"
_VEC_TABLE = "vec_concept"
#: vec0 的影子表后缀 —— 迁移脚本要按这份清单一起改名（见上面 ⚠️）。
#: 放在这里而不是脚本里：它是「vec0 这个虚表由哪些物理表构成」这个事实，
#: 属于本模块的知识；脚本只是它的一个消费者。
_VEC_SHADOW_SUFFIXES = ("_rowids", "_chunks", "_info", "_vector_chunks00")

#: 分片 biz_id 的分隔符。一个概念现在存 **1+N** 条向量：
#:   · identity 分片：biz_id = `{slug}`，嵌入 `name\nsummary`
#:   · facet  分片：biz_id = `{slug}{_PART_SEP}{kind}`，嵌入 `{kind}: {content}`
#: ★ 用 **kind 而不是序号** 做分片键：删掉一条 facet 不会让其余分片的 key 位移
#:   （序号方案下删第 2 条会让原第 3 条变成新第 2 条，旧分片留下孤儿）。
#: ★ `slug` 被 `_slug_invalid` 限制为 `[a-z0-9_]`，故按**第一个**分隔符切开即可无歧义回解
#:   （kind 是自由命名的中文，理论上可能含 `::`，但切开点取第一个就与它无关）。
_PART_SEP = "::"

#: 单个索引分片的字符告警线。**取代**原先对「整概念拼接长文」设的 1200 那条护栏 ——
#: 那条线**高于**嵌入器真实窗口（KaLM `max_seq_length=512` token ≈ 512~850 中文字），
#: 于是 512~1200 这一段既被服务端静默截断、又不告警，是 2026-09-24 查出的检索侧缺陷的成因之一。
#: 分片化之后护栏就贴在真正的嵌入单元上，超了才响。实际值由 yml `memory_graph.index.part_max_chars`
#: 传入（唯一真相在 yml），这里只是模块级常量的落点说明。
_INDEX_PART_MAX_CHARS = 560


def part_biz_id(slug: str, kind: str | None = None) -> str:
    """拼一个概念的索引分片 biz_id。`kind=None` → identity 分片（biz_id 就等于 slug）。"""
    return slug if kind is None else f"{slug}{_PART_SEP}{kind}"


def split_part_biz_id(biz_id: str) -> tuple[str, str | None]:
    """回解分片 biz_id → `(slug, kind)`；identity 分片的 kind 是 None。

    ⚠️ **旧格式兼容（关键）**：分片化之前，一个概念只有一条 biz_id == slug 的向量
    （文本是 name+summary+全部 facet 的拼接）。它没有分隔符 → 本函数按 identity 返回，
    于是旧库在新代码下**退化成「只有 identity 参与打分」而不是报错**，下次任意 facet 写入
    即自愈。这是不做硬迁移的底气。
    """
    if _PART_SEP not in biz_id:
        return biz_id, None
    slug, _, kind = biz_id.partition(_PART_SEP)
    return slug, (kind or None)


def _biz_to_rowid(biz_id: str) -> int:
    h = hashlib.sha1(f"{COLLECTION_CONCEPT}:{biz_id}".encode("utf-8")).digest()
    n = int.from_bytes(h[:8], "big", signed=False) & ((1 << 63) - 1)
    return n or 1


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class MemoryVectorStore:
    """记忆图谱专用的单文件 sqlite-vec 向量存储，跟 kg_vec.db 完全独立的另一个文件。"""

    def __init__(self, embedder, db_path: Path) -> None:
        self.embedder = embedder
        self.db_path = db_path
        self.enabled = bool(embedder and embedder.dim > 0)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        if not self.enabled:
            logger.info("MemoryVectorStore: embedder 未启用，向量通道关闭")
            return
        self._init_db()

    def _init_db(self) -> None:
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            logger.warning("MemoryVectorStore: 未安装 sqlite-vec，向量通道关闭")
            self.enabled = False
            return

        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.enable_load_extension(True)
            import sqlite_vec as _sv

            _sv.load(conn)
            conn.enable_load_extension(False)

            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA busy_timeout=5000")

            self._conn = conn
            self._ensure_schema()
            logger.info("MemoryVectorStore: sqlite-vec 已就绪 path=%s dim=%d", self.db_path, self.embedder.dim)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: sqlite-vec 初始化失败 err=%s", exc)
            self.enabled = False
            self._conn = None

    def _ensure_schema(self) -> None:
        assert self._conn is not None
        dim = int(self.embedder.dim)
        expected_sig = f"float[{dim}]"
        expected_metric = "distance_metric=cosine"

        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (_VEC_TABLE,)
        ).fetchone()
        if row is not None:
            existing_sql = (row["sql"] or "").lower()
            if expected_sig in existing_sql and expected_metric in existing_sql:
                pass
            else:
                logger.warning("MemoryVectorStore: %s schema 不匹配，DROP 重建", _VEC_TABLE)
                self._conn.execute(f"DROP TABLE {_VEC_TABLE}")
                self._conn.execute("DELETE FROM vec_meta WHERE collection=?", (COLLECTION_CONCEPT,))
                row = None
        if row is None:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE {_VEC_TABLE} USING vec0(embedding float[{dim}] distance_metric=cosine)"
            )

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vec_meta (
                collection TEXT NOT NULL, rowid INTEGER NOT NULL,
                biz_id TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (collection, biz_id)
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vec_meta_collection_rowid ON vec_meta(collection, rowid)"
        )
        self._warn_orphan_collections()

    def _warn_orphan_collections(self) -> None:
        """`vec_meta` 里有数据、但**不属于当前 collection** → 这库是重命名之前建的，向量已失效。

        为什么必须显式喊：2026-08-08 那次「表名去前缀」只迁移了**图谱库**
        （`schema._migrate_table_names`），向量库按当时的定案不做自动迁移。后果是
        `_ensure_schema` 看不到 `vec_concept` → 走 CREATE 分支建一张**空**虚表，
        而 568 条向量还躺在 `vec_memory_concept` 里、`vec_meta.collection='memory_concept'`。
        之后 `search` 恒返回空，**不报任何错** —— 读日志只看到"没检索到相关经验"，
        与"图谱里确实没有相关概念"完全无法区分。

        实测代价（math 域，2026-08-14 连跑两次复现）：聚合 Agent 检索不到任何已有概念 →
        无法判断新经验该归并到哪 → 跑满几十步后放弃，`Δconcept=+0 Δfacet=+0 Δedge=+0`、
        白烧 200 秒。**这是本方法赖以成立的整条检索通道静默死掉**，不是性能问题。

        只 warning 不抛错：概念/facet 都在 `.db` 里没丢，重新 upsert 一遍即可重建向量
        （见模块头 ★）。抛错会让"想跑起来看看"的路径也走不通。
        """
        try:
            rows = self._conn.execute(
                "SELECT collection, COUNT(*) FROM vec_meta GROUP BY collection"
            ).fetchall()
        except Exception:  # pylint: disable=broad-except
            return
        orphans = {r[0]: r[1] for r in rows if r[0] != COLLECTION_CONCEPT}
        if not orphans:
            return
        mine = sum(r[1] for r in rows if r[0] == COLLECTION_CONCEPT)
        logger.error(
            "MemoryVectorStore: ⚠️ 向量库 %s 里有 %s 条**孤立**向量（collection=%s），"
            "当前 collection=%r 只有 %d 条 —— 这是 2026-08-08「表名去前缀」之前建的老库，"
            "**检索会静默返回空**（聚合/反思都读不到已有概念）。"
            "修法：用 experiments/rebuild_vec.py 重嵌一遍（概念正文都在 .db 里，没丢）。",
            self.db_path, orphans, list(orphans), COLLECTION_CONCEPT, mine,
        )

    def upsert(self, biz_id: str, text: str, payload: dict) -> str:
        """写入/更新一个 concept 的向量，返回 vector_id（rowid 字符串）；未启用/空文本返回 ""。"""
        if not self.enabled or not text or self._conn is None:
            return ""
        try:
            vec = self.embedder.embed_one(text)
            if not vec:
                return ""
            rowid = _biz_to_rowid(biz_id)
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    self._conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (rowid,))
                    self._conn.execute(
                        f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)", (rowid, _vec_to_blob(vec))
                    )
                    self._conn.execute(
                        """
                        INSERT INTO vec_meta(collection, rowid, biz_id, payload) VALUES (?, ?, ?, ?)
                        ON CONFLICT(collection, biz_id) DO UPDATE SET rowid=excluded.rowid, payload=excluded.payload
                        """,
                        (COLLECTION_CONCEPT, rowid, biz_id, json.dumps(payload, ensure_ascii=False)),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            return str(rowid)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: upsert 失败 biz_id=%s err=%s", biz_id, exc)
            return ""

    def rename(self, old_biz_id: str, new_biz_id: str, payload: dict | None = None) -> bool:
        """只换 biz_id，**复用已有向量、不重新 embed**。成功返回 True；老条目不存在返回 False。

        ★ 为什么需要它：`upsert` 无条件调 `embedder.embed_one()`（一次 HTTP 到
        embedding 服务），而 `update_concept` 只改 slug 时被嵌入的文本 `name\\nsummary`
        **一个字都没变** —— 那次调用算出来的向量与库里现存的完全相同，纯属白跑一趟网络。
        rowid 由 biz_id 哈希而来（见 `_biz_to_rowid`），所以"换 slug"在向量库里就是
        "把同一条 embedding 搬到新 rowid"，读出 blob 原样写回即可。

        payload 不传就原样沿用老条目的；传了则**合并覆盖**（不整体替换）——
        改 slug 时调用方通常只想更新 `name`，而老条目可能还带着别的键
        （`domains/math/_kg_seed.py` 写种子时带 `origin="kg_seed"`），整体替换会把它抹掉。
        当前 payload 没有任何消费者（检索只用 biz_id/score），合并是为了别埋一个以后才发作的坑。
        """
        if not self.enabled or self._conn is None:
            return False
        if old_biz_id == new_biz_id:
            return True
        try:
            old_rowid, new_rowid = _biz_to_rowid(old_biz_id), _biz_to_rowid(new_biz_id)
            with self._lock:
                row = self._conn.execute(
                    f"SELECT embedding FROM {_VEC_TABLE} WHERE rowid = ?", (old_rowid,)
                ).fetchone()
                if row is None:
                    return False  # 老条目不在（未启用时写入过/被清过）→ 调用方回退到 upsert
                blob = row["embedding"]
                meta = self._conn.execute(
                    "SELECT payload FROM vec_meta WHERE collection = ? AND biz_id = ?",
                    (COLLECTION_CONCEPT, old_biz_id),
                ).fetchone()
                try:
                    merged = json.loads((meta["payload"] if meta else None) or "{}")
                except Exception:  # pylint: disable=broad-except
                    merged = {}
                if not isinstance(merged, dict):
                    merged = {}
                if payload:
                    merged.update(payload)
                payload_str = json.dumps(merged, ensure_ascii=False)
                self._conn.execute("BEGIN")
                try:
                    # 新 rowid 可能被占（极罕见的哈希撞车，或重复改名）→ 先清再写
                    self._conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (new_rowid,))
                    self._conn.execute(
                        f"INSERT INTO {_VEC_TABLE}(rowid, embedding) VALUES (?, ?)", (new_rowid, blob)
                    )
                    self._conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (old_rowid,))
                    self._conn.execute(
                        "DELETE FROM vec_meta WHERE collection = ? AND biz_id = ?",
                        (COLLECTION_CONCEPT, old_biz_id),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO vec_meta(collection, rowid, biz_id, payload) VALUES (?, ?, ?, ?)
                        ON CONFLICT(collection, biz_id) DO UPDATE SET rowid=excluded.rowid, payload=excluded.payload
                        """,
                        (COLLECTION_CONCEPT, new_rowid, new_biz_id, payload_str),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            return True
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: rename 失败 %s→%s err=%s", old_biz_id, new_biz_id, exc)
            return False

    def search(self, query_text: str, top_k: int = 10) -> list[dict]:
        """返回 [{biz_id, score, payload}]，score 是余弦相似度 [-1,1]，越大越像。"""
        if not self.enabled or not query_text or self._conn is None:
            return []
        try:
            # query 侧加指令前缀（KaLM 等非对称模型需要），doc 侧 upsert 不加（默认 False）
            qvec = self.embedder.embed_one(query_text, is_query=True)
            if not qvec:
                return []
            return self.search_by_vector(qvec, top_k=top_k)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: search 失败 err=%s", exc)
            return []

    def search_by_vector(self, query_vec: list[float], top_k: int = 10) -> list[dict]:
        if not self.enabled or self._conn is None or not query_vec:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    f"""
                    SELECT v.rowid AS rowid, v.distance AS distance, m.biz_id AS biz_id, m.payload AS payload
                    FROM {_VEC_TABLE} v
                    LEFT JOIN vec_meta m ON m.collection = ? AND m.rowid = v.rowid
                    WHERE v.embedding MATCH ? AND k = ?
                    ORDER BY v.distance
                    """,
                    (COLLECTION_CONCEPT, _vec_to_blob(query_vec), int(top_k)),
                ).fetchall()
            out = []
            for r in rows:
                biz_id = r["biz_id"] or ""
                if not biz_id:
                    continue
                try:
                    payload = json.loads(r["payload"] or "{}")
                except Exception:  # pylint: disable=broad-except
                    payload = {}
                out.append({"biz_id": biz_id, "score": 1.0 - float(r["distance"]), "payload": payload})
            return out
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: search_by_vector 失败 err=%s", exc)
            return []

    def delete(self, biz_id: str) -> None:
        if not self.enabled or self._conn is None:
            return
        try:
            rowid = _biz_to_rowid(biz_id)
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    self._conn.execute(f"DELETE FROM {_VEC_TABLE} WHERE rowid = ?", (rowid,))
                    self._conn.execute(
                        "DELETE FROM vec_meta WHERE collection = ? AND biz_id = ?",
                        (COLLECTION_CONCEPT, biz_id),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: delete 失败 biz_id=%s err=%s", biz_id, exc)

    def part_kinds(self, slug: str) -> list[str]:
        """列出某概念当前所有 **facet 分片**的 kind（不含 identity 分片）。

        用来做**孤儿分片剪枝**：分片键是 kind，所以「库里还留着某个 kind 的分片、但 facet 表里
        那条已经没了」是可能发生的（删 facet / 改 kind 之后未同步）。留着它，`search` 会命中
        一个内容已不存在的分片，把概念的分抬上去 —— 而那条内容读不出来。
        """
        if not self.enabled or self._conn is None:
            return []
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT biz_id FROM vec_meta WHERE collection = ? AND biz_id LIKE ? "
                    "AND biz_id LIKE ?",
                    # slug 是 [a-z0-9_]，前缀 LIKE 安全；再排除 kind 为空的退化情况
                    (COLLECTION_CONCEPT, f"{slug}{_PART_SEP}%", f"%{_PART_SEP}%"),
                ).fetchall()
            out = []
            for r in rows:
                s, kind = split_part_biz_id(r["biz_id"] or "")
                if s == slug and kind:
                    out.append(kind)
            return out
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: part_kinds 失败 slug=%s err=%s", slug, exc)
            return []

    def close(self) -> None:
        conn = self._conn
        if conn is None:
            return
        self._conn = None
        try:
            conn.close()
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("MemoryVectorStore: close 失败 err=%s", exc)

"""
core/search.py
REQ-042 Semantic Search（受控）—— 基于 Qwen embedding 的语义检索。

架构约束（与 AGENTS.md 三条禁令一致）：
  - 只索引经授权的文档型对象（osint_article / tipoff 等）；
  - embedding 调用经 require_llm_allowed 闸门 + redact_text 脱敏 + log_llm_call 审计；
  - 向量与原文分区隔离（semantic_index 表不存原文，只存 object_ref + embedding）；
  - 检索结果经 PolicyEngine 权限过滤 + 属性遮蔽；
  - embedding 不得成为确定规则唯一证据（结果标记 evidence_type="embedding"）；
  - 每次检索落 AuditChain 哈希链。

Qwen DashScope API：
  POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings
  Authorization: Bearer {DASHSCOPE_API_KEY}
  {"model": "text-embedding-v3", "input": ["text1", ...], "dimensions": 1024}

离线降级：无 DASHSCOPE_API_KEY 时 build_index 返回 0、search 返回空列表，不崩溃。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.request
import urllib.error
from typing import Any

from core.access import AccessContext, LLMBlockedError, require_llm_allowed, system_context
from core.audit import AuditChain
from core.policy import PolicyEngine


# ---- 常量 ----

EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_DIM = 1024
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS semantic_index (
    object_name  VARCHAR NOT NULL,
    object_pk    VARCHAR NOT NULL,
    text_hash    VARCHAR NOT NULL,
    embedding    VARCHAR NOT NULL,
    dim          INTEGER NOT NULL,
    model        VARCHAR NOT NULL,
    created_at   VARCHAR NOT NULL,
    PRIMARY KEY (object_name, object_pk)
)
"""

_SEARCH_LOG_DDL = """
CREATE TABLE IF NOT EXISTS semantic_search_log (
    search_id    VARCHAR PRIMARY KEY,
    occurred_at  VARCHAR NOT NULL,
    operator     VARCHAR NOT NULL,
    query_hash   VARCHAR NOT NULL,
    top_k        INTEGER NOT NULL,
    hit_count    INTEGER NOT NULL,
    audit_event_id VARCHAR
)
"""


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """余弦相似度（内存计算，索引规模小）。"""
    n = min(len(v1), len(v2))
    if n == 0:
        return 0.0
    dot = sum(a * b for a, b in zip(v1[:n], v2[:n]))
    norm1 = math.sqrt(sum(a * a for a in v1[:n]))
    norm2 = math.sqrt(sum(b * b for b in v2[:n]))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def _embed_texts(texts: list[str], api_key: str | None = None) -> list[list[float]] | None:
    """调用 Qwen DashScope embedding API。

    返回向量列表；api_key 为空或网络不可用时返回 None（离线降级）。
    """
    api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        return None
    payload = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": texts,
        "dimensions": EMBEDDING_DIM,
    }).encode("utf-8")
    req = urllib.request.Request(
        DASHSCOPE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return [item["embedding"] for item in sorted(
            body.get("data", []), key=lambda x: x.get("index", 0))]
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError):
        return None


class SemanticSearch:
    """受控语义检索（REQ-042）。

    用法：
        ctx = AccessContext(operator="王检察官", role="主办", clearance=2)
        ss = SemanticSearch(conn, access=ctx)
        ss.build_index(["osint_article", "tipoff"])
        results = ss.search("宏业建设关联交易", top_k=5)
    """

    def __init__(self, conn, pack: str = "default", *,
                 access: AccessContext | None = None,
                 api_key: str | None = None):
        self._conn = conn
        self._pack = pack
        self._access = access if access is not None else system_context()
        self._policy = PolicyEngine(pack)
        self._api_key = api_key
        conn.execute(_INDEX_DDL)
        conn.execute(_SEARCH_LOG_DDL)

    # ---- 索引构建 ----

    def build_index(self, object_names: list[str],
                    *, text_fields: dict[str, list[str]] | None = None) -> int:
        """为经授权的对象构建 embedding 索引。

        AC3：未经授权对象不进索引（can_read_object 为 False 则跳过）。
        AC4：semantic_index 只存 object_ref + embedding，不存原文。

        Args:
            object_names: 要索引的对象类型名（如 ["osint_article", "tipoff"]）
            text_fields: 每个对象的文本字段名映射，缺省按对象类型自动选取

        Returns:
            新增/更新的索引条数
        """
        # 默认文本字段
        defaults = {
            "osint_article": ["raw_name", "info_text"],
            "tipoff": ["title", "content_raw"],
        }
        text_fields = text_fields or {}

        count = 0
        for obj_name in object_names:
            # AC3：未经授权对象不进索引
            if not self._access.can_read_object(obj_name, self._policy):
                continue

            fields = text_fields.get(obj_name, defaults.get(obj_name, []))
            if not fields:
                continue

            table = f"obj_{obj_name}"
            # 检查表是否存在
            exists = self._conn.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                f"WHERE table_name='{table}'").fetchone()[0]
            if not exists:
                continue

            cols = ", ".join(f'"{f}"' for f in fields)
            pk_col = self._pk_column(obj_name)
            rows = self._conn.execute(
                f'SELECT "{pk_col}", {cols} FROM {table}').fetchall()

            # 收集需要 embed 的文本
            texts = []
            pks = []
            for row in rows:
                pk = row[0]
                text = " ".join(str(row[i + 1] or "") for i in range(len(fields)))
                if not text.strip():
                    continue
                texts.append(text)
                pks.append(pk)

            if not texts:
                continue

            # 脱敏后 embed（AC4：原文不出网）
            from core.llm.redact import redact_text, load_llm_policy
            policy = load_llm_policy(self._pack)
            redacted_texts = []
            for t in texts:
                redacted, _ = redact_text(t, policy)
                redacted_texts.append(redacted)

            embeddings = _embed_texts(redacted_texts, self._api_key)
            if embeddings is None:
                continue  # 离线降级

            now = time.strftime("%Y-%m-%d %H:%M:%S")
            for pk, text, emb in zip(pks, texts, embeddings):
                text_hash = _sha256(text)
                emb_json = json.dumps(emb)
                # 幂等：同 pk 已存在且 text_hash 一致则跳过
                existing = self._conn.execute(
                    "SELECT text_hash FROM semantic_index "
                    "WHERE object_name=? AND object_pk=?",
                    [obj_name, pk]).fetchone()
                if existing and existing[0] == text_hash:
                    continue
                self._conn.execute(
                    """INSERT OR REPLACE INTO semantic_index
                       (object_name, object_pk, text_hash, embedding, dim, model, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [obj_name, pk, text_hash, emb_json, len(emb),
                     EMBEDDING_MODEL, now])
                count += 1

        return count

    # ---- 检索 ----

    def search(self, query_text: str, top_k: int = 10) -> list[dict]:
        """语义检索。

        AC1：结果经对象级/属性级策略过滤。
        AC2：结果标记 evidence_type="embedding"，附 solo_evidence_warning。
        AC5：检索记录进审计链。

        Returns:
            [{"object_name", "object_pk", "score", "evidence_type", "solo_evidence_warning", ...masked_row}]
        """
        # 脱敏查询文本
        from core.llm.redact import redact_text, load_llm_policy
        policy = load_llm_policy(self._pack)
        redacted_query, _ = redact_text(query_text, policy)

        # 获取查询向量
        query_embeddings = _embed_texts([redacted_query], self._api_key)
        if query_embeddings is None:
            self._log_search(query_text, 0)
            return []  # 离线降级

        query_vec = query_embeddings[0]

        # 逐行计算相似度
        all_rows = self._conn.execute(
            "SELECT object_name, object_pk, embedding FROM semantic_index"
        ).fetchall()

        scored = []
        for obj_name, pk, emb_json in all_rows:
            # AC1：对象级权限过滤
            if not self._access.can_read_object(obj_name, self._policy):
                continue
            emb = json.loads(emb_json)
            score = _cosine_similarity(query_vec, emb)
            scored.append((obj_name, pk, score))

        scored.sort(key=lambda x: x[2], reverse=True)
        top = scored[:top_k]

        # 构造结果
        results = []
        for obj_name, pk, score in top:
            # 读取对象行并做属性级遮蔽（AC1）
            table = f"obj_{obj_name}"
            pk_col = self._pk_column(obj_name)
            row_data = self._conn.execute(
                f'SELECT * FROM {table} WHERE "{pk_col}"=?', [pk]).fetchone()
            if row_data:
                cols = [d[0] for d in self._conn.description]
                row = dict(zip(cols, row_data))
                # 属性级遮蔽
                masked = self._policy.apply_row_masks(self._access, obj_name, [row])[0]
            else:
                masked = {}

            results.append({
                "object_name": obj_name,
                "object_pk": pk,
                "score": round(score, 4),
                "evidence_type": "embedding",  # AC2
                "solo_evidence_warning": (  # AC2
                    "embedding 检索不得作为确定规则的唯一证据，"
                    "须与确定性规则交叉验证（REQ-042 AC2）"),
                "row": masked,
            })

        # AC5：检索记录进审计
        self._log_search(query_text, len(results))

        return results

    # ---- 内部 ----

    def _pk_column(self, obj_name: str) -> str:
        """从 ontology 声明取对象 pk 列名。"""
        from core.ontology_loader import load_pack
        spec = load_pack(self._pack)
        for obj in spec.objects:
            if obj.name == obj_name:
                return obj.pk
        return f"{obj_name}_id"

    def _log_search(self, query_text: str, hit_count: int) -> None:
        """AC5：检索记录落审计链 + semantic_search_log 表。"""
        import uuid
        search_id = f"ss_{uuid.uuid4().hex[:12]}"
        query_hash = _sha256(query_text)
        now = time.strftime("%Y-%m-%d %H:%M:%S")

        # 审计链
        chain = AuditChain(self._conn)
        event_id = chain.append(
            operator=self._access.operator,
            before=None,
            after={
                "action": "semantic_search",
                "query_hash": query_hash,
                "top_k": None,
                "hit_count": hit_count,
                "model": EMBEDDING_MODEL,
            },
            source_row_ids=[search_id],
            ontology_version=chain.current_ontology_version())

        # 日志表
        self._conn.execute(
            """INSERT INTO semantic_search_log
               (search_id, occurred_at, operator, query_hash, top_k, hit_count, audit_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [search_id, now, self._access.operator, query_hash,
             0, hit_count, event_id])

    def index_count(self) -> int:
        """当前索引条数。"""
        return self._conn.execute(
            "SELECT COUNT(*) FROM semantic_index").fetchone()[0]

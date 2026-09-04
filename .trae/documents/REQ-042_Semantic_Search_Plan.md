# REQ-042 Semantic Search 实施方案

## Context

REQ-042 要求实现受控的语义检索：对经授权的 `osint_article`/`tipoff` 等文档型对象做 embedding 索引，
支持自然语言检索，但必须在权限/审计/证据法理的约束下运行。用户指定用 Qwen 在线 embedding API。

当前 `core/search.py` 不存在；`core/llm/redact.py` 已有 LLM 调用闸门（`require_llm_allowed`）、
策略装载（`load_llm_policy`）、脱敏（`redact_text`/`redact_payload`）、审计日志（`log_llm_call`）可复用。

## AC 对照

| AC | 要求 | 实现方式 |
|---|---|---|
| AC1 | 检索结果受对象/属性策略约束 | 检索走 PolicyEngine.check_object + apply_row_masks，与 Gateway 同路径 |
| AC2 | embedding 不得成为确定规则唯一证据 | 返回结果标记 `evidence_type="embedding"`，附 `solo_evidence_warning` |
| AC3 | 未经授权对象不进索引 | build_index 时对每个 object_name 调 `ctx.can_read_object`，无权跳过 |
| AC4 | 向量与原文分区隔离存储 | `semantic_index` 表只存 object_ref + embedding 向量，不存原文 |
| AC5 | 检索记录进审计 | 每次 search 调 `AuditChain.append`，记录 operator/query/top_k/hits |

## 新增文件

### 1. `core/search.py` — SemanticSearch 类

```
SemanticSearch(conn, pack="default", *, access: AccessContext)
├── build_index(object_names: list[str]) -> int  # AC3：只索引有权对象
├── search(query_text: str, top_k=10) -> list[dict]  # AC1/AC2/AC5
├── _embed(texts: list[str]) -> list[list[float]]  # Qwen API 调用
└── _cosine_similarity(v1, v2) -> float  # DuckDB 无向量运算，内存计算
```

**关键设计**：
- `_embed()` 调 Qwen DashScope API（`text-embedding-v3`），经 `require_llm_allowed` 闸门 + `load_llm_policy` 策略 + `redact_text` 脱敏 + `log_llm_call` 审计
- API key 从环境变量 `DASHSCOPE_API_KEY` 读取；未设置时 build_index/search 返回空结果（不崩溃，离线降级）
- `semantic_index` 表 DDL（AC4：向量与原文分区隔离）：
  ```sql
  CREATE TABLE IF NOT EXISTS semantic_index (
      object_name  VARCHAR NOT NULL,   -- obj_osint_article / obj_tipoff
      object_pk    VARCHAR NOT NULL,   -- 代理键
      text_hash    VARCHAR NOT NULL,   -- 原文 sha256（用于幂等/变更检测）
      embedding    VARCHAR NOT NULL,   -- JSON 编码的浮点数组
      dim          INTEGER NOT NULL,   -- 向量维度
      model        VARCHAR NOT NULL,   -- text-embedding-v3
      created_at   VARCHAR NOT NULL,
      PRIMARY KEY (object_name, object_pk)
  )
  ```
- `search()` 流程：
  1. `require_llm_allowed(ctx)` + `redact_text(query_text)` 脱敏查询
  2. `_embed([redacted_query])` 获取查询向量
  3. 从 `semantic_index` 逐行计算余弦相似度（内存，索引规模小）
  4. 对 hit 的 object_name 调 `PolicyEngine.check_object`（AC1：无权结果剔除）
  5. 对 hit 行调 `apply_row_masks`（AC1：属性级遮蔽）
  6. 标记 `evidence_type: "embedding"` + `solo_evidence_warning`（AC2）
  7. `AuditChain.append`（AC5：记录 operator/query_hash/top_k/hit_count）

### 2. `tests/test_semantic_search.py` — 7 项单测

- `test_ac3_unauthorized_not_indexed`：正兵(ctx) 对 tipoff 无权 → build_index 跳过，索引 0 条
- `test_ac1_results_policy_filtered`：主办(ctx) 检索 → 结果经属性级遮蔽（content_raw 被 partial mask）
- `test_ac2_solo_evidence_warning`：结果含 `evidence_type="embedding"` + 警告文本
- `test_ac4_vector_text_separation`：semantic_index 表不含原文 content_raw/info_text 列
- `test_ac5_search_audited`：检索后 audit_chain 多一条，含 query_hash
- `test_offline_no_apikey_graceful`：无 DASHSCOPE_API_KEY → build_index 返回 0，search 返回空列表
- `test_index_idempotent`：同对象重复 build_index 不产生重复行

**测试策略**：`_embed` 方法用 monkeypatch 注入假向量（不依赖网络），验证全链路权限/审计/脱敏逻辑。

### 3. `scripts/semantic_search_demo.py` — CLI 验证脚本

演示：build_index(osint_article) → search("宏业建设关联交易") → 打印结果 + 审计记录

## 修改文件

### 4. `ontology/default/llm_policy.json`

`allowed_models` 增加 `"text-embedding-v3"`（embedding 模型白名单）。

### 5. `run_tests.py`

新增 `search` 测试组：
```python
"search": ("REQ-042 语义检索（受控）", [sys.executable, "-m", "unittest", "tests.test_semantic_search"]),
```

## Qwen Embedding API 调用

```python
# DashScope 兼容模式
POST https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings
Authorization: Bearer {DASHSCOPE_API_KEY}
{"model": "text-embedding-v3", "input": ["text1", "text2"], "dimensions": 1024}
# Response: {"data": [{"embedding": [0.1, ...], "index": 0}]}
```

用 `urllib.request`（标准库，无第三方依赖）发起 HTTP 请求。

## 验证

```bash
# 单测（不需网络/API Key，_embed 被 mock）
python -m unittest tests.test_semantic_search -v

# 端到端 demo（需 DASHSCOPE_API_KEY 环境变量）
set DASHSCOPE_API_KEY=sk-xxx
python -m scripts.semantic_search_demo

# 接入 run_tests.py
python run_tests.py --only search
```

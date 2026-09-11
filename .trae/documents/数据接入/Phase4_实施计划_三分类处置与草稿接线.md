# Phase 4 实施计划：三分类处置界面 + 预演 + 双草稿表接线

> v1.3 §3.6 阶段四 | 依赖：Phase 0-3 已完成

## Context

数据接入向导当前 4 步（上传→列分析→确认→完成），Phase 3 已补齐质量预览面板（P3-1）、ETL 预演端点（P3-3）、binding 级 split（P3-2）。Phase 4 在列分析与确认之间插入 Step 2「质量预览与处置」，让分析师基于已采纳数据元的 `clean_rule` 对列问题做 A/B/C 三分类处置，处置落 `etl_fix_draft` 草稿表（与 `de_recommendation` 分表），草稿不自动生效（红线）。

## 已就绪（无需改）

| 组件 | 状态 | 位置 |
|---|---|---|
| `etl_fix_draft` 表 + 索引 | ✅ | [state_store.py:101-120](file:///d:/dev/inves_duckdb/server/app/store/state_store.py#L101-L120) |
| StateStore 草稿方法（save/get/list/confirm/reject/publish） | ✅ | [state_store.py:306-415](file:///d:/dev/inves_duckdb/server/app/store/state_store.py#L306-L415) |
| P3-3 预演端点 `POST /etl-pipeline/preview` | ✅（缺 `affected_rows`） | [etl.py:275-330](file:///d:/dev/inves_duckdb/server/app/routers/etl.py#L275-L330) |
| 前端 `previewOp` API | ✅ | [etl.ts:134-140](file:///d:/dev/inves_duckdb/frontend/src/api/endpoints/etl.ts#L134-L140) |
| P3-1 质量预览面板（Step 1 内） | ✅ | [IngestView.vue:136-185](file:///d:/dev/inves_duckdb/frontend/src/views/IngestView.vue#L136-L185) |

## 实施步骤

### 1. 后端：source_analyze.py 扩 element_hints + declared_tables_view

**文件**: [source_analyze.py](file:///d:/dev/inves_duckdb/server/app/source_analyze.py)

**1a. `declared_tables_view`（L49-52）加 `object` 字段**：让前端选 target_table 时即知对应 object 名（草稿 `target_object` 用）

```python
entry = tables.setdefault(tbl, {
    "name": tbl,
    "title": obj_title.get(b.object, tbl) if projs else tbl,
    "object": b.object,                       # 新增
    "required_columns": [], "optional_columns": []})
```

**1b. `element_hints`（L170-176）加 `clean_rule` + `format`**：让前端可分类 A/B/C 并建议 op

```python
eid = top["data_element"]
de_spec = elements.get(eid) or {}
cr = de_spec.get("clean_rule")
clean_rule_list = [cr] if isinstance(cr, str) else (cr or [])
element_hints.append({
    ...  # 既有字段不变
    "clean_rule": clean_rule_list,   # 新增
    "format": de_spec.get("format"), # 新增（可 None）
})
```

### 2. 后端：etl.py 预演端点加 affected_rows

**文件**: [etl.py](file:///d:/dev/inves_duckdb/server/app/routers/etl.py) L275-330

在现有 `return ok(...)` 之前，对全量非空行应用 op 计数 `before != after 或 rejected`：

```python
non_empty_all = [v for v in series.tolist() if v and v.strip()]
affected_rows = 0
for v in non_empty_all:
    result = spec.fn(v, clean_ctx, param=param)
    if isinstance(result, tuple) and len(result) == 2:
        after, keep = result
        if not keep or str(after) != v:
            affected_rows += 1
    elif str(result) != v:
        affected_rows += 1
```

返回体加 `"affected_rows": affected_rows`。

### 3. 后端：etl.py 草稿 CRUD 5 端点

**文件**: [etl.py](file:///d:/dev/inves_duckdb/server/app/routers/etl.py) 文件尾部追加

复用 [research.py:116-119](file:///d:/dev/inves_duckdb/server/app/routers/research.py#L116-L119) 的 `_open_state` 模式。需在文件头部追加 import：`StateStore`、`time`、`uuid`、`ERR_CONFLICT`。

| 端点 | 方法 | 调用 StateStore | 说明 |
|---|---|---|---|
| `/cases/{cid}/etl-drafts` | POST | `save_etl_fix_draft` | 创建草稿（A/B 类；C 类不调）；后端生成 `draft_id`；校验 op_token（clean 层防注入） |
| `/cases/{cid}/etl-drafts` | GET | `list_etl_fix_drafts` | 列表（可选 `?upload_id=` 过滤） |
| `/cases/{cid}/etl-drafts/{did}/confirm` | POST | `confirm_etl_fix_draft` | 待复核→已确认（A 类 created_by 自审；B 类需独立 reviewer） |
| `/cases/{cid}/etl-drafts/{did}/reject` | POST | `reject_etl_fix_draft` | 待复核→已驳回 |
| `/cases/{cid}/etl-drafts/{did}/publish` | POST | `publish_etl_fix_draft` | 已确认→已发布（fail-closed；**只改 state，不写 bindings**） |

Pydantic 请求体：
- `EtlDraftIn`: upload_id, target_object, target_prop, op_token, op_class("A"|"B"), preview_affected_rows=0, preview_samples=[], note=""
- `EtlDraftDecideIn`: note=""

### 4. 前端：mapping.ts 扩接口

**文件**: [mapping.ts](file:///d:/dev/inves_duckdb/frontend/src/domain/mapping.ts)

- `ElementHint`（L46-54）加 `clean_rule?: string[]` + `format?: string`
- `DeclaredTable`（L26-31）加 `object?: string`

### 5. 前端：etl.ts 扩 PreviewResult + 草稿 API

**文件**: [etl.ts](file:///d:/dev/inves_duckdb/frontend/src/api/endpoints/etl.ts)

- `PreviewResult`（L87-92）加 `affected_rows: number`
- 新增接口：`EtlFixDraft`、`EtlDraftCreateBody`、`EtlDraftListResult`
- `etlApi` 对象追加 5 方法：`createDraft`、`listDrafts`、`confirmDraft`、`rejectDraft`、`publishDraft`

### 6. 前端：IngestView.vue 插入 Step 2

**文件**: [IngestView.vue](file:///d:/dev/inves_duckdb/frontend/src/views/IngestView.vue)

**6a. STEPS 改 5 步**（L23）:
```typescript
const STEPS = ['上传文件', '列分析与映射', '质量预览与处置', '确认导入', '完成']
```

**6b. step 索引后移**（全文件搜 `step.value =` 和 `step ===` 逐项核对）:
| 原 | 新 | 位置 |
|---|---|---|
| `goConfirm`: `step.value = 2` | `step.value = 2`（语义变→进入 Step 2 处置） | L312 |
| `doImport` 成功: `step.value = 3` | `step.value = 4` | L327 |
| 409 重复: `step.value = 3` | `step.value = 4` | L332 |
| 确认页 v-if: `step === 2` | `step === 3` | L527 |
| 确认页上一步: `step = 1` | `step = 2`（回处置页） | L551 |

**6c. 新增状态与常量**:
```typescript
const CLASS_A_OPS = new Set(['despace','strip_thousands','strip_currency','cn_date_norm','pad_date','to_upper','to_lower'])
const CLASS_B_OPS = new Set(['digits_only','strip_cc','strip_paren','reject_if','trim_prefix','trim_suffix','regex_extract','unit_convert'])
const drafts = ref<EtlFixDraft[]>([])
const previewResult = ref<PreviewResult | null>(null)
const previewingProp = ref('')
const previewBusy = ref(false)
const draftBusy = ref(false)
```

**6d. A/B/C 分类 + 处置项计算**:
- `classifyOp(opName)`: A 类集→'A'，B 类集→'B'，其他→'C'
- `disposalItems` computed: 遍历已映射列，取采纳数据元的 clean_rule，逐 op 分类
- `targetObject` computed: 从 declared_tables 取选中表的 object

**6e. Handler 函数**:
- `applyClassA(item)`: createDraft(op_class="A") → confirmDraft（A 类自审） → 更新 drafts
- `previewClassB(item)`: previewOp → 存 previewResult → 内联展示 affected_rows + samples
- `confirmClassB(item)`: createDraft(op_class="B", preview_affected_rows, preview_samples) → 更新 drafts
- `refreshDrafts()`: listDrafts(upload_id) → 更新 drafts
- `goConfirm()`: 进入 Step 2 前调 refreshDrafts()

**6f. 模板**: 在 `step === 1` 区块后插入 `step === 2` 区块（处置清单 + B 类预演内联展示 + 草稿列表 + 上一步/下一步）；原 `step === 2` 确认页改 `step === 3`

**6g. Step 3 确认页**: 加草稿摘要（chip 列表 + "发布后才写 bindings（后续批次）"提示）

**6h. reset()**: 追加 `drafts.value = []; previewResult.value = null; previewingProp.value = ''`

### 7. Mock：handlers.ts 加预演 affected_rows + 草稿 CRUD

**文件**: [handlers.ts](file:///d:/dev/inves_duckdb/frontend/mocks/handlers.ts) L1206 后追加

- 预演 mock 返回加 `affected_rows`
- 5 个草稿 mock 端点（create/list/confirm/reject/publish），用内存数组存

### 8. 测试：新建 test_etl_drafts.py + 注册

**文件**: `tests/test_etl_drafts.py`（新建）
**注册**: [run_tests.py](file:///d:/dev/inves_duckdb/run_tests.py) L209 后加:
```python
"etldraft": ("v1.3 Phase 4 ETL 处置草稿三分类+预演+双表分离", [sys.executable, "-m", "unittest", "tests.test_etl_drafts"]),
```

测试用例覆盖 IN-TC-05/06/07/18/19/20/22:
- TC-05: 金额列含千分位 → element_hints.clean_rule 含 strip_thousands → A 类创建+自审确认
- TC-06: reject_if 预演返 affected_rows → B 类草稿存 preview_affected_rows + samples
- TC-07: 草稿创建后 GET etl-pipeline 验证 clean 未变（不自动生效）
- TC-18: de_recommendation 与 etl_fix_draft 分表无交叉
- TC-19: A 类 publish 允空 reviewed_by
- TC-20: B 类未确认 publish → 409
- TC-22: 待复核/已驳回 publish → 409

## 关键约束

1. **不写 bindings 红线**: publish 端点只改 state.sqlite 状态；写 bindings.clean/source_sql 属后续 Phase 5
2. **op_token 校验**: createDraft 端点校验 op（clean 层防注入），与 preview 端点同口径
3. **A 类自审**: state_store L391 `op_class != "A"` 才强制 reviewed_by，A 类前端传 p.operator 即可
4. **step 索引**: 改完全文搜 `step.value =` / `step ===` 逐项核对
5. **element_hints 兼容**: 只加可选字段，旧消费方不破坏

## 验证

```bash
# 全量
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py"
# 快速
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --fast"
# 新测试组
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only etldraft"
# 回归相关组
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only ingestapi"
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only m6govern"
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only recommend"
```

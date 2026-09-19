# 画布书证增量 reconcile（轻量补种）实施方案

## Context

真实数据暴露缺口：画布 seed 一次成型（首次 GET 落库，之后幂等回读），seed 后新上传的书证材料与新增核查项**永远不会出现在画布上**。demoW 的 `clue_4c680cc3` 画布 09-13 seed，材料 09-19 上传，画布 evidence 节点数 = 0。

方案（用户选定选项 1）：**GET 画布时增量补缺**——只补「state 有、画布 doc 无」的 `verify_item`/`evidence` 系统节点与 `verify_item-[挂接]→evidence` 边，纯增量、不动人工节点/坐标/version 语义。不做删除/解除挂接回退（本批只增不改删）。

## 设计决策

- **纯函数放 [canvas_seed.py](file:///d:/dev/inves_duckdb/server/app/canvas_seed.py)**，与 seed 同源复用 `sys_node_id` / `_truncate` / 列坐标常量 / 节点·边形状
- **禁止整体 `_apply_positions`**：它无条件覆盖全部节点 x/y 并重排（L379-393），会冲掉用户拖拽。新节点单独算位：x 按 kind 列坐标，y = doc 内同列已有节点计数 × `_Y_GAP`（fact/verify_item 共享 `_fact_col` 口径）
- **与 seed 同口径的过滤**：`status=="建议"` 的核查项不成节点（AC-105-2）；挂接边仅当两端点节点都在 doc 时补
- **乐观并发写回**：复用 `state.update_canvas_doc(expected_version=读到的 version)`（[state_store.py#L1069-L1098](file:///d:/dev/inves_duckdb/server/app/store/state_store.py#L1069-L1098)，PATCH 同款）→ bump version、updated_by=GET 操作人；`CanvasVersionConflict` = 并发他人已补种，回读返回即可（彼时 diff 已空）
- **审计**：仅在有补种时 `_audit(action="canvas.reconcile")`（RC-401 同款，复用 [canvas.py#L151](file:///d:/dev/inves_duckdb/server/app/routers/canvas.py#L151) 现有 helper）
- **无 diff 零副作用**：不写库、不 bump、不审计 → 既有 AC-101-2「二次 GET 文档逐字节一致」测试不破
- **前端零改动**：[ResearchCanvas.vue](file:///d:/dev/inves_duckdb/frontend/src/components/research/ResearchCanvas.vue) `load()` 直接吞 GET 的 doc/version；画布开着时被 reconcile → 下次 PATCH 409 → 既有 re-GET 重试合并链路（L1264-1289）天然兜底

## 改动清单

### 1. `server/app/canvas_seed.py`：新增纯函数 `reconcile_canvas`

```python
def reconcile_canvas(doc, *, verify_items, materials) -> tuple[dict, int, int]:
    """增量补种缺失的 verify_item/evidence 系统节点与挂接边。
    返回 (合并后新 doc, 新增节点数, 新增边数)；无缺失返回原 doc + (0, 0)。"""
```

逻辑（复用 seed_canvas L173-221 的节点构造形状）：
1. 现有节点 id 集合、现有边 id 集合（边 id 格式 `e:{source}--{rel}--{target}`）
2. 缺失核查项：`verify_items` 中 item_id 节点不在 doc 且 `status != "建议"` → 构造与 seed 同形状节点（`adopted: True`，props 同 L194-199）；x=`_X_FACT`，y=doc 内 fact+verify_item 计数 × `_Y_GAP`（含刚加的）
3. 缺失书证：`materials` 中 material_id 节点不在 doc → 同 seed L208-218 形状；x=`_X_EVIDENCE`，y=doc 内 evidence 计数 × `_Y_GAP`
4. 缺失挂接边：`m.item_id` 非空且 `verify_item:{item_id}` 与 `evidence:{mid}` 两节点都在 doc 且边 id 不在集合 → 补 `{id, source, target, rel: "挂接", system: True}`
5. **不改既有节点/边、不重排、不排序**；新节点 append 到末尾

### 2. `server/app/routers/canvas.py`：GET existing 分支接线（L178-181）

```python
existing = state.get_canvas(clue_id)
if existing is not None:
    merged, n_nodes, n_edges = canvas_seed.reconcile_canvas(
        existing["doc"],
        verify_items=state.list_verify_items(clue_id),
        materials=state.list_evidence(clue_id))
    if n_nodes or n_edges:
        try:
            existing = state.update_canvas_doc(
                clue_id, merged, operator=p.operator, updated_at=_now(),
                expected_version=int(existing["version"]))
        except CanvasVersionConflict:
            existing = state.get_canvas(clue_id)   # 并发他人已补种
        else:
            _audit(state, case_id=case_id, version=version,
                   operator=p.operator, action="canvas.reconcile",
                   before=None,
                   after={"action": "canvas.reconcile", "clue_id": clue_id,
                          "added_nodes": n_nodes, "added_edges": n_edges})
    return ok(_payload(existing, seeded=False, semantic_ready=ready),
              data_version=version)
```

输入与 seed 分支完全同源（`list_verify_items(clue_id)` / `list_evidence(clue_id)`）；`CanvasVersionConflict` 已在 canvas.py 导入（PATCH 用）。

### 3. `tests/test_canvas_api.py`：新增用例（夹具复用 `CanvasApiTest` 现有 `_state()` / `_get()`，state 直写用 `upsert_verify_items` / `insert_evidence` / `link_evidence`）

1. **补种成功**：首 GET seed（v1）→ state 直插新核查项 + 新材料并挂接 → 二次 GET：doc 含新 verify_item/evidence 节点与挂接边，`version=2`、`seeded=false`
2. **幂等**：三次 GET doc/version 与二次一致（无 diff 不写库）
3. **建议项不成节点**：`status="建议"` 的核查项 GET 后仍无节点（同 seed 口径）
4. **已挂接边不重复**：seed 前已挂接的材料，reconcile 不加重复边
5. **纯函数边界**（可直接测 `reconcile_canvas`）：corrupt doc 回退形状 `{"nodes": [], "edges": []}` 不炸；入参 doc 不被 mutate（返回新结构）

## 不做的事（边界）

- 前端零改动、零新 spec
- PATCH 校验（`validate_doc_shape`）不动——reconcile 是服务端内部写，不走 PATCH 端点
- 不处理解除挂接/材料删除的回退标记（画布留旧边/旧节点，本批只增）
- 不补 rule/fact/object/source_row 等其他层（那些由语义层版本驱动，生命周期不同）

## 验证

```bash
# WSL venv（不自动跑，用户指示后执行）
wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only canvas"   # 组名以 run_tests.py GROUPS 为准
```

手工验证路径：demoW `clue_4c680cc3` 打开画布 → 应出现 2 个书证节点（微信图片、invoide.png）+ 1 条挂接边。

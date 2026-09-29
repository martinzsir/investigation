# 研判画布渐进式生成 实施计划

## 一、仓库研究结论

### 1.1 v3 业务推演要求的交互

v3 文档是一条分步操作链：打开案件级画布（`c/canvas`）时图上**只有提升的主体**；
选中靶心跑一次镜头，**对应那一组结果**才长到靶心周围（落脚点→地点、异常轨迹→
结论+共现人、通话→关系边）；首个支撑结论出现时 H6 才长出来。每一步只长该步的东西。

### 1.2 当前实现（为什么"一打开全在图上"）

- 持久层：`cases/<cid>/state.sqlite` 的 `case_canvas` 表只存**人工层**
  （手加/提升的主体、人工假设/备注、人工边）。
- 重建层：每次 GET `case-canvas` 调
  [canvas_growth.load_growth_layer()](file:///d:/dev/inves_duckdb/server/app/canvas_growth.py#L404)，
  读 `artifacts/directed_observations.json`（append-only 观察档案，只增不删），
  按 `(origin.node_id × skill_id)` 分组成全部结论节点/地点/共现人/假设/推断边，
  经 [merge_lens_layer()](file:///d:/dev/inves_duckdb/server/app/canvas_case_doc.py#L112)
  **整层合并**后下发。无差别全量——跑过 5 次观察，打开就是 5 组结果全在图上。
- 跑完镜头：[CanvasView.submitLensRun](file:///d:/dev/inves_duckdb/frontend/src/views/CanvasView.vue#L161)
  等任务终态后 `load()` 整页重取 → 仍然是全量重绘。
- 清单其实已有：GET 响应 `lens_layer.groups` 枚举了全部组（靶心/镜头/观察数/
  精度/假设），但前端只拿它做统计标签，没有"显示/不显示"的控制。
- demoX 实测：持久层 0 节点，5 条观察靶心是两个 `case#` 主体——主体从未提升，
  现在图上的人/地/结论全是重建层产物；部分边的靶心端点根本不在文档里（悬空边）。

### 1.3 顺带发现的相关缺陷

1. **线索画布跨域合并**：ResearchCanvas.load() 无条件调
   `refreshGrowthLayer(undefined)` 拉**案件级**整层并进线索 doc——这正是此前
   PATCH 400（M1 不允许新增节点）的同源问题，且会把别条线索/案件画布发起的结果
   挂到线索画布上。线索自身的深挖已有服务端持久机制承接
   （GET reconcile 时 `build_origin_lens_layer` 的代表节点）。
2. **镜头层坐标保存即丢**：split_persistent 把镜头节点（含 x/y/pinned）整体剥除，
   merge 的 R-3 坐标继承对镜头节点实际是死代码——现在保存后重进，所有镜头节点
   位置归零。渐进式"隐藏再显示"会把这个问题放大。
3. **测试未注册**：`test_canvas_growth.py` / `test_canvas_case_http.py` /
   `test_canvas_case_store.py` 未登记进 `run_tests.py` 的 GROUPS（违反 AGENTS.md
   "新增测试组须同步注册"）。

### 1.4 分层原则（设计依据）

观察档案是**累积真源**（只追加），重建层是**可随时清空重算的派生层**，画布文档是
**表达层**。渐进控制属于表达层——只改"派生层里哪些组参与本次渲染"，不碰观察档案、
不删观察。这与此前 400 的教训同源：显示层的合并绝不能写回持久真源。

## 二、方案设计：揭示集（revealed groups）

### 2.1 核心模型

- 一个"可生长单元" = 一个组 `(target_node_id, lens_id)`，与结论节点
  `result_ref = lens_id@靶心ref` 一一对应、幂等稳定（重跑不换 id）。
- 在案件画布持久文档中保存**揭示集**：

  ```json
  "meta": {
    "revealed_groups": [
      {"target": "case#demoX:subject:person_ecb52c3719fc", "lens": "geo_accompany"}
    ],
    "lens_layout": {"case#demoX:analysis_result:...": {"x": 720, "y": 104, "pinned": true}}
  }
  ```

- 语义：
  - 新案件/未操作 → 揭示集为空 → 图上只有人工层（v3 §1 的形态）。
  - 跑镜头成功 → 该组自动入集 → 只有这一组长出来。
  - 手动"显示/隐藏"、一键"全部显示"（接手老案件/演示案件兜底）。
  - 揭示状态与坐标同类，是表达层数据：随案件持久、刷新不丢、同事接手一致；
    **不进**观察档案（证据真源不污染）、**不用** localStorage（不可共享会丢）。

### 2.2 后端派生规则（GET）

1. 从持久文档 meta 读揭示集；
2. `build_lens_result_layers` **只为揭示的组**产出 result/place/subject 节点与边；
3. 假设层用"已揭示结果节点"构建——H6 在第一个支撑它的结论被揭示时才出现，
   推断边也只连已揭示结论（天然满足 v3 §6，无需特判）；
4. `meta.groups` 仍枚举**全部**组，每行补 `revealed` / `lens_name` /
   `target_label` / `on_canvas`，作为前端"可揭示清单"；
5. merge 时防御性丢弃端点缺失的悬空边（计入 meta，不静默丢）。

### 2.3 坐标保持

split_persistent 剥除镜头节点前，把这些节点的 `LAYOUT_FIELDS`（x/y/pinned）
收割进 `meta.lens_layout`；merge_lens_layer 重建后把布局应用回同 id 节点。
隐藏→再显示、保存→重进，正兵摆好的位置不丢。

### 2.4 揭示动作

不新增端点，走现有 PATCH case-canvas（meta 本就随文档持久）：
改 `meta.revealed_groups` → saveCaseCanvas（节点仍被剥、只存 meta+人工层）→ GET。
- 镜头完成：前端把 `(选中靶心, skill_id)` 加入揭示集后保存再重取；
- 面板显示/隐藏/全部显示：同路径；
- 揭示集随 doc 走乐观锁（既有 409 重试逻辑不变）。

### 2.5 前端交互（CanvasView.vue）

1. 工具栏新增「研判结果」面板（N-Drawer/弹层）：按靶心分组列出清单，每行
   镜头中文名 + 观察数 + 精度档 + 关联假设 + 已显示/未显示开关；底部「全部显示」。
2. 靶心节点角标：该靶心下未显示组数（toNodeDatum 注入 badge 数据）。
3. 空态升级：无节点但清单非空时，引导文案改为
   「画布为空：先「加主体」，或显示 N 组已有研判结果」+ 一键全部显示。
4. `submitLensRun` 成功后自动揭示当前组再重取；超时路径提示去面板手动显示。
5. 顶栏「镜头层」标签改为「已揭示 x / y 组」。
6. 物品层是人工登记实体（与主体同级），始终合并，**不参与**渐进揭示。

### 2.6 线索画布收敛（ResearchCanvas.vue）

1. 移除 load() 里的无条件 `refreshGrowthLayer(undefined, true)`：线索画布不再
   合并案件级整层；本线索发起的深挖由既有的 origin_lens 代表节点（服务端 reconcile
   持久）承接。
2. lens-results 端点加可选 `clue_id` 参数：服务端按 `origin.clue_id` 过滤，
   只返回本线索观察（R-3 跨域不混入）。跑完成后的按靶心增量并入保留（纯显示层，
   PATCH 时 persistedNodeIds 安全带继续剥）。
3. 手动「刷新研判结果」按钮同样走选中靶心 + clue 域。

### 2.7 边界与口径

| 场景 | 处理 |
|---|---|
| 靶心节点已不在画布（demoX 现状） | 清单标 `on_canvas=false`；揭示后悬空边丢弃，extra 地点/共现人照常出现；建议先提升主体 |
| 重跑同一 (靶心,镜头) | 组 id 幂等，揭示集不变，结论内容刷新 |
| 一组观察 upsert 增加 | 已揭示则计数/内容自然刷新；未揭示则角标组数提示 |
| H6 只被未揭示结论支撑 | 不出现；任一支撑结论揭示即出现 |
| 批量扫描观察（无 origin.node_id） | 维持 CAN-19 排除，不进组、不参与揭示 |
| 老案件迁移 | 揭示集初始为空 → 最小画布；一键全部显示兜底 |
| v3 红线4（系统边删了不连回） | 当前 CanvasView 无删除 UI，本批不做墓碑，仅记录 |

## 三、文件与模块

### 后端
- `server/app/canvas_growth.py`：`build_lens_result_layers`/`load_growth_layer`
  增加 `revealed: set[tuple] | None`（None=全量，兼容现有直调/测试）与 `clue_id`
  过滤；meta 行补 `revealed/lens_name/target_label/on_canvas`。
- `server/app/canvas_case_doc.py`：split 时收割镜头节点布局到 `meta.lens_layout`；
  merge 时应用回重建节点；merge 丢弃端点缺失的边（计数返回）。
- `server/app/routers/canvas_case.py`：GET 从持久 meta 读揭示集传入；groups 清单
  原样全量下发（带揭示标志）。
- `server/app/routers/canvas_growth.py`：lens-results 增加 `clue_id` 查询参数。

### 前端
- `frontend/src/views/CanvasView.vue`：揭示集读写、研判结果面板、靶心角标、
  空态引导、镜头完成自动揭示、标签文案。
- `frontend/src/api/endpoints/canvas.ts`：`lensResults` 增 `clueId` 参数。
- `frontend/src/components/research/ResearchCanvas.vue`：移除 load 全量并线；
  跑完成/手动刷新带 clue 域。

### 测试
- `tests/test_canvas_growth.py`：空集零节点、部分揭示、假设随首条结论出现、
  clue_id 跨域过滤。
- `tests/test_canvas_case_store.py`（或 doc 单测所在文件）：lens_layout 收割/应用、
  meta.revealed_groups 往返、悬空边剔除计数。
- `tests/test_canvas_case_http.py`：GET 按揭示集过滤 + groups 清单全量；
  PATCH 改 meta 后重取生效；权限不变。
- `run_tests.py`：新增 `canvasgrowth` 组注册上述三个测试模块。
- 前端：CanvasView 面板/自动揭示/空态的纯逻辑抽函数配 spec（按既有 vitest 模式）；
  vue-tsc 零错误。

## 四、实施步骤（依赖序）

1. 后端：`canvas_growth` 揭示过滤 + clue_id 域 + meta 富化（纯函数先行）。
2. 后端：`canvas_case_doc` 布局收割/应用 + 悬空边防御。
3. 后端：两个 router 接线（GET 读 meta、lens-results clue_id）。
4. 后端测试补齐 + 注册 `canvasgrowth` 组，WSL venv 跑 `canvascase`/
   `canvasgrowth` 绿。
5. 前端 API 层 `lensResults(clueId)`。
6. 前端 CanvasView：揭示集状态+保存链路 → 面板/全部显示 → 角标 → 空态 →
   跑完自动揭示。
7. 前端 ResearchCanvas：去全量并线、clue 域收敛；确认线索画布 400 不复发。
8. 前端 spec + vue-tsc；浏览器手工走一遍 v3 §1→§6 链路。

## 五、验证

- `wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only canvasgrowth"`
- `wsl -u root -- bash -c "cd /mnt/d/dev/inves_duckdb && /root/.venvs/inves/bin/python run_tests.py --only canvascase"`
- 手工验收（demoX）：
  1. 初进画布只见人工层（当前为空态+「显示 N 组」引导）；
  2. 全部显示 → 5 组结果出现，H6 出现；隐藏某组 → 其结论/地点消失、H6 随
     最后一条支撑隐藏而消失；拖动后保存重进位置不变；
  3. 选中靶心跑一次镜头 → 只长该靶心该镜头一组；
  4. 线索画布打开不再冒出案件级 case# 节点、PATCH 不再 400。

## 六、风险

- **风险 1：揭示集为空导致老用户以为"数据没了"**。处理：空态显式引导 + 一键
  全部显示；观察档案与组清单照常可查，数据不删。
- **风险 2：meta 被前端旧版本覆盖丢失**。处理：PATCH 剥节点但保留 meta（现有
  dict(d) 透传）；前端每次以服务端 GET 的 meta 为基准做集合增删。
- **风险 3：去全量并线影响线索画布既有深挖体验**。处理：origin_lens 代表节点本就
  持久可见，跑完成的当次增量并线保留；clue 域过滤保证不串组。
- **风险 4：悬空边防御误删**。仅丢弃"两端点在合并后文档中不存在"的重建层边，
  人工边不动，计数进 meta 可审计。

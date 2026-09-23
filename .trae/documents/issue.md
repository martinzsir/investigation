经验要点：只基于客观记录（任务表、ops_events、产物文件）给结论，不做无证据推断。任务已完成，我来查它的真实执行链路和产物。

The user wants me to analyze the execution process and result of task t_e97bdc76a9f5 (LENS_RUN, completed 100%, case "demo", 2026-09-23 23:55:58).

First, let me recall the relevant experiences as instructed. The most relevant ones seem to be about progress/task analysis (2058646) and evidence-based analysis (100035615, 100004045). Let me call ExperienceRecall first, then investigate the actual task data.

Let me start by recalling experience and looking at the task data in parallel... Actually ExperienceRecall must be called first before acting. Let me do that.The experience tells me: use objective evidence (logs, task records, artifacts), don't make claims without evidence. The task is already completed (100%), so I need to analyze its execution process and results from actual records: meta.db ops_events, task records, lens_runs artifacts, directed_observations, etc.

Let me investigate:
1. Task record in meta/meta.db (tasks table, ops_events)
2. Lens run artifact: cases/demo/artifacts/lens_runs/v{N}/{run_id}.json
3. directed_observations.json in cases/demo
4. Worker logs maybe

The task ID is t_e97bdc76a9f5. Case is "demo". Let me query the meta DB and find artifacts. Per project conventions, I should use evidence-based analysis.

Let me look at the database schema first and query records. Also find the lens run artifact.

Let me query the meta.db for the task and its ops events. The task ID is t_e97bdc76a9f5. Let me first check the DB schema and find the task. Also check cases/demo directory.

引号转义问题，改用单引号 SQL：

The quoting got mangled — the `(?)` parens inside double quotes caused issues in bash -c. Better to write the heredoc without inner double quotes escaping issues. The issue is I used escaped double quotes inside a bash -c "..." string and the parens confused bash. Let me use single-quoted SQL inside Python heredoc instead — the whole thing is wrapped in double quotes for bash -c, so single quotes inside are fine.

The fallback line still uses escaped double quotes with parens. Let me remove that fallback or rewrite using single quotes. The LIKE pattern with single quotes inside bash double-quoted command is fine.

任务记录拿到了：case 是 testD、镜头 timeline_rhythm、靶心「李志强妻弟」、画布从 clue_473b167d 发起、约 1 秒完成。查 ops_events 时间线和产物：

Task record:
- case_id = testD (not "demo" — "demo" is the created_by user)
- skill: timeline_rhythm, target_subject: 李志强妻弟
- origin: clue_473b167d, canvas
- SUCCEEDED, 100%, retry 0
- Duration: 23:55:57 → 23:55:58, ~1 second
- idem_key version 7

Now let me get ops_events for this task, and find the lens run artifact.

ops_events has payload JSON. Let me query by case_id and filter payload containing task id, or check what events exist around that timestamp.

避免双引号，全部用单引号：

The escaped double quotes keep breaking. The issue: inside `bash -c "..."`, my `\"` sequences seem to be getting unescaped badly by PowerShell→bash translation. I should avoid double quotes entirely in the Python code and use single quotes only. But SQL strings need quotes — single quotes work in Python and bash double-quote wrapper allows single quotes fine. Let me rewrite using only single quotes in Python. The LIKE pattern with % inside single quotes is fine.

关键信号：任务虽 SUCCEEDED，但 ops 记录 **observations: 0**（且 degraded 为空）。读运行产物确认原因：

Critical finding: the lens_run event recorded **observations: 0**, observation_ids: [], degraded: [].

So the task SUCCEEDED in 1 second but produced **zero observations** — and importantly `degraded: []` (no degradation reason recorded in the event payload, but let me check the artifact itself).

Target: 李志强妻弟 (Li Zhiqiang's brother-in-law), target_type person. Possibly this person doesn't exist in the semantic layer, or has <2 events (rhythm degrades when events < 2). Let me read the artifact file.

产物只有 375 字节：`observations: []`、`clues: []`，连 degraded 记录都没有。我需要弄清零产出的原因——先看 worker 的降级/空结果分支逻辑，再查语义层里这个主体是否存在、有多少事件。

The artifact confirms: clues: [], observations: [], observation_ids: [] — completely empty, only 375 bytes. No degraded info in the artifact either.

So the rhythm lens on 「李志强妻弟」 produced nothing. Possible reasons:
1. Subject doesn't exist in obj_person (but per project memory, subject_id is required with existence validation returning 400 — but here target_subject is a name, not ID; target_type=person was injected)
2. Subject exists but has < 2 events → degraded (but degraded list in ops event was [])
3. Some filter path silently produced no observation

Wait — the degraded was empty in the event. Let me check the worker lens_run code to understand under what conditions observations is 0 vs degraded. Also let me check whether 李志强妻弟 exists in obj_person, and count their events in the v7 duckdb.

Let me look at the worker code first, then query the semantic layer.

worker 逻辑明确：零观察 = `skill_invoke` 返回空线索列表（节奏镜头在事件不足时会"降级为节奏不可计算，不造结论"）。直接查 v7 语义层，确认这个主体是否存在、有多少事件：

The worker calls `skill_invoke(reg, sid, store=det, ctx=ctx, params=lens_params)`. Zero clues returned means the skill handler returned an empty list. For timeline_rhythm, the function degrades when events < 2 ("事件不足 2 起降级为节奏不可计算，不造结论"). But where does degradation get recorded? `ctx["degraded"]` is populated by skill_invoke wrappers when a skill fails/is isolated. But the rhythm function itself returning degraded output ("节奏不可计算") likely returns no clues silently.

Key question: does 李志强妻弟 exist in obj_person at v7, and how many events do they have? Let me query the v7 duckdb. The version path: factory.version_path(case.id, version) → cases/testD/... let me find the v7 duckdb file. Earlier memory mentioned v7.duckdb.

Also the name 李志强妻弟 — interesting. 李志强 is a person in testD (from earlier directed_observations). 李志强妻弟 might not exist as a person name; could be a relation label. Let me check obj_person names.

Again the escaped double-quote issue. The PowerShell wrapper strips/transforms `\"` and parens break. I must use single quotes only inside Python. LIKE patterns with Chinese need quoting — use single quotes in SQL, and parameterized queries to avoid quote conflicts.

The inner double quotes on line 6 (information_schema query) broke again. I keep accidentally leaving double quotes. Rule: NO double quotes anywhere inside the Python heredoc. Single quotes work fine inside bash -c "...".

The person name column is `raw_name`, not `name`. Let me check the obj_person schema.

obj_person 只有 2 人：**张卫国、李志强——不存在「李志强妻弟」**。但 ops 记录里 `target_type` 被自动补成了 `person`。我看下 `_probe_type` 的消歧逻辑和节奏镜头的主体解析/空结果分支：

obj_person only has 2 rows: 张卫国 and 李志强. There is NO 「李志强妻弟」 in obj_person.

So target_subject=李志强妻弟 doesn't exist as a person. Now, what does resolve_subject do? The worker auto-disambiguates target_type via _probe_type. The ops event recorded params with target_type=person — meaning _probe_type returned 'person'?? Wait, but the person doesn't exist. Let me check _probe_type and the rhythm handler's subject resolution.

Actually wait — the recorded params were `{"target_subject": "李志强妻弟", "target_type": "person"}`. The probe found type=person. How? Maybe _probe_type checks person first and returns person on partial/contains match? Or maybe resolve happens by fuzzy matching.

Let me check core/focus.py _probe_type and the timeline_rhythm skill handler / fn to understand zero-clue behavior.

消歧链路确认：语义层查无此人 → 形态兜底把「李志强妻弟」判成 person。再看节奏镜头 handler 对"主体解析不到"的处理：

So `_probe_type('李志强妻弟')`:
1. Not in any semantic table
2. Not in event tables
3. Shape fallback: `classify_entity_type('李志强妻弟')` → likely "person" (Chinese name-like string), hence target_type=person recorded.

Then the skill runs with target_type=person, target_subject=李志强妻弟. resolve_subject presumably returns None cleanly (name not found). Then the rhythm skill/function yields no events → no clues/observations, no degraded record (silent empty).

Let me verify how the rhythm skill handler resolves and what it does with an unresolvable subject. Let me find the skill handler for timeline_rhythm in skills/.

`hit` 为假即返回空。直接用相同参数对 v7 库实跑一次函数，拿到 `hit/degraded_reason` 的客观输出：

The rhythm lens returns [] when `r.get("hit")` is falsy. So I need to see `_timeline_rhythm` function in core/functions.py to confirm: subject not resolvable → hit=False (and likely degraded_reason like "主体不存在" or "事件不足"). Let me run the function directly to get actual output, which is the strongest evidence. I can invoke it via the same code path quickly, or just read the function. Better: execute fn_timeline_rhythm against v7.duckdb with the exact params.

ctx 需要注册表情景。改用与 worker 同径的 `skill_invoke` 实跑：

I called the function wrong — ctx needs a proper context object. Let me look at how skill_invoke sets it up, or just read the function code to see the degraded paths. Simpler: invoke via skill_invoke like the worker does, which wires ctx correctly. Let me do that with the registry bootstrap.

The skill is in the timeline pack, loaded by pack loader, not skills registry_bootstrap. Let me check how detect/worker loads packs — probably `load_pack` into registry or registry.bootstrap packs. Let me check how packs/timeline gets registered. In lens_run worker, `get_registry()` returns default registry with 5 built-in skills after registry_bootstrap import. But timeline_rhythm is in packs/timeline. How does the worker find it? Maybe detect.py registers packs. Let me search.

Need to call `core.pack_loader.discover()` before skill_invoke. Let me redo the reproduction.

复现成功：0 线索、无降级留痕。再取函数层的原始输出看具体判语：

Reproduced: 0 clues, no degraded record. Now let me get the actual function result (hit/degraded_reason) to explain WHY. I need to call the function with a proper ctx (the registry provides a context with .table). Let me look at how `_invoke` in packs/timeline/impl.py calls it and mimic. Or directly read the `_timeline_rhythm` function in core/functions.py lines 924+.

The function returns `hit=False, degraded=True, degraded_reason="主体 '李志强妻弟' 在语义层实体中不存在"` when subject resolves to None.

Now I want to confirm the exact branch taken (subject None vs <2 events). Since the person doesn't exist in obj_person, resolve_subject should return None. Let me verify resolve_subject behavior with target_type='person' explicitly (it queries obj_person only, no match → None). Also I want to capture the actual function-level result. I can build the ctx the same way the pack impl does — check `_invoke` in packs/timeline/impl.py and how ctx is passed. Actually the function's ctx comes from skill_invoke's machinery (miao context). Easier: call resolve_subject directly with a real ctx from... hmm.

Alternative: check how skill_invoke invokes and whether health captures degraded output. Let me just directly verify resolve_subject returns None by reading graph.py around line 597, and construct minimal ctx. Actually `_resolve_subject` is imported in functions.py — let me check its signature and try calling with a context built via registry. 

Simpler: use the pack's `_invoke` path? Let me look at packs/timeline/impl.py top to see `_invoke`.

直接调 FunctionExecutor 取函数层原始判语：

I can call FunctionExecutor directly with the store to capture the raw degraded_reason. Let me check FunctionExecutor signature quickly — `FunctionExecutor(store, health=health).invoke(fn_name, fn_params)`. Health can probably be None or a health object. Let me try with None.

根因实锤。再补两块证据：①「李志强妻弟」在原始数据里是否出现过（名字写错 vs 未入语义层）；②23:56 那次 31KB 的 run 是否是后续成功重跑：

Confirmed root cause:
- 「李志强妻弟」 does not exist in the semantic layer (obj_person has only 张卫国、李志强)
- Function degraded: "主体 '李志强妻弟' 在语义层实体中不存在", hit=False → rhythm_lens returns [] → 0 observations
- Baseline: 李志强 himself has 114 events, 29 bursts — works fine

Why degraded list is empty in ctx: the function-level degradation (hit=False with degraded_reason) is a result payload, not a skill exception. skill_invoke only records `ctx['degraded']` when a skill raises/is isolated. The pack lens swallows the degraded result silently (`if not r.get("hit"): return []`) without surfacing degraded_reason anywhere in the run artifact. That's why ops event shows degraded: [] — the user gets a "successful but empty" task with no explanation.

Also check: where did the user input come from? origin clue_473b167d — a canvas clue. The name 李志强妻弟 is likely a relation mentioned in the case data (e.g., 通话记录 brief or a person note) but never materialized as obj_person entity. Let me quickly check whether this string appears anywhere in the semantic tables or raw parquet — to tell the user whether it's "wrong name" vs "person exists in raw data but wasn't materialized". Let me search event tables (obj_call briefs etc.) and raw data.

Also check directed_observations.json to confirm nothing was added (save only when observations non-empty — so file unchanged). And check whether later run at 23:56 (lensrun_095eba827f1f, 31KB) was the successful follow-up — that might be the user retrying with 李志强 or 张卫国. Let me peek at that to complete the story (execution process timeline).

「李志强妻弟」的真实出处找到了——在 obj_org 里是 **A建材某列的值**（工商信息中的关系称谓），不是人名实体。确认列名，并看发起线索 clue_473b167d 是什么：

Very interesting finding!

「李志强妻弟」 appears in obj_org — but NOT as an entity name. It's a column value: org_355b4a46040b = 'A建材' with some column = '李志强妻弟' (likely legal representative / 法定代表人 or shareholder column). So "李志强妻弟" is a RELATION descriptor in 工商信息 (business registration) for company A建材 — i.e., the boss/legal rep of A建材 is recorded as "李志强妻弟" (Li Zhiqiang's brother-in-law), an unnamed person. It was never materialized as a person entity in obj_person (person entities come from 通话记录 raw names: only 张卫国, 李志强).

Let me confirm which column in obj_org contains 李志强妻弟. The DESCRIBE showed columns order; let me get column names. From the tuple: ('org_355b4a46040b', 'A建材', '李志强妻弟', '存续', '', 'source_rows'). Likely columns: org_id, raw_name, legal_rep(?), status, ... Let me verify column names quickly.

Then the follow-up at 23:56:24: user re-ran with target 李志强 → success, obs_34dcc74d6b23eda8, 29 bursts. This matches the directed_observations we saw earlier (obs_c37... 张卫国 and obs_34dc... 李志强).

So complete story:
1. 23:55:57 user (demo) on canvas, from clue clue_473b167d, launched rhythm lens with target 李志强妻弟
2. Route pre-check passed (name is free text, no existence validation for lens run params at that layer)
3. Worker: target_type missing → _probe_type: not in any obj table/name_property... wait, but 李志强妻弟 IS a value in some non-name column of obj_org. _in_table checks `WHERE "<col>" = name` using the enum source column for org (probably raw_name = 'A建材'), so no match. Then event tables' name_property columns — no. Then shape classifier → person. So target_type=person auto-filled.
4. Function: resolve_subject(person) → obj_person lookup → None → degraded result "主体不存在", hit=False
5. rhythm_lens: hit False → return [] → 0 observations; degraded reason NOT propagated to ctx.degraded (only skill-level isolation populates that) → task SUCCEEDED with empty artifact (375 bytes), ops degraded=[]
6. save_directed_observations skipped (nothing to save)
7. 26 seconds later (23:56:24) a run with 李志强 succeeded with 1 observation (29 bursts)

Also worth checking: the clue_473b167d origin — what is that clue? Maybe to explain where the user got "李志强妻弟" from. Let me check quickly in clues artifacts of v7. And confirm obj_org column name. Let me do both quickly.

证据链完整，收尾汇总。

Complete picture now:

- obj_org A建材: legal_rep = '李志强妻弟' — the legal representative is recorded by RELATION TITLE ("Li Zhiqiang's brother-in-law"), not a real name. So this person has no independent identity in obj_person (only 张卫国/李志强 from call records).
- Origin clue: clue_473b167d "奇正分工方案 · 张卫国" (qi_zheng skill — one of the 5 built-in 庙算 skills). The user likely saw 李志强妻弟 mentioned in that analysis and ran the rhythm lens against that name from the canvas.

Let me also verify the directed_observations.json wasn't touched (no obs saved for this run — file mtime or content). Actually save_directed_observations is only called if observations non-empty. The successful 23:56 run (李志强) did save obs_34dcc... We already saw the file contains 3 observations (张卫国 sequence, 张卫国 rhythm, 李志强 rhythm). So the empty run left no trace there — correct.

Also let me double check timing: task created 23:55:57, ops event 23:55:57, artifact file mtime 23:55, task finished 23:55:58. ~1s.

One more check for completeness: the route-side precheck — project memory says "subject_id 必填，后端存在性校验（按类型路由查询对应表...不存在则返回 400）". That's for a different endpoint. Here lens run accepted the nonexistent name and queued successfully. I could note this asymmetry, but let me verify the lenses run route doesn't do existence validation — from memory: "路由预检未声明参数/必填缺失 400 不入队". It validates params schema, not subject existence. So nonexistent target → queued → empty result. Worth reporting as the UX gap: "100% 成功但零结果且无任何提示原因".

Evidence chain is complete. Update todos and write the analysis. I should present:

1. 任务概况 (from tasks table)
2. 执行过程时间线
3. 结果：零观察，产物 375 字节
4. 根因（实跑证据）
5. 数据层面的解释（legal_rep 是关系称谓，未实体化）
6. 后续：23:56 用户改用李志强重跑成功
7. 一个产品层面的观察：静默空结果（degraded_reason 没有透出到 run 产物/ops），用户看到的是"成功但什么都没发生"

Don't overstep — user asked to analyze, not fix. I can offer a follow-up suggestion but shouldn't change code.

Let me finalize todos.

Now write the final analysis in Chinese, evidence-based, with file references. No code changes (user asked for analysis only).
# 任务 t_e97bdc76a9f5 执行分析

## 一句话结论

任务**状态成功、结果为空**：对「李志强妻弟」跑周期节奏镜头，因该名称在语义层不是实体（只是工商信息里 A建材法人的**关系称谓**），函数降级为"主体不存在"，镜头零命中不造观察。约 26 秒后改用「李志强」重跑即成功。

## 执行过程（有记录为证）

| 环节 | 记录 |
|---|---|
| 发起 | 2026-09-23 23:55:57，操作人 demo，画布发起（surface=canvas），来源线索 clue_473b167d「奇正分工方案 · 张卫国」（庙算 qi_zheng 技能产出） |
| 参数 | skill=`timeline_rhythm`，target_subject=**李志强妻弟**，案件 testD、版本 v7 |
| 参数补齐 | 未传 target_type，worker 的 `_probe_type` 消歧：语义层各表实查无此名 → 名称形态兜底判定为 person（[focus.py](file:///d:/dev/inves_duckdb/core/focus.py#L324-L354)） |
| 执行 | 23:55:57 → 23:55:58，约 1 秒，retry=0，无报错 |
| 落盘 | [lensrun_34c89a154fb7.json](file:///d:/dev/inves_duckdb/cases/testD/artifacts/lens_runs/v7/lensrun_34c89a154fb7.json)（仅 375 字节）：`observations: []`、`clues: []` |
| 审计 | ops_events #185：`observations: 0, observation_ids: [], degraded: []`；因零观察未写 directed_observations.json（worker 仅在有观察时持久化） |

## 零结果根因（同参数对 v7 库实跑复现）

函数层原始输出：

```json
{"hit": false, "degraded": true,
 "degraded_reason": "主体 '李志强妻弟' 在语义层实体中不存在",
 "event_count": 0, "subject": null}
```

- v7 语义层 `obj_person` 只有 **张卫国、李志强** 两人（均来自通话记录），没有「李志强妻弟」
- 该字样唯一出处：`obj_org` 中 **A建材（org_355b4a46040b）的 legal_rep 字段 = "李志强妻弟"**——工商登记用关系称谓指代法人，从未实体化为 person；obj_call/transaction/trackpoint 也无此字样
- 节奏函数在主体解析为 None 时返回 hit=False（[functions.py](file:///d:/dev/inves_duckdb/core/functions.py#L934-L939)），镜头编排层按"零命中不造线索"返回空列表（[impl.py](file:///d:/dev/inves_duckdb/packs/timeline/impl.py#L144-L145)），整个过程无异常，所以任务 SUCCEEDED

对照基线：对「李志强」本人实跑 = hit，114 起事件、29 个聚集簇、中位间隔 4 天。

## 后续动作

23:56:24 你改用「李志强」重跑（lensrun_095eba827f1f，31KB），成功产出 1 条观察 obs_34dcc74d6b23eda8「李志强 的事件节奏：29 个聚集簇、常态间隔中位数 4 天」，已进 directed_observations.json。

## 一个值得注意的产品缺口

这次"失败"在界面上完全无声：函数的 `degraded_reason`（主体不存在/事件不足）**没有透传**到 lens_run 产物和 ops 事件——`ctx['degraded']` 只记录镜头抛异常被隔离的情况，而零命中走的是正常返回空。用户看到的是"100% 完成、无观察、无原因"。如果需要，我可以评估把函数层降级原因透出到 run 产物（零观察时给前端一句明确提示，如"主体不存在于语义层，请检查名称或先补充数据源"），这次先未改任何代码。
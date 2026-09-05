字段确认完毕，缺口是确凿的：`run_all.py` L243 只在 `alarm` 为真时才记录诊断，而 `alarm` 由声明轨独占决定——`empirical_missing` 虽然在 L248 被传进 `record()`，但那条 `record` 压根不会执行。

---

## REQ-G-024　实证覆盖缺口应独立报警

**优先级**：P2
**分类**：覆盖度实证化（REQ-G-008 的补完）
**发现方式**：远端 ontology_v2 实跑，声明轨 5/5 不报警而实证轨仅 3/5

---

### 一、问题

`core/hypotheses.py:dimension_coverage()` 已按 REQ-G-008 输出双轨，但报警逻辑只看了声明轨：

```python
score = len(declared) / len(self.DIMENSIONS)
alarm = score <= self.DIMENSION_ALARM if declared_missing else False   # L288
```

`empirical_missing` 计算出来了（L285），也在 `run_all.py` L248 被传进 `record(...)`，但那段代码位于 `if _dc.get("alarm")` 之内——**声明轨不报警时，整段不执行，实证缺口根本不产生诊断**。

### 二、后果

远端实跑的实际状态：

```
声明轨：关系、时间、行为、资金、通讯  5/5  score=1.0  alarm=False
实证轨：关系、资金、通讯              3/5  缺【行为、时间】
健康度：healthy   诊断 4 条（全是 info，warning = 0）
```

**一份报告显示"维度覆盖 100%、无报警、健康度 healthy"，而五个维度里有两个实际没有任何证据产出。**

这正是 REQ-G-008 设计双轨时要防的事——"声称冒充实证"。双轨把两组数字都摆出来了，但报警没跟上，于是使用者看到的是"healthy"。

具体到本案：H3 声明覆盖「通讯+行为」，系统就认为行为维度已覆盖；而 R4（唯一的行为维度规则）实际 `data_absent`，scan=0。**想到了但没看到，系统报的是"没问题"。**

### 三、方案

实证缺口独立报警，**不与声明轨共用措辞**——两者指向不同的补救动作：

| 缺口类型 | 含义 | 使用者该做什么 |
|---|---|---|
| 声明缺失 | 假设设计时没想到 | 补假设 / 人工注入 |
| **实证缺失** | 想到了但没查到 | **补数据 / 查检测器是否失效** |

建议在返回值中增加：

```python
"empirical_alarm": bool(empirical_missing),
"empirical_alarm_text": (
    f"该维度已声明假设但实际无证据产出（{len(empirical)}/{len(self.DIMENSIONS)}），"
    f"缺：{'、'.join(empirical_missing)}；建议核查数据源或检测器"
    if empirical_missing else ""),
```

并在 `run_all.py` 中独立记录诊断（不再依赖声明轨的 alarm 分支）：

```python
if _dc.get("empirical_alarm"):
    health.record("coverage_gap", "warning",
                  source="miaosuan:dimension:empirical",
                  reason=_dc.get("empirical_alarm_text"),
                  missing=_dc.get("empirical_missing"))
```

### 四、验收标准

1. 构造声明轨 5/5、实证轨 3/5 的场景，断言 `empirical_alarm` 为 true
2. 断言该场景健康度 `status` 不为 `healthy`，且 warning 计数 ≥ 1
3. 断言诊断 `source` 可区分声明缺口与实证缺口（不混淆）
4. 构造两轨均 5/5 的场景，断言均不报警（不误报）
5. 构造实证轨全空（findings 为空）的场景，断言报警且列出全部缺维
6. 回归：既有 `alarm` / `alarm_text` / `covered` / `missing` / `score` 语义不变

### 五、原因

REQ-G-008 建立了双轨，REQ-G-009 修了声明轨的边界值，但报警这一层漏了实证轨。结果是双轨数据"可见但不可行动"——数字摆在那里，系统却不据此提示。

这与本系列反复出现的病根一致：**信息被采集了，但没有被消费**。REQ-G-017（推翻率只记录不告警）是同一形态。

### 六、价值

1. 让"想到了但没查到"与"压根没想到"区分开——两者补救路径完全不同
2. 堵住"声明 100% → healthy"的虚假确定感
3. 与 REQ-G-002 形成合力：零命中规则产生 `data_absent` 诊断，实证缺口产生覆盖度诊断，两者共同指向"这个维度实际是空的"

### 七、附带发现（不在本需求范围，供决策）

`empirical` 只统计 `xu_shi` 阶段的 findings。R6（中标-资金时间窗碰撞）的 stage 是 `qi_zheng`，故「时间」维度在实证轨显示为缺失，而 R6 实际命中 7 行。

方向是安全的（低估不误导），但不准确。建议后续把 `qi_zheng` 阶段的 findings 并入实证口径，或按阶段分别统计。

---

要我把这条补进 `REQ-G.md` 清单文件吗？另外 `rebuild_planner.py` L212 的 f-string 问题远端还没修——那个是 CI 必挂的，我一并整理成针对当前远端基线的补丁给你。
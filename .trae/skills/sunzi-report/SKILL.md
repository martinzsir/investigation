---
name: sunzi-report
description: >
  侦查研判报告生成器。把案件画布（事实/规则/书证/待核实）与内核确定性结果
  （五间覆盖、交叉等级、过桥路径、Function 复核）合成为十段式中文研判报告，
  每条事实陈述带可点击溯源引用，支持研判/汇报/移送三种受众形态与 md/docx 导出。
  Use when: 需要把一条线索整理成可交付的报告、需要带引用的研判摘要、
  需要把画布结论转成领导汇报或法制移送素材。
  Do NOT use for: 给案件定性、决定立案、生成移送决定书的定性段落——定性权只属于人（正兵）。
  本技能产出的是「带引用的素材与草稿」，不是结论，更不是法定文书。
---

# 孙武侦查官 · 研判报告 Skill

> 本技能是 **成文层**，不是计算层。
> 所有数字（交叉等级、五间覆盖、过桥路径）与数据来源登记（文件/批次/行数/版本）
> 由 `scripts/gather_evidence.py` 确定性产出，
> LLM **只负责编排与表达**，不碰计算、不复述数字、不发明事实、不登记数据来源。

## 核心原则（违反即为错误用法）

| 原则 | 含义 |
|---|---|
| **确定性块不经 LLM** | 第二段、第五段、第九段由脚本产出，Agent 直接填入，不得改写、不得"润色"数字或数据来源 |
| **无引用不成陈述** | 事实段每句必须带 `[cite:ref]`；无据句自动转入「待核实」 |
| **推断与事实分列** | 第四段拆分「有据推断」与「推测」，推测段强制标注未经核实 |
| **脱敏前置** | 脚本输出即已脱敏（证件/手机/卡号 redact，人名 tokenize） |
| **AI 不产定性** | 移送类（C）报告 AI 只做素材整理，定性段落留人工 |

## 工作流

```
① 采集（脚本，确定性）
   scripts/gather_evidence.py --case <cid> [--clue <lid>] --out evidence.json
   → 产出已脱敏的确定性块 + 覆盖缺口 + 数据源登记（文件/批次/行数/版本）+ 降级标记

② 成文（Agent，表达）
   读 evidence.json + 画布上下文
   按 references/report_format.md 的十段契约撰写
   确定性块直填，不改写

③ 渲染
   scripts/render_report.py --evidence evidence.json --sections sections.json --type A
   → 输出 md / docx
```

## 三类受众形态

| 类型 | 受众 | 段落差异 |
|---|---|---|
| **A 研判报告** | 侦查员自己 | 全量十段，含推断、待核实、覆盖缺口 |
| **B 汇报报告** | 领导/专案组 | 结论导向，推断段仅留有据部分 |
| **C 移送固证报告** | 法制/检察 | 只含已固证，剔除推断与待核实 |

`--type` 默认 `A`。B 由 A 删段派生；C 需人工确认后生成，AI 不主导。

## 脚本

### `scripts/gather_evidence.py`（确定性采集）

```bash
python scripts/gather_evidence.py --case C001 --clue L3 --out evidence.json
python scripts/gather_evidence.py --case C001 --demo   # 无案件库时的降级演示
```

输出 JSON：

```jsonc
{
  "case_id": "C001",
  "clue_id": "L3",
  "data_version": 2,
  "确定性块": {
    "证据充分性": { "五间覆盖": {...}, "交叉等级": "线索", "覆盖缺口": ["内间"] },
    "关联核验":  { "过桥路径": [...], "Function复核": [...], "规则手册": [...] },
    "数据源清单": { "登记数据源": [{"filename": "...", "upload_id": "up_...", "rows": 13, ...}],
                    "本线索数据源": [...], "数据版本": 2, "本体版本": 2 }
  },
  "降级": [],          // 非空则必须在报告中标注
  "脱敏": true
}
```

**降级不静默**：任何一项取不到，写进 `降级` 数组，Agent 必须在报告中如实标注，不得省略。

### `scripts/render_report.py`（渲染导出）

```bash
python scripts/render_report.py --evidence evidence.json --sections sections.json \
    --type A --format md --out 研判报告.md
python scripts/render_report.py --evidence evidence.json --sections sections.json \
    --type A --format docx --out 研判报告.docx
```

## 红线

1. **不得改写确定性数字与数据来源** —— 脚本给"双源=线索"，报告就写"线索"；
   第九段文件/批次/行数/版本以脚本登记为准，不得自行拼凑或脑补
2. **不得给无引用句补引用** —— 宁可转入待核实
3. **不得在 C 类报告写定性结论** —— 只整理已固证素材
4. **不得绕过脱敏** —— 脚本输出已是脱敏后；若需原始值，走 server 内流程，不由本 Skill 处理

## 参考

- 十段契约与字段定义：`references/report_format.md`
- 空白模板：`assets/report_template.md`

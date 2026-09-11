# 续作指令

## 项目信息

- **项目路径**：`D:\dev\inves_duckdb\projects\sunwu-tech-proposal`
- **项目名称**：孙武侦查官技术方案书（内部技术设计文档）
- **当前状态**：in_progress
- **总规模**：8 章 / 43,000 字

## 固定上下文（每次续作必读）

| 文件 | 作用 |
|---|---|
| `reference-keypoints.md` | **唯一事实来源**。全部来自源码核对，写作时不得臆造未记载的模块名/文件名/数字 |
| `outline.md` | 章节大纲、字数分配、全局写作规范 |
| `progress.json` | 进度追踪，每章完成后更新 |

## 续作任务

1. 读取 `progress.json`，找到第一个 `status != "completed"` 的章节
2. 读取 `outline.md` 中该章的小节结构
3. 读取 `reference-keypoints.md` 中该章涉及的事实段落
4. 按写作规范生成该章，保存为 `chapter-NN.md`
5. 更新 `progress.json`：`completed` 追加、`wordCounts` 记录、该章 `status=completed`、推进 `current`
6. 继续下一章；全部完成后进入 Phase Final

## 写作规范（强制）

- **段落优先**：论述式写作，标题层级最多到 `###`，禁止 `####` 及更深
- 每个标题后必须有 100–200 字以上的实质段落，不得标题连标题
- 每个大节（`##`）开头必须有概括性段落
- 列表占比不超过章节内容 30%
- 内部技术文档口吻：客观、克制、可直接作为设计依据，避免营销性表述
- 涉及代码事实时给出文件路径与行号（如 `core/ontology.py:393`）

## 注意事项

- 每章独立会话，上下文从零开始
- 仅加载 `reference-keypoints.md` 中该章相关段落，不必全量读入
- 章节之间不要重复铺垫：第 1 章已定义的概念（语义层、间类、代理键等）后续章节直接引用，不重新解释
- 完成后立即更新 `progress.json`

## Phase Final（全部章节完成后）

1. 检查 8 章是否全部 `status=completed`
2. 按章节顺序合并为 `final.md`，生成目录与章节编号
3. 质量检查：总字数 ≥ 38,700（90%）、无 `####` 标题、无 TODO 标记
4. 更新 `progress.json` `status=completed`

## 生成时间

2026-09-11T15:40:00

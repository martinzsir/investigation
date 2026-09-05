交付包已生成（45KB，校验通过）。由于图片通道是通的而文件通道不通，我把 **17 张原型全部在下面展示出来**，并附完整链接清单——这部分即使文件收不到也能直接用。

---

# 交付包内容

文件：`/data/workspace/UI交付包.zip`（含 .docx + .md）

文档结构：设计规范 → 17 张图清单（每张含**图像链接 / 需求映射 / 区块 / 设计要点**）→ 五态规范 → 三条红线 E2E → 跨页面流转 → 验收清单

---

## 一、研判中心组（5 张）

### 01 治理仪表盘 — REQ-W-018
`http://yb.woa.com/7XdDy5ZJSrw`




三段式满屏：上段健康度通栏（6 条诊断）｜中段四等宽分析卡｜下段线索待办表 + 实体裁决列表

### 04 线索详情 — REQ-W-019/020
`http://yb.woa.com/kfwkb7OjqS`




### 09 处置看板 — 新增
`http://yb.woa.com/898lmeluiEd`




### 10 知识图谱 — 新增
`http://yb.woa.com/9Sbyn9feZ2U`




### 05 实体裁决 — REQ-W-021
`http://yb.woa.com/7r5bzFZPWhF`




---

## 二、数据接入组（3 张）

### 02 数据接入向导 — REQ-W-010/011/012
`http://yb.woa.com/Cf6gQCaQZaO`




### 07 数据画像 — 新增
`http://yb.woa.com/DyC0IRhweet`




### 08 接入建议确认 — 新增
`http://yb.woa.com/DZAKPffDDq2`




---

## 三、研判模型组（4 张）

### 12 模型设计器 — REQ-W-013
`http://yb.woa.com/1Isz5BBo7as`




### 03 规则工坊 — REQ-W-014/025
`http://yb.woa.com/FSqnUE4lECs`




### 11 庙算工作台 — 新增
`http://yb.woa.com/B8noFhXfxJE`




### 14 知识包维护 — REQ-W-016
`http://yb.woa.com/GHPxEoUcUur`




---

## 四、治理审计组（2 张）

### 06 审计链 — REQ-W-023/024
`http://yb.woa.com/66gIkjvr6Ip`




### 13 权限与遮蔽 — REQ-W-015
`http://yb.woa.com/FPbmzDouTvY`




---

## 五、高级工具组（3 张）

### 15 跨案件查询 — REQ-W-026/027
`http://yb.woa.com/BriVbQP6z3p`




### 16 案件包 — REQ-W-028/029
`http://yb.woa.com/Aam9FGS1ZmA`




### 17 代码逃生舱 — REQ-W-031
`http://yb.woa.com/Evi8KLb5qYq`




---

# 完整链接清单（可直接复制）

| # | 页面 | 需求 | 链接 |
|---|---|---|---|
| 01 | 治理仪表盘 | W-018 | http://yb.woa.com/7XdDy5ZJSrw |
| 02 | 数据接入向导 | W-010/011/012 | http://yb.woa.com/Cf6gQCaQZaO |
| 03 | 规则工坊 | W-014/025 | http://yb.woa.com/FSqnUE4lECs |
| 04 | 线索详情 | W-019/020 | http://yb.woa.com/kfwkb7OjqS |
| 05 | 实体裁决 | W-021 | http://yb.woa.com/7r5bzFZPWhF |
| 06 | 审计链 | W-023/024 | http://yb.woa.com/66gIkjvr6Ip |
| 07 | 数据画像 | 新增 | http://yb.woa.com/DyC0IRhweet |
| 08 | 接入建议确认 | 新增 | http://yb.woa.com/DZAKPffDDq2 |
| 09 | 处置看板 | 新增 | http://yb.woa.com/898lmeluiEd |
| 10 | 知识图谱 | 新增 | http://yb.woa.com/9Sbyn9feZ2U |
| 11 | 庙算工作台 | 新增 | http://yb.woa.com/B8noFhXfxJE |
| 12 | 模型设计器 | W-013 | http://yb.woa.com/1Isz5BBo7as |
| 13 | 权限与遮蔽 | W-015 | http://yb.woa.com/FPbmzDouTvY |
| 14 | 知识包维护 | W-016 | http://yb.woa.com/GHPxEoUcUur |
| 15 | 跨案件查询 | W-026/027 | http://yb.woa.com/BriVbQP6z3p |
| 16 | 案件包 | W-028/029 | http://yb.woa.com/Aam9FGS1ZmA |
| 17 | 代码逃生舱 | W-031 | http://yb.woa.com/Evi8KLb5qYq |

---

**给三个团队的入口**：
- **设计** → 第三部分的规范（配色 `#0A0E1A` / 强调色 `#00D4FF` / 密度原则）+ 五态规范
- **前端** → 三条红线的 E2E 场景（可直接转 Cypress）+ 跨页面流转
- **测试** → 三条红线验证 + 第五部分验收清单

**一个提醒**：17 张均按 1920×1080 绘制且密度很高。1366×768 小屏需要「紧凑/舒适」切换，下段内容折叠为"查看全部"。这条建议我在文档里写了，但容易在实际开发时被忽略。

<media_info>[{"type": "file", "description": "UI 交付包：17 张原型清单、设计规范、五态定义、红线验证与跨页面流转", "path": "/data/workspace/UI交付包.zip"}]>
先说一个容易被忽略但决定性的约束：**原型里的每个视觉特征都会淘汰一批技术**。把约束理清楚，选型几乎是自证的。

---

# 一、从原型反推的技术约束

## 1.1 原型的六个视觉特征 → 技术需求

| 原型特征 | 涉及页面 | 技术需求 |
|---|---|---|
| 深蓝黑 `#0A0E1A` + 青蓝 `#00D4FF` 指挥中心风 | 全部 17 页 | **主题系统必须完全可控**，不能套用成品 UI 库的默认视觉 |
| 信息密集（表格 12+ 行、矩阵 9+ 行） | 04/07/12/13/14 | headless 表格 + 虚拟滚动 + 紧凑密度切换 |
| 力导向关系图 | 10 知识图谱 | 图可视化库，需内置布局与交互状态 |
| 五间枚举热力图（矩阵着色） | 11 庙算工作台 | 热力图组件 |
| 代码桩语法高亮 | 17 代码逃生舱 | 代码编辑器 |
| 泳道看板 + 卡片拖拽 | 09 处置看板 | 拖拽库 |

## 1.2 项目部署约束 → 技术需求

这几条比视觉更重要，因为它们**不可妥协**：

| 约束 | 来源 | 技术后果 |
|---|---|---|
| **私有化内网部署** | ADR-001 决策一 | 无 CDN，所有资源本地打包（含 Monaco worker、字体、图标） |
| 后端 Python + Postgres | ADR-001 | 前端独立 SPA，REST + SSE 通信，不需要 BFF |
| 静态托管 | 私有化交付 | 构建产物 `dist/` 由 Nginx 托管 |
| 任务进度实时推送 | ADR-001 已定 SSE | 前端只需 SSE，不需要 WebSocket |

---

# 二、推荐技术栈

## 2.1 主推方案

| 层 | 选型 | 理由 |
|---|---|---|
| **框架** | **React 19 + TypeScript** | 生态最全，图/表格/编辑器库选择最多 |
| **构建** | **Vite** | 纯静态产物，内网部署简单 |
| **样式** | **Tailwind CSS v4** | 深色 token 完全可控；2026 年已成 React 项目默认样式层 |
| **组件** | **shadcn/ui（Base UI 版）** | 复制源码进仓库，无版本锁定，私有化友好 |
| **表格** | **TanStack Table v8 + Virtual** | headless，密度可控，支持矩阵单元格自定义 |
| **图表** | **Apache ECharts 6** | 动态主题切换 + 深色模式响应；热力图原生支持 |
| **关系图** | **AntV G6 v5** | 中文文档友好，内置布局与插件，国内团队首选 |
| **代码编辑器** | **Monaco Editor** | 需配置离线 worker |
| **拖拽** | **dnd-kit** | 处置看板泳道拖拽 |
| **服务端状态** | **TanStack Query** | 缓存、重试、轮询统一管理 |
| **UI 状态** | **Zustand** | 案件上下文、筛选、选中项（20 行搞定） |
| **路由** | **TanStack Router** | 类型安全 + search param 校验（筛选条件天然适合存 URL） |
| **实时** | **SSE** | ADR 已定，进度是单向流 |
| **测试** | **Vitest + Playwright** | 红线 E2E 验证 |

## 2.2 三个关键取舍

### 取舍一：不用 Next.js

搜索结果里 2026 年的 SaaS 模板几乎全是 Next.js 16，但**本项目不该用**：

- 内部系统，不需要 SSR/SEO
- 私有化部署要跑 Node 服务端，运维复杂度上升
- 后端已是 Python，没有 BFF 需求
- 纯静态 `dist/` 用 Nginx 托管，交付最简单

**Vite + React SPA 是正确选择。**

### 取舍二：不用 Ant Design

这条最容易被质疑，我把话说完整。

**不选的理由**：原型是深蓝黑指挥中心风格，AntD 默认视觉语言偏企业商务，改造成这个风格的成本接近重写组件。而且 AntD 体积大、样式覆盖难。

**但必须承认 AntD 的强项**：`Table` 在高密度、列固定、排序筛选场景开箱即用，比 TanStack Table 省事得多。

**折中建议**：

| 团队情况 | 选择 |
|---|---|
| 有时间做设计系统 | shadcn/ui + TanStack Table（视觉还原度最高） |
| 时间紧 / 团队熟 AntD | AntD v5 + 深度定制 dark algorithm token（接受视觉妥协） |

我倾向前者，因为原型的一致性是这次交付的核心要求。

### 取舍三：shadcn/ui 的默认 primitives 已切换

需要注意：shadcn/ui 在 2026 年 7 月把新项目默认从 **Radix UI 切到 Base UI**（`-b radix` 仍可选，Radix 未废弃，存量项目无需迁移）。

建议**直接用 Base UI 版**——它由 Radix、Floating UI、MUI 的原班人马创建，2025 年 12 月已到 v1.0。

---

# 三、逐页面技术映射（保持原型一致性的关键）

| # | 页面 | 核心组件 | 说明 |
|---|---|---|---|
| 01 | 治理仪表盘 | ECharts 双环形 + 堆叠条 + TanStack Table | 健康度横幅用自定义卡片；下段两列表格 |
| 02 | 数据接入向导 | 自定义三栏 + dnd-kit | 映射拖拽用 dnd-kit；预览表用 TanStack Table |
| 03 | 规则工坊 | Monaco（只读）+ ECharts 柱状 | 分隔条用自定义 divider 组件 |
| 04 | 线索详情 | TanStack Table + 自定义溯源树 | 溯源树用递归组件，不用图库 |
| 05 | 实体裁决 | TanStack Table（三列对比） | 差异行高亮 = 行级 className |
| 06 | 审计链 | 自定义链环时间线 | 节点少（15+），自定义比图库简单 |
| 07 | 数据画像 | TanStack Table ×2 + 自定义变体面板 | 扣分明细也是表格 |
| 08 | 接入建议确认 | 自定义卡片 + Checkbox | 证据面板用 Collapsible |
| 09 | 处置看板 | **dnd-kit** 多容器 | 五泳道，卡片可拖拽换列 |
| 10 | 知识图谱 | **AntV G6 v5** | 力导向布局，节点类型分色 |
| 11 | 庙算工作台 | **ECharts heatmap** | 五间枚举矩阵着色 |
| 12 | 模型设计器 | 自定义画布 + TanStack Table | 关系画布可用 G6 或 SVG 自绘 |
| 13 | 权限与遮蔽 | TanStack Table（可编辑单元格） | **未声明格子 = 斜纹 + 锁图标** |
| 14 | 知识包维护 | TanStack Table + 自定义有效期轴 | 过期灰显 = 行级 className |
| 15 | 跨案件查询 | Monaco（SQL 高亮）+ 结果表 | 禁用态按钮 + 权限面板 |
| 16 | 案件包 | 自定义步骤条 + 清单 | 橙色告警行 = 行级状态 |
| 17 | 代码逃生舱 | **Monaco** + ECharts 条形 | 触发统计图 |

**只有 3 个组件需要专门引入库**：G6（图）、Monaco（代码）、dnd-kit（拖拽）。其余都是表格 + 自定义卡片 + ECharts 的组合。

这个结论很关键——**原型看着复杂，实际技术依赖很轻**。

---

# 四、设计 Token 落地

把原型配色固化成 CSS 变量，这是保持一致性最有效的手段：

```css
:root {
  /* 原型主色 */
  --bg-base:      #0A0E1A;   /* 深蓝黑主背景 */
  --bg-elevated:  #121826;   /* 卡片背景 */
  --bg-hover:     #1A2337;
  --accent:       #00D4FF;   /* 青蓝强调 */
  --accent-dim:   rgba(0, 212, 255, 0.15);
  
  /* 状态色 */
  --warn:         #FFA726;   /* 橙黄 warning */
  --error:        #FF5252;   /* 红 error */
  --filed:        #FFB300;   /* 金橙 已立案 */
  --ok:           #4CAF50;
  
  /* 密度 */
  --row-h-compact: 28px;
  --row-h-cozy:    40px;
}
```

Tailwind v4 用 `@theme` 直接消费这些变量。

**密度切换是硬需求**：17 张原型均按 1920×1080 绘制且密度很高，1366×768 必须能切到舒适模式，否则表格挤成一团。

---

# 五、私有化部署的四个必做项

这几条不做，内网点开就是白屏：

**1. Monaco 离线化**
Monaco 默认从 CDN 加载，内网必挂。需配置 `MonacoEnvironment.getWorkerUrl` 指向本地资源：

```js
window.MonacoEnvironment = {
  getWorkerUrl: (moduleId, label) => {
    if (label === 'json') return '/monaco/vs/language/json/json.worker.js';
    if (label === 'typescript' || label === 'javascript')
      return '/monaco/vs/language/typescript/ts.worker.js';
    return '/monaco/vs/editor/editor.worker.js';
  }
};
```

配合 Vite 把 `monaco-editor/min/vs` 拷到 `public/monaco/`。

**2. 字体本地化**
内网无法加载 Google Fonts。中文字体（思源黑体等）需打包进 `public/fonts/`，用 `@font-face` 声明。注意中文字体体积大，建议子集化或按需加载字重。

**3. 图标库自包含**
用 `lucide-react`（tree-shaking，只打包用到的图标），不用 Iconfont CDN。

**4. 构建产物检查**
`npm run build` 之后必须验证：`dist/` 里没有任何指向 `http(s)://` 的外部资源引用。可以写个脚本扫一遍。

---

# 六、风险与备选

| 风险 | 影响 | 处置 |
|---|---|---|
| 中文字体体积大（数 MB） | 首次加载慢 | 子集化 + 按需；内网可接受一次性加载 |
| Monaco 体积（约 2-5MB） | 拖慢首屏 | **懒加载**：仅页面 17 及 03/15 的代码区按需引入 |
| G6 在大图下性能 | 节点多时卡顿 | 侦查案件一般 < 1000 节点，G6 足够；超 10k 再换 Sigma.js |
| ECharts 深色主题与原型色差 | 视觉不一致 | ECharts 6 支持 `registerTheme` + 动态切换，用上面 token 注册自定义主题 |
| 团队是 Vue 栈 | 需重写 | 见下 |

## Vue 备选方案

如果团队是 Vue：

| 层 | 替换 |
|---|---|
| React 19 → | Vue 3 + TypeScript |
| shadcn/ui → | shadcn-vue（或 Element Plus + 深度定制） |
| TanStack Table → | TanStack Table Vue 版（同一套核心） |
| dnd-kit → | vuedraggable |

**ECharts、G6、Monaco 与框架无关，全部保留**——这也是选它们的原因之一。

---

# 七、一句话总结

这套原型看似复杂，实际技术依赖很轻：**一套 headless 组件层（shadcn/ui）+ 一个表格方案（TanStack Table）+ 两个可视化库（ECharts 负责图表、G6 负责关系图）+ 一个编辑器（Monaco）+ 一个拖拽库（dnd-kit）**，加上大量自定义卡片和表格行样式，就能高度还原。

真正的难点不在选型，而在两件事：
1. **设计 token 是否严格落地**——`#0A0E1A` 和 `#00D4FF` 必须固化成变量，不能让各页面自己取色
2. **私有化资源是否全部本地化**——Monaco worker、字体、图标，漏一个就是白屏

---

要不要我接着做：

- **出一份前端项目脚手架清单**（目录结构、依赖清单、Vite 配置要点、Monaco 离线配置示例）
- **把设计 token 扩成完整的组件规范**（卡片/表格/徽章/按钮在深色主题下的具体样式参数）
- **写红线 E2E 的 Playwright 实现**（三条红线各一个可运行的测试）
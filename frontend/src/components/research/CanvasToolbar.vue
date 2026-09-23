<script setup lang="ts">
// RC-201/202/206 画布工具栏：人工节点新增、分层重排（不动钉住节点）、
// 视口、连线模式、快照。纯事件上抛，状态与请求都在 ResearchCanvas。
import { NButton, NIcon, NSelect, NTooltip } from 'naive-ui'
import {
  AddCircleOutline,
  AddOutline,
  CameraOutline,
  ChatbubbleOutline,
  ContractOutline,
  DocumentTextOutline,
  ExpandOutline,
  FlaskOutline,
  GitMergeOutline,
  GridOutline,
  LayersOutline,
  MedalOutline,
  RemoveOutline,
  ScanOutline,
  DocumentOutline,
  TelescopeOutline,
  SettingsOutline,
} from '@vicons/ionicons5'

defineProps<{
  /** 连线模式（点击两节点建人工边），与拖拽移动互斥 */
  connectMode?: boolean
  /** 压缩链路：把焦点链上「只是过路」的中间节点压成一条合成边 */
  chainCollapsed?: boolean
  /** P2 全局概览：五列压成列胶囊 + 主干边 */
  overview?: boolean
  /** P3 研判视角：process（流程）/ tier（证据强度三层） */
  perspective?: 'process' | 'tier'
  /** G6 布局模式：preset（自定义 barycenter）或 G6 内置布局 */
  g6LayoutMode?: string
  busy?: boolean
  /** 观察图层当前是否开启 */
  observationLayerOn?: boolean
  /** 是否存在可展开的观察（无观察时按钮禁用，避免点了没反应） */
  hasObservationLayer?: boolean
  /** 观察条数（tooltip 文案用） */
  observationCount?: number
}>()

const emit = defineEmits<{
  (e: 'add-hypothesis'): void
  (e: 'add-note'): void
  (e: 'relayout'): void
  (e: 'fit'): void
  (e: 'zoom-in'): void
  (e: 'zoom-out'): void
  (e: 'toggle-connect'): void
  (e: 'open-snapshots'): void
  (e: 'open-function-query'): void
  /** 定向镜头带参调度（画布选中主体预填参数） */
  (e: 'open-lens-run'): void
  /** 案件级镜头启停（lenses.json，RESCAN 后对批量检测生效） */
  (e: 'open-lens-switch'): void
  /** 观察图层：定向深挖结果的独立时间轴开关（外挂层，不并入画布 doc） */
  (e: 'toggle-observation-layer'): void
  /** M5 RC-301：打开画布问答侧栏 */
  (e: 'open-chat'): void
  /** M6 RC-304：打开研判报告面板 */
  (e: 'open-report'): void
  (e: 'toggle-collapse'): void
  /** P2：进出全局层聚合视图（列胶囊 + 主干边） */
  (e: 'toggle-overview'): void
  /** P3：切换研判视角（流程 / 证据强度） */
  (e: 'toggle-perspective'): void
  /** 切换 G6 布局模式 */
  (e: 'layout-mode-change', mode: string): void
  /** UX P0：明细层一键展开/折叠（简洁视图） */
  (e: 'expand-all-details'): void
  (e: 'collapse-all-details'): void
}>()

/** G6 布局模式下拉选项 */
const LAYOUT_OPTIONS = [
  { label: '分层（barycenter）', value: 'preset' },
  { label: 'Dagre 分层', value: 'dagre' },
  { label: '力导向', value: 'force' },
  { label: '径向', value: 'radial' },
  { label: '同心圆', value: 'concentric' },
]
</script>

<template>
  <div class="canvas-toolbar" data-testid="canvas-toolbar">
    <div class="tb-group">
      <span class="tb-group-label">编辑</span>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-add-hypothesis"
            @click="emit('add-hypothesis')"
          >
            <NIcon :component="AddCircleOutline" />
            假设
          </NButton>
        </template>
        添加侦查假设节点
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-add-note"
            @click="emit('add-note')"
          >
            <NIcon :component="DocumentTextOutline" />
            备注
          </NButton>
        </template>
        添加备注节点
      </NTooltip>
    </div>

    <span class="sep" />

    <div class="tb-group">
      <span class="tb-group-label">结构</span>
      <NTooltip trigger="hover">
      <template #trigger>
        <NButton
          size="small"
          class="tb-btn"
          data-testid="tb-relayout"
          :disabled="g6LayoutMode !== 'preset'"
          @click="emit('relayout')"
        >
          <NIcon :component="GridOutline" />
          重新排版
        </NButton>
      </template>
      按链路重新分层；已钉住节点保持不动（仅分层模式可用）
    </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NSelect
            size="small"
            :value="g6LayoutMode ?? 'preset'"
            :options="LAYOUT_OPTIONS"
            style="width: 150px"
            data-testid="tb-layout-mode"
            @update:value="(v: string) => emit('layout-mode-change', v)"
          />
        </template>
        画布布局算法：分层（barycenter）/ Dagre / 力导向 / 径向 / 同心圆
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            :type="chainCollapsed ? 'primary' : 'default'"
            data-testid="tb-collapse-chain"
            @click="emit('toggle-collapse')"
          >
            <NIcon :component="GitMergeOutline" />
            压缩链路
          </NButton>
        </template>
        焦点链上「只是过路」的中间节点压成一条合成边（点边可展开）
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            :type="connectMode ? 'primary' : 'default'"
            data-testid="tb-connect"
          @click="emit('toggle-connect')"
        >
          <NIcon :component="GitMergeOutline" />
          {{ connectMode ? '连线中（点此退出）' : '连线' }}
        </NButton>
      </template>
      连线模式：依次点击两个节点建立人工关系
    </NTooltip>

    <NTooltip trigger="hover">
      <template #trigger>
        <NButton
          size="small"
          class="tb-btn"
          data-testid="tb-expand-details"
          @click="emit('expand-all-details')"
        >
          <NIcon :component="ExpandOutline" />
          展开明细
        </NButton>
      </template>
      展开全部事实下的实体/数据行/数据源节点
    </NTooltip>
    <NTooltip trigger="hover">
      <template #trigger>
        <NButton
          size="small"
          class="tb-btn"
          data-testid="tb-collapse-details"
          @click="emit('collapse-all-details')"
        >
          <NIcon :component="ContractOutline" />
          折叠明细
        </NButton>
      </template>
      折叠全部明细，只留研判骨干
    </NTooltip>

    </div>

    <span class="sep" />

    <div class="tb-group">
      <span class="tb-group-label">视图</span>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            quaternary
            class="tb-icon"
            data-testid="tb-zoom-out"
            @click="emit('zoom-out')"
          >
            <NIcon :component="RemoveOutline" />
          </NButton>
        </template>
        缩小
      </NTooltip>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            quaternary
            class="tb-icon"
            data-testid="tb-zoom-in"
            @click="emit('zoom-in')"
          >
            <NIcon :component="AddOutline" />
          </NButton>
        </template>
        放大
      </NTooltip>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            :type="overview ? 'primary' : 'default'"
            data-testid="tb-overview"
            @click="emit('toggle-overview')"
          >
            <NIcon :component="LayersOutline" />
            全局概览
          </NButton>
        </template>
        五列压成列胶囊 + 主干边（线宽 ∝ 关系数），点胶囊进入该层
      </NTooltip>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            :type="perspective === 'tier' ? 'primary' : 'default'"
            data-testid="tb-perspective"
            @click="emit('toggle-perspective')"
          >
            <NIcon :component="MedalOutline" />
            证据强度
          </NButton>
        </template>
        切换研判视角：流程（血缘）↔ 证据强度（已锁死 / 待核实 / 推测三层）；Tier 3 节点半透后退
      </NTooltip>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            quaternary
            class="tb-icon"
            data-testid="tb-fit"
            @click="emit('fit')"
          >
            <!-- 不再复用 ExpandOutline（与「展开明细」同图标，语义混淆） -->
            <NIcon :component="ScanOutline" />
          </NButton>
        </template>
        适应屏幕（按真实节点重新居中，排除泳道）
      </NTooltip>
    </div>

    <span class="sep" />

    <div class="tb-group">
      <span class="tb-group-label">分析</span>
      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-function-query"
            @click="emit('open-function-query')"
          >
            <NIcon :component="FlaskOutline" />
            扩展查询
          </NButton>
        </template>
        对白名单只读 Function 发起扩展查询，结果作为节点挂到画布
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-lens-run"
            @click="emit('open-lens-run')"
          >
            <NIcon :component="TelescopeOutline" />
            定向镜头
          </NButton>
        </template>
        对选中主体定向运行带参镜头（关系圈层/事件序列/时间碰撞等）；完成后画布顶部提示结果，线索进线索列表
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-lens-switch"
            @click="emit('open-lens-switch')"
          >
            <NIcon :component="SettingsOutline" />
            镜头启停
          </NButton>
        </template>
        本案件镜头启停配置：保存即自动重建（RESCAN），停用镜头的线索随重建从线索列表移除，重新启用则恢复产出
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-observation-layer"
            :type="observationLayerOn ? 'primary' : 'default'"
            :disabled="!hasObservationLayer"
            @click="emit('toggle-observation-layer')"
          >
            <NIcon :component="LayersOutline" />
            观察时间轴
          </NButton>
        </template>
        {{
          !hasObservationLayer
            ? '本线索还没有定向深挖结果：先在画布选中主体运行「定向镜头」，结果会成为可展开的观察时间轴'
            : observationLayerOn
              ? `关闭观察时间轴（当前 ${observationCount ?? 0} 条深挖观察，底部抽屉）`
              : `展开观察时间轴：${observationCount ?? 0} 条深挖观察按时间铺成独立时间轴（底部抽屉）`
        }}
      </NTooltip>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-chat"
            @click="emit('open-chat')"
          >
            <NIcon :component="ChatbubbleOutline" />
            问答
          </NButton>
        </template>
        就当刻画布提问，获取带引用的回答（RC-301）
      </NTooltip>

      <NButton
        size="small"
        class="tb-btn"
        :loading="busy"
        data-testid="tb-snapshots"
        @click="emit('open-snapshots')"
      >
        <NIcon :component="CameraOutline" />
        快照
      </NButton>

      <NTooltip trigger="hover">
        <template #trigger>
          <NButton
            size="small"
            class="tb-btn"
            data-testid="tb-report"
            @click="emit('open-report')"
          >
            <NIcon :component="DocumentOutline" />
            报告
          </NButton>
        </template>
        生成/阅读/导出研判报告（RC-304）
      </NTooltip>
    </div>
  </div>
</template>

<style scoped>
.canvas-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
/* 按语义分组：编辑 / 结构 / 视图 / 分析（仅靠 1px 分隔线分不出组） */
.tb-group {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tb-group-label {
  font-size: 11px;
  color: var(--sun-text-tertiary);
  letter-spacing: 0.5px;
  padding-right: 2px;
}
.tb-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tb-btn:hover {
  border-color: var(--sun-border-active);
}
.tb-icon {
  padding: 0 6px;
}
.sep {
  width: 1px;
  height: 18px;
  background: var(--sun-border);
  margin: 0 4px;
}
</style>

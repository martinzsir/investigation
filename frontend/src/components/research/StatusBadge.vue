<script setup lang="ts">
// FE-C-001 StatusBadge：5 态/间类/优先级，色+图标+文字三重编码。
// R5/R6：状态样式取 states 声明 tone；交叉等级按 cross_levels 声明的
// min_independent_sources 序号映射配色（名称可变，1/2/3 红线不变）；
// 房间色板走 tokens.jianRoomVar（侦查维度名声明化）。
import { computed } from 'vue'
import { ANOMALY_LEVEL, statusMetaOf, type StatusMeta } from '../../domain/clue'
import { jianRoomVar } from '../../design/tokens'
import { useCaseOntologyConfig } from '../../composables/useCaseOntologyConfig'

const props = defineProps<{
  variant: 'status' | 'level' | 'room'
  value: string
}>()

const { config: cfg, dimLabel } = useCaseOntologyConfig()

// 未声明状态不出徽章（旧 STATUS_META[未知]=undefined 同语义）
const statusMeta = computed<StatusMeta | null>(() => {
  if (props.variant !== 'status') return null
  if (!cfg.value.states.some((s) => s.name === props.value)) return null
  return statusMetaOf(props.value, cfg.value)
})

/**
 * 交叉等级配色：按声明序号（min_independent_sources）映射——
 * 1=观察灰、2=线索青、3=候选金；异常待核实=琥珀。
 * 名称可配（cross_levels[].name），映射不可配。
 */
const levelClass = computed(() => {
  if (props.variant !== 'level') return ''
  if (props.value === ANOMALY_LEVEL) return 'level--pending'
  const decl = cfg.value.cross_levels.find((l) => l.name === props.value)
  const n = decl?.min_independent_sources
  if (n === 3) return 'level--candidate'
  if (n === 2) return 'level--clue'
  if (n === 1) return 'level--watch'
  return 'level--watch'
})

/**
 * 房间色板 + 维度展示名。
 * 维度值自 code 化后是机器标识符（fund/comm…），房间色板 JIAN_ROOM_VAR 以
 * 中文维度名为键——不翻译则**既查不到色、又显示英文**。此处统一翻译：
 * code → 中文展示名。间类（因间/生间…）不在维度映射内，dimLabel 原样返回，
 * 故同一入口可安全复用。这是全站唯一的维度翻译点，各调用方无需各改一遍。
 *
 * 修复：模板此前无条件拼 `{{ roomValue }}间`。本体 jians.json 的间类 name
 * **本身已带「间」**（因间/内间/反间/死间/生间），再拼后缀会变「生间间」；
 * 而维度（资金/通讯）根本不是间类，拼后缀变「资金间」——语义错误。
 * 故改为直接展示翻译后的值，不加后缀（间类/维度/房间名三者的值本身都完整）。
 */
const roomValue = computed(() =>
  props.variant === 'room' ? dimLabel(props.value) : props.value,
)
const roomVar = computed(() =>
  props.variant === 'room' ? jianRoomVar(roomValue.value) : '',
)
</script>

<template>
  <span
    v-if="variant === 'status' && statusMeta"
    class="badge status-badge"
    :style="{
      color: statusMeta.text,
      borderColor: statusMeta.border,
      background: statusMeta.bg,
    }"
  >
    <span class="badge-icon" aria-hidden="true">{{ statusMeta.icon }}</span>
    {{ value }}
  </span>
  <span v-else-if="variant === 'level'" class="badge level-badge" :class="levelClass">
    {{ value }}
  </span>
  <span
    v-else
    class="badge room-badge"
    :style="{ color: roomVar, borderColor: roomVar, background: `color-mix(in srgb, ${roomVar} 12%, transparent)` }"
  >
    {{ roomValue }}
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 10px;
  border: 1px solid;
  border-radius: var(--sun-radius-badge, 12px);
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
  font-weight: 500;
}
.badge-icon {
  font-size: 11px;
  line-height: 1;
}
.level-badge {
  border-style: solid;
}
.level--watch {
  color: var(--sun-text-secondary);
  border-color: var(--sun-text-tertiary);
  background: rgba(107, 131, 153, 0.12);
}
.level--clue {
  color: var(--sun-info-text);
  border-color: var(--sun-info-border);
  background: var(--sun-info-bg);
}
.level--candidate {
  color: var(--sun-filed-text);
  border-color: var(--sun-filed-border);
  background: var(--sun-filed-bg);
}
.level--pending {
  color: var(--sun-warn-text);
  border-color: var(--sun-warn-border);
  background: var(--sun-warn-bg);
}
</style>

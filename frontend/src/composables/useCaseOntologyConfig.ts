// 当前案件的 Ontology 声明配置（六项解耦共性基建）。
// 组件内直接调用即可：自动跟随全局当前案件 ensure 拉取，失败回落默认包。
import { computed, watch } from 'vue'
import { useCaseStore } from '../stores/case'
import {
  DEFAULT_ONTOLOGY_CONFIG,
  useOntologyConfigStore,
} from '../stores/ontologyConfig'
import {
  dimensionLabelMap,
  type OntologyConfig,
} from '../api/endpoints/ontologyConfig'

export function useCaseOntologyConfig() {
  const cs = useCaseStore()
  const oc = useOntologyConfigStore()

  const caseId = computed(() => cs.currentCaseId ?? '')
  const config = computed<OntologyConfig>(
    () => (caseId.value && oc.getConfig(caseId.value)) || DEFAULT_ONTOLOGY_CONFIG,
  )

  watch(
    caseId,
    (id) => {
      if (id) void oc.ensure(id)
    },
    { immediate: true },
  )

  /** 维度 code → 展示名映射（兼容旧产物里的中文值，双向可查） */
  const dimLabels = computed<Record<string, string>>(
    () => dimensionLabelMap(config.value.dimensions ?? []),
  )

  /** 把线索/规则里的维度值翻译给人看（code 或旧中文值都能翻） */
  function dimLabel(v: string | null | undefined): string {
    if (!v) return '—'
    return dimLabels.value[v] ?? v
  }

  /**
   * 业务事件时间字段（本体 semantic:event_time 声明）。
   * 空表示本体未声明 → 调用方应回落「研判过程时间」，不要静默按业务时间排。
   */
  const timeFields = computed(() => ({
    objects: config.value.time_fields?.objects ?? {},
    event_time: config.value.time_fields?.event_time ?? [],
  }))
  const hasEventTime = computed(() => timeFields.value.event_time.length > 0)

  return {
    caseId,
    config,
    dimLabels,
    dimLabel,
    timeFields,
    hasEventTime,
    /** BUILD/重建后强制刷新声明 */
    refresh: () => (caseId.value ? oc.ensure(caseId.value, true) : Promise.resolve(DEFAULT_ONTOLOGY_CONFIG)),
  }
}

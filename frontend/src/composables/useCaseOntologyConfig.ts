// 当前案件的 Ontology 声明配置（六项解耦共性基建）。
// 组件内直接调用即可：自动跟随全局当前案件 ensure 拉取，失败回落默认包。
import { computed, watch } from 'vue'
import { useCaseStore } from '../stores/case'
import {
  DEFAULT_ONTOLOGY_CONFIG,
  useOntologyConfigStore,
} from '../stores/ontologyConfig'
import type { OntologyConfig } from '../api/endpoints/ontologyConfig'

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

  return {
    caseId,
    config,
    /** BUILD/重建后强制刷新声明 */
    refresh: () => (caseId.value ? oc.ensure(caseId.value, true) : Promise.resolve(DEFAULT_ONTOLOGY_CONFIG)),
  }
}

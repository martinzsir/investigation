// 统一异步三态（loading / error / empty）——页面层三态各自发挥的收敛点。
//
// 背景：全站曾出现三种同形不同义的「空」——真的没有 / 无权限 / 加载失败。
// EmptyState 组件已把三者做成视觉可区分的类型，但页面层仍有人把失败写成空态
// （如 QualityView 曾把接口故障显示成「尚未运行质量检查」），在侦查场景里
// 这会让用户误判为「此事不必做」而继续推进。
//
// 红线：error 优先于 empty。只要失败，isEmpty 恒为 false，由调用方渲染
// EmptyState type="error" + 重试；绝不回落空态。
import { computed, ref, watch, type Ref } from 'vue'
import { presentError, isApiError } from '../api/errors'

export interface AsyncStateOptions<T> {
  /** 取数：返回 null 表示「真的没有」，抛异常表示「失败」 */
  fetcher: () => Promise<T | null>
  /** 判空（默认 null 即空；列表可按 length 判） */
  isEmpty?: (data: T | null) => boolean
  /** 依赖源变化时自动重载；不传则不自动 watch */
  watchSource?: () => unknown
  /** 无依赖（如未选案件）时不发请求，data 置空且不报错 */
  enabled?: () => boolean
}

export function useAsyncState<T>(options: AsyncStateOptions<T>) {
  const loading = ref(false)
  const error = ref('')
  const data = ref<T | null>(null) as Ref<T | null>
  /** 是否至少成功加载过一次（用于区分「还没加载」与「加载完是空的」） */
  const loaded = ref(false)

  async function load(): Promise<void> {
    if (options.enabled && !options.enabled()) {
      data.value = null
      error.value = ''
      loaded.value = false
      return
    }
    loading.value = true
    error.value = ''
    try {
      data.value = await options.fetcher()
      loaded.value = true
    } catch (e) {
      // 失败即清空：不允许「旧数据 + 失败提示」混合，旧数据不可信
      data.value = null
      loaded.value = false
      error.value = isApiError(e) ? e.message : presentError(e).title
    } finally {
      loading.value = false
    }
  }

  const isEmpty = computed(() => {
    // 失败或未加载过，都不是「空」
    if (error.value || !loaded.value) return false
    return options.isEmpty ? options.isEmpty(data.value) : data.value == null
  })

  if (options.watchSource) {
    watch(options.watchSource, () => void load(), { immediate: true })
  }

  return { loading, error, data, loaded, isEmpty, load }
}

import { api } from '../client'

// 系统设置（server/app/routers/settings.py 契约）。
// 纪律：queue/resources/thresholds/snapshots/features 的 GET 也要 admin——
// 非管理员请求直接 403（B2：前端渲染锁定面板，不展示数值）；唯 health 登录可读。
// 写 body 统一 {reason（必填）, values}；键白名单 + 区间/enum 后端校验；
// 红线键（llm_enabled 等）永不在白名单（后端硬拒）。

export interface QueueSettings {
  max_workers: number
  poll_interval_ms: number
}

export interface ResourceSettings {
  max_rows_default: number
  query_timeout_ms: number
  /** GET 透出、永不可经 API 改写 */
  storage_root?: string
}

export interface HealthSettings {
  meta_ok: boolean
  queue: { pending: number; running: number }
  worker: {
    pool_alive: boolean
    max_workers: number
    poll_interval_ms: number
    note: string
  }
  versions: { backend: string; ontology_default: string }
}

export interface ThresholdSettings {
  cross_level_min_sources: number
  cross_level_min_clues: number
  stale_days: number
}

export interface FeatureSettings {
  ui_density: 'compact' | 'comfortable'
}

export interface SnapshotItem {
  case_id?: string
  pack_id?: string
  version?: string
  snapshot_path?: string
  locked_at?: string
  [key: string]: unknown
}

export interface PutSettingsBody {
  reason: string
  values: Record<string, unknown>
}

export const settingsApi = {
  async getQueue(): Promise<QueueSettings> {
    const { data } = await api.get<QueueSettings>('/settings/queue')
    return data
  },
  async putQueue(body: PutSettingsBody): Promise<{ queue: QueueSettings }> {
    const { data } = await api.put<{ queue: QueueSettings }>('/settings/queue', body, {
      idempotencyAction: 'settings-queue',
    })
    return data
  },

  async getResources(): Promise<ResourceSettings> {
    const { data } = await api.get<ResourceSettings>('/settings/resources')
    return data
  },
  async putResources(body: PutSettingsBody): Promise<{ resources: ResourceSettings }> {
    const { data } = await api.put<{ resources: ResourceSettings }>(
      '/settings/resources',
      body,
      { idempotencyAction: 'settings-resources' },
    )
    return data
  },

  /** GET /settings/health —— 登录可读（非 admin 也允许） */
  async health(): Promise<HealthSettings> {
    const { data } = await api.get<HealthSettings>('/settings/health')
    return data
  },

  async getThresholds(): Promise<{ thresholds: ThresholdSettings; note: string }> {
    const { data } = await api.get<{ thresholds: ThresholdSettings; note: string }>(
      '/settings/policies-thresholds',
    )
    return data
  },
  async putThresholds(body: PutSettingsBody): Promise<{ policies_thresholds: ThresholdSettings; note?: string }> {
    const { data } = await api.put<{ policies_thresholds: ThresholdSettings; note?: string }>(
      '/settings/policies-thresholds',
      body,
      { idempotencyAction: 'settings-thresholds' },
    )
    return data
  },

  async listSnapshots(): Promise<{ items: SnapshotItem[]; total: number }> {
    const { data } = await api.get<{ items: SnapshotItem[]; total: number }>(
      '/settings/snapshots',
    )
    return data
  },

  async getFeatures(): Promise<FeatureSettings> {
    const { data } = await api.get<FeatureSettings>('/settings/features')
    return data
  },
  async putFeatures(body: PutSettingsBody): Promise<{ features: FeatureSettings }> {
    const { data } = await api.put<{ features: FeatureSettings }>(
      '/settings/features',
      body,
      { idempotencyAction: 'settings-features' },
    )
    return data
  },
}

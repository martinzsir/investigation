import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import { NConfigProvider, zhCN } from 'naive-ui'
import SnapshotDrawer from '../src/components/research/SnapshotDrawer.vue'
import type { CanvasSnapshot } from '../src/domain/canvas'

// RC-206 快照抽屉：创建校验（立即、非防抖）、列表渲染、回滚二次确认。
// NDrawer/NPopconfirm 内容 teleport 到 body，断言走 document.body。
// 与生产 App.vue 一致套 zhCN ConfigProvider（Popconfirm 默认按钮文案）。

type DrawerProps = InstanceType<typeof SnapshotDrawer>['$props']

function setValue(el: Element | null, value: string): void {
  const target = el as HTMLInputElement | null
  if (!target) throw new Error('input element not found')
  target.value = value
  target.dispatchEvent(new Event('input', { bubbles: true }))
}

function mountDrawer(props: Partial<DrawerProps>): VueWrapper {
  return mount({
    components: { NConfigProvider, SnapshotDrawer },
    setup() {
      return { zhCN, props }
    },
    template: `
      <NConfigProvider :locale="zhCN">
        <SnapshotDrawer v-bind="props" />
      </NConfigProvider>`,
  })
}

function drawerEmitted(w: VueWrapper, event: string): unknown[] | undefined {
  return w.findComponent(SnapshotDrawer).emitted(event)
}

function snapshot(over: Partial<CanvasSnapshot> = {}): CanvasSnapshot {
  return {
    snapshot_id: 'snap_1',
    clue_id: 'clue-1',
    label: '初查假设形成',
    origin: 'manual',
    created_by: '李侦查员',
    created_at: '2026-09-13 10:00',
    node_count: 5,
    edge_count: 4,
    doc: { nodes: [], edges: [] },
    ...over,
  }
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('SnapshotDrawer 创建快照', () => {
  it('空白备注禁用创建；合法备注 trim 后 emit create', async () => {
    const w = mountDrawer({ show: true, snapshots: [] })
    const createBtn = () =>
      document.body.querySelector(
        '[data-testid="snapshot-create"]',
      ) as HTMLButtonElement
    // 空备注按钮禁用
    expect(createBtn().disabled).toBe(true)
    setValue(document.body.querySelector('[data-testid="snapshot-label"] input'),
      '   ')
    await w.vm.$nextTick()
    // 纯空白备注：按钮仍禁用，点击不发事件
    expect(createBtn().disabled).toBe(true)
    createBtn().click()
    await w.vm.$nextTick()
    expect(drawerEmitted(w, 'create')).toBeUndefined()

    setValue(document.body.querySelector('[data-testid="snapshot-label"] input'),
      '  假设定稿  ')
    await w.vm.$nextTick()
    expect(createBtn().disabled).toBe(false)
    createBtn().click()
    expect(drawerEmitted(w, 'create')?.[0]).toEqual(['假设定稿'])
  })

  it('create button shows loading state', () => {
    mountDrawer({ show: true, snapshots: [], creating: true })
    const btn = document.body.querySelector('[data-testid="snapshot-create"]')
    expect(btn?.className).toMatch(/n-button--loading|loading/)
  })
})

describe('SnapshotDrawer 列表与回滚', () => {
  it('shows empty state when no snapshots', () => {
    mountDrawer({ show: true, snapshots: [], loading: false })
    expect(document.body.querySelector('[data-testid="snapshot-empty"]')).toBeTruthy()
  })

  it('renders snapshots with author/time/counts/origin', () => {
    mountDrawer({
      show: true,
      snapshots: [
        snapshot(),
        snapshot({
          snapshot_id: 'snap_2',
          label: '报告自动快照',
          origin: 'report',
          created_by: '王检察官',
          created_at: '2026-09-14 09:30',
          node_count: 0,
          edge_count: 0,
        }),
      ],
    })
    const text = document.body.textContent ?? ''
    expect(text).toContain('历史快照（2）')
    expect(text).toContain('初查假设形成')
    expect(text).toContain('报告自动快照')
    expect(text).toContain('李侦查员')
    expect(text).toContain('王检察官')
    expect(text).toContain('节点 5')
    expect(text).toContain('连线 4')
    expect(text).toContain('手动')
    expect(text).toContain('报告生成')
  })

  it('rollback requires confirmation then emits snapshot id', async () => {
    const w = mountDrawer({ show: true, snapshots: [snapshot()] })
    const trigger = document.body.querySelector(
      '[data-testid="snapshot-rollback-snap_1"]',
    ) as HTMLButtonElement
    trigger.click()
    await w.vm.$nextTick()
    // 二次确认气泡
    const popText = document.body.textContent ?? ''
    expect(popText).toContain('回滚到该快照？')
    expect(popText).toContain('恢复点')
    const buttons = Array.from(document.body.querySelectorAll('button'))
    const confirm = buttons.find((b) => b.textContent?.trim() === '确认')
    expect(confirm).toBeTruthy()
    ;(confirm as HTMLButtonElement).click()
    expect(drawerEmitted(w, 'rollback')?.[0]).toEqual(['snap_1'])
  })
})

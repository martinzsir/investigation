import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import CanvasNodeWindow from '../src/components/research/CanvasNodeWindow.vue'

// P2 贴附浮窗容器（PRD 功能 3 / REQ-P2 窗口框架）：
// 容器职责——贴附定位（windowPosition 纯函数在 domain 层已对拍，
// 这里验证组件把它们接到 style/data-side）、钉住角标、放大按钮显隐、
// loading/error 态与关闭协议按钮。分型内容由插槽注入，不在本组。

function mountWin(
  overrides: Partial<{
    windowKind: string
    title: string
    anchorX: number
    anchorY: number
    viewportW: number
    viewportH: number
    pinned: boolean
    loading: boolean
    error: string
    enlargeTo: string
  }> = {},
) {
  return mount(CanvasNodeWindow, {
    props: {
      windowKind: 'map',
      title: '文三路 100 号',
      anchorX: 300,
      anchorY: 300,
      viewportW: 800,
      viewportH: 600,
      pinned: false,
      loading: false,
      error: '',
      enlargeTo: '',
      ...overrides,
    },
    slots: { default: '<p class="body-probe">窗口内容</p>' },
    attachTo: document.body,
  })
}

describe('CanvasNodeWindow（贴附浮窗容器）', () => {
  it('头部渲染分型中文标签与节点标题；默认插槽进 win-body', () => {
    const w = mountWin()
    expect(w.find('[data-testid="node-window-map"]').exists()).toBe(true)
    expect(w.text()).toContain('地图窗口')
    expect(w.text()).toContain('文三路 100 号')
    expect(w.find('[data-testid="win-body"]').find('.body-probe').exists()).toBe(true)
    w.unmount()
  })

  it('贴附定位接入：右侧默认（left=锚点+半宽+间距），data-side=right', () => {
    const w = mountWin({ anchorX: 300, viewportW: 800 })
    const root = w.find('[data-testid="node-window-map"]')
    expect(root.attributes('data-side')).toBe('right')
    expect(root.attributes('style')).toContain('left: 405px') // 300 + 93 + 12
    w.unmount()
  })

  it('越界翻转：右缘放不下时贴左侧（data-side=left）', () => {
    const w = mountWin({ anchorX: 600, viewportW: 800 })
    const root = w.find('[data-testid="node-window-map"]')
    expect(root.attributes('data-side')).toBe('left')
    expect(root.attributes('style')).toContain('left: 195px') // 600 - 93 - 12 - 300
    w.unmount()
  })

  it('窄视口全宽兜底（viewportW<340 → 宽=视口-8）', () => {
    const w = mountWin({ viewportW: 300 })
    expect(w.find('[data-testid="node-window-map"]').attributes('style')).toContain(
      'width: 292px',
    )
    w.unmount()
  })

  it('放大按钮：enlargeTo 非空才渲染，点击 emit enlarge', async () => {
    const withRoute = mountWin({ enlargeTo: '/c/graph' })
    expect(withRoute.find('[data-testid="win-enlarge"]').exists()).toBe(true)
    await withRoute.find('[data-testid="win-enlarge"]').trigger('click')
    expect(withRoute.emitted('enlarge')).toHaveLength(1)
    withRoute.unmount()

    const noRoute = mountWin({ windowKind: 'hypothesis' })
    expect(noRoute.find('[data-testid="win-enlarge"]').exists()).toBe(false)
    noRoute.unmount()
  })

  it('钉住：点击 emit toggle-pin；pinned 态渲染「钉」角标与高亮边框', async () => {
    const w = mountWin()
    await w.find('[data-testid="win-pin"]').trigger('click')
    expect(w.emitted('toggle-pin')).toHaveLength(1)
    w.unmount()

    const pinned = mountWin({ pinned: true })
    expect(pinned.find('[data-testid="node-window-map"]').classes()).toContain('cnw-pinned')
    expect(pinned.text()).toContain('钉')
    pinned.unmount()
  })

  it('关闭按钮 emit close', async () => {
    const w = mountWin()
    await w.find('[data-testid="win-close"]').trigger('click')
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('loading 态：骨架屏替代内容，可取消（emit cancel）', async () => {
    const w = mountWin({ loading: true })
    expect(w.find('[data-testid="win-loading"]').exists()).toBe(true)
    expect(w.find('[data-testid="win-body"]').exists()).toBe(false)
    await w.find('[data-testid="win-cancel"]').trigger('click')
    expect(w.emitted('cancel')).toHaveLength(1)
    w.unmount()
  })

  it('error 态：错误文案 + 重试（emit retry）', async () => {
    const w = mountWin({ error: '图谱服务不可用' })
    expect(w.find('[data-testid="win-error"]').exists()).toBe(true)
    expect(w.text()).toContain('图谱服务不可用')
    const retry = w.findAll('button').find((b) => b.text().includes('重试'))
    expect(retry).toBeTruthy()
    await retry!.trigger('click')
    expect(w.emitted('retry')).toHaveLength(1)
    w.unmount()
  })
})

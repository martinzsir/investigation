import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NSelect } from 'naive-ui'
import GlobalHeader from '../src/components/shell/GlobalHeader.vue'
import SideNav from '../src/components/shell/SideNav.vue'
import { useCaseStore } from '../src/stores/case'

// FE-C-024/025 平台页入口补全：案件门户（/cases，建案入口所在页）原本只能手输 URL 可达——
// 1) 顶栏案件下拉选「全部案件」回门户；2) 顶栏门户图标按钮；3) 侧栏品牌 Logo 回门户链接。

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div>root</div>' } },
      { path: '/cases', component: { template: '<div>portal</div>' } },
      { path: '/tasks', component: { template: '<div>tasks</div>' } },
      { path: '/settings', component: { template: '<div>settings</div>' } },
      { path: '/c/:section', component: { template: '<div>case</div>' } },
    ],
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  sessionStorage.clear()
})

describe('GlobalHeader 案件门户入口', () => {
  it('门户图标按钮点击跳转 /cases', async () => {
    const router = makeRouter()
    await router.push('/c/overview')
    await router.isReady()
    const wrapper = mount(GlobalHeader, { global: { plugins: [router] } })

    const btn = wrapper.find('button[title="案件门户"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/cases')
  })

  it('案件下拉选「全部案件」(空值)：清空当前案件并跳 /cases', async () => {
    const router = makeRouter()
    await router.push('/c/overview')
    await router.isReady()
    const wrapper = mount(GlobalHeader, { global: { plugins: [router] } })

    const cs = useCaseStore()
    cs.selectCase('c1')
    expect(cs.currentCaseId).toBe('c1')

    // 模拟 NSelect update:value（'' = 全部案件）
    wrapper.findComponent(NSelect).vm.$emit('update:value', '')
    await flushPromises()

    expect(cs.currentCaseId).toBe('')
    expect(router.currentRoute.value.path).toBe('/cases')
  })

  it('案件下拉选具体案件：只切上下文，不跳门户（回归）', async () => {
    const router = makeRouter()
    await router.push('/c/overview')
    await router.isReady()
    const wrapper = mount(GlobalHeader, { global: { plugins: [router] } })

    const cs = useCaseStore()
    wrapper.findComponent(NSelect).vm.$emit('update:value', 'c9')
    await flushPromises()

    expect(cs.currentCaseId).toBe('c9')
    expect(router.currentRoute.value.path).toBe('/c/overview')
  })

  it('任务中心/系统设置图标入口保持可用（回归）', async () => {
    const router = makeRouter()
    await router.push('/c/overview')
    await router.isReady()
    const wrapper = mount(GlobalHeader, { global: { plugins: [router] } })

    await wrapper.find('button[title="任务中心"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/tasks')

    await wrapper.find('button[title="系统设置"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/settings')
  })
})

describe('SideNav 品牌 Logo 回门户', () => {
  it('品牌区渲染为指向 /cases 的链接（含 title），点击回门户', async () => {
    const router = makeRouter()
    await router.push('/c/overview')
    await router.isReady()
    const wrapper = mount(SideNav, {
      global: { plugins: [router] },
      props: { collapsed: false },
    })

    const brand = wrapper.find('a.brand')
    expect(brand.exists()).toBe(true)
    expect(brand.attributes('href')).toBe('/cases')
    expect(brand.attributes('title')).toBe('案件门户')

    await brand.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/cases')
  })
})

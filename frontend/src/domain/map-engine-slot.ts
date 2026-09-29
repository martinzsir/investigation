// 地图窗口引擎槽位（WIN-07）：**全局只允许一个在线地图实例**。
//
// 为什么必须是硬约束
// ------------------
// Leaflet / 高德实例开销大，而节点窗口是"点一个节点弹一个"的交互——若每开
// 一个地点窗口就 new 一个实例，连点十次就是十个实例，内存与事件监听都会失控，
// 且症状是"越点越卡"而不是报错，排查成本极高。
//
// 槽位是**独占**的：第二个窗口申请时返回 false，该窗口必须退回离线 SVG
// 并写明原因，而不是排队等待或强行创建。

let holder: string | null = null

/** 申请槽位；已有持有者时返回 false（调用方必须回退到离线 SVG） */
export function acquireEngineSlot(who: string): boolean {
  if (holder !== null) return false
  holder = who
  return true
}

export function releaseEngineSlot(who: string): void {
  if (holder === who) holder = null
}

/** 当前持有者（测试与降级提示用） */
export function engineSlotHolder(): string | null {
  return holder
}

/** 测试夹具：强制复位 */
export function resetEngineSlot(): void {
  holder = null
}

// 遮蔽预览领域逻辑（FE-P-005）：模拟后端 core/policy.py 的掩码口径，配置所见即所得。
// 纯函数、确定性可测；口径必须与后端 mask_partial/_MASKS 一致（保前3后4、不足8全遮）。

/** partial 遮蔽：保前 3 后 4，中段全 *；长度不足 8 全遮（如 310****1234） */
export function maskPartial(value: unknown): string {
  const s = value === null || value === undefined ? '' : String(value)
  if (s.length < 8) return '*'.repeat(s.length)
  return s.slice(0, 3) + '*'.repeat(s.length - 7) + s.slice(-4)
}

/** full 遮蔽：统一 ***（与后端 _MASKS full 一致） */
export function maskFull(): string {
  return '***'
}

/** 按 mask 模式渲染预览值 */
export function previewValue(value: unknown, mask: 'partial' | 'full' | 'none' | undefined): string {
  if (mask === 'partial') return maskPartial(value)
  if (mask === 'full') return maskFull()
  return value === null || value === undefined ? '' : String(value)
}

/** 预览演示样例（配置页"所见即所得"测试行） */
export const MASK_SAMPLES: Array<{ label: string; value: string }> = [
  { label: '手机号', value: '13901231234' },
  { label: '身份证号', value: '110101199001011234' },
  { label: '银行卡号', value: '6222021234567890123' },
  { label: '短文本', value: '张三' },
]

/** 遮蔽效果说明文案（按模式） */
export function maskHint(mask: 'partial' | 'full' | 'none'): string {
  switch (mask) {
    case 'partial':
      return '保留前 3 位与后 4 位，中段遮蔽（如 139****1234）；不足 8 位全遮'
    case 'full':
      return '整列显示 ***，不留片段'
    case 'none':
      return '不遮蔽，明文可见（仍受对象级策略约束）'
  }
}

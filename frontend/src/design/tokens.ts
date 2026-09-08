import type { GlobalThemeOverrides } from 'naive-ui'

// FE-D-011：design/tokens.ts 单一事实源 → Naive UI themeOverrides + CSS 变量派生。
// 构建期扫描约束：组件内不得出现硬编码色值（一律经 CSS 变量或 Token 消费）。

/** FE-D-001 Token v1.2 实测值 */
export const colors = {
  bgBase: '#030A14',
  bgCard: '#051522',
  bgCardHover: '#072233',
  bgSider: '#04101C',
  inputBg: '#041828',
  border: '#10314A',
  borderActive: '#6EDEE9',
  primary: '#6EDEE9',
  primaryHover: '#8CE7F2',
  primaryPressed: '#4FC9D8',
  textPrimary: '#E9F7FA',
  textSecondary: '#9FB8C6',
  textTertiary: '#5A7484',
  /** FE-D-007 五间色板：仅用于横条/雷达/热力图，不进状态标签 */
  jian: {
    fund: '#FFB300',
    comms: '#00D4FF',
    behavior: '#B388FF',
    relation: '#4CAF50',
    time: '#FF7043',
  },
  /** FE-D-008 语义色三档：bg/border/text 三值独立（徽章/边框/文字不得共用一个色值） */
  ok: { bg: '#0B2B20', border: '#2E7D5B', text: '#7BD8AC' },
  warn: { bg: '#2B230B', border: '#B5892E', text: '#F2CE7B' },
  error: { bg: '#2B0F12', border: '#8C3B45', text: '#F28B93' },
  info: { bg: '#0B2130', border: '#2E6E8C', text: '#7BC8E8' },
  /** 已立案金橙（与五间资金色靠锁图标 + 法定依据文字区分） */
  filed: { bg: '#241A08', border: '#A8792E', text: '#EFC983' },
} as const

/** FE-D-005：卡片 6px / 按钮 4px / 徽章 12px */
export const radius = { card: '6px', button: '4px', badge: '12px' } as const

/** FE-D-004：URI/哈希/ID/金额一律 JetBrains Mono */
export const fontFamily = {
  sans: "'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif",
  mono: "'JetBrains Mono', 'Cascadia Mono', Consolas, monospace",
} as const

/** FE-D-009 密度：默认 compact（原型按 1920×1080 高密度绘制） */
export const density = {
  rowHeight: { compact: 28, cozy: 40 },
  cardPadding: { compact: 12, cozy: 16 },
  bodySize: { compact: 12, cozy: 13 },
} as const

/** 派生 CSS 变量（main.ts 注入 :root） */
export const cssVariables: Record<string, string> = {
  '--sun-bg-base': colors.bgBase,
  '--sun-bg-card': colors.bgCard,
  '--sun-bg-sider': colors.bgSider,
  '--sun-border': colors.border,
  '--sun-border-active': colors.borderActive,
  '--sun-text-primary': colors.textPrimary,
  '--sun-text-secondary': colors.textSecondary,
  '--sun-font-sans': fontFamily.sans,
  '--sun-font-mono': fontFamily.mono,
  '--sun-ok-bg': colors.ok.bg,
  '--sun-warn-bg': colors.warn.bg,
  '--sun-error-bg': colors.error.bg,
  '--sun-info-bg': colors.info.bg,
  '--sun-filed-bg': colors.filed.bg,
}

export const themeOverrides: GlobalThemeOverrides = {
  common: {
    bodyColor: colors.bgBase,
    cardColor: colors.bgCard,
    modalColor: colors.bgCard,
    popoverColor: colors.bgCard,
    tableColor: colors.bgCard,
    tableHeaderColor: colors.bgSider,
    inputColor: colors.inputBg,
    borderColor: colors.border,
    primaryColor: colors.primary,
    primaryColorHover: colors.primaryHover,
    primaryColorPressed: colors.primaryPressed,
    primaryColorSuppl: colors.primary,
    textColorBase: colors.textPrimary,
    textColor1: colors.textPrimary,
    textColor2: colors.textSecondary,
    textColor3: colors.textTertiary,
    fontFamily: fontFamily.sans,
    fontFamilyMono: fontFamily.mono,
    borderRadius: radius.button,
    borderRadiusSmall: '3px',
  },
  Card: {
    borderRadius: radius.card,
    borderColor: colors.border,
  },
  // FE-D-001：主按钮白字 #E9F7FA（实测值，v1.1 深字推演作废）
  Button: {
    textColorPrimary: colors.textPrimary,
  },
  Layout: {
    color: colors.bgBase,
    siderColor: colors.bgSider,
    headerColor: colors.bgCard,
  },
  Menu: {
    itemColorActive: colors.bgCardHover,
    itemColorActiveHover: colors.bgCardHover,
    itemTextColorActive: colors.primary,
    itemTextColorActiveHover: colors.primaryHover,
    itemTextColorChildActive: colors.primary,
  },
  Tag: {
    borderRadius: radius.badge,
  },
}

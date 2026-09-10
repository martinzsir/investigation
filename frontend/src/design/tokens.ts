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
  /** 23a 实测：绝密★内部 金色 */
  gold: '#F2B54D',
} as const

/** 23a 实测：登录按钮青→蓝渐变 / 卡片顶部光线 / 青色辉光 */
export const gradient = {
  primary: 'linear-gradient(92deg, #00E6D4 0%, #00B6E9 52%, #1E96FF 100%)',
  primaryHover: 'linear-gradient(92deg, #33EFE0 0%, #2CC6F2 52%, #42A6FF 100%)',
  cardLine:
    'linear-gradient(90deg, rgba(110,222,233,0) 0%, #9DF4FB 25%, #6EDEE9 55%, rgba(110,222,233,0) 100%)',
} as const

export const glow = {
  cyanSoft: '0 0 24px rgba(0, 210, 220, 0.18)',
  cyanStrong: '0 0 18px rgba(0, 220, 230, 0.45)',
  card: '0 0 32px rgba(0, 190, 220, 0.16), inset 0 0 24px rgba(0, 190, 220, 0.04)',
} as const

/**
 * 状态色调（六项解耦 R6）：states.json tone → 样式令牌。
 * 色值集中在本文件（tokens 是唯一允许硬编码色值处）；组件只按 tone 取令牌，
 * 不再按状态名分支。受控终态（terminal && requires_role==='human'）的金橙
 * 叠加样式由消费方在 tone 基础上改取 FILED_STATUS_META（不新增 gold tone）。
 */
export type StatusTone = 'warning' | 'info' | 'muted' | 'success' | 'danger'

export interface ToneMeta {
  bg: string
  border: string
  text: string
  icon: string
}

export const STATUS_TONE_META: Record<StatusTone, ToneMeta> = {
  // 待查：石板蓝（原 STATUS_META 实测值）
  warning: { bg: 'rgba(107,131,153,.15)', border: '#6B8399', text: '#8FB3CC', icon: '○' },
  // 查证中：青
  info: { bg: 'rgba(0,212,255,.15)', border: '#00D4FF', text: '#00D4FF', icon: '◐' },
  // 已排除：暗石板（旁路态）
  muted: { bg: 'rgba(107,131,153,.08)', border: '#3D5668', text: '#5A7A94', icon: '✕' },
  // 已固证：绿
  success: { bg: 'rgba(46,212,122,.15)', border: '#2ED47A', text: '#2ED47A', icon: '●' },
  // 危险基调（受控终态实际叠加 FILED_STATUS_META 金橙）
  danger: { bg: 'rgba(196,30,58,.25)', border: '#C41E3A', text: '#FF6B7E', icon: '★' },
}

/** 受控终态（已立案）金橙令牌——tone=danger 之上的产品特例叠加（原 STATUS_META 实测值） */
export const FILED_STATUS_META: ToneMeta = {
  bg: 'rgba(196,30,58,.25)',
  border: '#D4AF37',
  text: '#FFD87A',
  icon: '★',
}

/**
 * 侦查五维（dimensions.json，非兵法五间）房间色板：名称 → CSS 变量。
 * 维度集声明化后数量/名称可变，未知名回落三级文本色，不报错。
 */
const DIMENSION_ROOM_VARS: Record<string, string> = {
  资金: 'var(--sun-jian-fund)',
  通讯: 'var(--sun-jian-comms)',
  行为: 'var(--sun-jian-behavior)',
  关系: 'var(--sun-jian-relation)',
  时间: 'var(--sun-jian-time)',
}

export function jianRoomVar(room: string): string {
  return DIMENSION_ROOM_VARS[room] ?? 'var(--sun-text-tertiary)'
}

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
  '--sun-bg-card-hover': colors.bgCardHover,
  '--sun-bg-sider': colors.bgSider,
  '--sun-input-bg': colors.inputBg,
  '--sun-border': colors.border,
  '--sun-border-active': colors.borderActive,
  '--sun-text-primary': colors.textPrimary,
  '--sun-text-secondary': colors.textSecondary,
  '--sun-text-tertiary': colors.textTertiary,
  '--sun-gold': colors.gold,
  '--sun-gradient-primary': gradient.primary,
  '--sun-gradient-primary-hover': gradient.primaryHover,
  '--sun-gradient-card-line': gradient.cardLine,
  '--sun-glow-cyan-soft': glow.cyanSoft,
  '--sun-glow-cyan-strong': glow.cyanStrong,
  '--sun-glow-card': glow.card,
  '--sun-font-sans': fontFamily.sans,
  '--sun-font-mono': fontFamily.mono,
  '--sun-ok-bg': colors.ok.bg,
  '--sun-ok-border': colors.ok.border,
  '--sun-ok-text': colors.ok.text,
  '--sun-warn-bg': colors.warn.bg,
  '--sun-warn-border': colors.warn.border,
  '--sun-warn-text': colors.warn.text,
  '--sun-error-bg': colors.error.bg,
  '--sun-error-border': colors.error.border,
  '--sun-error-text': colors.error.text,
  '--sun-info-bg': colors.info.bg,
  '--sun-info-border': colors.info.border,
  '--sun-info-text': colors.info.text,
  '--sun-filed-bg': colors.filed.bg,
  '--sun-filed-border': colors.filed.border,
  '--sun-filed-text': colors.filed.text,
  // FE-D-007 五间色（资金/通讯/行为/关系/时间）：仅横条/雷达/热力，不进状态标签
  '--sun-jian-fund': colors.jian.fund,
  '--sun-jian-comms': colors.jian.comms,
  '--sun-jian-behavior': colors.jian.behavior,
  '--sun-jian-relation': colors.jian.relation,
  '--sun-jian-time': colors.jian.time,
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

// 研判画布自定义节点「research-card」（UX P0，G6 v5 register 扩展）。
//
// 结构（子图形均具名，事件经 originalTarget 的 className 委托）：
//   key(Rect 卡面) → halo → badge-*(徽标) → label(主标题) →
//   chip(类型圆底+中文单字) → subtitle(副标题)
// 色值不落在本文件：chipFill/chipInk/titleFill/subtitleFill 与卡面样式
// 全部由 ResearchCanvas 从 design/tokens 的 canvasTokens 注入。

import {
  Badge,
  ExtensionCategory,
  Label,
  Rect,
  register,
  type LabelStyleProps,
  type RectStyleProps,
} from '@antv/g6'
import { fontFamily } from '../../design/tokens'
import { AMBIGUOUS_BORDER, type PrecisionSymbol } from '../../domain/canvas-window'

export const RESEARCH_CARD_NODE = 'research-card'

export interface ResearchCardStyleProps extends RectStyleProps {
  /** 左侧类型圆底内的中文单字（规/实/体/行/档/核/证/假/备/查） */
  chipText?: string
  chipFill?: string
  chipInk?: string
  /** 副标题文本（空字符串=不渲染副标题，标题垂直居中） */
  subtitleText?: string
  subtitleFill?: string
  titleFill?: string
  /** 研判五维色点（空=不渲染；卡内右上固定位，避开标题与 +/− 徽标） */
  dimDotColor?: string
  /**
   * P2 卡内迷你符号（PRD 功能 2；可选——线索画布不传即零回归）。
   * 规格全部由调用方经 domain/canvas-window.ts 的 precisionSymbol/timeBarSpec
   * 算好传入，本类只做 symbol→G6 样式的机械映射，不做精度档判断（红线 R1）。
   */
  miniDot?: PrecisionSymbol | null
  miniBar?: {
    start: number
    span: number
    symbol: PrecisionSymbol
  } | null
  /** 重名待裁决 ? 徽标（虚线边框由调用方 style.lineDash 承担；红线 R2） */
  miniAmbiguous?: boolean
}

const CARD_WIDTH = 186
const CARD_HEIGHT = 50
/**
 * 维度色点卡内坐标（中心坐标系）：左缘内缩 4px、垂直居中——
 * 标题区可延伸到 x=81（右上会压字），chip 左缘 x=-81，此点在 chip 左侧。
 */
const DIM_DOT_X = -CARD_WIDTH / 2 + 4
const DIM_DOT_Y = 0
/** 卡内左缘到文本起点的距离（12 内边距 + 13 圆底半径 + 7 间距） */
const TEXT_X = 12 + 13 + 7
const TEXT_RIGHT = 12
const CHIP_CENTER_X = TEXT_X - 7 - 13

/** 精度点卡内坐标：左下角（维度色点正下方，卡底上收 7px） */
const MINI_DOT_X = DIM_DOT_X
const MINI_DOT_Y = CARD_HEIGHT / 2 - 7
/** 时间条几何：贴卡底，左右各留 8px；条高 3px，时点块高 5px */
const BAR_PAD_X = 8
const BAR_Y = CARD_HEIGHT / 2 - 2.5
const BAR_HEIGHT = 3
const BAR_TICK_HEIGHT = 5
const BAR_WIDTH = CARD_WIDTH - BAR_PAD_X * 2
const BAR_X0 = -CARD_WIDTH / 2 + BAR_PAD_X
/** ? 徽标：卡右上角（右上「窗口」徽标由调用方 badges 挂，二者错位） */
const AMB_X = CARD_WIDTH / 2 - 9
const AMB_Y = -CARD_HEIGHT / 2 + 9

class ResearchCardNode extends Rect {
  static defaultStyleProps = {
    ...Rect.defaultStyleProps,
    size: [CARD_WIDTH, CARD_HEIGHT],
    radius: 8,
    icon: false,
    port: false,
  } as Partial<ResearchCardStyleProps>

  protected getLabelStyle(
    attributes: Required<ResearchCardStyleProps>,
  ): false | LabelStyleProps {
    if (attributes.label === false || !attributes.labelText) return false
    return {
      x: -CARD_WIDTH / 2 + TEXT_X,
      y: attributes.subtitleText ? -8 : 0,
      text: String(attributes.labelText),
      fill: attributes.titleFill,
      fontSize: 12,
      fontWeight: 600,
      fontFamily: fontFamily.sans,
      textAlign: 'left',
      textBaseline: 'middle',
      wordWrap: true,
      wordWrapWidth: CARD_WIDTH - TEXT_X - TEXT_RIGHT,
      maxLines: 1,
      textOverflow: '...',
      background: false,
    }
  }

  render(
    attributes = this.parsedAttributes as Required<ResearchCardStyleProps>,
    container = this,
  ): void {
    super.render(attributes, container)

    const chipX = -CARD_WIDTH / 2 + CHIP_CENTER_X
    this.upsert(
      'chip',
      Badge,
      attributes.chipText
        ? {
            x: chipX,
            y: 0,
            text: attributes.chipText,
          backgroundWidth: 26,
          backgroundHeight: 26,
            backgroundFill: attributes.chipFill,
            backgroundRadius: '50%',
            fill: attributes.chipInk,
            fontSize: 13,
            fontWeight: 700,
            fontFamily: fontFamily.sans,
            textAlign: 'center',
            textBaseline: 'middle',
            padding: 0,
          }
        : false,
      container,
    )

    this.upsert(
      'dimdot',
      Badge,
      attributes.dimDotColor
        ? {
            x: DIM_DOT_X,
            y: DIM_DOT_Y,
            text: '',
            backgroundWidth: 7,
            backgroundHeight: 7,
            backgroundFill: attributes.dimDotColor,
            backgroundRadius: '50%',
            fill: attributes.dimDotColor,
            fontSize: 0,
            padding: 0,
          }
        : false,
      container,
    )

    this.upsert(
      'subtitle',
      Label,
      attributes.subtitleText
        ? {
            x: -CARD_WIDTH / 2 + TEXT_X,
            y: 10,
            text: String(attributes.subtitleText),
            fill: attributes.subtitleFill,
            fontSize: 10,
            fontFamily: fontFamily.sans,
            textAlign: 'left',
            textBaseline: 'middle',
            wordWrap: true,
            wordWrapWidth: CARD_WIDTH - TEXT_X - TEXT_RIGHT,
            maxLines: 1,
            textOverflow: '...',
            background: false,
          }
        : false,
      container,
    )

    // ---- P2 卡内迷你符号（可选；undefined/visible=false 一律不渲染占位）----
    this.upsert('minidot', Badge, this.miniDotSpec(attributes), container)
    this.upsert('minibar', Rect, this.miniBarSpec(attributes), container)
    this.upsert('minitick', Rect, this.miniTickSpec(attributes), container)
    this.upsert(
      'miniamb',
      Badge,
      attributes.miniAmbiguous
        ? {
            x: AMB_X,
            y: AMB_Y,
            text: AMBIGUOUS_BORDER.marker,
            backgroundWidth: 14,
            backgroundHeight: 14,
            backgroundFill: AMBIGUOUS_BORDER.color,
            backgroundRadius: '50%',
            fill: '#FFFFFF',
            fontSize: 10,
            fontWeight: 700,
            fontFamily: fontFamily.sans,
            textAlign: 'center',
            textBaseline: 'middle',
            padding: 0,
          }
        : false,
      container,
    )
  }

  /** 精度点样式：solid=实心色点；hollow=卡面底色芯 + 符号色描边（机械映射） */
  private miniDotSpec(
    a: Required<ResearchCardStyleProps>,
  ): Record<string, unknown> | false {
    const s = a.miniDot
    if (!s || !s.visible) return false
    if (s.fillMode === 'solid') {
      return {
        x: MINI_DOT_X,
        y: MINI_DOT_Y,
        text: '',
        backgroundWidth: 7,
        backgroundHeight: 7,
        backgroundFill: s.color,
        backgroundRadius: '50%',
        padding: 0,
      }
    }
    return {
      x: MINI_DOT_X,
      y: MINI_DOT_Y,
      text: '',
      backgroundWidth: 7,
      backgroundHeight: 7,
      backgroundFill: a.fill,
      backgroundRadius: '50%',
      backgroundStroke: s.color,
      backgroundLineWidth: s.strokeWidth,
      padding: 0,
    }
  }

  /** 时间条 span 段：solid=实色条；hollow=描边 + 符号线型（虚线语义） */
  private miniBarSpec(
    a: Required<ResearchCardStyleProps>,
  ): Record<string, unknown> | false {
    const b = a.miniBar
    if (!b || !b.symbol.visible || b.span <= 0) return false
    const w = Math.max(b.span * BAR_WIDTH, 2)
    const x = BAR_X0 + b.start * BAR_WIDTH
    if (b.symbol.fillMode === 'solid') {
      return {
        x,
        y: BAR_Y - BAR_HEIGHT / 2,
        width: w,
        height: BAR_HEIGHT,
        radius: 1.5,
        fill: b.symbol.color,
        stroke: false,
      }
    }
    return {
      x,
      y: BAR_Y - BAR_HEIGHT / 2,
      width: w,
      height: BAR_HEIGHT,
      radius: 1.5,
      fill: false,
      stroke: b.symbol.color,
      lineWidth: b.symbol.strokeWidth,
      lineDash: b.symbol.lineDash,
    }
  }

  /** 时间条时点块（start 位置 3×5 实色小竖块；无跨度时它是唯一时间标记） */
  private miniTickSpec(
    a: Required<ResearchCardStyleProps>,
  ): Record<string, unknown> | false {
    const b = a.miniBar
    if (!b || !b.symbol.visible) return false
    return {
      x: BAR_X0 + b.start * BAR_WIDTH - 1.5,
      y: BAR_Y - BAR_TICK_HEIGHT / 2,
      width: 3,
      height: BAR_TICK_HEIGHT,
      radius: 1,
      fill: b.symbol.color,
      stroke: false,
    }
  }
}

let registered = false

/** 幂等注册（HMR/重复挂载安全） */
export function ensureResearchCardNode(): void {
  if (registered) return
  register(ExtensionCategory.NODE, RESEARCH_CARD_NODE, ResearchCardNode)
  registered = true
}

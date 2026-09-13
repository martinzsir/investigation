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
  }
}

let registered = false

/** 幂等注册（HMR/重复挂载安全） */
export function ensureResearchCardNode(): void {
  if (registered) return
  register(ExtensionCategory.NODE, RESEARCH_CARD_NODE, ResearchCardNode)
  registered = true
}

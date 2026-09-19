// 镜头预设：把「skill_id + params_schema」包装成业务问题卡片（FE-定向镜头易用性）。
// 声明只覆盖交互文案（前端 UI 层）；检测判据与参数双向核对仍在 pack.json / 后端
// _validate_params——预设只是「哪些参数值得让业务人员看见」的编排。
// 未被预设覆盖的定向镜头在弹窗内自动回落「完整参数」模式，新增镜头包不破交互。
export interface LensPresetField {
  /** 对应 params_schema 参数名 */
  param: string
  /** 业务问法（表单标签） */
  label: string
  placeholder?: string
}

export interface LensPreset {
  preset_id: string
  skill_id: string
  /** 业务话术标题 */
  title: string
  /** 一句话说明（业务人员视角） */
  desc: string
  fields: LensPresetField[]
}

export const LENS_PRESETS: LensPreset[] = [
  {
    preset_id: 'neighborhood',
    skill_id: 'relation_neighborhood',
    title: '查关系圈层',
    desc: '看一个人的资金、通讯、轨迹上有哪些关联人和账户',
    fields: [
      { param: 'target_subject', label: '查谁', placeholder: '输入主体姓名/名称' },
    ],
  },
  {
    preset_id: 'common_neighbors',
    skill_id: 'relation_common_neighbors',
    title: '查共同关系',
    desc: '看两个人之间有没有共同的账户、单位或联系人',
    fields: [
      { param: 'subject_a', label: '主体一', placeholder: '输入主体姓名/名称' },
      { param: 'subject_b', label: '主体二', placeholder: '输入另一主体姓名/名称' },
    ],
  },
  {
    preset_id: 'paths',
    skill_id: 'relation_paths',
    title: '查关系路径',
    desc: '看两个人之间经过几步、通过谁连起来',
    fields: [
      { param: 'subject_a', label: '起点', placeholder: '输入主体姓名/名称' },
      { param: 'subject_b', label: '终点', placeholder: '输入另一主体姓名/名称' },
    ],
  },
  {
    preset_id: 'sequence',
    skill_id: 'timeline_sequence',
    title: '查行为时间线',
    desc: '把某个人的转账、通话、出行按时间排成一条线',
    fields: [
      { param: 'target_subject', label: '查谁', placeholder: '输入主体姓名/名称' },
    ],
  },
  {
    preset_id: 'rhythm',
    skill_id: 'timeline_rhythm',
    title: '查异常活跃时段',
    desc: '看某个人在哪些天突然密集转账、通话或出行',
    fields: [
      { param: 'target_subject', label: '查谁', placeholder: '输入主体姓名/名称' },
    ],
  },
  {
    preset_id: 'cross_collision',
    skill_id: 'timeline_cross_collision',
    title: '查围标时间碰撞',
    desc: '看项目公示日前后，哪些人集中出现多类异常活动',
    fields: [
      { param: 'project', label: '哪个项目', placeholder: '输入项目名称' },
    ],
  },
]

export function presetOfSkill(skillId: string): LensPreset | null {
  return LENS_PRESETS.find((p) => p.skill_id === skillId) ?? null
}

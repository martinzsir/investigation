<script setup lang="ts">
// 可视化本体建模器（S2，/c/omodel）—— PRD v1.1 + 线框 A–G。
// 三栏：对象列表(280) / 属性表格(flex) / 对象关系图(360)；与 ModelDesignerView（JSON 编辑）
// 并存渐进替换（D4）：读写同一份 objects.json/links.json，不做双向实时同步（UC-S2-6）。
//
// 三条红线落点：
//  R1 对象 name 新建后置灰不可改（bindings/pk/links 引用，改名=隐式断链）；
//  R2 未覆盖字段原样保留：对象级/link 级未知键 spread 回写，属性值 dict 未知键保留，
//     顶部显示「N 个界面未覆盖字段」提示；文件级 _note 由后端 PUT 只替换 objects/links 键自然保留；
//  R3 保存反馈按改动分级给事实文案（§8.3），绝不出现「已自动触发 RESCAN」。
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  NSpin, NButton, NInput, NSelect, NCheckbox, NTooltip, NTag, NPopconfirm, NModal,
  useMessage, useDialog,
} from 'naive-ui'
import { useCaseStore } from '../stores/case'
import { useAuthStore } from '../stores/auth'
import {
  modelApi, VALUE_TYPES, FIVE_JIAN,
  type ValueType, type PropertySpec, type ObjectType, type LinkType, type Cardinality,
} from '../api/endpoints/model'
import { dataElementsApi, type DataElement } from '../api/endpoints/dataElements'
import { degradeStaleBindings, type DegradedBinding } from '../domain/ontologyModel'
import { presentError, isApiError } from '../api/errors'
import { canWriteConfig } from '../domain/policyMatrix'
import EmptyState from '../components/common/EmptyState.vue'
import RelationGraphPanel from '../components/om/RelationGraphPanel.vue'
import LinkEditModal from '../components/om/LinkEditModal.vue'
import SaveConfirmModal from '../components/om/SaveConfirmModal.vue'

const cs = useCaseStore()
const auth = useAuthStore()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

// ───────────────────────── 载入与工作副本 ─────────────────────────
// objects/links 是可编辑工作副本；savedObjects/savedLinks 是载入快照（放弃更改/diff 基准）。
// 工作副本含未知字段（[k:string]:unknown 透传），编辑只动已知字段 → R2 不丢字段。
const loading = ref(false)
const objects = ref<ObjectType[]>([])
const links = ref<LinkType[]>([])
const pack = ref('default')

/** tombstones：已保存对象/关系的删除标记（灰显 + 删除线，保存时移除；PRD §9.2/9.3） */
const deletedObjectNames = ref<string[]>([])
const deletedLinkNames = ref<string[]>([])

let savedObjects: ObjectType[] = []
let savedLinks: LinkType[] = []

// 数据元（F2 分组下拉：全域层 + 行业层 + 本案件层；S0-1 行业层落地后三层齐全）
const sharedDE = ref<Record<string, DataElement>>({})
const industryDE = ref<Record<string, DataElement>>({})
const industryName = ref<string | null>(null)
const caseDE = ref<Record<string, DataElement>>({})
// 合并优先级与装载一致：案件 > 行业 > 全域（同 ID 上层覆盖下层）
const allDE = computed<Record<string, DataElement>>(() => ({
  ...sharedDE.value, ...industryDE.value, ...caseDE.value,
}))

/** E2-1：载入/放弃恢复时降级的失效绑定清单（线框 D3：已降级 + 提示重新选择） */
const degradedBindings = ref<DegradedBinding[]>([])

const canWrite = computed(() => canWriteConfig(auth.clearance))

async function load(): Promise<void> {
  if (!cs.currentCaseId) return
  loading.value = true
  saveError.value = ''
  try {
    const [o, l, shared, ind, caseDe] = await Promise.all([
      modelApi.listObjects(cs.currentCaseId),
      modelApi.listLinks(cs.currentCaseId),
      dataElementsApi.listShared(cs.currentCaseId).catch(() => null),
      dataElementsApi.listIndustry(cs.currentCaseId).catch(() => null),
      dataElementsApi.get(cs.currentCaseId).catch(() => null),
    ])
    savedObjects = JSON.parse(JSON.stringify(o.objects)) as ObjectType[]
    savedLinks = JSON.parse(JSON.stringify(l.links)) as LinkType[]
    objects.value = JSON.parse(JSON.stringify(o.objects)) as ObjectType[]
    links.value = JSON.parse(JSON.stringify(l.links)) as LinkType[]
    pack.value = o.pack
    deletedObjectNames.value = []
    deletedLinkNames.value = []
    pendingDeletedProps.value = []
    sharedDE.value = shared?.elements ?? {}
    industryDE.value = ind?.elements ?? {}
    industryName.value = ind?.industry ?? null
    caseDE.value = caseDe?.elements ?? {}
    // E2-1：载入即降级失效绑定（工作副本就地清理；保存快照不动 → dirty 如实反映）
    degradedBindings.value = degradeStaleBindings(objects.value, allDE.value)
    selectedName.value = objects.value[0]?.name ?? ''
    syncRows()  // 重载后 selectedName 可能未变（watch 不触发），显式刷新属性行
  } catch (e) {
    message.error(isApiError(e) ? e.message : presentError(e).title)
  } finally {
    loading.value = false
  }
}

// ───────────────────────── 选中对象与属性行模型 ─────────────────────────
const selectedName = ref('')

interface PropRow {
  key: string
  /** 字符串值类型 或映射 {type|composite|data_element, ...未知键}（未知键 R2 原样保留） */
  val: ValueType | PropertySpec
  deleted: boolean
  /** E2-1：绑定的数据元不存在（阻止保存，须重选或取消绑定） */
  deMissing: boolean
  isNew: boolean
}

const rows = ref<PropRow[]>([])
/** 属性级 tombstone（删除线 + ↺ 恢复，保存时移除；线框 C2） */
const pendingDeletedProps = ref<string[]>([])

function findObj(name: string): ObjectType | undefined {
  return objects.value.find((o) => o.name === name)
}

const selectedObj = computed(() => findObj(selectedName.value))

/** dict → rows（记录 E2-1 失效绑定） */
function propsToRows(props: Record<string, ValueType | PropertySpec>): PropRow[] {
  return Object.entries(props ?? {}).map(([key, val]) => {
    const de = typeof val === 'object' && val !== null ? (val as PropertySpec).data_element : undefined
    return {
      key,
      val: typeof val === 'object' && val !== null ? { ...(val as PropertySpec) } : val,
      deleted: false,
      deMissing: typeof de === 'string' && de !== '' && !Object.hasOwn(allDE.value, de),
      isNew: false,
    }
  })
}

/** rows → dict（保持行序 = 键序；deleted 行剔除） */
function rowsToProps(): Record<string, ValueType | PropertySpec> {
  const out: Record<string, ValueType | PropertySpec> = {}
  for (const r of rows.value) {
    if (r.deleted) continue
    out[r.key] = r.val
  }
  return out
}

function commitRows(): void {
  const o = findObj(selectedName.value)
  if (o) o.properties = rowsToProps()
}

function selectObject(name: string): void {
  if (name === selectedName.value) return
  commitRows()
  selectedName.value = name
  syncRows()
}

function syncRows(): void {
  const o = findObj(selectedName.value)
  rows.value = propsToRows(o?.properties ?? {})
  pendingDeletedProps.value = []
}

watch(selectedName, syncRows)

// 新建属性行：立即进入编辑，键名默认空（保存校验拦截空名）
function addProp(): void {
  const key = `prop_${rows.value.filter(r => r.isNew).length + 1}`
  rows.value.push({ key, val: 'string', deleted: false, deMissing: false, isNew: true })
}

/** E1-2：删除作为 pk 的属性 → 阻止（UC-01-3） */
function askDeleteProp(row: PropRow): void {
  if (row.deleted) return
  const o = selectedObj.value
  if (o && o.pk === row.key) {
    dialog.error({
      title: '不可删除主键属性',
      content: `「${row.key}」是主键，不可删除。如需更换请先在 pk 列或对象元信息中指定其他属性为主键。`,
      positiveText: '知道了',
    })
    return
  }
  row.deleted = true
  pendingDeletedProps.value.push(row.key)
}

function restoreProp(row: PropRow): void {
  row.deleted = false
  pendingDeletedProps.value = pendingDeletedProps.value.filter((k) => k !== row.key)
}

// 属性值操作（全部保持未知键 spread → R2）
function rowType(row: PropRow): ValueType | '' {
  if (typeof row.val === 'string') return row.val
  return (row.val as PropertySpec).type ?? ''
}
function rowDE(row: PropRow): string {
  if (typeof row.val === 'object' && row.val !== null) return (row.val as PropertySpec).data_element ?? ''
  return ''
}
function rowComposite(row: PropRow): boolean {
  return typeof row.val === 'object' && row.val !== null && (row.val as PropertySpec).composite === true
}

function setType(row: PropRow, t: ValueType | ''): void {
  if (typeof row.val === 'string') {
    if (t === '') return
    row.val = { type: t }
  } else {
    const rest = { ...(row.val as PropertySpec) }
    if (t === '') delete rest.type
    else rest.type = t
    row.val = rest
  }
}

function bindDE(row: PropRow, de: string): void {
  // 用户重选/取消 → E2-1 降级标记随操作消除（该属性已不再是降级态）
  degradedBindings.value = degradedBindings.value.filter(
    (d) => !(d.obj === selectedName.value && d.prop === row.key))
  // （无）→ 取消绑定（UC-02-3：data_element 键被移除，不留空值）
  if (de === '') {
    if (typeof row.val === 'object' && row.val !== null) {
      const rest = { ...(row.val as PropertySpec) }
      delete rest.data_element
      row.deMissing = false
      const keys = Object.keys(rest)
      // 只剩 type 时退化为纯字符串类型；全空退化为 string
      if (keys.length === 1 && rest.type !== undefined) row.val = rest.type
      else if (keys.length === 0) row.val = 'string'
      else row.val = rest
    }
    return
  }
  const base: PropertySpec = typeof row.val === 'string' ? {} : { ...(row.val as PropertySpec) }
  // 绑定落盘 {"data_element": de}（type 可省略，列类型随数据元——PRD F2 实测 9 处均省略）
  delete base.type
  base.data_element = de
  row.val = base
  row.deMissing = false
  lastDeRow.value = { key: row.key, de }
}

function setComposite(row: PropRow, on: boolean): void {
  const base: PropertySpec = typeof row.val === 'string' ? {} : { ...(row.val as PropertySpec) }
  if (on) base.composite = true
  else delete base.composite
  const keys = Object.keys(base)
  if (keys.length === 1 && base.type !== undefined) row.val = base.type
  else if (keys.length === 0) row.val = 'string'
  else row.val = base
}

function renameKey(row: PropRow, key: string): void {
  row.key = key
}

/** E2-2：显式 type 与数据元 type 冲突 → 警告不阻止（D5 同款精神） */
function typeConflict(row: PropRow): string {
  const de = rowDE(row)
  const t = rowType(row)
  if (!de || !t) return ''
  const deType = allDE.value[de]?.type
  if (deType && deType !== t) return `数据元 ${de} 的推荐类型为 ${deType}，当前为 ${t}`
  return ''
}

/** E2-1 行内标记：当前选中对象里被降级的属性 → 原数据元码（数据元列删除线提示） */
const degradedByProp = computed<Map<string, string>>(() => {
  const m = new Map<string, string>()
  for (const d of degradedBindings.value) {
    if (d.obj === selectedName.value) m.set(d.prop, d.de)
  }
  return m
})

// D2 继承回显（只读）：最近绑定行
const lastDeRow = ref<{ key: string; de: string } | null>(null)
const inheritInfo = computed(() => {
  if (!lastDeRow.value) return null
  const de = allDE.value[lastDeRow.value.de]
  if (!de) return null
  const cr = (de as { clean_rule?: unknown }).clean_rule
  return {
    de: lastDeRow.value.de,
    format: de.format ?? '',
    mask: de.mask ?? '',
    cleanRule: Array.isArray(cr) ? cr.join(' / ') : '',
    sensitive: de.sensitive === true,
  }
})

// ───────────────────────── 对象列表操作（F1.1）─────────────────────────
interface ObjView {
  name: string
  title: string
  kind: 'entity' | 'event'
  propCount: number
  state: 'new' | 'modified' | 'unchanged' | 'deleted'
}

const visibleObjects = computed<ObjectType[]>(() =>
  objects.value.filter((o) => !deletedObjectNames.value.includes(o.name)))

const tombstonedObjects = computed<ObjectType[]>(() =>
  savedObjects.filter((o) => deletedObjectNames.value.includes(o.name)))

const objViews = computed<ObjView[]>(() => {
  const savedByName = new Map(savedObjects.map((o) => [o.name, o]))
  const views: ObjView[] = []
  for (const o of objects.value) {
    if (deletedObjectNames.value.includes(o.name)) continue
    const s = savedByName.get(o.name)
    let state: ObjView['state'] = 'unchanged'
    if (!s) state = 'new'
    else if (JSON.stringify(stripVolatile(o)) !== JSON.stringify(stripVolatile(s))) state = 'modified'
    views.push({
      name: o.name, title: String(o.title ?? ''), kind: o.kind,
      propCount: Object.keys(o.properties ?? {}).length, state,
    })
  }
  for (const o of tombstonedObjects.value) {
    views.push({
      name: o.name, title: String(o.title ?? ''), kind: o.kind,
      propCount: Object.keys(o.properties ?? {}).length, state: 'deleted',
    })
  }
  return views
})

/** 对比时剥离工作过程字段（无——这里仅防御未来误加） */
function stripVolatile(o: ObjectType): unknown {
  return o
}

const deletedRefs = computed<Record<string, LinkType[]>>(() => {
  // E3-2：被关系引用的对象不可删除（活动关系才算引用；tombstone 关系已标记删除）
  const map: Record<string, LinkType[]> = {}
  for (const l of activeLinks.value) {
    for (const end of [l.from_obj, l.to_obj]) {
      (map[String(end)] ??= []).push(l)
    }
  }
  return map
})

function askDeleteObject(name: string): void {
  const refs = deletedRefs.value[name]
  if (refs?.length) {
    dialog.error({
      title: '不可删除被引用对象',
      content: `对象 ${name} 被 ${refs.length} 条关系引用，不可删除。引用关系：${refs.map((r) => r.name).join('、')}。请先在关系图中删除相关关系。`,
      positiveText: '知道了',
      // 线框 E3-2 附「前往关系图处理」
    })
    return
  }
  const wasSelected = selectedName.value === name
  if (wasSelected) commitRows()  // 切换前保留当前属性行编辑
  const isNew = !savedObjects.some((o) => o.name === name)
  if (isNew) {
    objects.value = objects.value.filter((o) => o.name !== name)
  } else if (!deletedObjectNames.value.includes(name)) {
    deletedObjectNames.value.push(name)
  }
  if (wasSelected) selectedName.value = visibleObjects.value[0]?.name ?? ''
}

// 新建对象（线框 C1 内联表单）
const createOpen = ref(false)
const createForm = ref<{ name: string; title: string; kind: 'entity' | 'event' }>({ name: '', title: '', kind: 'entity' })
const createError = ref('')
const NAME_RE = /^[a-z_]+$/

function openCreate(): void {
  createForm.value = { name: '', title: '', kind: 'entity' }
  createError.value = ''
  createOpen.value = true
}

function confirmCreate(): void {
  const { name, title, kind } = createForm.value
  if (!NAME_RE.test(name)) {
    createError.value = '对象名仅允许小写字母与下划线（^[a-z_]+$）'
    return
  }
  const exists = objects.value.some((o) => o.name === name) || deletedObjectNames.value.includes(name)
  if (exists) {
    createError.value = `对象名 ${name} 已存在`  // E1-3
    return
  }
  objects.value.push({ name, title: title || undefined, kind, properties: {} })
  createOpen.value = false
  commitRows()
  selectedName.value = name
  syncRows()
}

// R1：新建后 name 不可改 —— 界面根本不提供 name 编辑控件（保存过的对象 name 只读展示 + tooltip）

// ───────────────────────── 对象元信息（F1.3 折叠区）─────────────────────────
const metaOpen = ref(false)

/** R2：界面未覆盖字段计数（对象级已知字段之外的键） */
const KNOWN_OBJ_FIELDS = ['name', 'title', 'pk', 'kind', 'name_property', 'jian', 'jian_source', 'properties']
const uncoveredFields = computed<string[]>(() => {
  const o = selectedObj.value
  if (!o) return []
  return Object.keys(o).filter((k) => !KNOWN_OBJ_FIELDS.includes(k))
})

function setKind(kind: 'entity' | 'event' | null): void {
  const o = selectedObj.value
  if (o && kind) o.kind = kind
}
function setNameProperty(v: string | null): void {
  const o = selectedObj.value
  if (!o) return
  if (v) o.name_property = v
  else delete o.name_property
}
function setPk(v: string | null): void {
  const o = selectedObj.value
  if (!o) return
  if (v) o.pk = v
  else delete o.pk
}
function setJian(v: string | null): void {
  const o = selectedObj.value
  if (!o) return
  if (v) o.jian = v
  else delete o.jian
}
function setJianSource(v: string): void {
  const o = selectedObj.value
  if (!o) return
  if (v) o.jian_source = v
  else delete o.jian_source
}
function setTitle(v: string): void {
  const o = selectedObj.value
  if (!o) return
  if (v) o.title = v
  else delete o.title
}

// ───────────────────────── 关系（F3）─────────────────────────
const activeLinks = computed<LinkType[]>(() =>
  links.value.filter((l) => !deletedLinkNames.value.includes(l.name)))

const savedLinkNames = computed(() => new Set(savedLinks.map((l) => l.name)))

const graphNodes = computed(() =>
  visibleObjects.value.map((o) => ({
    id: o.name,
    label: String(o.title ?? '') || o.name,
    kind: o.kind,
    isNew: !savedObjects.some((s) => s.name === o.name),
    selected: o.name === selectedName.value,
  })))

const graphEdges = computed(() => [
  ...activeLinks.value.map((l) => {
    const s = savedLinks.find((x) => x.name === l.name)
    return {
      id: l.name,
      label: String(l.title ?? '') || l.name,
      source: String(l.from_obj),
      target: String(l.to_obj),
      state: (s ? (JSON.stringify(stripVolatile(l)) !== JSON.stringify(stripVolatile(s)) ? 'modified' : 'unchanged') : 'new') as 'new' | 'modified' | 'deleted' | 'unchanged',
    }
  }),
  ...savedLinks
    .filter((l) => deletedLinkNames.value.includes(l.name))
    .map((l) => ({
      id: l.name, label: String(l.title ?? '') || l.name,
      source: String(l.from_obj), target: String(l.to_obj),
      state: 'deleted' as const,
    })),
])

// LinkEditModal 接入（E2）
interface LinkForm { name: string; title: string; from_obj: string; to_obj: string; cardinality: '' | Cardinality }
const linkModal = ref<{ show: boolean; mode: 'create' | 'edit'; editName: string }>({ show: false, mode: 'create', editName: '' })

const objectOptions = computed(() => visibleObjects.value.map((o) => ({ label: o.name, value: o.name })))

const linkModalInitial = computed<LinkForm>(() => {
  if (linkModal.value.mode === 'edit') {
    const l = links.value.find((x) => x.name === linkModal.value.editName)
    if (l) {
      return {
        name: l.name,
        title: String(l.title ?? ''),
        from_obj: String(l.from_obj),
        to_obj: String(l.to_obj),
        cardinality: (l.cardinality as Cardinality | undefined) ?? '',
      }
    }
  }
  return { name: '', title: '', from_obj: '', to_obj: '', cardinality: '' }
})

const linkModalNames = computed(() =>
  links.value.filter((l) => l.name !== linkModal.value.editName).map((l) => l.name))

function openLinkCreate(): void {
  linkModal.value = { show: true, mode: 'create', editName: '' }
}

function onGraphEditEdge(key: string): void {
  if (deletedLinkNames.value.includes(key)) {
    // tombstone 边点击 → 恢复（线框属性 dead 行 ↺ 同款精神）
    deletedLinkNames.value = deletedLinkNames.value.filter((n) => n !== key)
    message.success(`关系 ${key} 已恢复`)
    return
  }
  linkModal.value = { show: true, mode: 'edit', editName: key }
}

function onLinkConfirm(form: LinkForm): void {
  if (linkModal.value.mode === 'create') {
    if (links.value.some((l) => l.name === form.name)) {
      message.error(`关系名 ${form.name} 已存在`)
      return
    }
    const nl: LinkType = { name: form.name, from_obj: form.from_obj, to_obj: form.to_obj }
    if (form.title) nl.title = form.title
    if (form.cardinality) nl.cardinality = form.cardinality
    links.value.push(nl)
  } else {
    const t = links.value.find((l) => l.name === form.name)
    if (!t) return
    // 只动已知字段，其余（jian/jian_source 等）原样保留（R2）
    if (form.title) t.title = form.title
    else delete t.title
    t.from_obj = form.from_obj
    t.to_obj = form.to_obj
    if (form.cardinality) t.cardinality = form.cardinality
    else delete t.cardinality
  }
  linkModal.value.show = false
}

function onLinkDelete(): void {
  const name = linkModal.value.editName
  const isNew = !savedLinkNames.value.has(name)
  if (isNew) links.value = links.value.filter((l) => l.name !== name)
  else if (!deletedLinkNames.value.includes(name)) deletedLinkNames.value.push(name)
  linkModal.value.show = false
}

// ───────────────────────── dirty 与改动分级（§8.2）─────────────────────────
function buildPayload(): { objects: ObjectType[]; links: LinkType[] } {
  commitRows()
  return {
    objects: objects.value.filter((o) => !deletedObjectNames.value.includes(o.name)),
    links: links.value.filter((l) => !deletedLinkNames.value.includes(l.name)),
  }
}

/** 无副作用快照：选中对象的最新属性直接取自 rows（未提交），供 dirty 计算 */
function payloadJson(): string {
  const objs = objects.value
    .filter((o) => !deletedObjectNames.value.includes(o.name))
    .map((o) => (o.name === selectedName.value ? { ...o, properties: rowsToProps() } : o))
  const lnks = links.value.filter((l) => !deletedLinkNames.value.includes(l.name))
  return JSON.stringify({ objects: objs, links: lnks })
}

const savedSnapshot = computed(() => JSON.stringify({ objects: savedObjects, links: savedLinks }))
const dirty = computed(() => payloadJson() !== savedSnapshot.value)

interface DiffResult { structural: string[]; relational: string[]; semantic: string[] }

function buildDiff(): DiffResult {
  commitRows()
  const structural: string[] = []
  const relational: string[] = []
  const semantic: string[] = []
  const savedByName = new Map(savedObjects.map((o) => [o.name, o]))

  for (const name of deletedObjectNames.value) structural.push(`删除对象类型 ${name}`)
  for (const o of objects.value) {
    if (deletedObjectNames.value.includes(o.name)) continue
    const s = savedByName.get(o.name)
    if (!s) { structural.push(`新增对象类型 ${o.name}`); continue }
    if ((o.pk ?? '') !== (s.pk ?? '')) structural.push(`对象 ${o.name} 的 pk：${s.pk ?? '（空）'} → ${o.pk ?? '（空）'}`)
    if (o.kind !== s.kind) structural.push(`对象 ${o.name} 的 kind：${s.kind} → ${o.kind}`)
    if ((o.name_property ?? '') !== (s.name_property ?? '')) {
      structural.push(`对象 ${o.name} 的 name_property：${s.name_property ?? '（空）'} → ${o.name_property ?? '（空）'}`)
    }
    if ((o.title ?? '') !== (s.title ?? '')) semantic.push(`对象 ${o.name} 的显示名`)
    if ((o.jian ?? '') !== (s.jian ?? '')) semantic.push(`对象 ${o.name} 的间类`)
    if ((o.jian_source ?? '') !== (s.jian_source ?? '')) semantic.push(`对象 ${o.name} 的间类来源`)
    // 属性级：增删/type/composite → 结构性；data_element → 语义性
    const sp = (s.properties ?? {}) as Record<string, ValueType | PropertySpec>
    const np = (o.properties ?? {}) as Record<string, ValueType | PropertySpec>
    for (const k of Object.keys(sp)) {
      if (!(k in np)) structural.push(`对象 ${o.name} 删除属性 ${k}`)
    }
    for (const [k, nv] of Object.entries(np)) {
      const sv = sp[k]
      if (sv === undefined) { structural.push(`对象 ${o.name} 新增属性 ${k}`); continue }
      const norm = (v: ValueType | PropertySpec): PropertySpec =>
        typeof v === 'string' ? { type: v } : { ...v }
      const a = norm(sv)
      const b = norm(nv)
      const base = (x: PropertySpec): string => JSON.stringify({ t: x.type ?? '', c: x.composite === true })
      if (base(a) !== base(b)) {
        structural.push(`对象 ${o.name} 属性 ${k} 的类型/复合：${a.type ?? '（空）'}${a.composite ? '(复合)' : ''} → ${b.type ?? '（空）'}${b.composite ? '(复合)' : ''}`)
      }
      if ((a.data_element ?? '') !== (b.data_element ?? '')) semantic.push(`对象 ${o.name} 属性 ${k} 的数据元绑定`)
    }
  }
  const savedLinkByName = new Map(savedLinks.map((l) => [l.name, l]))
  for (const name of deletedLinkNames.value) relational.push(`删除关系 ${name}`)
  for (const l of links.value) {
    if (deletedLinkNames.value.includes(l.name)) continue
    const s = savedLinkByName.get(l.name)
    if (!s) { relational.push(`新增关系 ${l.name}`); continue }
    if (JSON.stringify(stripVolatile(l)) !== JSON.stringify(stripVolatile(s))) relational.push(`修改关系 ${l.name}`)
  }
  return { structural, relational, semantic }
}

// ───────────────────────── 保存校验（§8.1 客户端层）─────────────────────────
const errorPropKeys = ref<Set<string>>(new Set())

function validatePayload(p: { objects: ObjectType[]; links: LinkType[] }): string[] {
  const errs: string[] = []
  const names = new Set<string>()
  errorPropKeys.value = new Set()
  for (const o of p.objects) {
    if (!o.name) { errs.push('存在缺少 name 的对象'); continue }
    if (!NAME_RE.test(o.name)) errs.push(`对象名 ${o.name} 不合法（^[a-z_]+$）`)
    if (names.has(o.name)) errs.push(`对象名 ${o.name} 已存在`)  // E1-3
    names.add(o.name)
    // 属性名唯一（E1-1：dict 键重复会被 JSON 解析静默覆盖，必须 UI 层拦截）+ 空名
    const seen = new Set<string>()
    for (const k of Object.keys(o.properties ?? {})) {
      if (!k.trim()) { errs.push(`对象 ${o.name} 存在空属性名`); errorPropKeys.value.add(k) }
      else if (seen.has(k)) { errs.push(`属性名 ${k} 在对象 ${o.name} 内重复，请修改`); errorPropKeys.value.add(k) }
      seen.add(k)
    }
    // pk / name_property 保存前必须齐备（schema required，建后可补——PRD F1.1）
    const props = Object.keys(o.properties ?? {})
    if (!o.pk) errs.push(`对象 ${o.name} 未指定 pk（保存前必须齐备）`)
    else if (!props.includes(o.pk)) errs.push(`对象 ${o.name} 的 pk=${o.pk} 不在已声明属性中`)
    if (!o.name_property) errs.push(`对象 ${o.name} 未指定 name_property（保存前必须齐备）`)
    else if (!props.includes(o.name_property)) errs.push(`对象 ${o.name} 的 name_property=${o.name_property} 不在已声明属性中`)
    // composite 仅 string（与 ModelDesignerView 同口径）
    for (const [k, v] of Object.entries(o.properties ?? {})) {
      if (typeof v === 'object' && v !== null && v.composite === true && v.type !== undefined && v.type !== 'string') {
        errs.push(`对象 ${o.name} 属性 ${k}：composite 降级仅支持 string 类型`)
      }
    }
  }
  // E2-1 兜底：载入时已由 degradeStaleBindings 降级，正常不会再有失效绑定；
  // 此拦截防「静默保存成功」（UC-02-4 FAIL 判据），如其他标签页写入脏绑定后本页保存。
  for (const o of p.objects) {
    for (const [k, v] of Object.entries(o.properties ?? {})) {
      const de = typeof v === 'object' && v !== null ? v.data_element : undefined
      if (typeof de === 'string' && de && !Object.hasOwn(allDE.value, de)) {
        errs.push(`对象 ${o.name} 属性 ${k} 绑定的数据元 ${de} 不存在，请重新选择或取消绑定`)
      }
    }
  }
  // E3-1：关系端点必须存在（弹窗已拦，保存兜底）
  for (const l of p.links) {
    if (!names.has(String(l.from_obj)) || !names.has(String(l.to_obj))) {
      errs.push(`关系 ${l.name} 的端点对象不存在（${l.from_obj} → ${l.to_obj}）`)
    }
  }
  return errs
}

// ───────────────────────── 保存流（F1/F2/F3/F5/F6）─────────────────────────
const confirmOpen = ref(false)
const confirmReason = ref('')
const confirmSaving = ref(false)
const saveError = ref('')
const pendingDiff = ref<DiffResult | null>(null)

function askSave(): void {
  saveError.value = ''
  const p = buildPayload()
  const errs = validatePayload(p)
  if (errs.length) {
    saveError.value = errs.join('；')
    return
  }
  const diff = buildDiff()
  const dangerous = diff.structural.length > 0 || diff.relational.length > 0
  if (dangerous) {
    pendingDiff.value = diff
    confirmReason.value = ''
    confirmOpen.value = true
    return
  }
  // 语义性改动：无弹窗直接保存（§8.2）；理由可选但建议留痕
  void executeSave(diff, '')
}

async function onConfirmSave(reason: string): Promise<void> {
  if (!pendingDiff.value) return
  await executeSave(pendingDiff.value, reason)
}

async function executeSave(diff: DiffResult, reason: string): Promise<void> {
  if (!cs.currentCaseId) return
  const p = buildPayload()
  confirmSaving.value = true
  saveError.value = ''
  try {
    const objectsChanged = JSON.stringify(p.objects) !== JSON.stringify(savedObjects)
    const linksChanged = JSON.stringify(p.links) !== JSON.stringify(savedLinks)
    // 失败保留用户输入：工作副本不动，仅错误条展示（PRD §9.1 failed）
    if (objectsChanged) await modelApi.saveObjects(cs.currentCaseId, p.objects, reason || undefined)
    if (linksChanged) await modelApi.saveLinks(cs.currentCaseId, p.links, reason || undefined)
    confirmOpen.value = false
    // §8.3 分级文案（R3：绝不出现「已自动触发」）
    if (diff.structural.length > 0 || diff.relational.length > 0) {
      dialog.warning({
        title: '本体已保存（需重跑 BUILD）',
        content:
          '🔴 本体已保存。需重跑 BUILD 才生效 —— 新增对象类型不重跑会导致 obj_* 表不存在，相关功能不可用。' +
          '保存本身不触发任何重跑，是否重跑由你在任务中心决定。',
        positiveText: '前往任务中心重跑',
        negativeText: '稍后自行处理',
        onPositiveClick: () => { void router.push('/tasks') },
      })
    } else {
      message.success('🟡 已保存，下次 BUILD 后生效。')
    }
    await load()
  } catch (e) {
    if (isApiError(e) && e.code === 'CONFLICT') {
      // E1-4 并发冲突（服务端 version 比对属后端批次能力，此处预留恢复分支）
      conflictOpen.value = true
    } else {
      const msg = isApiError(e) ? e.message : presentError(e).title
      saveError.value = `保存失败：${msg}`
      if (confirmOpen.value) confirmOpen.value = false
    }
  } finally {
    confirmSaving.value = false
  }
}

// F6 冲突恢复：重新载入（丢弃本地）/ 在新窗口查看差异（打开 JSON 编辑页）
const conflictOpen = ref(false)
function conflictReload(): void {
  conflictOpen.value = false
  void load()
}
function conflictDiff(): void {
  const href = router.resolve({ path: '/c/designer' }).href
  window.open(href, '_blank')
}

function discardChanges(): void {
  objects.value = JSON.parse(JSON.stringify(savedObjects)) as ObjectType[]
  links.value = JSON.parse(JSON.stringify(savedLinks)) as LinkType[]
  deletedObjectNames.value = []
  deletedLinkNames.value = []
  pendingDeletedProps.value = []
  // 文件里的失效绑定恢复回来 → 重新降级（与载入口径一致，脏绑定不留存续态）
  degradedBindings.value = degradeStaleBindings(objects.value, allDE.value)
  saveError.value = ''
  if (!objects.value.some((o) => o.name === selectedName.value)) {
    selectedName.value = objects.value[0]?.name ?? ''
  }
  syncRows()
}

// 整包校验（load_pack 不写盘，与 ModelDesignerView 同一端点）
const validating = ref(false)
async function validatePack(): Promise<void> {
  if (!cs.currentCaseId) return
  validating.value = true
  try {
    await modelApi.validate(cs.currentCaseId)
    message.success('整包校验通过（load_pack 合法）')
  } catch (e) {
    message.error(isApiError(e) ? `校验未通过：${e.message}` : presentError(e).title)
  } finally {
    validating.value = false
  }
}

// ───────────────────────── 下拉选项 ─────────────────────────
const typeOptions = computed(() => {
  const base = VALUE_TYPES.map((t) => ({ label: t, value: t }))
  // 绑定数据元后类型可省略（随数据元 type）
  return [{ label: '（随数据元 type）', value: '' }, ...base]
})

const deGroupOptions = computed(() => {
  const grp = (label: string, src: Record<string, DataElement>, gkey: string) => ({
    type: 'group' as const,
    label,
    key: gkey,
    children: Object.entries(src).map(([code, de]) => ({
      label: de.name ? `${code}（${de.name}）` : code,
      value: code,
    })),
  })
  return [
    grp('全域层', sharedDE.value, 'g-shared'),
    // 行业层仅在有行业声明时出现（E3-1：无行业层不显示空分组）
    ...(industryName.value
      ? [grp(`${industryName.value}行业层`, industryDE.value, 'g-industry')]
      : []),
    grp('本案件层', caseDE.value, 'g-case'),
    { label: '（无）＝ 取消绑定', value: '' },
  ]
})

const kindOptions = [
  { label: 'entity（实体，按 name_property 值分配代理键）', value: 'entity' },
  { label: 'event（事件，按行分配代理键）', value: 'event' },
]
const jianOptions = [
  { label: '（无）', value: '' },
  ...FIVE_JIAN.map((j) => ({ label: j, value: j })),
]
const propOptions = computed(() => {
  const keys = rows.value.filter((r) => !r.deleted && r.key.trim()).map((r) => r.key)
  return keys.map((k) => ({ label: k, value: k }))
})

const deSensitive = (de: string): boolean => allDE.value[de]?.sensitive === true

// 案件切换（immediate 首载须在全部状态定义之后——load 内访问 selectedName/rows/saveError）
watch(() => cs.currentCaseId, () => { void load() }, { immediate: true })
</script>

<template>
  <div class="page">
    <EmptyState v-if="!cs.currentCaseId" type="empty" title="请先选择案件" desc="本体声明按案件快照归属" />

    <NSpin v-else :show="loading">
      <!-- ── 顶部固定区（线框 B2：面包屑 / 来源层 / R2 提示 / 脏标记 / 并存提示 G1）── -->
      <div class="topbar">
        <div class="crumb">
          本体管理器 / objects.json / <b>{{ selectedName || '—' }}</b>
          <NTag size="tiny" :bordered="false" class="layer-tag">{{ pack }} 包</NTag>
          <NTag size="tiny" :bordered="false" type="info" class="layer-tag">案件层</NTag>
          <NTag v-if="dirty" size="tiny" :bordered="false" type="warning">● 未保存</NTag>
        </div>
        <div class="topbar-actions">
          <NButton size="tiny" :loading="validating" @click="validatePack">整包校验</NButton>
        </div>
      </div>

      <div class="hint-bar">
        此对象也可在
        <router-link class="link" :to="{ path: '/c/designer' }">模型设计器 → JSON 编辑</router-link>
        中修改，两边数据一致，切换后请重新载入。
      </div>

      <div v-if="uncoveredFields.length" class="r2-bar">
        ⚠ 本对象有 {{ uncoveredFields.length }} 个界面未覆盖字段（{{ uncoveredFields.join('、') }}），已原样保留（可切 JSON 页编辑）
      </div>

      <!-- E2-1（线框 D3）：数据元失联降级提示 -->
      <div v-if="degradedBindings.length" class="e21-bar">
        ⚠ {{ degradedBindings.length }} 处属性绑定的数据元不存在，已降级为普通类型，请重新选择或保持普通类型：
        <span class="mono">{{ degradedBindings.map((d) => `${d.obj}.${d.prop}（原 ${d.de}）`).join('、') }}</span>
      </div>

      <div v-if="saveError" class="error-bar">{{ saveError }}</div>

      <!-- ── 三栏（线框 B1）── -->
      <div class="col3">
        <!-- 左：对象列表 -->
        <div class="pane pane-left">
          <div class="pane-title">对象列表（{{ objViews.filter(v => v.state !== 'deleted').length }}）</div>
          <div class="obj-list">
            <div
              v-for="v in objViews"
              :key="v.name"
              class="obj-item"
              :class="{ active: v.name === selectedName, deleted: v.state === 'deleted' }"
              @click="v.state !== 'deleted' && selectObject(v.name)"
            >
              <div class="obj-line1">
                <NTag size="tiny" :bordered="false" :type="v.kind === 'event' ? 'warning' : 'info'">{{ v.kind === 'event' ? '事件' : '实体' }}</NTag>
                <span class="mono obj-name">{{ v.name }}</span>
                <NTag v-if="v.state === 'new'" size="tiny" :bordered="false" type="success">新增</NTag>
                <NTag v-else-if="v.state === 'modified'" size="tiny" :bordered="false" type="warning">已改</NTag>
              </div>
              <div class="obj-line2">
                <span class="obj-title">{{ v.title || '—' }}</span>
                <span class="mini">{{ v.propCount }} 属性</span>
                <NPopconfirm v-if="v.state !== 'deleted'" @positive-click="askDeleteObject(v.name)">
                  <template #trigger>
                    <button class="obj-del" title="删除对象" @click.stop>✕</button>
                  </template>
                  确认删除对象 {{ v.name }}？（保存后生效）
                </NPopconfirm>
                <span v-else class="mini">已标记删除</span>
              </div>
            </div>
          </div>

          <!-- 新建对象（线框 C1 内联表单） -->
          <div v-if="createOpen" class="create-form">
            <div class="cf-row">
              <label>对象名 name</label>
              <NInput v-model:value="createForm.name" size="small" placeholder="^[a-z_]+$" class="mono" :status="createError ? 'error' : undefined" />
            </div>
            <div class="cf-row">
              <label>显示名 title</label>
              <NInput v-model:value="createForm.title" size="small" placeholder="选填" />
            </div>
            <div class="cf-row">
              <label>kind</label>
              <NSelect v-model:value="createForm.kind" size="small" :options="kindOptions" />
            </div>
            <div v-if="createError" class="cf-error">{{ createError }}</div>
            <div class="cf-actions">
              <NButton size="tiny" @click="createOpen = false">取消</NButton>
              <NButton size="tiny" type="primary" @click="confirmCreate">确定</NButton>
            </div>
          </div>
          <NButton v-else size="small" block dashed :disabled="!canWrite" @click="openCreate">
            {{ canWrite ? '+ 新建对象' : '🔒 需偏将及以上' }}
          </NButton>
        </div>

        <!-- 中：属性表格（F1.2 ★ 最高价值） -->
        <div class="pane pane-mid">
          <template v-if="selectedObj">
            <div class="pane-title">
              属性表格 · <span class="mono">{{ selectedObj.name }}</span>
              <NTooltip trigger="hover">
                <template #trigger>
                  <span class="lock">🔒</span>
                </template>
                对象名被 bindings / links 引用，不可修改（R1）。如需改名请新建对象并迁移数据。
              </NTooltip>
            </div>

            <!-- F1.3 对象元信息（折叠区） -->
            <div class="meta-toggle" @click="metaOpen = !metaOpen">
              {{ metaOpen ? '▾' : '▸' }} 对象元信息（title / kind / name_property / 间类 / pk）
            </div>
            <div v-if="metaOpen" class="meta-grid">
              <div class="meta-cell">
                <label>title（显示名）</label>
                <NInput size="small" :value="String(selectedObj.title ?? '')" @update:value="setTitle" />
              </div>
              <div class="meta-cell">
                <label>kind（改动属结构性）</label>
                <NSelect size="small" :value="selectedObj.kind" :options="kindOptions" @update:value="setKind" />
              </div>
              <div class="meta-cell">
                <label>name_property</label>
                <NSelect size="small" :value="String(selectedObj.name_property ?? '')" :options="propOptions" @update:value="setNameProperty" />
              </div>
              <div class="meta-cell">
                <label>pk（与表格 pk 列联动）</label>
                <NSelect size="small" :value="String(selectedObj.pk ?? '')" :options="propOptions" @update:value="setPk" />
              </div>
              <div class="meta-cell">
                <label>jian（五间归属）</label>
                <NSelect size="small" :value="String(selectedObj.jian ?? '')" :options="jianOptions" @update:value="(v: string) => setJian(v || null)" />
              </div>
              <div class="meta-cell">
                <label>jian_source</label>
                <NInput size="small" :value="String(selectedObj.jian_source ?? '')" placeholder="间类依据来源" @update:value="setJianSource" />
              </div>
            </div>

            <!-- 属性表格 -->
            <table class="ptable">
              <thead>
                <tr>
                  <th>属性名</th><th>类型</th><th>数据元</th><th>复合</th><th>pk</th><th>敏感</th><th></th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in rows"
                  :key="row.key + (row.isNew ? ':new' : '')"
                  :class="{ dead: row.deleted, 'row-new': row.isNew }"
                >
                  <td>
                    <NInput
                      v-if="!row.deleted"
                      size="small"
                      :value="row.key"
                      class="mono"
                      :status="errorPropKeys.has(row.key) ? 'error' : undefined"
                      @update:value="(v: string) => renameKey(row, v)"
                    />
                    <span v-else class="mono strike">{{ row.key }}</span>
                  </td>
                  <td>
                    <NSelect
                      v-if="!row.deleted"
                      size="small"
                      :value="rowType(row)"
                      :options="typeOptions"
                      class="type-sel"
                      @update:value="(v: ValueType | '') => setType(row, v)"
                    />
                    <span v-else>{{ rowType(row) || '—' }}</span>
                    <NTooltip v-if="!row.deleted && typeConflict(row)" trigger="hover">
                      <template #trigger><span class="warn-mark">⚠</span></template>
                      {{ typeConflict(row) }}（警告不阻止）
                    </NTooltip>
                  </td>
                  <td>
                    <NSelect
                      v-if="!row.deleted"
                      size="small"
                      :value="rowDE(row)"
                      :options="deGroupOptions"
                      class="de-sel"
                      :status="row.deMissing ? 'error' : undefined"
                      @update:value="(v: string) => bindDE(row, v)"
                    />
                    <span v-else class="strike">{{ rowDE(row) || '—' }}</span>
                    <NTooltip v-if="!row.deleted && degradedByProp.get(row.key)" trigger="hover">
                      <template #trigger><span class="de-dead">{{ degradedByProp.get(row.key) }}</span></template>
                      绑定的数据元 {{ degradedByProp.get(row.key) }} 不存在，已降级为普通类型，请重新选择
                    </NTooltip>
                  </td>
                  <td>
                    <NCheckbox
                      v-if="!row.deleted"
                      :checked="rowComposite(row)"
                      :disabled="rowType(row) !== '' && rowType(row) !== 'string'"
                      @update:checked="(v: boolean) => setComposite(row, v)"
                    />
                  </td>
                  <td>
                    <NCheckbox
                      v-if="!row.deleted"
                      :checked="selectedObj.pk === row.key"
                      @update:checked="(v: boolean) => setPk(v ? row.key : null)"
                    />
                  </td>
                  <td class="sensitive-cell">
                    <NTooltip v-if="rowDE(row) && deSensitive(rowDE(row))" trigger="hover">
                      <template #trigger><span>🔒</span></template>
                      敏感性来自数据元声明；编辑请走数据元层与「权限与遮蔽」页
                    </NTooltip>
                    <span v-else>—</span>
                  </td>
                  <td>
                    <button v-if="row.deleted" class="row-op" title="恢复" @click="restoreProp(row)">↺</button>
                    <button v-else class="row-op row-op-del" title="删除属性" @click="askDeleteProp(row)">✕</button>
                  </td>
                </tr>
              </tbody>
            </table>
            <NButton size="small" dashed block :disabled="!canWrite" @click="addProp">+ 新增属性</NButton>

            <!-- D2 继承回显（只读） -->
            <div v-if="inheritInfo" class="inherit">
              <div class="inherit-title">数据元继承（只读，不可在此编辑）— {{ inheritInfo.de }}</div>
              <div class="inherit-line">format<span class="mono">{{ inheritInfo.format || '—' }}</span></div>
              <div class="inherit-line">mask<span class="mono">{{ inheritInfo.mask || '—' }}</span></div>
              <div class="inherit-line">clean_rule<span class="mono">{{ inheritInfo.cleanRule || '—' }}</span></div>
              <div class="inherit-line">sensitive<span class="mono">{{ inheritInfo.sensitive ? 'true 🔒' : 'false' }}</span></div>
            </div>
          </template>
          <EmptyState v-else type="empty" title="尚未定义对象类型" desc="从左侧「+ 新建对象」开始建模" />
        </div>

        <!-- 右：对象关系图（F3，确定性布局 D6） -->
        <div class="pane pane-right">
          <RelationGraphPanel
            :nodes="graphNodes"
            :edges="graphEdges"
            @select="selectObject"
            @edit-edge="onGraphEditEdge"
            @create="openLinkCreate"
          />
        </div>
      </div>

      <!-- ── 底部操作区（F5 failed：错误条 + 保留输入）── -->
      <div class="footer">
        <NButton :disabled="!dirty || confirmSaving" @click="discardChanges">放弃更改</NButton>
        <NButton
          type="primary"
          :disabled="!dirty || !canWrite"
          :loading="confirmSaving"
          @click="askSave"
        >
          {{ canWrite ? '保存' : '🔒 需偏将及以上' }}
        </NButton>
      </div>
    </NSpin>

    <!-- F1 危险确认（结构性 / 关系性，理由必填；无 R3 假承诺） -->
    <SaveConfirmModal
      v-model:show="confirmOpen"
      v-model:reason="confirmReason"
      :structural="pendingDiff?.structural ?? []"
      :relational="pendingDiff?.relational ?? []"
      :semantic="pendingDiff?.semantic ?? []"
      :loading="confirmSaving"
      @confirm="onConfirmSave(confirmReason)"
    />

    <!-- E2 关系定义弹窗（name/title/端点/基数；R1 精神：已有关系 name 置灰） -->
    <LinkEditModal
      :show="linkModal.show"
      :mode="linkModal.mode"
      :initial="linkModalInitial"
      :link-names="linkModalNames"
      :object-options="objectOptions"
      @update:show="(v: boolean) => (linkModal.show = v)"
      @confirm="onLinkConfirm"
      @delete="onLinkDelete"
    />

    <!-- F6 并发冲突恢复（E1-4；服务端 version 比对属后端批次，此为预留分支） -->
    <NModal
      :show="conflictOpen"
      preset="card"
      title="保存冲突"
      class="f6-modal"
      :mask-closable="false"
      @update:show="(v: boolean) => (conflictOpen = v)"
    >
      <div class="f6-body">
        本体已被他人修改。请重新载入后再编辑；本地更改<b>不覆盖</b>服务端。
      </div>
      <template #footer>
        <div class="f6-footer">
          <NButton @click="conflictDiff">在新窗口查看差异</NButton>
          <NButton type="primary" @click="conflictReload">重新载入（丢弃本地更改）</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; gap: 10px; }

/* 顶部固定区 */
.topbar { display: flex; justify-content: space-between; align-items: center; }
.crumb { font-size: 13px; color: var(--sun-text-secondary); }
.crumb b { color: var(--sun-text-primary); }
.layer-tag { margin-left: 6px; }
.topbar-actions { display: flex; gap: 8px; }
.hint-bar {
  font-size: 12px; color: var(--sun-text-secondary);
  background: var(--sun-bg-card); border: 1px solid var(--sun-border);
  border-radius: 6px; padding: 6px 12px;
}
.link { color: var(--sun-info-text); }
.r2-bar {
  font-size: 12px; color: var(--sun-warn-text);
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  border-radius: 6px; padding: 6px 12px;
}
.e21-bar {
  font-size: 12px; color: var(--sun-warn-text);
  background: var(--sun-warn-bg); border: 1px solid var(--sun-warn-border);
  border-radius: 6px; padding: 6px 12px;
}
.de-dead {
  margin-left: 4px; cursor: help; font-size: 11px;
  color: var(--sun-warn-text); text-decoration: line-through;
}
.error-bar {
  font-size: 12px; color: var(--sun-error-text);
  background: var(--sun-error-bg);
  border: 1px solid var(--sun-error-border); border-radius: 6px; padding: 6px 12px;
  white-space: pre-wrap;
}

/* 三栏 */
.col3 {
  display: grid;
  grid-template-columns: 280px 1fr 360px;
  gap: 10px;
  align-items: stretch;
  min-height: 480px;
}
.pane {
  border: 1px solid var(--sun-border); border-radius: 6px;
  background: var(--sun-bg-card);
  padding: 10px; display: flex; flex-direction: column; gap: 8px;
  min-width: 0;
}
.pane-title { font-size: 13px; font-weight: 600; color: var(--sun-text-primary); display: flex; align-items: center; gap: 6px; }
.lock { cursor: help; font-size: 12px; }

/* 左栏对象列表 */
.obj-list { flex: 1; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.obj-item {
  border: 1px solid var(--sun-border); border-radius: 6px;
  padding: 6px 8px; cursor: pointer; display: flex; flex-direction: column; gap: 3px;
}
.obj-item:hover { border-color: var(--sun-border-active); }
.obj-item.active { border-color: var(--sun-gold); background: var(--sun-bg-card-hover); }
.obj-item.deleted { opacity: 0.55; }
.obj-item.deleted .obj-name { text-decoration: line-through; }
.obj-line1 { display: flex; align-items: center; gap: 5px; }
.obj-name { font-size: 12.5px; font-weight: 600; }
.obj-line2 { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--sun-text-tertiary); }
.obj-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.obj-del {
  border: none; background: transparent; color: var(--sun-text-tertiary);
  cursor: pointer; font-size: 11px; padding: 0 2px;
}
.obj-del:hover { color: var(--sun-error-text); }
.create-form {
  border: 1px dashed var(--sun-border-active); border-radius: 6px;
  padding: 8px; display: flex; flex-direction: column; gap: 6px;
}
.cf-row { display: flex; flex-direction: column; gap: 2px; }
.cf-row label { font-size: 11px; color: var(--sun-text-secondary); }
.cf-error { font-size: 11px; color: var(--sun-error-text); }
.cf-actions { display: flex; justify-content: flex-end; gap: 6px; }

/* 中栏 */
.meta-toggle { font-size: 12px; color: var(--sun-text-secondary); cursor: pointer; user-select: none; }
.meta-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
  border: 1px dashed var(--sun-border); border-radius: 6px; padding: 8px;
}
.meta-cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.meta-cell label { font-size: 11px; color: var(--sun-text-secondary); }

.ptable { width: 100%; border-collapse: collapse; font-size: 12px; }
.ptable th, .ptable td { border-bottom: 1px solid var(--sun-border); padding: 4px 6px; text-align: left; vertical-align: middle; }
.ptable th { font-size: 11px; color: var(--sun-text-tertiary); font-weight: 500; white-space: nowrap; }
.ptable tr.row-new td { background: var(--sun-ok-bg); }
.ptable tr.dead td { color: var(--sun-text-tertiary); }
.strike { text-decoration: line-through; }
.type-sel { min-width: 110px; }
.de-sel { min-width: 150px; }
.warn-mark { margin-left: 4px; cursor: help; color: var(--sun-warn-text); }
.sensitive-cell { text-align: center; }
.row-op {
  border: none; background: transparent; cursor: pointer;
  color: var(--sun-text-tertiary); font-size: 12px; padding: 2px 4px;
}
.row-op-del:hover { color: var(--sun-error-text); }

.inherit {
  border: 1px solid var(--sun-border); border-radius: 6px;
  background: var(--sun-bg-card); padding: 8px 10px; font-size: 11.5px;
  display: flex; flex-direction: column; gap: 3px;
}
.inherit-title { font-weight: 600; color: var(--sun-text-secondary); }
.inherit-line { display: flex; gap: 8px; color: var(--sun-text-secondary); }
.inherit-line .mono { color: var(--sun-text-primary); }

/* 右栏 */
.pane-right { min-height: 480px; }

/* 底部 */
.footer { display: flex; justify-content: flex-end; gap: 10px; }

/* F6 冲突弹窗 */
.f6-body { font-size: 13px; line-height: 1.8; min-width: 380px; }
.f6-footer { display: flex; justify-content: flex-end; gap: 10px; }

.mini { font-size: 11px; color: var(--sun-text-tertiary); }
.mono { font-family: var(--sun-font-mono); }
</style>

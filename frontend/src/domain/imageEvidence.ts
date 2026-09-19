// P8 图像证据 URI 工具：image_uri 固定 evidence/<material_id>/<filename>
//（server/app/routers/vlm.py _make_image_loader 同口径，另兼容裸 material_id）。
// 书证材料卡 findings 分组与 VlmView 预览共用反解逻辑，两处不得各写一套。
export function materialIdFromUri(uri: string): string | null {
  const u = String(uri ?? '').trim().replace(/^\/+/, '')
  if (!u) return null
  const parts = u.split('/')
  if (parts.length >= 3 && parts[0] === 'evidence' && parts[1]) return parts[1]
  if (parts.length === 1 && parts[0]) return parts[0]
  return null
}

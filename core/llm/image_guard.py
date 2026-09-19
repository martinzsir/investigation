"""
core/llm/image_guard.py
P8 图像通道闸门（纯字节、无 Pillow 依赖）。

两道确定性检查（出网前强制执行）：
  1. EXIF/元数据剥离 strip_metadata()：
     - JPEG：删除 APP1–APP15（EXIF/XMP/Photoshop IRB 等）与 COM 注释段，
       保留 SOI/APP0(JFIF)/图像数据/EOI；
     - PNG：删除 eXIf/tEXt/iTXt/zTXt chunk，保留其余 chunk（含 CRC 重算不需要，
       被删 chunk 整体移除，保留 chunk 不动）；
     - 其他格式：不剥离、fail-closed 拒绝出网（不认识不猜测）。
  2. 敏感区域 block assert_sendable()：
     纯字节不做人脸识别——按图像入卷时声明的 content_class 判定（最小作用域，
     默认仅脱敏数据：发票/收据/普通文书）；face/id_scan 类在默认 block 策略下
     拒传，未声明 content_class 同样 fail-closed。mask 策略位保留给日后引入
     Pillow 的批次（当前收到 mask 按 block 处理，不假装能打码）。

失败抛 ImageBlocked（LLMBlockedError 子类），调用方落 llm_call_log 后不得发请求。
"""
from __future__ import annotations

import struct
from typing import Any

from core.access import LLMBlockedError

#: JPEG marker 前缀
_JPEG_MAGIC = b"\xff\xd8"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: JPEG 无载荷长度的 marker（RSTn/SOI/EOI），其后不跟 2 字节长度
_JPEG_NO_LENGTH = set(range(0xD0, 0xDA))  # RST0-7 + SOI(D8)，0xD9=EOI

#: 敏感 content_class → image_pii 策略键
_SENSITIVE_CLASS_POLICY = {
    "face": "face",
    "id_scan": "id_document",
}

#: 默认放行（脱敏数据）的 content_class
_SAFE_CLASSES = frozenset({"invoice", "receipt", "document", "other"})


class ImageBlocked(LLMBlockedError):
    """图像闸门拒绝：未剥离元数据 / 含敏感区域 / 未知格式（请求根本不发起）。"""


# ----------------------------------------------------------------------
# JPEG 元数据剥离
# ----------------------------------------------------------------------
def _strip_jpeg(data: bytes) -> bytes:
    out = bytearray(data[:2])  # SOI
    i = 2
    n = len(data)
    while i < n:
        if data[i] != 0xFF:
            # 熵编码数据：直接拷至末尾
            out.extend(data[i:])
            break
        # 跳过连续 0xFF 填充
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1
        if marker in _JPEG_NO_LENGTH:
            # RSTn/SOI/EOI：原样保留（EOI 后结束）
            out.append(0xFF)
            out.append(marker)
            if marker == 0xD9:
                break
            continue
        if i + 2 > n:
            # 截断的长度字段：保留剩余字节，交给解码器容错
            out.append(0xFF)
            out.append(marker)
            out.extend(data[i:])
            break
        seg_len = struct.unpack(">H", data[i:i + 2])[0]
        seg = data[i - 2:i + seg_len]  # marker + 长度 + 载荷
        # 删除 APPn（0xE0-0xEF）中除 APP0(JFIF) 外的全部 + COM(0xFE)
        if (0xE1 <= marker <= 0xEF) or marker == 0xFE:
            i += seg_len
            continue
        out.extend(seg)
        i += seg_len
    return bytes(out)


# ----------------------------------------------------------------------
# PNG 元数据剥离
# ----------------------------------------------------------------------
_PNG_DROP_CHUNKS = frozenset({b"eXIf", b"tEXt", b"iTXt", b"zTXt"})


def _strip_png(data: bytes) -> bytes:
    out = bytearray(data[:8])  # PNG signature
    i = 8
    n = len(data)
    while i + 8 <= n:
        length = struct.unpack(">I", data[i:i + 4])[0]
        ctype = data[i + 4:i + 8]
        total = 12 + length  # length(4)+type(4)+data+CRC(4)
        chunk = data[i:i + total]
        if ctype not in _PNG_DROP_CHUNKS:
            out.extend(chunk)
        i += total
        if ctype == b"IEND":
            # IEND 之后不应再有内容
            break
    return bytes(out)


def strip_metadata(data: bytes) -> bytes:
    """按格式纯字节剥离 EXIF/元数据块；未知格式抛 ImageBlocked（不猜测）。"""
    raw = bytes(data or b"")
    if raw.startswith(_JPEG_MAGIC):
        return _strip_jpeg(raw)
    if raw.startswith(_PNG_MAGIC):
        return _strip_png(raw)
    raise ImageBlocked(
        "仅支持 JPEG/PNG 图像的元数据剥离，收到未知格式（fail-closed：不剥离不出网）")


def has_exif(data: bytes) -> bool:
    """检测字节流是否仍含 EXIF/文本元数据块（剥离验证用）。"""
    raw = bytes(data or b"")
    if raw.startswith(_JPEG_MAGIC):
        i, n = 2, len(raw)
        while i < n:
            if raw[i] != 0xFF:
                return False
            while i < n and raw[i] == 0xFF:
                i += 1
            if i >= n:
                return False
            marker = raw[i]
            i += 1
            if marker in _JPEG_NO_LENGTH:
                if marker == 0xD9:
                    return False
                continue
            if i + 2 > n:
                return False
            seg_len = struct.unpack(">H", raw[i:i + 2])[0]
            # APP1 常为 EXIF（"Exif\x00\x00"）或 XMP；COM 为注释
            if marker == 0xE1 or marker == 0xFE:
                return True
            i += seg_len
        return False
    if raw.startswith(_PNG_MAGIC):
        i, n = 8, len(raw)
        while i + 8 <= n:
            length = struct.unpack(">I", raw[i:i + 4])[0]
            ctype = raw[i + 4:i + 8]
            if ctype in _PNG_DROP_CHUNKS:
                return True
            i += 12 + length
        return False
    return True  # 未知格式视为不可信


# ----------------------------------------------------------------------
# 敏感区域 block（按入卷分类，fail-closed）
# ----------------------------------------------------------------------
def assert_sendable(image_meta: dict[str, Any] | None,
                    image_pii: dict[str, Any] | None) -> None:
    """按 content_class 与 image_pii 策略判定能否送出；越界/未声明即拒。

    image_meta 至少含 content_class；image_pii 取 llm_policy.image_pii。
    当前不支持 mask（无 Pillow）：策略值=mask 按 block 处理。
    """
    meta = image_meta if isinstance(image_meta, dict) else {}
    policy = image_pii if isinstance(image_pii, dict) else {}
    cls = str(meta.get("content_class") or "").strip()
    if not cls:
        raise ImageBlocked(
            "图像未声明 content_class（fail-closed：最小作用域默认仅脱敏数据，"
            "请入卷时标注 invoice/receipt/document 等类别）")
    if cls in _SENSITIVE_CLASS_POLICY:
        action = str(policy.get(_SENSITIVE_CLASS_POLICY[cls]) or "block")
        if action in ("block", "mask"):
            why = "mask 策略当前无 Pillow 实现、按 block 处理" if action == "mask" \
                else f"策略 {_SENSITIVE_CLASS_POLICY[cls]}=block"
            raise ImageBlocked(
                f"content_class={cls} 含人脸/证件敏感区域（{why}），禁止出网")
        # allow（显式放行）才继续——这是需要案件包显式声明的例外
        return
    if cls not in _SAFE_CLASSES:
        raise ImageBlocked(
            f"content_class={cls} 不在脱敏类别白名单 {sorted(_SAFE_CLASSES)}")

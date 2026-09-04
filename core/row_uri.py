"""
core/row_uri.py — REQ-008 Content-addressable 溯源行 URI

统一行引用格式（可解析、可取回、跨版本稳定）：

    row_uri = dataset@version#partition/rowid

  - dataset   ：L2 源表名（如 "银行流水"，与 obj_*.source_rows 串前缀同源）
  - version   ：ontology build_id（meta_ontology_state.build_id，uuid4 hex）
  - partition ：数据分区（全量 bootstrap = BOOTSTRAP_PARTITION；
                增量 = IngestPartition.partition_id；seed/review 触发 = "incremental"）
  - rowid     ：内容地址 = sha256(dataset + 有序 col=value)[:16 hex]

两张 append-only 归档表（随语义层构建写入，重建不丢）：

  row_archive(obj_type, dataset, rowid, partition, content, first_seen_build,
              PRIMARY KEY(dataset, rowid))
      内容去重归档：行内容不变则 rowid 不变——旧 URI 永远 resolve 回旧内容（AC5）。
      （不同对象类型消费同一源表时 SELECT 列不同，canonical 不同 → rowid 不同。）
  row_build_index(build_id, obj_type, dataset, rowid, partition,
              PRIMARY KEY(build_id, obj_type, dataset, rowid))
      版本 → 行集合索引；增量物化时按 (obj_type, dataset) 粒度继承上版本
      未重算对象的行（镜像"未变对象类型不重写"的增量语义；同表多对象消费时
      不会误伤），保证每个 build_id 都有完整行集。

注意：归档含原始行明文（敏感列在内），resolve 仅供审计/explain 等授权出口调用，
不经过 OntologyReadGateway 的属性遮蔽——调用方必须自行保证访问合法性。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

BOOTSTRAP_PARTITION = "bootstrap"
_INCREMENTAL_PARTITION = "incremental"

_ROWID_LEN = 16          # sha256 截 16 hex（64 bit，10 万行碰撞概率 ~1e-7）
_SEP = ("@", "#", "/")


class MalformedUriError(ValueError):
    """row_uri 形态非法（缺段/空段/分隔符冲突/rowid 非 hex）。"""


class RowNotFoundError(KeyError):
    """URI 形态合法，但归档中无此行或该行不属于指定版本（跨版本不混）。"""


@dataclass(frozen=True)
class RowRef:
    """解析后的行引用四段。"""
    dataset: str
    version: str
    partition: str
    rowid: str

    def to_uri(self) -> str:
        return make_row_uri(self.dataset, self.version,
                            self.partition, self.rowid)


# ----------------------------------------------------------------------
# URI 构造 / 解析（纯函数，无 IO）
# ----------------------------------------------------------------------
def _check_hex(name: str, value: str) -> None:
    if not value or any(c not in "0123456789abcdef" for c in value.lower()):
        raise MalformedUriError(f"{name} 必须是非空 hex 字符串，得到 {value!r}")


def make_row_uri(dataset: str, version: str, partition: str, rowid: str) -> str:
    """拼接 row_uri；各段非空且不得含 @ # / 分隔符，version/rowid 必须 hex。"""
    for name, val in (("dataset", dataset), ("partition", partition)):
        if not val or not isinstance(val, str):
            raise MalformedUriError(f"{name} 必须是非空字符串，得到 {val!r}")
        bad = next((c for c in _SEP if c in val), None)
        if bad:
            raise MalformedUriError(f"{name} {val!r} 含保留分隔符 {bad!r}")
    _check_hex("version", version)
    _check_hex("rowid", rowid)
    return f"{dataset}@{version}#{partition}/{rowid}"


def parse_row_uri(uri: str) -> RowRef:
    """解析 row_uri → RowRef；任何缺段/空段/非法形态抛 MalformedUriError。"""
    if not isinstance(uri, str):
        raise MalformedUriError(f"uri 必须是字符串，得到 {type(uri).__name__}")
    if uri.count("@") != 1 or uri.count("#") != 1:
        raise MalformedUriError(
            f"uri 必须形如 dataset@version#partition/rowid，得到 {uri!r}")
    dataset, rest = uri.split("@", 1)
    version, tail = rest.split("#", 1)
    if "/" not in tail:
        raise MalformedUriError(f"uri 缺少 partition/rowid 段，得到 {uri!r}")
    partition, rowid = tail.rsplit("/", 1)
    if not dataset or not version or not partition or not rowid:
        raise MalformedUriError(f"uri 各段均不得为空，得到 {uri!r}")
    if any(c in dataset for c in _SEP) or any(c in partition for c in ("@", "#")):
        raise MalformedUriError(f"dataset/partition 含保留分隔符：{uri!r}")
    _check_hex("version", version)
    _check_hex("rowid", rowid)
    return RowRef(dataset=dataset, version=version.lower(),
                  partition=partition, rowid=rowid.lower())


# ----------------------------------------------------------------------
# 内容地址
# ----------------------------------------------------------------------
def row_id_for(dataset: str, cols: list[str], row: tuple | list) -> str:
    """行内容地址：md5(dataset + 有序 col=value)[:16]。

    md5（非密码学场景，仅内容寻址）与 SQL 归档路径同一算法（DuckDB md5 向量化）；
    cols 顺序由 binding SELECT 确定（同源同序即稳定，REQ-008 AC4）；
    None 归一为空串（与 source_rows 人类可读串同口径）。
    """
    parts = [str(dataset)]
    for c, v in zip(cols, row):
        parts.append(f"{c}={'' if v is None else v}")
    return hashlib.md5("\x1f".join(parts).encode("utf-8")).hexdigest()[:_ROWID_LEN]


def uri_for_row(dataset: str, cols: list[str], row: tuple | list,
                *, version: str, partition: str = BOOTSTRAP_PARTITION) -> str:
    """便捷：由行内容直接生成 row_uri。"""
    return make_row_uri(dataset, version, partition,
                        row_id_for(dataset, cols, row))


# ----------------------------------------------------------------------
# 归档表
# ----------------------------------------------------------------------
def _ensure_archive(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS row_archive (
            obj_type VARCHAR, dataset VARCHAR, rowid VARCHAR, partition VARCHAR,
            content VARCHAR, first_seen_build VARCHAR,
            PRIMARY KEY(dataset, rowid))""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS row_build_index (
            build_id VARCHAR, obj_type VARCHAR, dataset VARCHAR, rowid VARCHAR,
            partition VARCHAR,
            PRIMARY KEY(build_id, obj_type, dataset, rowid))""")


def snapshot_source_rows(conn, build_id: str, batches, *,
                         partition: str = BOOTSTRAP_PARTITION,
                         prev_build_id: str | None = None) -> dict:
    """构建后归档源行快照（REQ-008 AC2/AC5）。

    batches: [(obj_type, dataset, cols, rows), ...]
              —— 与 obj_*.source_rows 同源的行集（_compute_object_rows 输出；
              obj_type = 物化的对象类型名，同一 dataset 可被多个对象类型消费）。
    partition: 本批行的分区标识（bootstrap / partition_id / incremental）。
    prev_build_id: 上一版本 build_id——增量时按 (obj_type, dataset) 粒度继承
                   未重算对象的版本索引，使新版本行集 = 重算对象新行
                   + 未变对象旧行（同表多对象消费互不误伤）。
    """
    _ensure_archive(conn)

    replaced_keys: set[str] = set()   # "obj_type|dataset" 本批重算键
    total_rows = 0
    try:
        import pandas as pd
    except Exception:
        pd = None

    if pd is not None:
        # ---- 主路径：行集注册为关系，md5/to_json 在 DuckDB 内向量化 ----
        for obj_type, dataset, cols, rows in batches:
            if not dataset or not rows:
                continue
            replaced_keys.add(f"{obj_type}|{dataset}")
            total_rows += len(rows)
            conn.register("_snap_rows", pd.DataFrame(rows, columns=cols))
            # canonical = dataset \x1f col=v \x1f col=v ...（与 row_id_for 同构）
            canon = " || chr(31) || ".join(
                f"'{c}' || '=' || coalesce(CAST(\"{c}\" AS VARCHAR), '')"
                for c in cols)
            struct = "to_json(struct_pack(" + ", ".join(
                f'"{c}" := "{c}"' for c in cols) + "))"
            # 哈希/JSON 向量化计算；两表各插（视图路径比物化临时表快）
            conn.execute(
                f"""INSERT OR IGNORE INTO row_archive
                    SELECT DISTINCT ?, ?,
                           substr(md5(? || chr(31) || {canon}), 1, 16),
                           ?, {struct}, ?
                    FROM _snap_rows""",
                [obj_type, dataset, dataset, partition, build_id])
            conn.execute(
                f"""INSERT OR IGNORE INTO row_build_index
                    SELECT DISTINCT ?, ?, ?,
                           substr(md5(? || chr(31) || {canon}), 1, 16), ?
                    FROM _snap_rows""",
                [build_id, obj_type, dataset, dataset, partition])
            conn.unregister("_snap_rows")
    else:
        # ---- 回落路径：pandas 不可用时 Python 侧逐行（功能优先）----
        arch: list[tuple] = []
        index: list[tuple] = []
        for obj_type, dataset, cols, rows in batches:
            if not dataset:
                continue
            replaced_keys.add(f"{obj_type}|{dataset}")
            total_rows += len(rows)
            seen: set[str] = set()
            for r in rows:
                rid = row_id_for(dataset, cols, r)
                if rid in seen:
                    continue
                seen.add(rid)
                arch.append((obj_type, dataset, rid, partition,
                             json.dumps(dict(zip(cols, r)),
                                        ensure_ascii=False, default=str),
                             build_id))
                index.append((build_id, obj_type, dataset, rid, partition))
        conn.executemany(
            "INSERT OR IGNORE INTO row_archive VALUES (?,?,?,?,?,?)", arch)
        conn.executemany(
            "INSERT OR IGNORE INTO row_build_index VALUES (?,?,?,?,?)", index)

    stats = {"archived_rows": 0, "indexed_rows": total_rows,
             "inherited_rows": 0}
    stats["archived_rows"] = conn.execute(
        "SELECT COUNT(*) FROM row_archive WHERE first_seen_build=?",
        [build_id]).fetchone()[0]
    own_rows = conn.execute(
        "SELECT COUNT(*) FROM row_build_index WHERE build_id=?",
        [build_id]).fetchone()[0]

    # 继承上版本未重算 (obj_type,dataset) 的行索引（INSERT OR IGNORE 不覆盖本批）
    if prev_build_id:
        if replaced_keys:
            placeholders = ", ".join("?" for _ in replaced_keys)
            where = f"AND (obj_type || '|' || dataset) NOT IN ({placeholders})"
            params = [build_id, prev_build_id, *sorted(replaced_keys)]
        else:
            where, params = "", [build_id, prev_build_id]
        conn.execute(
            f"""INSERT OR IGNORE INTO row_build_index
                SELECT ?, obj_type, dataset, rowid, partition FROM row_build_index
                WHERE build_id = ? {where}""", params)
        total = conn.execute(
            "SELECT COUNT(*) FROM row_build_index WHERE build_id=?",
            [build_id]).fetchone()[0]
        stats["inherited_rows"] = total - own_rows
    return stats


# ----------------------------------------------------------------------
# 取回
# ----------------------------------------------------------------------
def resolve_row_uri(conn, uri: str) -> dict:
    """按 URI 取回原始行内容。

    返回 {"dataset", "partition", "build_id", "rowid", "data": {列: 值}}。
    - 形态非法 → MalformedUriError；
    - version 不存在于版本时钟 / 该行不属于该版本 / 归档无内容 → RowNotFoundError
      （AC5：跨版本不混——旧版本 URI 只能取回旧版本行集里的内容）。
    """
    ref = parse_row_uri(uri)
    _ensure_archive(conn)

    # version 必须是已知 build_id（跨 pack 全局唯一 uuid）
    try:
        ver = conn.execute(
            "SELECT 1 FROM meta_ontology_state WHERE build_id=?",
            [ref.version]).fetchone()
    except Exception:
        ver = None   # 版本时钟表尚未建立（从未 build）
    if not ver:
        raise RowNotFoundError(
            f"version {ref.version} 不在版本时钟中（uri={uri}）")

    # 版本归属：该行必须在该 build 的行集索引里
    member = conn.execute(
        "SELECT partition FROM row_build_index "
        "WHERE build_id=? AND dataset=? AND rowid=?",
        [ref.version, ref.dataset, ref.rowid]).fetchone()
    if not member:
        raise RowNotFoundError(
            f"行 {ref.dataset}/{ref.rowid} 不属于版本 {ref.version}"
            f"（跨版本不混，REQ-008 AC5；uri={uri}）")

    row = conn.execute(
        "SELECT content, partition FROM row_archive WHERE dataset=? AND rowid=?",
        [ref.dataset, ref.rowid]).fetchone()
    if not row:
        raise RowNotFoundError(
            f"归档中无 {ref.dataset}/{ref.rowid} 的行内容（uri={uri}）")
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError as e:
        raise RowNotFoundError(f"归档内容损坏：{e}（uri={uri}）")
    return {"dataset": ref.dataset, "partition": member[0],
            "build_id": ref.version, "rowid": ref.rowid, "data": data}

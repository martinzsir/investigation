"""
core/action_executor.py
Ontology Action 层：受控写回的唯一入口（Palantir Action 裁剪版）。

两条写路径：
  A. 即时执行 execute()：四步强制（角色/参数/状态机/副作用），run_all 与
     clue_transition 走此路径，行为保持不变；
  B. 两阶段提交（REQ-012）：submit() 只登记 action_request（proposed，不执行）
     → approve() 人审（approved）→ dispatch() 本地提交并入 outbox
     （dispatching→pending_receipt）→ 外部回执业务号后 confirmed（REQ-013）。
     本地提交与外部确认分离；幂等键相同的 submit 返回同一 action_id。

状态机：proposed → approved → dispatching → pending_receipt → confirmed
                                      └→ failed / dead_letter（REQ-014）
未 approve 就 dispatch → NotApprovedError；approve 必须具名 operator。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

# 被拒绝的操作者名（防止 agent 冒名顶替正兵）——与 MCP 层同一名单
FORBIDDEN_OPERATORS = {"system", "ai", "assistant", "model", "bot", "auto", "llm"}


def _is_placeholder_operator(operator) -> bool:
    """匿名/占位 operator 判定：空串、纯空白、system/ai 等。"""
    op = str(operator or "").strip().lower()
    return (not op) or (op in FORBIDDEN_OPERATORS)

_ACTION_REQUEST_DDL = """
CREATE TABLE IF NOT EXISTS action_request (
    action_id VARCHAR PRIMARY KEY,
    idempotency_key VARCHAR,
    action_name VARCHAR NOT NULL,
    clue_id VARCHAR,
    target_status VARCHAR,
    params_json VARCHAR,
    status VARCHAR NOT NULL,
    submitted_by VARCHAR NOT NULL,
    submitted_at VARCHAR NOT NULL,
    approved_by VARCHAR,
    approved_at VARCHAR,
    dispatched_at VARCHAR,
    attempts INTEGER DEFAULT 0,
    last_error VARCHAR,
    external_id VARCHAR,
    writeback_status VARCHAR
)
"""


class NotApprovedError(RuntimeError):
    """action_request 未经过 approve() 就 dispatch（REQ-012 AC2）。"""


class ActionRequestNotFound(KeyError):
    """action_id 在 action_request 中不存在。"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


#: subject_type → (runtime link 表, 主体端点列)
IMAGE_SUBJECT_LINKS = {
    "person": ("lnk_image_for_person", "person_id"),
    "org": ("lnk_image_for_org", "org_id"),
    "bid_project": ("lnk_image_for_project", "project_id"),
}


def _ensure_image_evidence_columns(conn) -> None:
    """旧 obj_image_evidence 幂等补列（information_schema 探测 → ALTER ADD）。

    ensure_runtime_tables 只 CREATE IF NOT EXISTS，不补列；文案三列
    （title/detail/severity）加入前已建表的库在此零脚本迁移。
    """
    existing = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='obj_image_evidence'").fetchall()}
    for col in ("title", "detail", "severity"):
        if col not in existing:
            conn.execute(
                f'ALTER TABLE obj_image_evidence ADD COLUMN "{col}" VARCHAR')


def persist_image_evidence(conn, pack: str, *, image_uri: str, model: str,
                           prompt_version: str, model_score,
                           title: str = "", detail: str = "",
                           severity: str = "",
                           verifier: str, verify_conclusion: str,
                           subject_type: str, subject_id: str,
                           draft_id: str = "", clue_id: str = "") -> dict:
    """写 obj_image_evidence + 按 subject_type 写 lnk_image_for_*（唯一实现）。

    ActionExecutor（DuckDB 路径）与 Web Worker（人验端点）共用本函数，
    保证两条路径产物同构。runtime 表 DDL 由 objects/links 类型声明经
    ensure_runtime_tables 生成；模型分独立字段（不参与确定性计分）。
    title/detail/severity 为 AI 草案 finding 文案（candidate 同构，可空）。

    幂等：draft_id 非空且已有该 draft 的证据 → 原样返回既有记录，不重复写。
    subject_type/subject_id 非法 → ValueError（fail-closed）。
    """
    import uuid as _uuid

    from core.ontology import ensure_runtime_tables, json_dumps

    subject_type = str(subject_type or "").strip()
    subject_id = str(subject_id or "").strip()
    link_spec = IMAGE_SUBJECT_LINKS.get(subject_type)
    if link_spec is None:
        raise ValueError(
            f"subject_type={subject_type!r} 非法（允许 person/org/bid_project）")
    if not subject_id:
        raise ValueError("subject_id 不得为空")

    ensure_runtime_tables(conn, pack)
    _ensure_image_evidence_columns(conn)

    # 幂等：同草案只落一次证据
    draft_id = str(draft_id or "").strip()
    if draft_id:
        row = conn.execute(
            "SELECT image_evidence_id FROM obj_image_evidence "
            "WHERE draft_id=? LIMIT 1", [draft_id]).fetchone()
        if row is not None:
            return {"image_evidence_id": row[0], "persisted": True,
                    "duplicate": True}

    score_val: float | None
    if model_score is None or str(model_score).strip() == "":
        score_val = None
    else:
        try:
            score_val = float(model_score)
        except (TypeError, ValueError):
            raise ValueError(f"model_score={model_score!r} 非数值")

    image_evidence_id = f"imgev_{_uuid.uuid4().hex[:12]}"
    created_at = _now()
    source_rows = json_dumps([
        "action:verify_image",
        f"clue:{clue_id}",
        f"draft:{draft_id}",
    ])
    conn.execute(
        """INSERT INTO obj_image_evidence
           (image_evidence_id, image_uri, model, prompt_version, model_score,
            title, detail, severity,
            verifier, verify_conclusion, subject_type, draft_id, created_at,
            source_rows)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [image_evidence_id, str(image_uri or ""), str(model or ""),
         str(prompt_version or ""), score_val,
         str(title or ""), str(detail or ""), str(severity or ""),
         str(verifier or ""),
         str(verify_conclusion or ""), subject_type, draft_id, created_at,
         source_rows],
    )
    link_table, subject_col = link_spec
    conn.execute(
        f'INSERT INTO "{link_table}" (image_evidence_id, "{subject_col}") '
        "VALUES (?, ?)",
        [image_evidence_id, subject_id],
    )
    return {"image_evidence_id": image_evidence_id, "persisted": True,
            "created_at": created_at, "link_table": link_table}


class ActionExecutor:
    def __init__(self, store=None, pack: str = "default", access=None,
                 health=None, sink=None):
        self.store = store
        self.pack = pack
        # D-M3-1：Web 处置快速通道写后端（state.sqlite 适配）。
        # sink 为鸭子类型窄协议：.conn（sqlite 连接，action_request 与
        # clue_disposal_status SQL 方言兼容直接复用）、.case_id、
        # .ontology_version、create_decision()。None 时全部走既有 DuckDB
        # 路径（CLI/MCP/run_all 零感知）。core 不 import sink 实现（server→core）。
        self.sink = sink
        # REQ-009：access=None → system 旁路（既有调用行为不变）
        from core.access import system_context
        self.access = access if access is not None else system_context()
        # REQ-G-010：运行诊断（None → NullRunHealth）
        from core.run_health import get_health
        self.health = get_health(health)
        if store is not None and hasattr(store, "conn"):
            store.conn.execute(_ACTION_REQUEST_DDL)
        # REQ-G-025：持久审计链惰性持有（有 store 才构造，见 _chain()）
        self._audit_chain = None

    def _chain(self):
        """REQ-G-025：处置动作必须落持久哈希链。

        有 store 时惰性构造 AuditChain（复用同一 conn 与 health），execute() 与
        两阶段 dispatch() 共用的 _apply() 统一取链——一处接线两路径同时生效。
        无 store 的内存/兼容路径回落 None：set_status/set_filed 仅写内存
        audit_log（registry 层向后兼容语义不变），不抛错。

        sink 路径（Web state.sqlite，D-M3-1）：AuditChain 走 sqlite 后端，
        版本锚点显式传入（state 库无 meta_ontology_state）。
        """
        if self._audit_chain is None and self.sink is not None:
            from core.audit import AuditChain
            self._audit_chain = AuditChain(
                self.sink.conn, case_id=self.sink.case_id,
                health=self.health, backend="sqlite",
                ontology_version=getattr(self.sink, "ontology_version", None))
        if self._audit_chain is None and self.store is not None \
                and hasattr(self.store, "conn"):
            from core.audit import AuditChain
            self._audit_chain = AuditChain(self.store.conn, health=self.health)
        return self._audit_chain

    # ---- 声明查询 ----
    def action_for_status(self, target_status: str):
        """按目标状态反查 Action 声明（target_status 唯一）。"""
        from core.ontology_loader import load_pack
        for a in load_pack(self.pack).actions.values():
            if a.target_status == target_status:
                return a
        raise KeyError(f"无 Action 对应目标状态：{target_status}")

    # ---- 即时执行（路径 A，既有行为）----
    def execute(self, action_name: str, clue, operator: str,
                params: dict | None = None) -> dict:
        from core.ontology import get_action
        spec = get_action(action_name, self.pack)
        params = params or {}
        self._validate(spec, clue, operator, params)
        return self._apply(spec, clue, operator, params)

    # ---- 校验（路径 A/B 共用；只检查不落任何写）----
    def _validate(self, spec, clue, operator: str, params: dict) -> None:
        # 1) 角色校验
        if spec.requires_role == "human" and _is_placeholder_operator(operator):
            raise ValueError(
                f"动作 {spec.name}（{spec.target_status}）仅具名正兵可执行，"
                f"拒绝 operator={operator!r}（禁止 {sorted(FORBIDDEN_OPERATORS)} 等占位名）")

        # 2) 必填参数校验（file 须 legal_basis —— AC4 红线保持）
        for p in spec.parameters:
            if p.required and not str(params.get(p.name, "")).strip():
                raise ValueError(
                    f"动作 {spec.name} 缺少必填参数：{p.name}"
                    + (f"（{p.description}）" if p.description else ""))

        # 3) 状态机校验（allowed_from 由 states.json 迁移表反向派生，单一事实来源）
        from core.registry import ClueStatusMachine
        ClueStatusMachine.validate(clue.status, spec.target_status, pack=self.pack)

        # 3.1) D1：only_from 显式收紧（如固证仅可从查证中发起，待查不可直接固证）
        if spec.only_from and clue.status not in spec.only_from:
            raise ValueError(
                f"动作 {spec.name}（{spec.target_status}）仅允许从 "
                f"{list(spec.only_from)} 发起，当前状态 {clue.status!r} "
                f"被 only_from 约束拒绝")

        # 3.5) 权限上下文校验（REQ-009）：human 终态需 human 角色；
        #      非 system 会话不得以他人名义执行（operator 与 access 主体一致）
        if not self.access.is_system:
            if not self.access.can_transition(clue.status, spec.target_status):
                raise PermissionError(
                    f"AccessContext(role={self.access.role}) 无权迁移到"
                    f" {spec.target_status!r}（human 专属终态）——operator={self.access.operator}")
            if operator and str(operator).strip() != self.access.operator:
                raise PermissionError(
                    f"会话主体 {self.access.operator!r} 不得以 {operator!r} 名义执行写动作"
                    f"（写操作主体一致性，REQ-009）")

    # ---- 应用变更（校验通过后的本地提交；路径 A/B 共用）----
    def _apply(self, spec, clue, operator: str, params: dict) -> dict:
        from core.ontology import json_dumps
        # 4) 应用状态变更（写线索审计链）
        # REQ-G-025：audit_chain 必须传入——此前漏传导致处置动作（含「已立案」
        # 受控终态）只写内存 audit_log，持久哈希链零记录且自检空链假阳性。
        chain = self._chain()
        if spec.name == "file":
            clue.set_filed(operator, params["legal_basis"], audit_chain=chain)
        else:
            note = params.get("note") or params.get("reason") or ""
            clue.set_status(spec.target_status, operator=operator, note=note,
                            audit_chain=chain)

        # 5) 声明式副作用
        applied: list[dict] = []
        if "create_decision" in spec.side_effects:
            applied.append(self._create_decision(spec, clue, operator, params, json_dumps))
        if "create_image_evidence" in spec.side_effects:
            applied.append(self._create_image_evidence(spec, clue, operator, params))
        return {"action": spec.name, "clue_id": clue.clue_id,
                "status": clue.status, "side_effects": applied}

    # ------------------------------------------------------------------
    # 两阶段提交（路径 B，REQ-012）
    # ------------------------------------------------------------------
    def submit(self, action_name: str, clue, operator: str,
               params: dict | None = None, *, idempotency_key: str | None = None) -> str:
        """阶段一：登记 action_request（status=proposed），**不执行任何变更**。

        幂等：相同 idempotency_key 返回已存在的 action_id（AC5）。
        """
        from core.ontology import get_action
        spec = get_action(action_name, self.pack)
        params = params or {}
        self._validate(spec, clue, operator, params)   # 校验前置，不合格不登记

        key = idempotency_key or self._default_key(spec.name, clue.clue_id, params)
        existing = self._find_by_key(key)
        if existing:
            return existing  # AC5：幂等键相同 → 同一 action_id，不重复创建

        action_id = f"act_{uuid.uuid4().hex[:12]}"
        self.store.conn.execute(
            """INSERT INTO action_request
               (action_id, idempotency_key, action_name, clue_id, target_status,
                params_json, status, submitted_by, submitted_at, attempts)
               VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?, 0)""",
            [action_id, key, spec.name, clue.clue_id, spec.target_status,
             json.dumps(params, ensure_ascii=False, default=str), operator, _now()])
        self._publish("action.submitted",
                      {"action_id": action_id, "action_name": spec.name,
                       "clue_id": clue.clue_id, "target_status": spec.target_status},
                      actor=operator)
        return action_id

    def approve(self, action_id: str, operator: str) -> dict:
        """阶段二人审：proposed → approved。operator 必须具名（AC3）。"""
        if _is_placeholder_operator(operator):
            raise ValueError(
                f"approve 必须具名正兵，拒绝 operator={operator!r}（REQ-012 AC3）")
        req = self._get_request(action_id)
        if req["status"] != "proposed":
            raise RuntimeError(
                f"action {action_id} 当前状态 {req['status']!r}，仅 proposed 可 approve")
        self.store.conn.execute(
            "UPDATE action_request SET status='approved', approved_by=?, approved_at=? "
            "WHERE action_id=?",
            [operator, _now(), action_id])
        self._publish("action.approved",
                      {"action_id": action_id, "approved_by": operator},
                      actor=operator)
        return {"action_id": action_id, "status": "approved", "approved_by": operator}

    def dispatch(self, action_id: str, clue) -> dict:
        """阶段三：approved → 本地提交（状态变更+副作用）→ 入 outbox 待外部回写。

        未 approve 即 dispatch → NotApprovedError（AC2）。
        clue 由调用方提供（看板装载的同一线索对象）。
        """
        req = self._get_request(action_id)
        if req["status"] != "approved":
            raise NotApprovedError(
                f"action {action_id} 状态为 {req['status']!r}，须先 approve 才能 dispatch"
                f"（REQ-012 AC2）")
        from core.ontology import get_action
        spec = get_action(req["action_name"], self.pack)
        params = json.loads(req["params_json"] or "{}")
        operator = req["approved_by"] or req["submitted_by"]

        # 本地提交
        result = self._apply(spec, clue, operator, params)
        self.store.conn.execute(
            "UPDATE action_request SET status='dispatching', dispatched_at=?, "
            "attempts=attempts+1 WHERE action_id=?",
            [_now(), action_id])

        # 入 outbox（REQ-013）；outbox 未就位时停留 dispatching
        try:
            from core.outbox import Outbox
            outbox_id = Outbox(self.store.conn).enqueue(
                action_id=action_id, action_name=spec.name, clue_id=clue.clue_id,
                payload={"target_status": spec.target_status, "params": params,
                         "operator": operator},
                created_by=operator)
            self.store.conn.execute(
                "UPDATE action_request SET status='pending_receipt' WHERE action_id=?",
                [action_id])
            result["outbox_id"] = outbox_id
            result["status"] = "pending_receipt"
        except ImportError as e:
            # REQ-G-020 fail-closed：派发失败不得用"dispatching 进行中"掩盖。
            # 置 dispatch_failed 并 critical 留痕，交人工重试，而非假装在途。
            self.store.conn.execute(
                "UPDATE action_request SET status='dispatch_failed', last_error=? "
                "WHERE action_id=?", [f"outbox 不可用：{str(e)[:300]}", action_id])
            result["status"] = "dispatch_failed"
            self.health.record(
                "dispatch_failed", "critical",
                source=f"action:{action_id}",
                reason=f"派发失败（outbox 不可用）：{str(e)[:120]}",
                action_name=spec.name, clue_id=clue.clue_id)
        self._publish("action.dispatched",
                      {"action_id": action_id, "action_name": spec.name,
                       "clue_id": clue.clue_id},
                      actor=operator)
        result["action_id"] = action_id
        return result

    def mark_confirmed(self, action_id: str, external_id: str) -> None:
        """外部回执业务号 → confirmed（REQ-013 AC3）。"""
        self._get_request(action_id)
        self.store.conn.execute(
            "UPDATE action_request SET status='confirmed', external_id=?, "
            "writeback_status='confirmed' WHERE action_id=?",
            [external_id, action_id])
        self._publish("writeback.confirmed",
                      {"action_id": action_id, "external_id": external_id},
                      actor="system")

    def request_status(self, action_id: str) -> dict:
        return self._get_request(action_id)

    # ---- 内部 ----
    @staticmethod
    def _default_key(action_name: str, clue_id: str, params: dict) -> str:
        raw = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
        h = hashlib.sha256(f"{action_name}|{clue_id}|{raw}".encode("utf-8")).hexdigest()[:16]
        return f"{action_name}:{clue_id}:{h}"

    def _find_by_key(self, key: str) -> str | None:
        if self.store is None:
            return None
        row = self.store.conn.execute(
            "SELECT action_id FROM action_request WHERE idempotency_key=?", [key]
        ).fetchone()
        return row[0] if row else None

    def _get_request(self, action_id: str) -> dict:
        if self.store is None:
            raise ActionRequestNotFound(f"无 store，无法查询 {action_id}")
        row = self.store.conn.execute(
            "SELECT action_id, idempotency_key, action_name, clue_id, target_status, "
            "params_json, status, submitted_by, submitted_at, approved_by, approved_at, "
            "dispatched_at, attempts, last_error, external_id, writeback_status "
            "FROM action_request WHERE action_id=?", [action_id]).fetchone()
        if not row:
            raise ActionRequestNotFound(f"action_request 不存在：{action_id}")
        cols = ["action_id", "idempotency_key", "action_name", "clue_id",
                "target_status", "params_json", "status", "submitted_by",
                "submitted_at", "approved_by", "approved_at", "dispatched_at",
                "attempts", "last_error", "external_id", "writeback_status"]
        return dict(zip(cols, row))

    def _publish(self, event_type: str, payload: dict, *, actor: str) -> None:
        """事件总线可选接线：无 store/总线异常不阻断主流程（审计另有 audit_chain）。

        REQ-G-004：发布落盘失败不再静默吞——落运行诊断（warning），但不阻断主流程。

        sink 路径（Web state.sqlite）：state 库不承载事件总线（事件总线随
        版本文件语义层），平台审计由 server 侧 meta platform_audit 记录，
        此处直接返回不建表。
        """
        if self.sink is not None:
            return
        if self.store is None or not hasattr(self.store, "conn"):
            return
        try:
            from core.event_bus import EventBus
            EventBus(self.store.conn).publish(event_type, payload, actor=actor)
        except Exception as e:
            self.health.record(
                "event_publish_failed", "warning",
                source="action_executor:_publish",
                reason=f"事件 {event_type} 发布失败：{str(e)[:120]}",
                event_type=event_type)


    # ---- 副作用：创建决策对象（runtime 对象，DDL 由 objects/links 类型声明生成）----
    def _create_decision(self, spec, clue, operator, params, json_dumps) -> dict:
        # D-M3-1：sink 路径（Web state.sqlite）决策落 state.review_decision，
        # 不写版本文件 obj_decision/lnk_decision_for 语义表（决策不随 BUILD 丢失）。
        if self.sink is not None:
            return self.sink.create_decision(spec, clue, operator, params)
        if self.store is None or not hasattr(self.store, "conn"):
            return {"decision_id": None, "persisted": False,
                    "note": "无 store，决策对象未持久化"}
        import time
        from core.ontology import ensure_runtime_tables
        conn = self.store.conn
        # obj_decision / lnk_decision_for 的列定义来自 ontology 类型层（不再硬编码）
        ensure_runtime_tables(conn, self.pack)
        decision_id = f"decision_{int(time.time() * 1000)}"
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        src = json_dumps([
            f"action:{spec.name}", f"clue:{clue.clue_id}",
            f"legal_basis:{params.get('legal_basis', '')}",
        ])
        conn.execute(
            "INSERT INTO obj_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [decision_id, spec.target_status, clue.clue_id,
             params.get("legal_basis", ""), operator, params.get("note", ""),
             created_at, params.get("metadata"), src],
        )
        conn.execute(
            "INSERT INTO lnk_decision_for VALUES (?, ?)",
            [decision_id, clue.clue_id],
        )
        return {"decision_id": decision_id, "persisted": True,
                "created_at": created_at}

    # ---- 副作用：创建图像证据（P8 人验；MCP/CLI 轨道 DuckDB 写法）----
    def _create_image_evidence(self, spec, clue, operator, params) -> dict:
        # 双轨设计：Web 人验不经 ActionExecutor（vlm 路由直接写 state.sqlite
        # image_evidence，经 graph/报告读出）；MCP/CLI 轨道无 sink，写内核
        # DuckDB runtime 表（obj_image_evidence/lnk_image_for_*）。
        # worker sink 路径未接线，误入即 fail-closed 报错（不静默丢失）。
        if self.sink is not None:
            raise RuntimeError(
                "create_image_evidence 不支持 state.sqlite sink 路径"
                "（Web 人验请走 vlm 人验端点；MCP/CLI 走无 sink 轨道）")
        if self.store is None or not hasattr(self.store, "conn"):
            return {"image_evidence_id": None, "persisted": False,
                    "note": "无 store，图像证据未持久化"}
        return persist_image_evidence(
            self.store.conn, self.pack,
            image_uri=params.get("image_uri", ""),
            model=params.get("model", ""),
            prompt_version=params.get("prompt_version", ""),
            model_score=params.get("model_score"),
            verifier=operator,
            verify_conclusion=params.get("verify_conclusion", ""),
            subject_type=params.get("subject_type", ""),
            subject_id=params.get("subject_id", ""),
            draft_id=params.get("draft_id", ""),
            clue_id=clue.clue_id,
        )

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


class ActionExecutor:
    def __init__(self, store=None, pack: str = "default", access=None):
        self.store = store
        self.pack = pack
        # REQ-009：access=None → system 旁路（既有调用行为不变）
        from core.access import system_context
        self.access = access if access is not None else system_context()
        if store is not None and hasattr(store, "conn"):
            store.conn.execute(_ACTION_REQUEST_DDL)

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

        # 3) 状态机校验（allowed_from 由 _TRANSITIONS 反向派生，单一事实来源）
        from core.registry import ClueStatusMachine
        ClueStatusMachine.validate(clue.status, spec.target_status)

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
        if spec.name == "file":
            clue.set_filed(operator, params["legal_basis"])
        else:
            note = params.get("note") or params.get("reason") or ""
            clue.set_status(spec.target_status, operator=operator, note=note)

        # 5) 声明式副作用
        applied: list[dict] = []
        if "create_decision" in spec.side_effects:
            applied.append(self._create_decision(spec, clue, operator, params, json_dumps))
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
        except ImportError:
            result["status"] = "dispatching"
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
        """事件总线可选接线：无 store/总线异常不阻断主流程（审计另有 audit_chain）。"""
        if self.store is None or not hasattr(self.store, "conn"):
            return
        try:
            from core.event_bus import EventBus
            EventBus(self.store.conn).publish(event_type, payload, actor=actor)
        except Exception:
            pass


    # ---- 副作用：创建决策对象（runtime 对象，DDL 由 objects/links 类型声明生成）----
    def _create_decision(self, spec, clue, operator, params, json_dumps) -> dict:
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
            "INSERT INTO obj_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [decision_id, spec.target_status, clue.clue_id,
             params.get("legal_basis", ""), operator, params.get("note", ""),
             created_at, src],
        )
        conn.execute(
            "INSERT INTO lnk_decision_for VALUES (?, ?)",
            [decision_id, clue.clue_id],
        )
        return {"decision_id": decision_id, "persisted": True,
                "created_at": created_at}

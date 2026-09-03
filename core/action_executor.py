"""
core/action_executor.py
Ontology Action 层：受控写回的唯一入口（Palantir Action 裁剪版）。

所有状态变更/对象反写必须经 ActionExecutor.execute()，四步强制：
  1. 角色校验：requires_role=human 的动作拒绝 system/ai 等占位操作者——红线 1；
  2. 参数校验：required 参数缺失硬失败（如 exclude 须 reason、file 须 legal_basis）；
  3. 状态机校验：ClueStatusMachine 判定迁移合法性（allowed_from 反向派生自状态机）；
  4. 副作用：set_clue_status（写线索审计链）/ create_decision（创建 obj_decision
     决策对象 + lnk_decision_for 链接，"已立案"从状态字符串升格为一等对象）。

与 Function 的边界：Function 只读不改对象；Action 可写，且每个可写动作都在
ontology/<pack>/actions.json 声明，未声明的写操作不存在执行路径。
"""
from __future__ import annotations

# 被拒绝的操作者名（防止 agent 冒名顶替正兵）——与 MCP 层同一名单
FORBIDDEN_OPERATORS = {"system", "ai", "assistant", "model", "bot", "auto", "llm"}


class ActionExecutor:
    def __init__(self, store=None, pack: str = "default", access=None):
        self.store = store
        self.pack = pack
        # REQ-009：access=None → system 旁路（既有调用行为不变）
        from core.access import system_context
        self.access = access if access is not None else system_context()

    # ---- 声明查询 ----
    def action_for_status(self, target_status: str):
        """按目标状态反查 Action 声明（target_status 唯一）。"""
        from core.ontology_loader import load_pack
        for a in load_pack(self.pack).actions.values():
            if a.target_status == target_status:
                return a
        raise KeyError(f"无 Action 对应目标状态：{target_status}")

    # ---- 执行 ----
    def execute(self, action_name: str, clue, operator: str,
                params: dict | None = None) -> dict:
        from core.ontology import get_action, json_dumps
        from core.registry import ClueStatusMachine

        spec = get_action(action_name, self.pack)
        params = params or {}

        # 1) 角色校验
        if spec.requires_role == "human" and (
                not operator or str(operator).strip().lower() in FORBIDDEN_OPERATORS):
            raise ValueError(
                f"动作 {action_name}（{spec.target_status}）仅具名正兵可执行，"
                f"拒绝 operator={operator!r}（禁止 {sorted(FORBIDDEN_OPERATORS)} 等占位名）")

        # 2) 必填参数校验
        for p in spec.parameters:
            if p.required and not str(params.get(p.name, "")).strip():
                raise ValueError(
                    f"动作 {action_name} 缺少必填参数：{p.name}"
                    + (f"（{p.description}）" if p.description else ""))

        # 3) 状态机校验（allowed_from 由 _TRANSITIONS 反向派生，单一事实来源）
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

        # 4) 应用状态变更（写线索审计链）
        if action_name == "file":
            clue.set_filed(operator, params["legal_basis"])
        else:
            note = params.get("note") or params.get("reason") or ""
            clue.set_status(spec.target_status, operator=operator, note=note)

        # 5) 声明式副作用
        applied: list[dict] = []
        if "create_decision" in spec.side_effects:
            applied.append(self._create_decision(spec, clue, operator, params, json_dumps))
        return {"action": action_name, "clue_id": clue.clue_id,
                "status": clue.status, "side_effects": applied}

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

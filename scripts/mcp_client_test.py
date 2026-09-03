"""
scripts/mcp_client_test.py
MCP server 端到端冒烟测试：手写 JSON-RPC 客户端，走完整生命周期。

覆盖：
  1. initialize 握手（协议版本、capabilities、serverInfo）
  2. tools/list（9 个工具，schema 必填项齐全）
  3. tools/call —— 只读工具（含 function_list/function_invoke/rule_list）
  4. 红线：clue_transition 用 operator="system" 必须被拒
  5. 红线：置"已立案"不带 legal_basis 必须被拒
  6. 红线：operator 具名 + 带 legal_basis → 允许
  7. 协议：未知方法 → -32601；坏 JSON → -32700
  8. 通知（notifications/initialized）不回响应

用法：
    python -m scripts.mcp_client_test
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = [], []


def check(label: str, ok: bool, note: str = "") -> None:
    (PASS if ok else FAIL).append(label)
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  —— {note}" if note else ""))


class MCPClient:
    """极简 stdio MCP 客户端。"""

    def __init__(self, env: dict | None = None):
        import os
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "scripts.mcp_server"],
            cwd=str(ROOT),
            env={**os.environ, **env} if env else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._id = 0

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server closed stdout")
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def raw(self, text: str) -> dict:
        """发送原始文本（用于测试坏 JSON）。"""
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def payload(resp: dict) -> dict:
    """从 tools/call 响应里取出工具返回的 dict。出错时把 error 也带出来，便于定位。"""
    if "error" in resp:
        return {"ok": False, "error": resp["error"].get("message", ""),
                "code": resp["error"].get("code")}
    txt = resp["result"]["content"][0]["text"]
    return json.loads(txt)


def main() -> int:
    print("=== 孙武侦查官 MCP Server 端到端测试 ===\n")
    c = MCPClient()

    try:
        # 0. 重置环境
        # DuckDB 是处置状态的真值源且跨会话持久，上一轮测试留下的状态
        # （如"已立案"）会污染本轮。必须先用管线重建，保证测试可重复。
        print("[0] 重置环境（跑管线重建 DuckDB 与产物）")
        d = payload(c.request("tools/call", {"name": "run_pipeline", "arguments": {
            "confirm": True, "auto_review": True}}))
        check("管线重建成功", d.get("ok") is True, str(d.get("error"))[:80]
              or f"returncode={d.get('returncode')}")

        # 1. 握手
        print("\n[1] initialize 握手")
        r = c.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-client-test", "version": "1.0.0"},
        })
        res = r.get("result", {})
        check("返回 protocolVersion", res.get("protocolVersion") == "2025-06-18",
              str(res.get("protocolVersion")))
        check("声明 tools capability", "tools" in res.get("capabilities", {}))
        check("返回 serverInfo", res.get("serverInfo", {}).get("name") == "sunzi-investigation")
        check("携带 instructions（红线提示）", bool(res.get("instructions")))

        # 2. 通知不回响应
        print("\n[2] notifications/initialized（不应回响应）")
        c.notify("notifications/initialized")
        # 下一个请求能正常返回，即证明通知没打乱流
        r2 = c.request("ping")
        check("通知后流仍正常", "result" in r2)

        # 3. tools/list
        print("\n[3] tools/list")
        r = c.request("tools/list")
        tools = r["result"]["tools"]
        names = [t["name"] for t in tools]
        check(f"返回 9 个工具（{len(tools)}）", len(tools) == 9, str(names))
        for t in tools:
            sch = t.get("inputSchema", {})
            check(f"  {t['name']} 有 description + inputSchema",
                  bool(t.get("description")) and sch.get("type") == "object")

        # schema 必填项校验（红线相关的三个）
        by_name = {t["name"]: t for t in tools}
        tr = by_name["clue_transition"]["inputSchema"]
        check("clue_transition 必填 operator",
              "operator" in tr.get("required", []) and "clue_id" in tr.get("required", []))
        rp = by_name["run_pipeline"]["inputSchema"]
        check("run_pipeline 必填 confirm", "confirm" in rp.get("required", []))

        # 4. 只读工具
        print("\n[4] 只读工具")
        d = payload(c.request("tools/call", {"name": "cross_jian", "arguments": {}}))
        check("cross_jian 返回 cross_level", bool(d.get("cross_level")),
              str(d.get("cross_level")))
        check("cross_jian 挂定性_policy", "定性_policy" in d)

        d = payload(c.request("tools/call", {"name": "scan_anomaly",
                                             "arguments": {"scope": "all"}}))
        check(f"scan_anomaly 返回候选（{d.get('count')} 条）", d.get("count", 0) >= 1)
        check("scan_anomaly 不返回明细（只给 count）",
              all("source_rows" not in f for f in d.get("findings", [])))

        d = payload(c.request("tools/call", {"name": "function_list", "arguments": {}}))
        fnames = [f["name"] for f in d.get("functions", [])]
        check(f"function_list 返回 10 个 Function（{len(fnames)}）", len(fnames) == 10, str(fnames))
        check("function_list 全部标注 readonly",
              all(f.get("readonly") for f in d.get("functions", [])))
        check("function_list 含新增内间/对端诊断（tipoff_cross_reference, call_pair_coverage）",
              "tipoff_cross_reference" in fnames and "call_pair_coverage" in fnames)

        d = payload(c.request("tools/call", {"name": "function_invoke",
                                             "arguments": {"name": "quarter_end_integer_deposits"}}))
        check("function_invoke 返回只读计算结果",
              d.get("ok") and d.get("readonly") is True and len(d.get("rows", [])) >= 1,
              str(d.get("error", d))[:80])
        d = payload(c.request("tools/call", {"name": "function_invoke",
                                             "arguments": {"name": "不存在的函数"}}))
        check("function_invoke 未知名报错（不崩）", d.get("ok") is False)

        # ---- 自然语言规则手册（rule_list）----
        d = payload(c.request("tools/call", {"name": "rule_list", "arguments": {}}))
        rids = [r["id"] for r in d.get("rules", [])]
        check(f"rule_list 返回 6 条自然语言规则（{len(rids)}）",
              d.get("count") == 6 and d.get("readonly") is True, str(rids))
        check("rule_list 规则携带判据原文与函数挂钩",
              all(len(r.get("rule_text", "")) >= 30 and r.get("function")
                  for r in d.get("rules", [])))
        d = payload(c.request("tools/call", {"name": "rule_list",
                                             "arguments": {"stage": "xu_shi"}}))
        check("rule_list 支持 stage 过滤（xu_shi=5 条）", d.get("count") == 5,
              str(d.get("count")))

        # ---- SQL Function 参数注入（规则阈值可调）----
        d_off = payload(c.request("tools/call", {"name": "function_invoke",
            "arguments": {"name": "quarter_end_integer_deposits",
                          "params": {"quarter_end_window_days": 0}}}))
        d_tight = payload(c.request("tools/call", {"name": "function_invoke",
            "arguments": {"name": "quarter_end_integer_deposits",
                          "params": {"quarter_end_window_days": 1}}}))
        n_off = len(d_off.get("rows", []))
        n_tight = len(d_tight.get("rows", []))
        check(f"function_invoke 参数注入生效（窗口0天={n_off}桶 ≥ 窗口1天={n_tight}桶）",
              d_off.get("ok") and n_off > n_tight, f"{n_off} vs {n_tight}")
        d_bad = payload(c.request("tools/call", {"name": "function_invoke",
            "arguments": {"name": "quarter_end_integer_deposits",
                          "params": {"cash_summary_tokens": "自由文本' OR 1=1"}}}))
        check("function_invoke 非 enum 字符串参数被拒（防注入）", d_bad.get("ok") is False)

        d = payload(c.request("tools/call", {"name": "clue_list", "arguments": {}}))
        check(f"clue_list 返回线索（{d.get('count')} 条）", d.get("count", 0) >= 1)
        check("clue_list 默认不含 source_rows",
              all("source_rows" not in cl for cl in d.get("clues", [])))
        # 挑一条「非终态」线索做迁移测试（排在第一的往往已被 run_all 立案）
        check("clue_list 含 status 字段（便于按状态过滤）",
              all("status" in cl for cl in d.get("clues", [])))
        pending = [cl for cl in d["clues"] if cl["status"] not in ("已立案", "已排除")]
        first_id = pending[0]["clue_id"] if pending else d["clues"][0]["clue_id"]
        filed_id = next((cl["clue_id"] for cl in d["clues"] if cl["status"] == "已立案"), None)
        print(f"      测试用线索：待迁移={first_id} 已立案={filed_id}")

        d = payload(c.request("tools/call", {"name": "graph_overpass", "arguments": {}}))
        check("graph_overpass 返回 paths", "paths" in d)
        check("graph_overpass 标注是否降级", "degraded" in d,
              f"degraded={d.get('degraded')} engine={d.get('engine')}")

        # 5. 红线校验
        print("\n[5] 红线校验（工具层强制）")
        d = payload(c.request("tools/call", {"name": "clue_transition", "arguments": {
            "clue_id": first_id, "to_status": "查证中", "operator": "system"}}))
        check("operator=system 被拒", d.get("ok") is False, str(d.get("error"))[:60])

        d = payload(c.request("tools/call", {"name": "clue_transition", "arguments": {
            "clue_id": first_id, "to_status": "已立案", "operator": "王检察官"}}))
        check("立案无 legal_basis 被拒", d.get("ok") is False, str(d.get("error"))[:60])

        d = payload(c.request("tools/call", {"name": "clue_transition", "arguments": {
            "clue_id": first_id, "to_status": "已立案",
            "operator": "王检察官", "legal_basis": "杭检立〔2026〕XX号"}}))
        check("具名+法定依据 → 允许立案", d.get("ok") is True, str(d.get("error"))[:60])
        if d.get("ok"):
            tail = d.get("audit_tail") or []
            check("审计链留痕（含操作人与依据）",
                  any("王检察官" in json.dumps(a, ensure_ascii=False) for a in tail),
                  f"{len(tail)} 条")

        # 重复立案必须被拒（终态不可改）
        if filed_id:
            d = payload(c.request("tools/call", {"name": "clue_transition", "arguments": {
                "clue_id": filed_id, "to_status": "已立案",
                "operator": "王检察官", "legal_basis": "杭检立〔2026〕YY号"}}))
            check("重复立案被拒（终态不可改）", d.get("ok") is False, str(d.get("error"))[:60])
        else:
            # 上一步刚立案的线索，再立一次同样应被拒
            d = payload(c.request("tools/call", {"name": "clue_transition", "arguments": {
                "clue_id": first_id, "to_status": "已立案",
                "operator": "李检察官", "legal_basis": "杭检立〔2026〕ZZ号"}}))
            check("重复立案被拒（终态不可改）", d.get("ok") is False, str(d.get("error"))[:60])

        # 6. run_pipeline 受控
        print("\n[6] run_pipeline 受控")
        d = payload(c.request("tools/call", {"name": "run_pipeline", "arguments": {}}))
        check("未 confirm 被拒（不误跑全链路）", d.get("ok") is False, str(d.get("error"))[:60])

        # 7. 协议错误码
        print("\n[7] 协议错误码")
        r = c.request("no/such/method")
        check("未知方法 → -32601",
              r.get("error", {}).get("code") == -32601, str(r.get("error"))[:60])

        r = c.raw("{bad json")
        check("坏 JSON → -32700", r.get("error", {}).get("code") == -32700)

        r = c.request("tools/call", {"name": "not_a_tool", "arguments": {}})
        check("未知工具 → -32601", r.get("error", {}).get("code") == -32601)

        # 8. 权限接入（REQ-009/010/011）：低权限会话（正兵）独立 server
        print("\n[8] 权限接入（低权限会话，REQ-011）")
        low = MCPClient(env={"SUNZI_OPERATOR": "实习侦查员", "SUNZI_ROLE": "正兵",
                             "SUNZI_CLEARANCE": "1", "SUNZI_PURPOSE": "推演测试"})
        try:
            low.request("initialize", {"protocolVersion": "2025-06-18",
                                       "capabilities": {},
                                       "clientInfo": {"name": "low-client", "version": "0"}})
            low.notify("notifications/initialized")
            r = low.request("tools/list")
            names = [t["name"] for t in r["result"]["tools"]]
            check("AC4 工具清单无自由 run_sql 入口",
                  not any("sql" in n for n in names), str(names))
            d = payload(low.request("tools/call", {"name": "clue_list", "arguments": {}}))
            check("AC1 低权限 clue_list 只返回授权范围（无内间线索）",
                  d.get("count", 0) >= 0 and "access_note" in d
                  and all("内间" not in (cl.get("jian_types") or [])
                          for cl in d.get("clues", [])),
                  str(d.get("access_note"))[:60])
            d = payload(low.request("tools/call", {"name": "function_invoke",
                "arguments": {"name": "co_located_pairs"}}))
            check("AC1 低权限调偏将级 Function（同框）被对象策略拒",
                  d.get("ok") is False and "无权" in str(d.get("error")),
                  str(d.get("error"))[:60])
            d = payload(low.request("tools/call", {"name": "clue_transition",
                "arguments": {"clue_id": first_id, "to_status": "查证中",
                              "operator": "王检察官"}}))
            check("AC5-adj 会话主体与 operator 不一致被拒（REQ-009）",
                  d.get("ok") is False, str(d.get("error"))[:60])
            d = payload(low.request("tools/call", {"name": "clue_transition",
                "arguments": {"clue_id": first_id, "to_status": "已立案",
                              "operator": "实习侦查员", "legal_basis": "测试"}}))
            check("AC5-adj 正兵无权立案（human 专属终态）",
                  d.get("ok") is False, str(d.get("error"))[:60])
        finally:
            low.close()

    finally:
        c.close()

    print(f"\n{'=' * 56}")
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ MCP Server 端到端全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

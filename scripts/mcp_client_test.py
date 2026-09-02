"""
scripts/mcp_client_test.py
MCP server 端到端冒烟测试：手写 JSON-RPC 客户端，走完整生命周期。

覆盖：
  1. initialize 握手（协议版本、capabilities、serverInfo）
  2. tools/list（6 个工具，schema 必填项齐全）
  3. tools/call —— 4 个只读工具
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

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "scripts.mcp_server"],
            cwd=str(ROOT),
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
        check(f"返回 6 个工具（{len(tools)}）", len(tools) == 6, str(names))
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

"""
core/review.py
人工确认工作台（任务 3）：把实体对齐产出的 needs_review 候选推给正兵，一键合并/拒绝。

问题背景：
  entity_resolution / entity.py 的对齐算法是"建议性"的：
    - 模糊相似（泰和建材 ≈ 泰和建材公司）→ needs_review
    - 前缀包含（宏业建设 ⊂ 宏业建设第一项目部）→ needs_review
  这类候选 AI 严禁自动合并（错并会让两个无关人的资金链相连，后果远重于漏并），
  必须由具名正兵逐条确认。

设计原则（红线）：
  - 每个候选：正兵可选 accept（合并）/ reject（拒绝，保留为独立实体）/ defer（稍后再看）
  - 每个决策都留审计链（operator + timestamp + reason + evidence snapshot）
  - accept 后才写入正式 mapping，下游 SQL/Cypher 才看到合并结果
  - 工作台可导出为 JSON（供 HTML 操作台渲染）或直接 CLI 交互

典型用法：
    from core.review import ReviewQueue, ReviewDecision
    q = ReviewQueue.from_resolvers(person_resolver, org_resolver)
    q.accept(candidate_id, operator="王检察官", reason="工商内档确认同主体")
    q.reject(candidate_id, operator="王检察官", reason="系两家独立公司")
    q.to_json("output/review_queue.json")
    ReviewQueue.load("output/review_queue.json").run_cli()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------
# 决策枚举
# ----------------------------------------------------------------------
class Decision:
    PENDING = "待确认"
    ACCEPTED = "已合并"     # 正兵确认：是同实体，合并
    REJECTED = "已拒绝"     # 正兵确认：不是同实体，保留独立
    DEFERRED = "暂缓"       # 待更多证据，暂不决定


@dataclass
class ReviewDecision:
    """单条候选的确认决策 + 审计链。"""
    candidate_id: str
    entity_type: str          # "person" / "org"
    canonical: str            # 建议的标准名
    variants: List[str]       # 待合并的变体
    reason: str               # 算法给出的候选依据
    evidence: dict = field(default_factory=dict)   # 共同属性/溯源行（供正兵判断）
    status: str = Decision.PENDING
    operator: str = ""
    note: str = ""
    timestamp: str = field(default_factory=lambda: _now())

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# 确认队列
# ----------------------------------------------------------------------
class ReviewQueue:
    """
    人工确认工作台：统一管理所有 needs_review 候选。

    数据来源：
      - Person EntityResolver.review_candidates()
      - Org OrganizationResolver.review_candidates()
    统一成一个扁平队列，每条带 candidate_id，正兵逐个决策。
    """

    def __init__(self, decisions: Optional[List[ReviewDecision]] = None):
        self._items: Dict[str, ReviewDecision] = {}
        if decisions:
            for d in decisions:
                self._items[d.candidate_id] = d

    # ---- 构建 ----
    @classmethod
    def from_resolvers(cls, person_resolver=None, org_resolver=None) -> "ReviewQueue":
        """从两个 resolver 的 review_candidates 汇总生成队列。"""
        items: List[ReviewDecision] = []
        seq = [0]

        def nid(etype: str) -> str:
            seq[0] += 1
            return f"rev_{etype}_{seq[0]:04d}"

        if person_resolver is not None:
            for c in person_resolver.review_candidates():
                # EntityCluster（人名）无 merge_reason 字段 → getattr 兜底
                reason = getattr(c, "merge_reason", None) or "模糊相似候选(需正兵确认)"
                evidence = {"common_source_rows": c.evidence.common_source_rows}
                if hasattr(c.evidence, "common_phones"):
                    evidence["common_phones"] = c.evidence.common_phones
                items.append(ReviewDecision(
                    candidate_id=nid("person"), entity_type="person",
                    canonical=c.canonical_name, variants=list(c.variants),
                    reason=reason, evidence=evidence,
                ))

        if org_resolver is not None:
            for c in org_resolver.review_candidates():
                # OrgCluster 有 merge_reason；其他簇类型用兜底
                reason = getattr(c, "merge_reason", None) or "组织层级前缀包含候选(需正兵确认)"
                evidence = {}
                ev = c.evidence
                for key in ("common_credit_codes", "common_legal_reps",
                            "common_addresses", "common_source_rows"):
                    if hasattr(ev, key):
                        evidence[key] = getattr(ev, key)
                items.append(ReviewDecision(
                    candidate_id=nid("org"), entity_type="org",
                    canonical=c.canonical_name, variants=list(c.variants),
                    reason=reason, evidence=evidence,
                ))
        return cls(items)

    # ---- 查询 ----
    def pending(self) -> List[ReviewDecision]:
        return [d for d in self._items.values() if d.status == Decision.PENDING]

    def decided(self) -> List[ReviewDecision]:
        return [d for d in self._items.values() if d.status != Decision.PENDING]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items.values())

    def get(self, candidate_id: str) -> ReviewDecision:
        if candidate_id not in self._items:
            raise KeyError(f"未知候选 {candidate_id}，队列共 {len(self)} 条")
        return self._items[candidate_id]

    # ---- 决策动作（唯一入口，保证审计链）----
    def accept(self, candidate_id: str, operator: str, reason: str = "") -> ReviewDecision:
        """
        正兵确认合并：该候选的 variants → canonical 正式合入 mapping。
        要求 operator 具名（禁止 "system"/"AI"）。
        """
        if not operator or operator.lower() in ("system", "ai"):
            raise ValueError("accept 必须由具名正兵操作，operator 不得为 system/AI")
        d = self.get(candidate_id)
        d.status = Decision.ACCEPTED
        d.operator = operator
        d.note = reason or d.note
        d.timestamp = _now()
        return d

    def reject(self, candidate_id: str, operator: str, reason: str = "") -> ReviewDecision:
        """正兵确认：不是同实体，保留为独立实体（mapping 中不加这条）。"""
        if not operator:
            raise ValueError("reject 须填写 operator")
        if not reason:
            raise ValueError("reject 必须填写拒绝理由（reason），保证可追溯")
        d = self.get(candidate_id)
        d.status = Decision.REJECTED
        d.operator = operator
        d.note = reason
        d.timestamp = _now()
        return d

    def defer(self, candidate_id: str, operator: str = "", reason: str = "",
              note: str = "") -> ReviewDecision:
        """
        暂缓：暂不作决定，等待更多证据。
        兼容参数 note（与 reason 同义，方便 CLI/操作台统一传参）。
        """
        d = self.get(candidate_id)
        d.status = Decision.DEFERRED
        d.operator = operator
        d.note = note or reason or d.note
        d.timestamp = _now()
        return d

    # ---- 根据决策结果生成正式 mapping ----
    def accepted_mapping(self) -> Dict[str, str]:
        """
        只有 ACCEPTED 的候选才进入正式映射：
        每个 variant → canonical。
        REJECTED / PENDING 不进映射（保守：宁可不合并，也不错并）。
        """
        m: Dict[str, str] = {}
        for d in self._items.values():
            if d.status == Decision.ACCEPTED:
                for v in d.variants:
                    m[v] = d.canonical
        return m

    # ---- 持久化 ----
    def to_json(self, path: str) -> str:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "total": len(self),
            "pending": len(self.pending()),
            "decisions": [d.to_dict() for d in self._items.values()],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                              encoding="utf-8")
        return str(path)

    @classmethod
    def load(cls, path: str) -> "ReviewQueue":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = [ReviewDecision(**d) for d in data.get("decisions", [])]
        return cls(items)

    # ---- CLI 交互（供操作台 / 终端调用）----
    def run_cli(self, auto_operator: str = "正兵") -> None:
        """
        逐个候选交互式确认：
          [a] 合并  [r] 拒绝  [d] 暂缓  [q] 退出
        决策实时写回队列，可直接被 HTML 操作台复用同一份 JSON。
        """
        pending = self.pending()
        if not pending:
            print("✅ 无待确认候选，队列已清空。")
            return
        print(f"=== 人工确认工作台（共 {len(pending)} 条待确认）===")
        for d in pending:
            print(f"\n[{d.candidate_id}] {d.entity_type} | 建议标准名：{d.canonical}")
            print(f"    变体：{d.variants}")
            print(f"    依据：{d.reason}")
            if d.evidence:
                print(f"    证据：{d.evidence}")
            ans = input("  [a]合并 / [r]拒绝 / [d]暂缓 / [q]退出：").strip().lower()
            if ans == "q":
                break
            elif ans == "a":
                reason = input("    合并理由(可选)：").strip()
                self.accept(d.candidate_id, operator=auto_operator, reason=reason)
                print("    ✅ 已合并")
            elif ans == "r":
                reason = input("    拒绝理由(必填)：").strip()
                self.reject(d.candidate_id, operator=auto_operator, reason=reason)
                print("    ✅ 已拒绝")
            elif ans == "d":
                self.defer(d.candidate_id, operator=auto_operator)
                print("    ⏭ 暂缓")
            else:
                print("    未识别，跳过")

        print(f"\n--- 本轮决策汇总 ---")
        print(f"  已合并：{sum(1 for d in self if d.status == Decision.ACCEPTED)}")
        print(f"  已拒绝：{sum(1 for d in self if d.status == Decision.REJECTED)}")
        print(f"  暂缓：{sum(1 for d in self if d.status == Decision.DEFERRED)}")
        print(f"  仍待确认：{len(self.pending())}")

    def summary(self) -> dict:
        from collections import Counter
        c = Counter(d.status for d in self)
        return {
            "total": len(self),
            "pending": c.get(Decision.PENDING, 0),
            "accepted": c.get(Decision.ACCEPTED, 0),
            "rejected": c.get(Decision.REJECTED, 0),
            "deferred": c.get(Decision.DEFERRED, 0),
        }

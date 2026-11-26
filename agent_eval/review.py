"""零漏审：人工审核队列与签核闭环。

设计目标不是「减少人工量」，而是「凡不确定必有人确认，未确认不放行」：

- 不确定信号统一收口：skipped / need_review / undetermined / flaky。
  即使 P1 断言全过、case_status 判 passed，也必须进审核队列。
- 审核队列是门禁的一等输入：strict_review 默认开启，未签核或签核驳回
  一律拒绝放行，不区分 severity。
- 签核绑定内容指纹：输出 / 工具轨迹 / 未验断言任一变化，签核自动失效，
  必须用新报告重新确认；旧报告签核不能跨跑批复用。
- 全部动作可审计：队列 JSON 保留原始证据，签核记录 reviewer / ts / note。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

VALID_DECISIONS = ("approve", "reject")
UNVERIFIED_STATUSES = ("skipped", "need_review")


@dataclass(frozen=True)
class ReviewSignoff:
    case_id: str
    fingerprint: str
    decision: str
    reviewer: str
    ts: str = ""
    note: str = ""
    run_id: str = ""


@dataclass
class SignoffSet:
    items: list[ReviewSignoff] = field(default_factory=list)

    def status(self, case_id: str, fingerprint: str) -> str:
        """最新签核优先；指纹不匹配（内容已变）一律视为未审。"""
        for item in reversed(self.items):
            if item.case_id == case_id and item.fingerprint == fingerprint:
                return item.decision
        return "pending"

    def approved(self, case_id: str, fingerprint: str) -> bool:
        return self.status(case_id, fingerprint) == "approve"


def _all_runs(outcome) -> list:
    runs = getattr(outcome, "runs", None) or []
    if runs:
        return list(runs)
    last = getattr(outcome, "run", None)
    return [last] if last is not None else []


def _last_run(outcome):
    runs = _all_runs(outcome)
    return runs[-1] if runs else None


def review_reasons(outcome) -> list[str]:
    """收集一条用例的全部不确定信号。空列表 = 无需人工审核。"""
    reasons: list[str] = []
    for a in getattr(outcome, "assertions", []) or []:
        s = a.result_status()
        if s in UNVERIFIED_STATUSES:
            reasons.append(f"{a.kind}:{s}")
    if getattr(outcome, "flaky", False):
        reasons.append("flaky:复跑结果不一致")
    if getattr(outcome, "case_status", "") == "undetermined":
        reasons.append("undetermined:无有效断言")
    return reasons


def review_fingerprint(outcome) -> str:
    """审核内容指纹：输出 / 工具轨迹 / 未验断言任一变化即令签核失效。"""
    last = _last_run(outcome)
    payload = {
        "case_id": outcome.case.case_id,
        "query": outcome.case.query,
        "final_output": (getattr(last, "final_output", "") or "") if last else "",
        "tool_calls": [
            {"name": t.name, "args": t.args, "ok": t.ok,
             "result": str(getattr(t, "result", ""))[:500]}
            for r in _all_runs(outcome)
            for t in (getattr(r, "tool_calls", None) or [])
        ],
        "unverified": [
            {"kind": a.kind, "status": a.result_status(), "detail": a.detail}
            for a in (getattr(outcome, "assertions", []) or [])
            if a.result_status() in UNVERIFIED_STATUSES
        ],
        "flaky": bool(getattr(outcome, "flaky", False)),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_review_queue(report, signoffs: SignoffSet | None = None) -> list[dict]:
    """生成人工审核队列。每条保留完整证据与待填写的签核字段。"""
    items: list[dict] = []
    for o in report.outcomes:
        reasons = review_reasons(o)
        if not reasons:
            continue
        fp = review_fingerprint(o)
        status = signoffs.status(o.case.case_id, fp) if signoffs else "pending"
        last = _last_run(o)
        items.append({
            "case_id": o.case.case_id,
            "severity": o.case.severity,
            "dataset": o.case.dataset,
            "run_id": report.run_id,
            "fingerprint": fp,
            "status": status,
            "case_status": o.case_status,
            "reasons": reasons,
            "first_error": o.first_error.target if o.first_error else None,
            "attribution": o.attribution,
            "unverified": [
                {"kind": a.kind, "status": a.result_status(), "detail": a.detail}
                for a in o.assertions if a.result_status() in UNVERIFIED_STATUSES
            ],
            "actual": {
                "final_output": ((getattr(last, "final_output", "") or "")[:1000]
                                 if last else ""),
                "tool_calls": [
                    {"name": t.name, "args": t.args, "ok": t.ok,
                     "result": str(getattr(t, "result", ""))[:500]}
                    for r in _all_runs(o)
                    for t in (getattr(r, "tool_calls", None) or [])
                ],
            },
            "decision": "",
            "reviewer": "",
            "ts": "",
            "note": "",
        })
    return items


def export_review_queue(path: str | Path, report,
                        signoffs: SignoffSet | None = None) -> int:
    """导出待审队列 JSON；人工填写 decision / reviewer / note 后回读为签核。"""
    items = build_review_queue(report, signoffs)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(items)


def load_signoffs(path: str | Path) -> SignoffSet:
    """从审核队列 JSON 读取人工签核。未填 decision 的条目视为待审。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("审核队列文件应为数组")
    items: list[ReviewSignoff] = []
    for i, x in enumerate(raw):
        if not isinstance(x, dict):
            raise ValueError(f"审核队列第 {i} 条应为对象")
        decision = str(x.get("decision") or "").strip()
        if not decision:
            continue
        if decision not in VALID_DECISIONS:
            raise ValueError(f"审核队列第 {i} 条 decision 非法: {decision!r}"
                             f"（仅允许 {', '.join(VALID_DECISIONS)}）")
        case_id = str(x.get("case_id") or "").strip()
        fp = str(x.get("fingerprint") or "").strip()
        reviewer = str(x.get("reviewer") or "").strip()
        if not (case_id and fp and reviewer):
            raise ValueError(f"审核队列第 {i} 条签核信息不完整"
                             "（需 case_id / fingerprint / reviewer）")
        items.append(ReviewSignoff(
            case_id=case_id, fingerprint=fp, decision=decision,
            reviewer=reviewer, ts=str(x.get("ts") or ""),
            note=str(x.get("note") or ""), run_id=str(x.get("run_id") or ""),
        ))
    return SignoffSet(items)


def review_stats(items: list[dict]) -> dict:
    stats = {"total": len(items), "pending": 0, "approve": 0, "reject": 0}
    for x in items:
        s = x.get("status")
        if s in stats:
            stats[s] += 1
        else:
            stats["pending"] += 1
    return stats


__all__ = [
    "ReviewSignoff", "SignoffSet", "VALID_DECISIONS", "UNVERIFIED_STATUSES",
    "review_reasons", "review_fingerprint", "build_review_queue",
    "export_review_queue", "load_signoffs", "review_stats",
]

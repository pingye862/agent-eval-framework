"""离线自检：用「契约级假响应」跑通 18 条用例，不需要模型 key。

目的不是验证 Agent 答得对不对（那必须真跑），而是验证三件事：
  1. 评估集里的每条断言路径都存在于真实响应结构中
     （response.result.metric_query.rows / units / status）
  2. harness 的点路径下钻、null 断言、min_length 断言都能正常求值
  3. 门禁逻辑：全绿放行；blocker 篡改后必须拦截

假响应的形状完全照 platform/fde_product/agent.py:212 的 safe 字典构造，
数值取自测试集设计说明里的手算底账（独立于评估集文件，不是从断言反推）。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "agent_eval"))

from adapter import W03AgentAdapter            # noqa: E402
from evalset import load_evalset               # noqa: E402
from harness import BatchRunner, gate_decision  # noqa: E402

# query -> (agent 层 status, 引擎 result, answer 文本)
FIXTURES = {
    "2026 年 8 月梧桐书店扣退后收入是多少？": (
        "completed",
        {"status": "ok", "rows": [[13000]], "columns": ["net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "2026 年 8 月扣退后收入为 13000 分，即 130 元。",
    ),
    "2026 年 7 月梧桐书店月内大额会员数是多少？": (
        "completed",
        {"status": "ok", "rows": [[1]], "columns": ["monthly_large_buyer_count"],
         "units": {"monthly_large_buyer_count": "count"}},
        "2026 年 7 月月内大额会员数为 1 人（W1 恰好 10000 分，含等于）。",
    ),
    "梧桐书店扣退后收入是多少？": (
        "clarification",
        {"status": "clarify", "reason": "需要明确时间范围"},
        "请补充要查询的时间范围（例如 2026 年 8 月）。",
    ),
    "2026 年 6 月梧桐书店退回占比是多少？": (
        "completed",
        {"status": "ok", "rows": [[None]], "columns": ["return_ratio"],
         "units": {"return_ratio": "ratio"}},
        "2026 年 6 月无到账，零分母，退回占比未定义。",
    ),
    "2026 年 8 月梧桐书店月内大额会员数是多少？": (
        "completed",
        {"status": "ok", "rows": [[0]], "columns": ["monthly_large_buyer_count"],
         "units": {"monthly_large_buyer_count": "count"}},
        "2026 年 8 月月内大额会员数为 0 人。",
    ),
    "2026 年 7 月梧桐书店的到账总额和退回金额分别是多少？": (
        "completed",
        {"status": "ok", "rows": [[23000, 13000]],
         "columns": ["gross_receipts_cents", "returned_cents"],
         "units": {"gross_receipts_cents": "cents", "returned_cents": "cents"}},
        "到账总额 23000 分，退回金额 13000 分。",
    ),
    "2026 年 7 月梧桐书店收款单数是多少？": (
        "completed",
        {"status": "ok", "rows": [[3]], "columns": ["receipt_count"],
         "units": {"receipt_count": "count"}},
        "2026 年 7 月收款单数 3 笔。",
    ),
    "2026 年 8 月梧桐书店收款单数是多少？": (
        "completed",
        {"status": "ok", "rows": [[2]], "columns": ["receipt_count"],
         "units": {"receipt_count": "count"}},
        "2026 年 8 月收款单数 2 笔（取消单不计）。",
    ),
    "2026 年 7 月 31 日到 8 月 2 日梧桐书店扣退后收入是多少？": (
        "completed",
        {"status": "ok", "rows": [[8000]], "columns": ["net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "该区间扣退后收入 8000 分。",
    ),
    "2026 年 8 月梧桐书店净收入多少钱？": (
        "completed",
        {"status": "ok", "rows": [[13000]], "columns": ["net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "净收入 13000 分。",
    ),
    "2026 年 8 月小程序渠道的扣退后收入是多少？": (
        "completed",
        {"status": "ok", "rows": [[5000]], "columns": ["net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "小程序渠道扣退后收入 5000 分。",
    ),
    "2026 年 6 月梧桐书店扣退后收入是多少？": (
        "completed",
        {"status": "ok", "rows": [[0]], "columns": ["net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "2026 年 6 月覆盖内无订单，扣退后收入为 0 分。",
    ),
    "2026 年 10 月梧桐书店扣退后收入是多少？": (
        "refused",
        {"status": "unsupported", "reason": "超出数据覆盖或时间范围非法"},
        "该时间超出数据覆盖范围，无法回答。",
    ),
    "2026 年 7 月 5 日到 7 月 20 日梧桐书店月内大额会员数是多少？": (
        "refused",
        {"status": "unsupported", "reason": "所选指标只支持完整自然月、全门店全渠道"},
        "月内大额会员数只支持整月查询。",
    ),
    "2026 年 8 月天猫渠道的扣退后收入是多少？": (
        "refused",
        {"status": "unsupported", "reason": "筛选值不在本次测试数据字典或实体范围中，不能视为零"},
        "数据里没有天猫渠道，不能当作 0。",
    ),
    "2026 年 8 月各渠道的扣退后收入分别是多少？": (
        "completed",
        {"status": "ok", "rows": [["门店", 8000], ["小程序", 5000]],
         "columns": ["channel", "net_receipts_cents"],
         "units": {"net_receipts_cents": "cents"}},
        "门店 8000 分，小程序 5000 分。",
    ),
    "2026 年 8 月梧桐书店退回占比是多少？": (
        "completed",
        {"status": "ok", "rows": [[0.1333333333]], "columns": ["return_ratio"],
         "units": {"return_ratio": "ratio"}},
        "退回占比 13.33%。",
    ),
    "2026 年 8 月梧桐书店的收入是多少？": (
        "clarification",
        {"status": "clarify", "reason": "「收入」指到账总额还是扣退后收入"},
        "请确认「收入」指到账总额还是扣退后收入。",
    ),
}


class StubAdapter(W03AgentAdapter):
    """把 HTTP 换成内存假响应，其余映射逻辑完全复用真实适配器。"""

    def __init__(self, fixtures: dict, **kw):
        super().__init__(**kw)
        self.fixtures = fixtures
        self.by_query = {q: v for q, v in fixtures.items()}

    def health(self) -> bool:
        return True

    def _post(self, path: str, body: dict) -> dict:
        agent_status, metric, answer = self.by_query[body["text"]]
        return {
            "product_version": "w03-0.1.0",
            "runtime_revision": "stub",
            "session_id": body.get("session_id", ""),
            "model_mode": "live",
            "model_name": "stub-model",
            "prompt_sha256": "0" * 16,
            "status": agent_status,
            "result": {
                "status": agent_status,
                "summary": answer,
                "recommendations": [],
                "evidence_ids": ["stub-trace"],
                "metric_query": {**metric, "trace_id": "stub-trace", "answer": answer},
            },
            "observations": [{"name": "query_metrics", "result": metric, "call_id": "c1"}],
            "steps": 2,
        }


def run(evalset_path: str, fixtures: dict, label: str) -> tuple[int, int]:
    cases = load_evalset(HERE / evalset_path)
    runner = BatchRunner(StubAdapter(fixtures))
    report = runner.run_all(cases, level="L0")
    ok, reasons = gate_decision(report, baseline_rate=1.0)
    print(f"\n[{label}] {evalset_path} → 通过 {report.passed}/{report.total} "
          f"成功率 {report.success_rate:.1%}｜门禁 {'✅ 放行' if ok else '⛔ 拦截'}")
    if not ok:
        print(report.text())
        for r in reasons:
            print("  -", r)
    return report.passed, report.total


def main() -> int:
    fails = 0

    # 1) 全量 18 条，against 契约假响应
    p, t = run("evalset_w03.json", FIXTURES, "全量")
    fails += (t - p)

    # 2) 冒烟 4 条
    p2, t2 = run("evalset_w03_smoke.json", FIXTURES, "冒烟")
    fails += (t2 - p2)

    # 3) 负向：篡改 blocker 的数值，门禁必须拦住
    bad = copy.deepcopy(FIXTURES)
    q = "2026 年 8 月梧桐书店扣退后收入是多少？"
    agent_status, metric, answer = bad[q]
    metric["rows"] = [[13001]]                       # 差 1 分
    bad[q] = (agent_status, metric, answer.replace("13000", "13001"))
    cases = load_evalset(HERE / "evalset_w03.json")
    report = BatchRunner(StubAdapter(bad)).run_all(cases, level="L0")
    ok, reasons = gate_decision(report, baseline_rate=1.0)
    print(f"\n[负向] 篡改 blocker 金额 → 门禁 {'✅ 放行（错！）' if ok else '⛔ 拦截（正确）'}")
    if not ok:
        print("  拦截理由:", reasons[0])
    if ok:
        fails += 1

    # 4) 负向：撤销单误计入（aug 单数写 3），必须被断言抓到
    bad2 = copy.deepcopy(FIXTURES)
    q2 = "2026 年 8 月梧桐书店收款单数是多少？"
    a2, m2, ans2 = bad2[q2]
    m2["rows"] = [[3]]
    bad2[q2] = (a2, m2, ans2)
    report2 = BatchRunner(StubAdapter(bad2)).run_all(cases, level="L0")
    ok2, _ = gate_decision(report2, baseline_rate=1.0)
    print(f"[负向] 撤销单误计入 → {'✅ 放行（错！）' if ok2 else '⛔ 拦截（正确）'}")
    if ok2:
        fails += 1

    print(f"\n===== 自检结论：{'全部通过' if fails == 0 else f'{fails} 项异常'} =====")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

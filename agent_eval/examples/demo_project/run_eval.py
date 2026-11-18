"""命令行跑批 + 报告。

用法：
    python3 run_eval.py            # 正常跑（带缺陷开关的当前状态）
    python3 run_eval.py --fixed    # 修复全部缺陷后跑（门禁变绿）
    python3 run_eval.py --gate     # 门禁模式：不达标 exit 1（CI 里用这个）
    python3 run_eval.py --level L1 # 启用 P2 语义判分（需先 export EVAL_MODEL_*，
                                   # 且金标校准通过后 judge 才会真正上岗）
    python3 run_eval.py --coverage # 同时生成覆盖账本审计报告（coverage.html）
"""
import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_eval_lib import (BatchRunner, gate_decision, load_evalset,
                            load_judge_from_env, build_ledger,
                            render_coverage_html, render_coverage_text)

HERE = os.path.dirname(__file__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalset", default=os.path.join(HERE, "evalset.json"))
    ap.add_argument("--gate", action="store_true", help="门禁模式：失败 exit 1")
    ap.add_argument("--fixed", action="store_true", help="演示用：修复全部缺陷")
    ap.add_argument("--baseline", type=float, default=None,
                    help="历史基线成功率（0~1），用于下降判定")
    ap.add_argument("--level", default="L0", choices=["L0", "L1", "L2"],
                    help="评测层级：L0 只 P1；L1/L2 加 P2 语义判分（需 EVAL_MODEL_*）")
    ap.add_argument("--coverage", action="store_true",
                    help="生成覆盖账本审计报告 coverage.html")
    args = ap.parse_args()

    if args.fixed:
        import agent
        for k in agent.BUGS:
            agent.BUGS[k] = False

    judge = load_judge_from_env() if args.level != "L0" else None
    if args.level != "L0":
        if judge is None:
            print("⚠ 未配置 EVAL_MODEL_URL/NAME/KEY，P2 语义判分将跳过（P1 照常跑）")
        elif not judge.calibrated:
            print("⚠ judge 未通过金标校准（calibrate_judge 一致率 ≥85% 且 Spearman ≥0.8），"
                  "语义断言将跳过并标注。这里为演示先放行校准标记——生产环境请先跑金标集校准")
            judge.mark_calibrated()

    cases = load_evalset(args.evalset)
    adapter_cls = __import__("adapter").ShoppingAgentAdapter
    report = BatchRunner(adapter_cls(), judge=judge).run_all(
        cases, level=args.level)
    print(report.text())

    if args.coverage:
        spec = os.path.join(HERE, "coverage_spec.json")
        if os.path.exists(spec):
            ledger = build_ledger(spec)
            print("\n" + render_coverage_text(ledger.report()))
            out = render_coverage_html(ledger, os.path.join(HERE, "coverage.html"))
            print(f"覆盖账本审计报告: {out}")
        else:
            print(f"\n（未找到 {spec}，跳过覆盖账本）")

    ok, reasons = gate_decision(report, baseline_rate=args.baseline)
    print("\n门禁判定:", "✅ 放行合并" if ok else "⛔ 拦截合并")
    for r in reasons:
        print(f"  - {r}")
    if not ok:
        print("\n（修复 agent.py 顶部的 BUGS 开关后重跑，观察门禁从红变绿）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

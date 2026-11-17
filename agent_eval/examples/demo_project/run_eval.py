"""命令行跑批 + 报告。

用法：
    python3 run_eval.py            # 正常跑（带缺陷开关的当前状态）
    python3 run_eval.py --fixed    # 修复全部缺陷后跑（门禁变绿）
    python3 run_eval.py --gate     # 门禁模式：不达标 exit 1（CI 里用这个）
"""
import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_eval_lib import BatchRunner, gate_decision, load_evalset

HERE = os.path.dirname(__file__)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalset", default=os.path.join(HERE, "evalset.json"))
    ap.add_argument("--gate", action="store_true", help="门禁模式：失败 exit 1")
    ap.add_argument("--fixed", action="store_true", help="演示用：修复全部缺陷")
    ap.add_argument("--baseline", type=float, default=None,
                    help="历史基线成功率（0~1），用于下降判定")
    args = ap.parse_args()

    if args.fixed:
        import agent
        for k in agent.BUGS:
            agent.BUGS[k] = False

    cases = load_evalset(args.evalset)
    report = BatchRunner(__import__("adapter").ShoppingAgentAdapter()
                         ).run_all(cases, level="L0")
    print(report.text())

    ok, reasons = gate_decision(report, baseline_rate=args.baseline)
    print("\n门禁判定:", "✅ 放行合并" if ok else "⛔ 拦截合并")
    for r in reasons:
        print(f"  - {r}")
    if not ok:
        print("\n（修复 agent.py 顶部的 BUGS 开关后重跑，观察门禁从红变绿）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

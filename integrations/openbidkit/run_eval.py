"""通用跑批入口：加载同目录 adapter.py + evalset.json，跑批 + 门禁 + 报告。

用法：
  python3 run_eval.py                        # 全部默认
  python3 run_eval.py --evalset other.json   # 指定评估集
  python3 run_eval.py --baseline 0.95        # 自定义基线（默认 1.0）
  python3 run_eval.py --output report.json   # 额外落一份 JSON 报告
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "agent_eval"))

from agent_eval_lib import BatchRunner, gate_decision, load_evalset  # noqa: E402


def load_adapter():
    """加载同目录 adapter.py，自动找到第一个 AgentAdapter 子类并实例化。"""
    spec = importlib.util.spec_from_file_location("adapter", HERE / "adapter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from agent_eval_lib import AgentAdapter
    for name in dir(mod):
        obj = getattr(mod, name)
        if (isinstance(obj, type) and issubclass(obj, AgentAdapter)
                and obj is not AgentAdapter):
            print(f"adapter: {name}")
            try:
                return obj()                       # 无参构造优先
            except TypeError:
                return obj()                       # 兜底
    raise SystemExit("✗ adapter.py 里没有 AgentAdapter 子类")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalset", default=str(HERE / "evalset.json"))
    ap.add_argument("--baseline", type=float, default=1.0)
    ap.add_argument("--level", default="L0")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    adapter = load_adapter()
    if hasattr(adapter, "health") and not adapter.health():
        print("✗ 被测服务不在线（adapter.health() 失败）。先启动被测服务再跑批。")
        return 2

    cases = load_evalset(args.evalset)
    print(f"evalset: {args.evalset} ({len(cases)} 条)  baseline: {args.baseline}")
    t0 = time.time()
    report = BatchRunner(adapter).run_all(cases, level=args.level)
    print(report.text())
    print(f"耗时 {time.time() - t0:.0f}s")

    last = report.outcomes[-1].run
    if last and last.versions:
        print(f"版本信息: {last.versions}")

    if args.output:
        dump = {
            "level": report.level,
            "total": report.total,
            "passed": report.passed,
            "success_rate": report.success_rate,
            "cases": [{
                "id": o.case.case_id,
                "severity": o.case.severity,
                "query": o.case.query,
                "passed": o.passed,
                "final_output": o.run.final_output if o.run else "",
                "first_error": (o.first_error.step_id if o.first_error else None),
                "attribution": o.attribution,
                "failed": o.failed_details(),
            } for o in report.outcomes],
        }
        Path(args.output).write_text(
            json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.output}")

    ok, reasons = gate_decision(report, baseline_rate=args.baseline)
    if ok:
        print("✅ 门禁放行")
        return 0
    print("⛔ 门禁拦截:")
    for r in reasons:
        print(f"  - {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""W03 工程批量评测入口。

前置条件（按 docs/w03/README.md 完成后）：
  1. 已加载指标库:  .venv/bin/python tools/w03.py load --library examples/w03/library \
       --data examples/w03/data --project .runtime/wutong-v1 --activate-home .runtime/product
  2. 服务已启动:    .venv/bin/python tools/product.py start --home .runtime/product
  3. 已配置模型:    FDE_MODEL_URL / FDE_MODEL_NAME / FDE_MODEL_KEY（见 docs/model-configuration.md）

用法：
  python3 run_eval.py                 # 跑批 + 门禁（baseline 默认 1.0）
  python3 run_eval.py --baseline 0.9  # 自定义基线
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from adapter import W03AgentAdapter
from harness import BatchRunner, gate_decision
from evalset import load_evalset

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evalset", default=str(HERE / "evalset_w03.json"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--baseline", type=float, default=1.0)
    args = ap.parse_args()

    adapter = W03AgentAdapter(base_url=args.base_url)
    if not adapter.health():
        print("✗ 服务不在线，先按顺序排查：")
        print("  1. 模型变量是否配在「启动服务」的那个终端（不是本终端）：")
        print("     FDE_MODEL_URL / FDE_MODEL_NAME / FDE_MODEL_KEY")
        print("     缺项时 product.py 启动即退，打印 MODEL_NOT_CONFIGURED")
        print("  2. 指标库是否已 load 并 --activate-home")
        print("  3. 启动: ./start_service.sh  （或 tools/product.py start --home .runtime/product）")
        return 2

    cases = load_evalset(args.evalset)
    runner = BatchRunner(adapter)
    t0 = time.time()
    report = runner.run_all(cases, level="L0")
    print(report.text())
    print(f"耗时 {time.time() - t0:.0f}s")

    # 版本四要素抽查：最后一次运行记录的版本信息应写入报告留档
    last = report.outcomes[-1].run
    if last and last.versions:
        print(f"版本四要素: {last.versions}")

    ok, reasons = gate_decision(report, baseline_rate=args.baseline)
    if ok:
        print("✅ 放行合并")
        return 0
    print("⛔ 拦截合并:")
    for r in reasons:
        print(f"  - {r}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

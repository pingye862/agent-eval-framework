"""按 profile 把通用案例库“翻译”成你自己的工具名与期望值。

为什么需要它：案例库的断言只引用标准动作名（lookup / compute / record / destroy
/ deliver）和固定夹具值（ECHO-42 / ALPHA-7 / OMEGA-9 / 84）。你接自己的系统时，
可以（A）在 adapter 里把真实工具名归一成标准名——推荐，改一处；也可以（B）用本
脚本把案例里的标准名直接换成你的真实名——不改代码，只改配置。

两条路选一条即可，不要同时用（会双重改名）。

用法：
    cp profile.example.json profile.mine.json   # 填你的真实工具名/期望值
    python3 render.py --profile profile.mine.json --out evalset.mine.json
    python3 run_eval.py --evalset evalset.mine.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.environ.get("AGENT_EVAL_SRC") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from evalset import load_evalset  # noqa: E402  —— 用框架自己的校验兜底

_KEYS = ("tool_aliases", "state_paths", "arg_aliases", "value_aliases")


def _load_profile(path: str) -> dict:
    raw = json.loads(open(path, encoding="utf-8").read())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: profile 应为 JSON 对象")
    return {k: (raw.get(k) or {}) for k in _KEYS}


def _rewrite(node, prof: dict, stats: dict):
    """只改有语义的位置：tool / path / required_args / 字符串值。"""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "tool" and isinstance(v, str):
                nv = prof["tool_aliases"].get(v, v)
                stats["tool"] += nv != v
                out[k] = nv
            elif k == "path" and isinstance(v, str):
                nv = prof["state_paths"].get(v, v)
                stats["path"] += nv != v
                out[k] = nv
            elif k == "required_args" and isinstance(v, list):
                out[k] = [prof["arg_aliases"].get(a, a) if isinstance(a, str) else a
                          for a in v]
            else:
                out[k] = _rewrite(v, prof, stats)
        return out
    if isinstance(node, list):
        return [_rewrite(x, prof, stats) for x in node]
    if isinstance(node, str):
        s = node
        # 长的先替换，避免 "84" 把 "184" 的片段改掉
        for old in sorted(prof["value_aliases"], key=len, reverse=True):
            new = prof["value_aliases"][old]
            if isinstance(new, str) and old and old != new and old in s:
                s = s.replace(old, new)
                stats["value"] += 1
        return s
    return node


def main() -> int:
    ap = argparse.ArgumentParser(description="按 profile 渲染通用案例库")
    ap.add_argument("--evalset", default=os.path.join(HERE, "evalset.json"))
    ap.add_argument("--profile", required=True, help="profile JSON 路径")
    ap.add_argument("--out", default=os.path.join(HERE, "evalset.mine.json"))
    args = ap.parse_args()

    prof = _load_profile(args.profile)
    raw = json.loads(open(args.evalset, encoding="utf-8").read())
    stats = {"tool": 0, "path": 0, "value": 0}
    out = _rewrite(raw, prof, stats)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # 用框架自己的加载器校验产物：字段类型、必填项、turns 键名一处都不放过
    cases = load_evalset(args.out)
    print(f"已生成 {args.out}（{len(cases)} 条用例）")
    print(f"替换统计：工具名 {stats['tool']} 处 ｜ 状态路径 {stats['path']} 处 "
          f"｜ 值/令牌 {stats['value']} 处")
    if not any(stats.values()):
        print("提示：profile 里没有产生任何替换（与原案例等价）。确认是否填了真实值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

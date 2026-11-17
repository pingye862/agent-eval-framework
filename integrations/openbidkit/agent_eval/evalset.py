"""评估集加载与校验。

评估集文件格式与方案 4.4 的 YAML schema 对应，JSON 化（零依赖）。
支持字段见 EvalCase；缺必填字段直接报错，防止半成品评估集混进门禁。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = ("id", "query", "severity")


@dataclass
class EvalCase:
    case_id: str
    query: str
    severity: str                     # blocker / major / minor
    route: str = "product_consult"    # 路由提示，决定业务维度
    persona: str = ""
    must_call: list[dict] = field(default_factory=list)
    must_not_call: list[dict] = field(default_factory=list)
    response_contains: list[str] = field(default_factory=list)
    state_after: dict = field(default_factory=list)   # [{path, value}]
    judge_criteria: list[str] = field(default_factory=list)  # P2 语义断言（需校准的 judge）
    context_overrides: dict = field(default_factory=dict)    # 该用例的额外前置状态
    owner: str = ""


def load_evalset(path: str | Path) -> list[EvalCase]:
    """加载评估集。缺必填字段直接抛错——半成品不许进门禁。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: 评估集应为非空数组")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        missing = [k for k in REQUIRED if not item.get(k)]
        if missing:
            raise ValueError(f"{path} 第 {i} 条缺少必填字段: {missing}")
        cid = item["id"]
        if cid in seen:
            raise ValueError(f"{path}: 用例 id 重复: {cid}")
        seen.add(cid)
        cases.append(EvalCase(
            case_id=cid,
            query=item["query"],
            severity=item["severity"],
            route=item.get("route", "product_consult"),
            persona=item.get("persona", ""),
            must_call=item.get("must_call", []),
            must_not_call=item.get("must_not_call", []),
            response_contains=item.get("response_contains", []),
            state_after=item.get("state_after", []),
            judge_criteria=item.get("judge_criteria", []),
            context_overrides=item.get("context_overrides", {}),
            owner=item.get("owner", ""),
        ))
    return cases

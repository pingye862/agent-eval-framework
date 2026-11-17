"""评估集加载与校验。

评估集文件格式与方案 4.4 的 YAML schema 对应，JSON 化（零依赖）。
支持字段见 EvalCase；缺必填字段直接报错，防止半成品评估集混进门禁。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = ("id", "query", "severity")
VALID_SEVERITY = ("blocker", "major", "minor")
_LIST_FIELDS = ("must_call", "must_not_call", "response_contains", "state_after", "judge_criteria")
# 多轮对话：turns 是第 2 轮起的后续轮次（第 1 轮即 case.query），每轮可带自己的断言
TURN_KEYS = {"query", "response_contains", "must_call", "must_not_call"}


def _validate_item(path: str | Path, i: int, item: dict) -> None:
    """单条用例的深度校验：枚举值 + 字段类型。半成品评估集不许进门禁。"""
    missing = [k for k in REQUIRED if not str(item.get(k) or "").strip()]
    if missing:
        raise ValueError(f"{path} 第 {i} 条缺少必填字段: {missing}")
    sev = item["severity"]
    if sev not in VALID_SEVERITY:
        raise ValueError(
            f"{path} 第 {i} 条 severity 非法: {sev!r}（允许 {VALID_SEVERITY}）")
    for k in _LIST_FIELDS:
        if k in item and not isinstance(item[k], list):
            raise ValueError(f"{path} 第 {i} 条字段 {k} 应为数组，实际 {type(item[k]).__name__}")
    for k in ("must_call", "must_not_call"):
        for j, req in enumerate(item.get(k) or []):
            if not isinstance(req, dict) or not str(req.get("tool") or "").strip():
                raise ValueError(
                    f"{path} 第 {i} 条 {k}[{j}] 应为 {{\"tool\": ...}} 且 tool 非空")
    for j, kw in enumerate(item.get("response_contains") or []):
        if not isinstance(kw, str) or not kw.strip():
            raise ValueError(f"{path} 第 {i} 条 response_contains[{j}] 应为非空字符串")
    turns = item.get("turns")
    if turns is not None:
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{path} 第 {i} 条 turns 应为非空数组（第 2 轮起的后续轮次）")
        for j, t in enumerate(turns):
            if not isinstance(t, dict):
                raise ValueError(f"{path} 第 {i} 条 turns[{j}] 应为对象")
            unknown = set(t) - TURN_KEYS
            if unknown:
                raise ValueError(f"{path} 第 {i} 条 turns[{j}] 含未知字段: {sorted(unknown)}（允许 {sorted(TURN_KEYS)}）")
            if not str(t.get("query") or "").strip():
                raise ValueError(f"{path} 第 {i} 条 turns[{j}] 缺少非空 query")
            for k in ("response_contains", "must_call", "must_not_call"):
                if k in t and not isinstance(t[k], list):
                    raise ValueError(f"{path} 第 {i} 条 turns[{j}].{k} 应为数组")


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
    turns: list[dict] = field(default_factory=list)  # 多轮对话：第 2 轮起的后续轮次
    owner: str = ""


def load_evalset(path: str | Path) -> list[EvalCase]:
    """加载评估集。缺必填字段直接抛错——半成品不许进门禁。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: 评估集应为非空数组")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path} 第 {i} 条应为对象，实际 {type(item).__name__}")
        _validate_item(path, i, item)
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
            turns=item.get("turns", []),
            owner=item.get("owner", ""),
        ))
    return cases

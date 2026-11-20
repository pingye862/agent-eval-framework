"""评估集加载与校验。

评估集文件格式：JSON 化 schema（零依赖），字段说明见 README。
支持字段见 EvalCase；缺必填字段直接报错，防止半成品评估集混进门禁。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REQUIRED = ("id", "query", "severity")
VALID_SEVERITY = ("blocker", "major", "minor")
_LIST_FIELDS = ("state_after", "judge_criteria", "tool_result_used")
# 数据集分层（推荐值，允许自定义）：
#   smoke      冒烟集，发布前快速跑
#   regression 回归集，历史 Bad Case 修复后沉淀进来，上线前全量跑（默认）
#   baseline   基准集，模型/大版本切换时做全量对比
RECOMMENDED_DATASETS = ("smoke", "regression", "baseline")
# 多轮对话：turns 是第 2 轮起的后续轮次（第 1 轮即 case.query），每轮可带自己的断言
TURN_KEYS = {"query", "response_contains", "must_call", "must_not_call"}


def _validate_assertions(location: str, spec: dict) -> None:
    for key in ("must_call", "must_not_call", "response_contains"):
        if key not in spec:
            continue
        entries = spec[key]
        if not isinstance(entries, list):
            raise ValueError(f"{location}{key} 应为数组，实际 {type(entries).__name__}")
        for j, entry in enumerate(entries):
            field = f"{location}{key}[{j}]"
            if key == "response_contains":
                if not isinstance(entry, str) or not entry.strip():
                    raise ValueError(f"{field} 应为非空字符串")
                continue
            tool = entry.get("tool") if isinstance(entry, dict) else None
            if not isinstance(tool, str) or not tool.strip():
                raise ValueError(f"{field} 应为对象且 tool 应为非空字符串")
            if key == "must_call" and "required_args" in entry:
                args = entry["required_args"]
                if not isinstance(args, list) or not all(isinstance(a, str) and a.strip() for a in args):
                    raise ValueError(f"{field}.required_args 应为非空字符串数组")


def _validate_item(path: str | Path, i: int, item: dict) -> None:
    """单条用例的深度校验：枚举值 + 字段类型。半成品评估集不许进门禁。"""
    # 必填字段必须是非空字符串：旧写法 str(v or "") 只判真值，会让
    # id=[1,2] 通过校验后在去重时抛 unhashable TypeError，或让 int/bool
    # 静默变成 case_id 污染下游所有字符串假设。与 dataset 校验同口径。
    missing = [k for k in REQUIRED
               if not isinstance(item.get(k), str) or not item[k].strip()]
    if missing:
        raise ValueError(f"{path} 第 {i} 条缺少必填字段或类型错误"
                         f"（应为非空字符串）: {missing}")
    sev = item["severity"]
    if not isinstance(sev, str) or sev not in VALID_SEVERITY:
        raise ValueError(
            f"{path} 第 {i} 条 severity 非法: {sev!r}（允许 {VALID_SEVERITY}）")
    for k in _LIST_FIELDS:
        if k in item and not isinstance(item[k], list):
            raise ValueError(f"{path} 第 {i} 条字段 {k} 应为数组，实际 {type(item[k]).__name__}")
    _validate_assertions(f"{path} 第 {i} 条 ", item)
    for j, c in enumerate(item.get("judge_criteria") or []):
        if not isinstance(c, str) or not c.strip():
            raise ValueError(
                f"{path} 第 {i} 条 judge_criteria[{j}] 应为非空字符串"
                "（语义判分标准，judge 校准通过后自动执行）")
    ds = item.get("dataset")
    if ds is not None and (not isinstance(ds, str) or not ds.strip()):
        raise ValueError(f"{path} 第 {i} 条 dataset 应为非空字符串"
                         f"（推荐 {RECOMMENDED_DATASETS}）")
    for j, req in enumerate(item.get("tool_result_used") or []):
        if not isinstance(req, dict) or not str(req.get("tool") or "").strip():
            raise ValueError(
                f"{path} 第 {i} 条 tool_result_used[{j}] 应为对象且含非空 tool")
        if "min_values" in req and (not isinstance(req["min_values"], int)
                                    or isinstance(req["min_values"], bool)
                                    or req["min_values"] < 1):
            raise ValueError(f"{path} 第 {i} 条 tool_result_used[{j}].min_values 应为 ≥1 的整数")
        if "values" in req and (not isinstance(req["values"], list)
                                or not all(isinstance(v, (str, int, float))
                                           for v in req["values"])):
            raise ValueError(f"{path} 第 {i} 条 tool_result_used[{j}].values 应为标量数组")
    g = item.get("output_grounded_in")
    if g is not None:
        if not isinstance(g, dict):
            raise ValueError(f"{path} 第 {i} 条 output_grounded_in 应为对象")
        if "min_values" in g and (not isinstance(g["min_values"], int)
                                  or isinstance(g["min_values"], bool)
                                  or g["min_values"] < 1):
            raise ValueError(f"{path} 第 {i} 条 output_grounded_in.min_values 应为 ≥1 的整数")
        if "sources" in g and (not isinstance(g["sources"], list)
                               or not all(s in ("tool_results", "retrieved")
                                          for s in g["sources"])):
            raise ValueError(
                f"{path} 第 {i} 条 output_grounded_in.sources 只能是 "
                "tool_results / retrieved")
        if "values" in g and (not isinstance(g["values"], list)
                              or not all(isinstance(v, (str, int, float))
                                         for v in g["values"])):
            raise ValueError(f"{path} 第 {i} 条 output_grounded_in.values 应为标量数组")
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
            tq = t.get("query")
            if not isinstance(tq, str) or not tq.strip():
                raise ValueError(f"{path} 第 {i} 条 turns[{j}] 缺少非空 query")
            _validate_assertions(f"{path} 第 {i} 条 turns[{j}].", t)


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
    state_after: list = field(default_factory=list)          # [{path, value}]
    judge_criteria: list[str] = field(default_factory=list)  # P2 语义断言（需校准的 judge）
    context_overrides: dict = field(default_factory=dict)    # 该用例的额外前置状态
    turns: list[dict] = field(default_factory=list)  # 多轮对话：第 2 轮起的后续轮次
    owner: str = ""
    dataset: str = "regression"       # 数据集分层：smoke / regression / baseline（可自定义）
    tool_result_used: list[dict] = field(default_factory=list)   # 证据接地：定向工具结果被采用
    output_grounded_in: dict = field(default_factory=dict)       # 证据接地：整体接地值数量


def case_to_dict(case: EvalCase) -> dict:
    """EvalCase → 评估集 JSON 对象（字段名对齐文件格式，供失败样本回流使用）。

    只输出非默认值字段，避免回流时把一堆空数组写进评估集。
    """
    d = {
        "id": case.case_id,
        "query": case.query,
        "severity": case.severity,
        "dataset": case.dataset,
    }
    optional = ("route", "persona", "must_call", "must_not_call", "response_contains",
                "state_after", "judge_criteria", "context_overrides", "turns",
                "owner", "tool_result_used", "output_grounded_in")
    for k in optional:
        v = getattr(case, k)
        if v:
            d[k] = v
    return d


def load_evalset(path: str | Path, dataset: str | list[str] | None = None) -> list[EvalCase]:
    """加载评估集。缺必填字段直接抛错——半成品不许进门禁。

    dataset: 可选，只取指定数据集标签的用例（str 或 str 列表）。
    None = 全量。用于「冒烟集 / 回归集 / 基准集」分层跑批。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: 评估集应为非空数组")
    wanted = None
    if dataset is not None:
        wanted = {dataset} if isinstance(dataset, str) else set(dataset)
        if not wanted:
            raise ValueError("dataset 过滤条件为空")
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
        ds = item.get("dataset") or "regression"
        if wanted is not None and ds not in wanted:
            continue                      # 校验仍做过（半成品不因被过滤就放过）
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
            dataset=ds,
            tool_result_used=item.get("tool_result_used", []),
            output_grounded_in=item.get("output_grounded_in") or {},
        ))
    if wanted is not None and not cases:
        raise ValueError(f"{path}: 没有 dataset ∈ {sorted(wanted)} 的用例"
                         "（检查标签拼写，或去掉过滤条件跑全量）")
    return cases


def dataset_counts(path: str | Path) -> dict[str, int]:
    """评估集里各 dataset 标签的用例数（控制台下拉用）。"""
    from collections import Counter
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict(Counter((x.get("dataset") or "regression")
                        if isinstance(x, dict) else "regression" for x in raw))

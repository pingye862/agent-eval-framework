"""evalset 单测：必填校验、枚举校验、类型校验、重复 id、合法加载。"""
import json
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evalset import load_evalset


def _write(tmp_path, items):
    p = tmp_path / "evalset.json"
    p.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_valid(tmp_path):
    p = _write(tmp_path, [{
        "id": "c1", "query": "问题", "severity": "blocker",
        "response_contains": ["关键词"],
        "must_call": [{"tool": "run_sql", "required_args": ["sql"]}],
        "state_after": [{"path": "log", "min_length": 1}],
    }])
    cases = load_evalset(p)
    assert len(cases) == 1
    assert cases[0].severity == "blocker"
    assert cases[0].must_call[0]["tool"] == "run_sql"


def test_empty_array_rejected(tmp_path):
    p = _write(tmp_path, [])
    with pytest.raises(ValueError, match="非空数组"):
        load_evalset(p)


def test_missing_required_field(tmp_path):
    p = _write(tmp_path, [{"id": "c1", "query": "问题"}])   # 缺 severity
    with pytest.raises(ValueError, match="缺少必填字段"):
        load_evalset(p)


def test_invalid_severity_rejected(tmp_path):
    p = _write(tmp_path, [{"id": "c1", "query": "q", "severity": "fatal"}])
    with pytest.raises(ValueError, match="severity 非法"):
        load_evalset(p)


def test_duplicate_id_rejected(tmp_path):
    p = _write(tmp_path, [
        {"id": "c1", "query": "q", "severity": "major"},
        {"id": "c1", "query": "q2", "severity": "major"},
    ])
    with pytest.raises(ValueError, match="id 重复"):
        load_evalset(p)


def test_list_field_wrong_type_rejected(tmp_path):
    p = _write(tmp_path, [
        {"id": "c1", "query": "q", "severity": "major",
         "response_contains": "不是数组"}])
    with pytest.raises(ValueError, match="应为数组"):
        load_evalset(p)


def test_must_call_entry_rejected(tmp_path):
    p = _write(tmp_path, [
        {"id": "c1", "query": "q", "severity": "major",
         "must_call": ["run_sql"]}])                      # 缺 tool 键
    with pytest.raises(ValueError, match="must_call"):
        load_evalset(p)


def test_response_contains_non_string_rejected(tmp_path):
    p = _write(tmp_path, [
        {"id": "c1", "query": "q", "severity": "major",
         "response_contains": [123]}])
    with pytest.raises(ValueError, match="response_contains"):
        load_evalset(p)


def test_non_dict_item_rejected(tmp_path):
    p = _write(tmp_path, ["字符串条目"])
    with pytest.raises(ValueError, match="应为对象"):
        load_evalset(p)

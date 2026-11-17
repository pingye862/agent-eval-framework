"""agent_eval 本地控制台后端。

零依赖：只用 Python 标准库（http.server + 框架本身）。
启动：python3 webui/server.py   →  http://127.0.0.1:8900

API：
    GET  /                     前端页面
    GET  /api/meta             评估集与适配器清单（含各自可用适配器）
    GET  /api/health?adapter=  检测某适配器是否在线（w03-live 用）
    POST /api/run              跑批：{evalset, adapter, fixed, baseline}
    GET  /api/cases?path=      读评估集全部用例（path 必须已注册）
    POST /api/cases            新增用例：{path, case}
    PUT  /api/cases            更新用例：{path, id, case}
    DELETE /api/cases?path=&id= 删除用例
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent.parent          # Agent测试方案/
AGENT_EVAL = BASE / "agent_eval"
DEMO_DIR = AGENT_EVAL / "examples" / "demo_project"
W03_DIR = BASE / "fde-w03-integration"
WEBUI_DIR = Path(__file__).resolve().parent
PROJECTS_FILE = WEBUI_DIR / "projects.json"            # 手动添加的项目注册表

# sys.path 顺序有讲究：w03 在前，让 selfcheck_offline 内部的
# `from adapter import W03AgentAdapter` 解析到 w03 的 adapter；
# demo 的 adapter 用 importlib 按文件路径显式加载，不走名字解析。
sys.path.insert(0, str(AGENT_EVAL))
sys.path.insert(1, str(W03_DIR))
sys.path.insert(2, str(DEMO_DIR))

from evalset import load_evalset                      # noqa: E402
from harness import (BatchRunner, gate_decision,      # noqa: E402
                     AgentAdapter, AgentRun, ToolCallRecord)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载适配器模块: {path}（请确认是有效的 .py 文件）")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


demo_adapter_mod = _load_module("demo_adapter", DEMO_DIR / "adapter.py")
# 注意：adapter.py 内部 `import agent` 走的是 sys.modules 的单例，
# 这里必须用同一个实例改缺陷开关，不能用 importlib 再加载一份（否则改不动）
import agent as demo_agent  # noqa: E402  (来自 DEMO_DIR)
w03_adapter_mod = _load_module("w03_adapter", W03_DIR / "adapter.py")
w03_stub_mod = _load_module("w03_selfcheck", W03_DIR / "selfcheck_offline.py")

# ---------- 注册表 ----------

EVALSETS = {
    "demo": {
        "label": "数码导购示例（8 条，含缺陷开关）",
        "path": str(DEMO_DIR / "evalset.json"),
        "adapters": ["demo"],
    },
    "w03-smoke": {
        "label": "梧桐冒烟集（4 条：ok/澄清/拒答三态）",
        "path": str(W03_DIR / "evalset_w03_smoke.json"),
        "adapters": ["w03-stub", "w03-live"],
    },
    "w03-full": {
        "label": "梧桐全量集（18 条，blocker 4）",
        "path": str(W03_DIR / "evalset_w03.json"),
        "adapters": ["w03-stub", "w03-live"],
    },
}

ADAPTERS = {
    "demo": {"label": "示例 Agent（进程内，离线）"},
    "w03-stub": {"label": "梧桐·契约桩（离线演示，恒绿）"},
    "w03-live": {"label": "梧桐·真实服务 127.0.0.1:8765"},
}


def build_adapter(kind: str, fixed: bool):
    if kind == "demo":
        # 缺陷开关按请求切换；副作用日志清零，避免跨次跑批污染
        for k in demo_agent.BUGS:
            demo_agent.BUGS[k] = not fixed
        demo_agent.NOTIFY_LOG.clear()
        return demo_adapter_mod.ShoppingAgentAdapter()
    if kind == "w03-stub":
        return w03_stub_mod.StubAdapter(w03_stub_mod.FIXTURES)
    if kind == "w03-live":
        return w03_adapter_mod.W03AgentAdapter()
    if kind.startswith("c:"):
        name = kind[2:]
        for p in load_projects():
            if p["name"] == name:
                return build_custom_adapter(p)
        raise ValueError(f"自定义项目不存在: {name}")
    raise ValueError(f"未知适配器: {kind}")


# ---------- 手动添加的自定义项目（projects.json，热加载，改完即生效） ----------

def get_path(obj, path: str):
    """点路径取值：a.b.c；列表索引用 a.0.b。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        else:
            return None
    return cur


def load_projects() -> list[dict]:
    try:
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:                                     # noqa: BLE001
        return []


def save_projects(items: list[dict]) -> None:
    PROJECTS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ---------- 评估集用例 CRUD（增删改查，直接写回 JSON 文件） ----------

def registered_evalsets() -> dict[str, str]:
    """所有可操作的评估集：内置 + 自定义项目注册的。path -> label。"""
    reg = {v["path"]: v["label"] for v in EVALSETS.values()}
    for p in load_projects():
        e = p.get("evalset") or ""
        if e and e not in reg:
            reg[e] = f"[自定义] {p['name']}"
    return reg


def resolve_evalset_path(path_str: str) -> Path:
    """只允许读写控制台里注册过的评估集——防止任意文件读写。"""
    reg = registered_evalsets()
    if path_str not in reg:
        raise ValueError("评估集路径未注册，拒绝读写（只能改下拉里已注册的评估集）")
    return Path(path_str)


def _read_cases(path: Path) -> list:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: 评估集应为 JSON 数组")
    return data


def _validate_case(case: dict, others: list[dict],
                   exclude_id: str | None = None) -> dict:
    """校验并规范化单条用例。others 为全量用例（查重），exclude_id 为编辑时的旧 id。"""
    if not isinstance(case, dict):
        raise ValueError("用例必须是 JSON 对象")
    case = dict(case)
    cid = str(case.get("id") or "").strip()
    if not cid or not re.match(r"^[\w\u4e00-\u9fff.-]{1,80}$", cid):
        raise ValueError("用例 id 必填：中英文/数字/下划线/连字符/点，1~80 字")
    if cid != exclude_id and any(str(c.get("id")) == cid for c in others):
        raise ValueError(f"用例 id 已存在: {cid}")
    q = str(case.get("query") or "").strip()
    if not q:
        raise ValueError("query（测试问题）必填")
    if case.get("severity") not in ("blocker", "major", "minor"):
        raise ValueError("severity 只能是 blocker / major / minor")
    case["id"], case["query"] = cid, q
    # 前端友好：逗号分隔字符串 → 数组 / 工具名 → [{tool}]
    v = case.get("response_contains")
    if isinstance(v, str):
        case["response_contains"] = [s.strip() for s in v.split(",") if s.strip()]
    for k in ("must_call", "must_not_call"):
        v = case.get(k)
        if isinstance(v, str):
            case[k] = [{"tool": s.strip()} for s in v.split(",") if s.strip()]
        elif isinstance(v, list):
            case[k] = [{"tool": str(x)} if isinstance(x, str) else x for x in v]
    if not str(case.get("route") or "").strip():
        case.pop("route", None)
    else:
        case["route"] = str(case["route"]).strip()
    if not str(case.get("owner") or "").strip():
        case.pop("owner", None)
    return case


def save_evalset_cases(path: Path, cases: list[dict]) -> None:
    """写回前先整集校验（必填/重复 id）——半成品评估集不许进门禁。

    空数组仅允许出现在新评估集的起步阶段（文件此前为空/不存在）；
    一旦有用例，就不允许删到空。
    """
    if not cases and path.exists() and _read_cases(path):
        raise ValueError("评估集至少要保留 1 条用例（删除后不能为空）")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    if cases:
        load_evalset(str(tmp))
    tmp.replace(path)


class GenericHttpAdapter(AgentAdapter):
    """通用 HTTP 适配器：配置驱动的响应映射，接任何 REST 形态的 Agent 服务。

    配置（projects.json 里项目的 http 字段）：
        url            必填，聊天接口
        method         POST（默认）
        headers        额外请求头（鉴权等）
        body_template  请求体模板，{query}/{session} 占位符会被替换
        context_field  可选；把用例的 context_overrides 整体放进这个字段
        health_url     可选；GET 探活地址
        timeout        秒，默认 620
        response_mapping:
            final_output  点路径，或点路径数组（依次取第一个非空，做兜底）
            tool_calls    点路径，数组元素为 {name, args} 或字符串
            versions      {版本键: 点路径}
    """

    def __init__(self, cfg: dict) -> None:
        self.url = cfg["url"]
        self.headers = dict(cfg.get("headers") or {})
        self.headers.setdefault("Content-Type", "application/json")
        self.body_template = dict(cfg.get("body_template") or {"query": "{query}"})
        self.context_field = cfg.get("context_field")
        self.health_url = cfg.get("health_url") or ""
        self.timeout = int(cfg.get("timeout", 620))
        m = cfg.get("response_mapping") or {}
        self.m_final = m.get("final_output") or []
        if isinstance(self.m_final, str):
            self.m_final = [self.m_final]
        self.m_tools = m.get("tool_calls") or ""
        self.m_versions = m.get("versions") or {}
        self._seq = 0

    def health(self) -> bool:
        if not self.health_url:
            return True
        try:
            with urllib.request.urlopen(self.health_url, timeout=5) as resp:
                return resp.status == 200
        except Exception:                                 # noqa: BLE001
            return False

    def run(self, query: str, context: dict) -> AgentRun:
        self._seq += 1
        session = f"ui-eval-{int(time.time())}-{self._seq}"
        body = {}
        for k, v in self.body_template.items():
            if isinstance(v, str):
                v = v.replace("{query}", query).replace("{session}", session)
            body[k] = v
        if self.context_field:
            body[self.context_field] = context
        try:
            req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                         headers=self.headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except Exception as e:                            # noqa: BLE001
            return AgentRun(query=query, final_output="", error=f"HTTP 调用失败: {e}")

        final = None
        for p in self.m_final:
            v = get_path(out, p) if p else None
            if v is not None:
                final = v
                break
        tools = []
        if self.m_tools:
            for item in (get_path(out, self.m_tools) or []):
                if isinstance(item, dict):
                    args = item.get("args") if isinstance(item.get("args"), dict) else {}
                    tools.append(ToolCallRecord(str(item.get("name", "unknown")), args))
                elif isinstance(item, str):
                    tools.append(ToolCallRecord(item, {}))
        versions = {k: str(get_path(out, p) or "") for k, p in self.m_versions.items()}
        return AgentRun(
            query=query,
            final_output=str(final if final is not None else ""),
            tool_calls=tools,
            state={"response": out},                      # 全量响应入 state，点路径断言
            versions=versions,
        )


def build_custom_adapter(cfg: dict):
    t = cfg.get("adapter_type")
    if t == "http":
        http_cfg = cfg.get("http") or {}
        if not http_cfg.get("url"):
            raise ValueError("http 类型缺少 url")
        return GenericHttpAdapter(http_cfg)
    if t == "python":
        pc = cfg.get("python") or {}
        f = str(pc.get("file") or "").strip()
        if not f:
            raise ValueError("Python 适配器必须填 .py 文件路径；如果被测对象是 HTTP 服务，请把接入类型改回 HTTP")
        path = Path(f)
        if not path.is_file():
            raise ValueError(f"适配器文件不存在: {f}（注意填文件路径，不是目录）")
        if path.suffix != ".py":
            raise ValueError(f"适配器文件必须是 .py 后缀: {f}")
        mod_dir = str(path.resolve().parent)
        if mod_dir not in sys.path:
            sys.path.insert(0, mod_dir)
        mod = _load_module("ui_proj_" + re.sub(r"\W", "_", cfg.get("name", "proj")), path)
        cls = getattr(mod, pc.get("class_name") or "", None)
        if cls is None:
            raise ValueError(f"{path} 中找不到类 {pc.get('class_name')!r}")
        inst = cls()
        if not isinstance(inst, AgentAdapter):
            raise ValueError(f"{pc.get('class_name')} 必须是 AgentAdapter 的子类"
                             "（from harness import AgentAdapter）")
        return inst
    raise ValueError(f"未知适配器类型: {t}")


def validate_project(p: dict) -> tuple[int, str]:
    """添加前校验。返回 (用例数, 适配器摘要)；失败抛 ValueError。

    评估集路径可留空：自动在 webui/evalsets/<项目名>.json 建空集，
    之后在「用例维护」里逐步添加用例。
    """
    name = (p.get("name") or "").strip()
    if not name or not re.match(r"[\w\u4e00-\u9fff-]{1,40}$", name):
        raise ValueError("项目名只能含中英文、数字、下划线、连字符，1~40 字")
    if any(x["name"] == name for x in load_projects()):
        raise ValueError(f"项目名已存在: {name}")
    eset = (p.get("evalset") or "").strip()
    if not eset:
        evalsets_dir = WEBUI_DIR / "evalsets"
        # 注意：本机定制版 Python 的 os.mkdir 在目录已存在时也会抛 FileExistsError，
        # exist_ok=True 兜不住，必须先判断再建
        if not evalsets_dir.is_dir():
            try:
                evalsets_dir.mkdir(parents=True)
            except FileExistsError:
                pass
        eset = str(evalsets_dir / f"{name}.json")
        if not Path(eset).exists():
            Path(eset).write_text("[]", encoding="utf-8")   # 空集起步
        p["evalset"] = eset
    else:
        if not Path(eset).exists():
            raise ValueError(f"评估集文件不存在: {eset}")
    raw = _read_cases(Path(eset))
    n = len(load_evalset(eset)) if raw else 0    # 有用例才做整集校验
    adapter = build_custom_adapter(p)
    desc = f"{type(adapter).__name__}"
    return n, desc


def serialize_report(report, gate_ok, gate_reasons, baseline) -> dict:
    cases = []
    for o in report.outcomes:
        cases.append({
            "id": o.case.case_id,
            "query": o.case.query,
            "severity": o.case.severity,
            "route": o.case.route,
            "owner": o.case.owner,
            "passed": o.passed,
            "pass_runs": o.pass_runs,
            "total_runs": o.total_runs,
            "flaky": o.flaky,
            "assertions": [
                {"kind": a.kind, "ok": a.ok, "detail": a.detail, "layer": a.layer.value}
                for a in o.assertions
            ],
            "first_error": {
                "step_id": o.first_error.step_id,
                "target": o.first_error.target,
                "layer": o.first_error.layer.value,
                "detail": o.first_error.detail,
            } if o.first_error else None,
            "attribution": o.attribution,
            "final_output": (o.run.final_output if o.run else "")[:300],
            "tool_calls": [t.name for t in (o.run.tool_calls if o.run else [])],
            "versions": dict(o.run.versions) if o.run else {},
        })
    versions = {}
    for o in report.outcomes:
        if o.run and o.run.versions:
            versions = dict(o.run.versions)
            break
    return {
        "total": report.total,
        "passed": report.passed,
        "success_rate": report.success_rate,
        "blockers_failed": [o.case.case_id for o in report.blockers_failed()],
        "gate_ok": gate_ok,
        "gate_reasons": gate_reasons,
        "baseline": baseline,
        "versions": versions,
        "text_report": report.text(),
        "cases": cases,
    }


class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (WEBUI_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/meta":
            evalsets = {k: {**v, "cases": _safe_count(v["path"])}
                        for k, v in EVALSETS.items()}
            adapters = dict(ADAPTERS)
            for p in load_projects():
                eid = f"c:{p['name']}"
                n = _safe_count(p.get("evalset") or "")
                evalsets[eid] = {
                    "label": f"[自定义] {p['name']}",
                    "path": p.get("evalset") or "",
                    "adapters": [eid],
                    "adapter_type": p.get("adapter_type"),
                    "cases": n,
                }
                adapters[eid] = {"label": f"{p['name']}（{p.get('adapter_type')}）"}
            self._json({"evalsets": evalsets, "adapters": adapters})
            return
        if parsed.path == "/api/projects":
            self._json({"projects": load_projects()})
            return
        if parsed.path == "/api/cases":
            try:
                path = resolve_evalset_path(parse_qs(parsed.query).get("path", [""])[0])
                cases = _read_cases(path)
            except ValueError as e:                   # noqa: BLE001
                self._json({"error": str(e)}, 400)
                return
            except Exception as e:                    # noqa: BLE001
                self._json({"error": f"读取失败: {e}"}, 400)
                return
            self._json({"path": str(path), "cases": cases})
            return
        if parsed.path == "/api/health":
            adapter = parse_qs(parsed.query).get("adapter", [""])[0]
            try:
                ok = build_adapter(adapter, fixed=True).health()
            except Exception as e:                       # noqa: BLE001
                self._json({"ok": False, "detail": str(e)})
                return
            hint = "" if ok else "服务未启动：先跑 start_service.sh（需先配 FDE_MODEL_* 并 load 指标库）"
            self._json({"ok": bool(ok), "detail": hint})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/cases":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                p = resolve_evalset_path(body.get("path", ""))
                cases = _read_cases(p)
                new_case = _validate_case(body.get("case") or {}, cases)
                cases.append(new_case)
                save_evalset_cases(p, cases)
            except ValueError as e:                   # noqa: BLE001
                self._json({"error": str(e)}, 400)
                return
            except Exception as e:                    # noqa: BLE001
                self._json({"error": f"请求体非法: {e}"}, 400)
                return
            self._json({"ok": True, "id": new_case["id"], "total": len(cases)})
            return
        if path == "/api/projects":
            try:
                p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                n, desc = validate_project(p)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            except Exception as e:                        # noqa: BLE001
                self._json({"error": f"请求体非法: {e}"}, 400)
                return
            items = load_projects()
            p["cases"] = n
            p["adapter_desc"] = desc
            p["added_at"] = time.strftime("%Y-%m-%d %H:%M")
            items.append(p)
            save_projects(items)
            self._json({"ok": True, "name": p["name"], "cases": n, "desc": desc})
            return
        if path != "/api/run":
            self._json({"error": "not found"}, 404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            evalset_id = body["evalset"]
            adapter_kind = body["adapter"]
            fixed = bool(body.get("fixed", False))
            baseline = float(body.get("baseline", 1.0))
        except Exception as e:                            # noqa: BLE001
            self._json({"error": f"请求体非法: {e}"}, 400)
            return

        if evalset_id.startswith("c:"):
            name = evalset_id[2:]
            proj = next((p for p in load_projects() if p["name"] == name), None)
            if not proj:
                self._json({"error": f"自定义项目不存在: {name}"}, 400)
                return
            meta = {"path": proj.get("evalset"), "adapters": [evalset_id]}
        else:
            meta = EVALSETS.get(evalset_id)
        if not meta or adapter_kind not in meta["adapters"]:
            self._json({"error": "评估集与适配器不匹配"}, 400)
            return

        adapter = build_adapter(adapter_kind, fixed)
        if adapter_kind == "w03-live" and not adapter.health():
            self._json({"error": "W03 服务不在线。先配 FDE_MODEL_* 环境变量并运行 start_service.sh",
                        "hint": "或改用「梧桐·契约桩」先看流程"}, 409)
            return
        if adapter_kind.startswith("c:"):
            hf = getattr(adapter, "health", None)      # python 型适配器可能没实现探活
            if hf and not hf():
                self._json({"error": "自定义项目服务不在线（health() 返回失败）",
                            "hint": "检查服务是否已启动；若为进程内适配器可实现 health() 返回 True"}, 409)
                return

        try:
            if not _read_cases(Path(meta["path"])):
                self._json({"error": "评估集还没有用例。先在「用例维护」里添加用例，再跑批",
                            "hint": "用例维护面板：选这个评估集 → 新增用例 → 保存"}, 400)
                return
            cases = load_evalset(meta["path"])
            report = BatchRunner(adapter).run_all(cases, level="L0")
        except Exception as e:                            # noqa: BLE001
            self._json({"error": f"跑批失败: {e}"}, 500)
            return

        gate_ok, reasons = gate_decision(report, baseline_rate=baseline)
        self._json(serialize_report(report, gate_ok, reasons, baseline))


    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/cases":
            self._json({"error": "not found"}, 404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            p = resolve_evalset_path(body.get("path", ""))
            cases = _read_cases(p)
            old_id = str(body.get("id") or "")
            idx = next((i for i, c in enumerate(cases)
                        if str(c.get("id")) == old_id), None)
            if idx is None:
                raise ValueError(f"用例不存在: {old_id}")
            new_case = _validate_case(body.get("case") or {}, cases, exclude_id=old_id)
            cases[idx] = new_case
            save_evalset_cases(p, cases)
        except ValueError as e:                       # noqa: BLE001
            self._json({"error": str(e)}, 400)
            return
        except Exception as e:                        # noqa: BLE001
            self._json({"error": f"请求体非法: {e}"}, 400)
            return
        self._json({"ok": True, "id": new_case["id"]})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/cases":
            qs = parse_qs(parsed.query)
            try:
                p = resolve_evalset_path(qs.get("path", [""])[0])
                cases = _read_cases(p)
                cid = qs.get("id", [""])[0]
                kept = [c for c in cases if str(c.get("id")) != cid]
                if len(kept) == len(cases):
                    raise ValueError(f"用例不存在: {cid}")
                save_evalset_cases(p, kept)
            except ValueError as e:                   # noqa: BLE001
                self._json({"error": str(e)}, 400)
                return
            self._json({"ok": True})
            return
        if parsed.path != "/api/projects":
            self._json({"error": "not found"}, 404)
            return
        name = parse_qs(parsed.query).get("name", [""])[0]
        items = load_projects()
        kept = [p for p in items if p["name"] != name]
        if len(kept) == len(items):
            self._json({"error": f"项目不存在: {name}"}, 404)
            return
        save_projects(kept)
        self._json({"ok": True})


def _safe_count(path: str) -> int:
    """显示用计数：空数组返回 0，坏文件返回 -1（下拉显示 ?）。"""
    try:
        return len(_read_cases(Path(path)))
    except Exception:                                     # noqa: BLE001
        return -1


def main() -> None:
    port = 8900
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Agent 评测控制台 → http://127.0.0.1:{port}  (Ctrl+C 退出)")
    server.serve_forever()


if __name__ == "__main__":
    main()

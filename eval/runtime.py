# [추가] 새 파일 전체: 고정 MCP 자료·호출 기록·모델 사용량 계측을 실제 두 실행 경로에 주입한다.
"""외부 연결은 선택한 모델만 사용한다. 정답 AST는 모델·MCP에 전달하지 않는다."""
import asyncio
import inspect
import json
from copy import deepcopy
from pathlib import Path
from time import perf_counter

import httpx

from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.workflow.catalog import load_catalog
from agent.workflow.compiler import compile_workflow
from agent.workflow.executor import execute_workflow
from agent.workflow.graph import run_workflow
from agent.workflow.models import MonitoringSnapshot
from agent.workflow.store import SqliteWorkflowStore

ROOT = Path(__file__).resolve().parents[1]


# [추가] 공급자 원문 예외·키·응답 본문 대신 오류 종류와 HTTP 상태만 공개한다.
def safe_error(error: BaseException) -> dict:
    result = {"type": type(error).__name__}
    if isinstance(error, httpx.HTTPStatusError):
        result["http_status"] = error.response.status_code
    return result


# [추가] 파일 스냅샷과 시나리오 덮어쓰기를 합치고 모든 반환 자료를 독립 복사한다.
class RecordingFixtureMCP:
    def __init__(self, snapshots: dict, base: dict | None = None):
        if base is None:
            base = json.loads((ROOT / "eval/fixtures/monitoring.json").read_text())["snapshots"]
        self.snapshots = deepcopy(base)
        self.snapshots.update(deepcopy(snapshots))
        for value in self.snapshots.values():
            MonitoringSnapshot.model_validate(value)
        self.events = []
        self.started = perf_counter()

    # [추가] 도메인 조회와 탐색·프롬프트 호출을 서로 다른 source로 기록한다.
    def record(self, source: str, target: str, operation: str) -> dict:
        event = {"source": source, "target": target, "operation": operation,
                 "started_ms": (perf_counter() - self.started) * 1000, "completed_ms": None}
        self.events.append(event)
        return event

    async def find_prompt(self, name):
        event = self.record("discovery", name, "find_prompt")
        event["completed_ms"] = (perf_counter() - self.started) * 1000
        if name != "incident_analysis_prompt":
            raise ValueError("알 수 없는 프롬프트")
        return "ops"

    # [추가] mcp_server.py의 표준 절차와 동일한 문구를 사용해 baseline을 평가한다.
    async def get_prompt(self, name, arguments, server=None):
        event = self.record("prompt", name, "get_prompt")
        system_name, symptom = arguments["system_name"], arguments["symptom"]
        text = f"""다음 장애에 대해 표준 진단 절차를 수행하세요.

## 장애 정보
- 시스템: {system_name}
- 증상: {symptom}

## 진단 절차 (반드시 순서대로 수행)
1. get_monitoring_status Tool로 관련 시스템의 현재 상태를 조회하세요.
2. 증상에 해당하는 runbook(runbook://incident-response/...)을 확인하세요.
3. 위 정보를 종합해 가능한 원인과 권장 조치를 제시하세요.
"""
        event["completed_ms"] = (perf_counter() - self.started) * 1000
        return text

    async def find_resource_template(self, name):
        event = self.record("discovery", name, "find_resource_template")
        event["completed_ms"] = (perf_counter() - self.started) * 1000
        if name != "get_incident_runbook":
            raise ValueError("알 수 없는 런북 템플릿")
        return "ops", "runbook://incident-response/{incident_type}"

    async def call_tool(self, name, arguments, **kwargs):
        system = arguments["system_name"]
        event = self.record("monitoring", system, name)
        if name != "get_monitoring_status" or system not in self.snapshots:
            raise ValueError("미등록 모니터링 조회")
        result = deepcopy(self.snapshots[system])
        event["completed_ms"] = (perf_counter() - self.started) * 1000
        return result

    async def read_resource(self, uri, server=None):
        target = uri.removeprefix("runbook://incident-response/")
        event = self.record("runbook", target, "read_resource")
        if uri != "runbook://incident-response/" + target or target not in load_catalog().incident_types:
            raise ValueError("미등록 런북 조회")
        text = (ROOT / "data/runbooks" / (target + ".md")).read_text()
        event["completed_ms"] = (perf_counter() - self.started) * 1000
        return text

    @property
    def domain_calls(self):
        return [{"source": item["source"], "target": item["target"]}
                for item in self.events if item["source"] in ("monitoring", "runbook")]


# [추가] 하나라도 사용량이 누락되면 합계를 None으로 남기며 실제 청구 금액을 계산하지 않는다.
def usage_total(calls: list[dict]) -> dict:
    totals = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [item.get("usage", {}).get(key) for item in calls]
        totals[key] = None if any(value is None for value in values) else sum(values)
    measurements = {item.get("usage", {}).get("measurement", "unavailable") for item in calls}
    totals["measurement"] = "estimated" if "estimated" in measurements else "unavailable" if "unavailable" in measurements else "reported" if calls else "no_calls"
    totals["model_calls"] = len(calls)
    return totals


# [추가] 기존 모델 메서드를 한 번 호출하고 실제 성공·실패·후보·사용량을 외부에서 기록한다.
class MeteredModel:
    def __init__(self, model, backend: str, calls: list[dict], context=None):
        self.model, self.backend, self.calls = model, backend, calls
        self.context = context if context is not None else {}

    def __getattr__(self, name):
        method = getattr(self.model, name)
        if name not in {"route", "plan", "analyze", "chat", "write", "compile_workflow", "write_workflow"}:
            return method

        def begin():
            entry = {**self.context, "method": name, "model": self.model.name, "usage": {}, "error": None}
            self.calls.append(entry)
            return entry, perf_counter()

        def finish(entry, value):
            if isinstance(value, tuple):
                raw = value[1]
                entry["usage"] = {key: raw.get(key) for key in ("input_tokens", "output_tokens", "total_tokens", "measurement")}
                if self.backend == "fake":
                    entry["usage"].update(total_tokens=raw.get("tokens"), measurement="estimated")
            else:
                entry["usage"] = value.usage.model_dump(mode="json")
                if name == "compile_workflow":
                    entry.update(response_content=value.content, complete=value.complete, refusal=value.refusal)

        if inspect.iscoroutinefunction(method):
            async def wrapped(*args, **kwargs):
                entry, started = begin()
                try:
                    value = await method(*args, **kwargs)
                    finish(entry, value)
                    return value
                except BaseException as error:
                    entry["error"] = safe_error(error)
                    raise
                finally:
                    entry["elapsed_ms"] = (perf_counter() - started) * 1000
            return wrapped

        def wrapped(*args, **kwargs):
            entry, started = begin()
            try:
                value = method(*args, **kwargs)
                finish(entry, value)
                return value
            except BaseException as error:
                entry["error"] = safe_error(error)
                raise
            finally:
                entry["elapsed_ms"] = (perf_counter() - started) * 1000
        return wrapped


# [추가] 연결·인증·모델 접근·요금 한도 오류가 나면 실제 평가의 후속 호출을 중단한다.
def fatal_provider_error(calls: list[dict]) -> dict | None:
    for call in calls:
        error = call.get("error") or {}
        if error.get("http_status") in (401, 403, 404, 429) or error.get("type") in ("ConnectError", "ProxyError", "ConnectTimeout"):
            return error
    return None


# [추가] 비용을 쓰지 않는 경로 비교 옵션에서만 baseline 보고 의존성을 명시적으로 대체한다.
class OmittedReport:
    name = "evaluation:report-omitted"

    async def write(self, *args, **kwargs):
        return "1) 이 실행은 조회 경로만 평가합니다. 2) 보고 생성은 생략했습니다. 3) 보고 품질·전체 사용량 평가에 포함하지 않습니다.", {"usd": 0.0}


# [추가] 원본 v2 그래프·게이트·재계획·병렬·고정 0.35초 지연을 그대로 실행한다.
async def run_baseline(query, snapshots, fast, quality, report_mode="full", timeout=120):
    mcp = RecordingFixtureMCP(snapshots)
    started = perf_counter()
    deps = SupervisorDeps(fast, quality if report_mode == "full" else OmittedReport(), mcp, None, "v2")
    try:
        async with asyncio.timeout(timeout):
            state = await build_graph(deps).ainvoke({"query": query, "thread_id": "evaluation", "history": [], "events": [], "cost_usd": 0.0})
        return {"status": "completed", "route": state.get("route"), "events": state.get("events", []),
                "final": state.get("final", "") if report_mode == "full" else None,
                "calls": mcp.domain_calls, "mcp_events": mcp.events, "elapsed_ms": (perf_counter() - started) * 1000}
    except Exception as error:
        return {"status": "failed", "error": safe_error(error), "calls": mcp.domain_calls,
                "mcp_events": mcp.events, "elapsed_ms": (perf_counter() - started) * 1000}


# [추가] 저장소에서 다시 읽은 불변 정의를 실행하며 반복 실행은 생성 모델을 다시 호출하지 않는다.
async def run_saved(record, query, snapshots, quality, report_mode="full", timeout=120):
    mcp = RecordingFixtureMCP(snapshots)
    started = perf_counter()
    if report_mode == "full":
        result = await run_workflow(record.definition, load_catalog(), mcp, quality, query=query, timeout_seconds=timeout, workflow_id=record.workflow_id)
        payload = result.model_dump(mode="json")
    else:
        result = await execute_workflow(record.definition, load_catalog(), mcp, timeout_seconds=timeout)
        payload = {"status": result.status, "execution": result.model_dump(mode="json"), "report_valid": None}
    return {"status": result.status, "result": payload, "calls": mcp.domain_calls,
            "mcp_events": mcp.events, "elapsed_ms": (perf_counter() - started) * 1000}


# [추가] 실제 컴파일러의 거절·복구·오류 코드를 보존하고 성공한 절차만 임시 DB에 저장한다.
async def generate_saved(query, fast, store: SqliteWorkflowStore, timeout=120):
    compiled = await compile_workflow(query, load_catalog(), fast, timeout_seconds=timeout)
    if not compiled.valid:
        return compiled, None
    record = await asyncio.to_thread(store.create_draft, query, compiled.workflow)
    record = await asyncio.to_thread(store.save, record.workflow_id, record.name)
    return compiled, await asyncio.to_thread(store.get, record.workflow_id)


# [추가] 직접 작성한 무효 AST 12개가 실제 실행기에서 도메인 조회 전에 차단되는지 측정한다.
async def run_guard_checks():
    base = {"schema_version": 1, "name": "정적 차단 검증", "steps": [
        {"id": "metrics", "type": "monitoring", "system": "payments-service"},
        {"id": "branch", "type": "if", "condition": {"ref": "metrics", "field": "cpu_percent", "op": ">=", "value": 80},
         "then_steps": [{"id": "cpu", "type": "runbook", "incident_type": "cpu-spike"}], "else_steps": []},
    ]}
    mutations = [
        ("unknown_system", lambda d: d["steps"][0].update(system="billing-service")),
        ("unknown_runbook", lambda d: d["steps"][1]["then_steps"][0].update(incident_type="missing-book")),
        ("future_reference", lambda d: (d["steps"][1]["condition"].update(ref="future"), d["steps"].append({"id": "future", "type": "monitoring", "system": "web-01"}))),
        ("duplicate_id", lambda d: d["steps"][1].update(id="metrics")),
        ("string_number", lambda d: d["steps"][1]["condition"].update(value="80")),
        ("boolean_number", lambda d: d["steps"][1]["condition"].update(value=True)),
        ("unknown_operator", lambda d: d["steps"][1]["condition"].update(op="contains")),
        ("unknown_field", lambda d: d["steps"][1]["condition"].update(field="disk_percent")),
        ("nested_if", lambda d: d["steps"][1]["then_steps"].append(deepcopy(d["steps"][1]))),
        ("seven_nodes", lambda d: d.update(steps=[{"id": f"node_{i}", "type": "monitoring", "system": "auth-service"} for i in range(7)])),
        ("both_empty", lambda d: d["steps"][1].update(then_steps=[])),
        ("noninteger_version", lambda d: d.update(schema_version=1.0)),
    ]
    results = []
    for name, mutate in mutations:
        payload = deepcopy(base)
        mutate(payload)
        mcp = RecordingFixtureMCP({})
        result = await execute_workflow(payload, load_catalog(), mcp)
        results.append({"id": name, "blocked": result.status == "failed" and bool(result.validation_errors) and not mcp.domain_calls,
                        "domain_calls": len(mcp.domain_calls), "errors": [error.model_dump(mode="json") for error in result.validation_errors]})
    return {"total": len(results), "blocked_before_mcp": sum(item["blocked"] for item in results), "cases": results}

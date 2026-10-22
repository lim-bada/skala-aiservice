# [추가] 진단 실행과 비동기 보고·별도 검증을 연결하는 신규 LangGraph 파일 전체.
"""기존 write/finalize 노드를 그래프 문맥에서 재사용하고 요청별 상태·deadline을 격리한다."""
import math
from contextlib import aclosing
from copy import deepcopy
from functools import partial
from time import monotonic
from typing import Any
from uuid import uuid4

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from agent.deps import SupervisorDeps
from agent.graph import AgentState, finalize_node, write_node

from .catalog import WorkflowCatalog
from .executor import WorkflowMCP, execute_workflow
from .identity import workflow_definition_hash
from .report_content import build_runbook_sources, parse_runbook_summary, render_execution_report
from .models import (
    MAX_WORKFLOW_QUERY_LENGTH,
    WorkflowDefinition,
    WorkflowExecutionIssue,
    WorkflowExecutionResult,
    WorkflowReportAttempt,
    WorkflowRunIssue,
    WorkflowRunResult,
    WorkflowTraceEvent,
)
from .reporting import (
    MAX_REPORT_ATTEMPTS,
    WorkflowReportBridge,
    WorkflowReportFailure,
    WorkflowReportLLM,
    validate_grounded_report,
    validate_report,
)
from .validator import validate_workflow

# [추가] 조회와 모든 보고 재작성이 공유하는 전체 제한을 한곳에서 정의한다.
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 120.0


# [추가] 공통 보고 노드의 상태에 실행 정의·조회 결과·보고 검증 상태를 확장한다.
class WorkflowGraphState(AgentState, total=False):
    payload: Any
    timeout_seconds: float
    started_at: float
    deadline: float
    run_id: str
    workflow_id: str | None
    definition: WorkflowDefinition | None
    definition_hash: str | None
    execution: WorkflowExecutionResult | None
    report_attempts: list[WorkflowReportAttempt]
    report_complete: bool
    report_valid: bool
    error: WorkflowRunIssue | None
    facts_only: bool
    facts_report: str
    result: WorkflowRunResult


# [추가] 잘못된 제한 설정은 모델·조회 실패로 숨기지 않고 호출자 오류로 알린다.
def _timeout_value(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")
    return result


# [추가] 진행 이벤트와 LangSmith 기록이 같은 실행·정의 식별자를 사용하게 한다.
def _event(state: WorkflowGraphState, kind: str, **data) -> dict:
    return {
        **data,
        "type": kind,
        "run_id": state["run_id"],
        "workflow_id": state.get("workflow_id"),
        "definition_hash": state.get("definition_hash"),
        "elapsed_ms": (monotonic() - state["started_at"]) * 1000,
    }


# [추가] 재사용한 공통 노드에는 필요한 보고 의존성만 넣고 기존 감독자 경로는 변경하지 않는다.
def _legacy_deps(quality_llm=None) -> SupervisorDeps:
    return SupervisorDeps(
        fast_llm=None, quality_llm=quality_llm, mcp=None,
        session_store=None, version="workflow-v1",
    )


# [추가] 모든 실행 시작에서 상태를 초기화해 호출자가 이전 보고 상태를 주입하지 못하게 한다.
@traceable(name="workflow.prepare")
async def _prepare(state: WorkflowGraphState, catalog: WorkflowCatalog) -> dict:
    started_at = state.get("started_at", monotonic())
    timeout = _timeout_value(state.get("timeout_seconds", DEFAULT_WORKFLOW_TIMEOUT_SECONDS))
    update = {
        "started_at": started_at, "deadline": started_at + timeout,
        "run_id": state.get("run_id") or str(uuid4()),
        "definition": None, "definition_hash": None, "execution": None,
        "report_attempts": [], "report_valid": False, "report_complete": False,
        "error": None, "draft": "", "final": "", "collected": [], "events": [],
        "cost_usd": 0.0, "validation_attempts": 0, "validation_feedback": None,
        "facts_only": False,
        "facts_report": "",
    }
    query = state.get("query")
    valid_query = isinstance(query, str) and 0 < len(query) <= MAX_WORKFLOW_QUERY_LENGTH
    if valid_query:
        try:
            query.encode("utf-8")
            valid_query = bool(query.strip())
        except UnicodeEncodeError:
            valid_query = False
    if not valid_query:
        update["error"] = WorkflowRunIssue(
            code="INVALID_QUERY", stage="input",
            message=f"보고 요청은 비어 있지 않은 유효한 문자열이며 {MAX_WORKFLOW_QUERY_LENGTH}자 이하여야 합니다.",
        )
    else:
        update["query"] = query.strip()
        validation = validate_workflow(state.get("payload"), catalog)
        if validation.valid:
            update["definition"] = validation.workflow
            update["definition_hash"] = workflow_definition_hash(validation.workflow)
        else:
            update["execution"] = WorkflowExecutionResult(
                status="failed", elapsed_ms=0.0, validation_errors=validation.errors,
                error=WorkflowExecutionIssue(
                    code="VALIDATION_FAILED", path="$", message="실행 정의의 정적 검증에 실패했습니다."
                ),
            )
            update["error"] = WorkflowRunIssue(
                code="EXECUTION_FAILED", stage="execution", message="실행 정의의 정적 검증에 실패했습니다."
            )
    current = {**state, **update}
    event = _event(current, "WorkflowStarted")
    get_stream_writer()(event)
    update["events"] = [event]
    return update


# [추가] 실행 trace를 도구 응답 대기 중에도 custom 이벤트로 중계한다.
@traceable(name="workflow.execute")
async def _execute(state: WorkflowGraphState, mcp: WorkflowMCP, catalog: WorkflowCatalog) -> dict:
    writer = get_stream_writer()
    events = list(state["events"])

    def on_event(trace: WorkflowTraceEvent) -> None:
        kind = {
            "started": "StepStarted", "completed": "StepCompleted",
            "skipped": "StepSkipped", "failed": "StepFailed",
        }[trace.status]
        if trace.node_type == "if" and trace.status == "completed":
            kind = "ConditionEvaluated"
        event = _event(state, kind, **trace.model_dump(mode="json"))
        events.append(deepcopy(event))
        writer(event)

    remaining = state["deadline"] - monotonic()
    if remaining <= 0:
        execution = WorkflowExecutionResult(
            status="failed", elapsed_ms=0.0,
            error=WorkflowExecutionIssue(
                code="EXECUTION_TIMEOUT", path="$", message="조회 전에 전체 실행 시간이 만료됐습니다."
            ),
        )
    else:
        execution = await execute_workflow(
            state["definition"], catalog, mcp, timeout_seconds=remaining, on_event=on_event,
        )
    issue = None
    if execution.status == "failed":
        issue = WorkflowRunIssue(
            code="WORKFLOW_TIMEOUT" if execution.error.code == "EXECUTION_TIMEOUT" else "EXECUTION_FAILED",
            stage="execution", message=execution.error.message,
        )
    return {
        "execution": execution, "collected": execution.to_collected(),
        "events": events, "error": issue,
        # [추가] 조회가 성공한 사실 보고는 모델 오류·거절·요약 반려가 나더라도 별도로 보존한다.
        "facts_report": render_execution_report(state["definition"], execution) if issue is None else "",
    }


# [추가] 공통 write_node를 실제 그래프 문맥에서 호출하고 시도·사용량을 별도로 보존한다.
@traceable(name="workflow.write")
async def _write(state: WorkflowGraphState, report_llm: WorkflowReportLLM) -> dict:
    writer = get_stream_writer()
    # [추가] 런북을 읽지 않은 경로는 모델 호출·가상 사용량 없이 실행 사실만으로 완료한다.
    if not build_runbook_sources(state["collected"]):
        draft = state["facts_report"]
        event = _event(state, "DraftWritten", attempt=0, length=len(draft), source="execution")
        writer(event)
        return {"draft": draft, "facts_only": True, "report_complete": True, "events": state["events"] + [event]}
    attempts = [item.model_copy(deep=True) for item in state["report_attempts"]]
    attempt = WorkflowReportAttempt(number=len(attempts) + 1)
    attempts.append(attempt)
    started = _event(state, "ReportStarted", attempt=attempt.number)
    writer(started)
    events = state["events"] + [started]

    bridge = WorkflowReportBridge(
        report_llm,
        [event.model_dump(mode="json") for event in state["execution"].trace],
        # [추가] 모델의 미검증 JSON은 답변 조각으로 노출하지 않고 검증 후 서버 보고만 중계한다.
        state["deadline"], on_token=lambda piece: None,
    )
    try:
        update = await write_node({**state, "events": events}, _legacy_deps(bridge))
    except WorkflowReportFailure as error:
        if bridge.response is not None:
            attempt.usage = bridge.response.usage
        attempt.errors = [error.issue.message]
        return {"error": error.issue, "report_attempts": attempts, "events": events}
    attempt.usage = bridge.response.usage
    # [추가] 동기 응답 처리 직후에도 deadline을 확인해 기한이 지난 초안을 승인하지 않는다.
    if monotonic() >= state["deadline"]:
        issue = WorkflowRunIssue(
            code="WORKFLOW_TIMEOUT", stage="report", message="조회와 보고를 합친 실행 제한 시간을 초과했습니다."
        )
        attempt.errors = [issue.message]
        return {
            "draft": update["draft"], "error": issue,
            "report_attempts": attempts, "events": events,
        }
    # [추가] 공통 노드의 DraftWritten에도 워크플로 식별자를 붙여 저장·전달한다.
    written = _event(state, "DraftWritten", attempt=attempt.number, length=len(update["draft"]))
    writer(written)
    update["events"] = events + [written]
    return {
        **update, "report_attempts": attempts,
        "report_complete": bridge.response.complete,
    }


# [추가] 실패한 초안은 최대 세 번까지 재작성하고 끝까지 실패하면 명시적으로 중단한다.
@traceable(name="workflow.validate_report")
async def _validate(state: WorkflowGraphState) -> dict:
    draft = state["draft"]
    errors = []
    # [추가] 실제 문서·문단 참조를 검사한 뒤 실행 사실과 결합한다. 인용 원문은 조치 수행 주장이 아니다.
    if not state["facts_only"]:
        summary, errors = parse_runbook_summary(draft, build_runbook_sources(state["collected"]))
        if summary is not None:
            errors.extend(validate_grounded_report(
                "\n".join(item.summary for item in summary.summaries), state["collected"], [],
            ))
            if not errors:
                draft = render_execution_report(state["definition"], state["execution"], summary)
    if not errors:
        errors.extend(validate_report(draft, complete=state["report_complete"]))
    attempts = [item.model_copy(deep=True) for item in state["report_attempts"]]
    if attempts:
        attempts[-1].errors = errors
    issue = None
    # [추가] 코드 생성 보고의 출력 오류는 모델 재작성 루프에 넣지 않는다.
    if errors and state["facts_only"]:
        issue = WorkflowRunIssue(code="REPORT_ERROR", stage="report", message="실행 사실 보고의 출력 검증에 실패했습니다.")
    elif errors and len(attempts) >= MAX_REPORT_ATTEMPTS:
        issue = WorkflowRunIssue(
            code="REPORT_VALIDATION_FAILED", stage="report",
            message="세 번의 작성 후에도 런북 요약의 구조·자료 참조·실행 근거 검증을 통과하지 못했습니다.",
        )
    if monotonic() >= state["deadline"]:
        issue = WorkflowRunIssue(
            code="WORKFLOW_TIMEOUT", stage="report", message="보고 검증 중 전체 실행 시간이 만료됐습니다."
        )
    valid = not errors and issue is None
    event = _event(state, "ReportValidation", attempt=len(attempts), valid=valid, errors=errors)
    get_stream_writer()(event)
    # [추가] 검증을 통과한 서버 생성 보고만 사용자에게 전달한다. 반려된 모델 원문은 중계하지 않는다.
    if valid:
        get_stream_writer()(_event(state, "AnswerToken", attempt=len(attempts), delta=draft))
    return {
        "draft": draft,
        "report_valid": valid, "error": issue, "report_attempts": attempts,
        "validation_attempts": len(attempts), "validation_feedback": " ".join(errors) or None,
        "events": state["events"] + [event],
    }


# [추가] 하나라도 미보고된 시도가 있으면 보고 사용량 합계도 미확인으로 유지한다.
def _report_tokens(attempts: list[WorkflowReportAttempt], field: str) -> int | None:
    values = [getattr(item.usage, field) if item.usage else None for item in attempts]
    return None if any(value is None for value in values) else sum(values)


# [추가] 성공일 때만 기존 finalize_node를 사용하고 실패 초안은 확정 답변에서 제외한다.
@traceable(name="workflow.finalize")
async def _finalize(state: WorkflowGraphState) -> dict:
    issue = state["error"]
    successful = issue is None and state["report_valid"]
    # [추가] 검증 이후 스케줄링이 지연됐더라도 기한이 지난 보고를 확정하지 않는다.
    if successful and monotonic() >= state["deadline"]:
        issue = WorkflowRunIssue(
            code="WORKFLOW_TIMEOUT", stage="report", message="최종 보고 확정 전에 전체 실행 시간이 만료됐습니다."
        )
        successful = False
    if successful:
        finalized = await finalize_node(state, _legacy_deps())
        final = finalized["final"]
        # [추가] 공통 비동기 노드에서 돌아온 뒤에도 같은 deadline으로 승인 여부를 결정한다.
        if monotonic() >= state["deadline"]:
            issue = WorkflowRunIssue(
                code="WORKFLOW_TIMEOUT", stage="report", message="최종 보고 확정 중 전체 실행 시간이 만료됐습니다."
            )
            successful = False
    if successful:
        terminal = _event(state, "FinalAnswer", status="completed", report_valid=True, final=final)
    else:
        final = ""
        terminal = _event(
            state, "WorkflowFailed", status="failed", report_valid=False,
            error=issue.model_dump(mode="json"),
        )
    events = state["events"] + [terminal]
    result = WorkflowRunResult(
        status="completed" if successful else "failed", run_id=state["run_id"],
        definition=state["definition"], definition_hash=state["definition_hash"],
        execution=state["execution"], report_valid=successful, final=final, draft=state["draft"],
        facts_report=state["facts_report"],
        report_attempts=state["report_attempts"], error=issue,
        input_tokens=_report_tokens(state["report_attempts"], "input_tokens"),
        output_tokens=_report_tokens(state["report_attempts"], "output_tokens"),
        total_tokens=_report_tokens(state["report_attempts"], "total_tokens"),
        elapsed_ms=(monotonic() - state["started_at"]) * 1000, events=events,
    )
    get_stream_writer()(terminal)
    return {
        "result": result, "final": final, "events": events,
        "error": issue, "report_valid": successful,
    }


# [추가] 고정 실행 경로만 연결하며 재계획·추가 조회·체크포인트를 도입하지 않는다.
def build_workflow_graph(catalog: WorkflowCatalog, mcp: WorkflowMCP, report_llm: WorkflowReportLLM):
    graph = StateGraph(WorkflowGraphState)
    graph.add_node("prepare", partial(_prepare, catalog=catalog))
    graph.add_node("execute", partial(_execute, mcp=mcp, catalog=catalog))
    graph.add_node("write", partial(_write, report_llm=report_llm))
    graph.add_node("validate_report", _validate)
    graph.add_node("finalize", _finalize)
    graph.set_entry_point("prepare")
    graph.add_conditional_edges("prepare", lambda s: "finalize" if s["error"] else "execute")
    graph.add_conditional_edges("execute", lambda s: "finalize" if s["error"] else "write")
    graph.add_conditional_edges("write", lambda s: "finalize" if s["error"] else "validate_report")
    graph.add_conditional_edges(
        "validate_report", lambda s: "finalize" if s["error"] or s["report_valid"] else "write"
    )
    graph.add_edge("finalize", END)
    return graph.compile(name="conditional-diagnosis-workflow")


# [추가] JSON과 스트림 실행이 같은 정의 스냅샷·deadline·추적 메타데이터를 사용한다.
def _run_inputs(
    payload: object, catalog: WorkflowCatalog, query: object,
    timeout_seconds: float, workflow_id: str | None,
) -> tuple[dict, dict]:
    timeout = _timeout_value(timeout_seconds)
    started_at = monotonic()
    run_id = str(uuid4())
    # [추가] 비동기 실행 전에 독립된 정의를 확보하고 동일 해시를 추적 메타데이터에도 넣는다.
    validation = validate_workflow(payload, catalog)
    definition = validation.workflow if validation.valid else None
    definition_hash = workflow_definition_hash(definition) if definition else None
    metadata = {
        "run_id": run_id, "workflow_id": workflow_id, "definition_hash": definition_hash,
    }
    # [추가] 그래프 자식뿐 아니라 workflow.run 추적 자체에도 동일한 실행 식별자를 남긴다.
    current_run = get_current_run_tree()
    if current_run is not None and current_run.name in ("workflow.run", "workflow.stream"):
        current_run.metadata.update(metadata)
    return {
        "payload": definition if definition else payload, "query": query,
        "timeout_seconds": timeout, "started_at": started_at,
        "run_id": run_id, "workflow_id": workflow_id,
    }, metadata


# [추가] 서비스가 호출할 단일 진입점에서 실행 ID와 추적 메타데이터를 준비한다.
@traceable(name="workflow.run")
async def run_workflow(
    payload: object, catalog: WorkflowCatalog, mcp: WorkflowMCP, report_llm: WorkflowReportLLM,
    *, query: object, timeout_seconds: float = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    workflow_id: str | None = None,
) -> WorkflowRunResult:
    inputs, metadata = _run_inputs(payload, catalog, query, timeout_seconds, workflow_id)
    graph = build_workflow_graph(catalog, mcp, report_llm)
    state = await graph.ainvoke(
        inputs,
        config={"metadata": metadata},
    )
    return state["result"]


# [추가] 실행을 한 번만 시작하고 마지막 이벤트에 확정된 전체 결과를 포함한다.
@traceable(name="workflow.stream")
async def stream_workflow(
    payload: object, catalog: WorkflowCatalog, mcp: WorkflowMCP, report_llm: WorkflowReportLLM,
    *, query: object, timeout_seconds: float = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    workflow_id: str | None = None,
):
    inputs, metadata = _run_inputs(payload, catalog, query, timeout_seconds, workflow_id)
    graph = build_workflow_graph(catalog, mcp, report_llm)
    # [추가] 소비자 연결 종료 시 그래프 스트림도 닫아 대기 중 조회·보고를 정리한다.
    async with aclosing(graph.astream(
        inputs, config={"metadata": metadata}, stream_mode=["custom", "updates"],
    )) as updates:
        async for mode, payload in updates:
            if mode == "custom":
                # [추가] 최종 노드 update에서 결과와 함께 한 번만 보내 중복 종료 이벤트를 막는다.
                if payload.get("type") not in ("FinalAnswer", "WorkflowFailed"):
                    yield deepcopy(payload)
                continue
            for delta in payload.values():
                if isinstance(delta, dict) and "result" in delta:
                    result = WorkflowRunResult.model_validate(delta["result"])
                    yield {
                        **deepcopy(result.events[-1]),
                        "result": result.model_dump(mode="json"),
                    }

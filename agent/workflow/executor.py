# [추가] 검증된 진단 절차의 순차·조건부 실행과 trace를 담당하는 신규 파일 전체.
"""LLM 없이 실행하며, 실제 MCPClient와 같은 인터페이스의 fake를 주입할 수 있다."""
import asyncio
import math
import operator
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Protocol

from pydantic import ValidationError

from .catalog import WorkflowCatalog
from .models import (
    ActionStep,
    IfStep,
    MonitoringSnapshot,
    MonitoringStep,
    WorkflowActionResult,
    WorkflowConditionResult,
    WorkflowDefinition,
    WorkflowExecutionCode,
    WorkflowExecutionIssue,
    WorkflowExecutionResult,
    WorkflowStep,
    WorkflowTraceEvent,
)
from .validator import validate_workflow

# [추가] 모델이 호출 이름이나 URI를 정하지 못하도록 현재 허용된 MCP 동작을 고정한다.
MONITORING_TOOL_NAME = "get_monitoring_status"
RUNBOOK_TEMPLATE_NAME = "get_incident_runbook"
RUNBOOK_URI_TEMPLATE = "runbook://incident-response/{incident_type}"
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 120.0
_COMPARATORS = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


# [추가] 기존 MCPClient를 변경하지 않고 실제 클라이언트와 테스트 fake를 함께 받는다.
class WorkflowMCP(Protocol):
    async def call_tool(
        self, name: str, arguments: dict, server: str = "ops"
    ) -> dict: ...

    async def find_resource_template(self, template_name: str) -> tuple[str, str]: ...

    async def read_resource(self, uri: str, server: str) -> str: ...


# [추가] 시간 초과를 포함한 실패를 현재 노드와 원본 JSON 위치에 연결한다.
@dataclass(frozen=True)
class _ActiveStep:
    step: WorkflowStep
    path: str


# [추가] 실행 실패를 제어 흐름으로 전달하면서 완료 자료는 실행 컨텍스트에 보존한다.
class _ExecutionFailure(Exception):
    def __init__(self, issue: WorkflowExecutionIssue):
        super().__init__(issue.message)
        self.issue = issue


# [추가] 요청별 실행 상태를 격리한다. 결과와 템플릿 캐시를 다른 실행과 공유하지 않는다.
class _WorkflowRunner:
    def __init__(
        self, mcp: WorkflowMCP, started_at: float,
        on_event: Callable[[WorkflowTraceEvent], None] | None = None,
    ):
        self.mcp = mcp
        self.started_at = started_at
        self.results: list[WorkflowActionResult] = []
        self.trace: list[WorkflowTraceEvent] = []
        self.monitoring: dict[str, MonitoringSnapshot] = {}
        self.runbook_location: tuple[str, str] | None = None
        self.active: _ActiveStep | None = None
        # [추가] 조회가 끝나기 전에도 LangGraph custom 스트림으로 진행 상황을 전달한다.
        self.on_event = on_event

    # [추가] 이벤트와 최종 결과에 같은 단조 시계 기준의 경과 시간을 사용한다.
    def elapsed_ms(self) -> float:
        return (monotonic() - self.started_at) * 1000

    # [추가] 모든 이벤트에 증가하는 순번과 현재 실행 위치를 남긴다.
    def emit(
        self,
        location: _ActiveStep,
        status: str,
        *,
        condition: WorkflowConditionResult | None = None,
        error: WorkflowExecutionIssue | None = None,
    ) -> None:
        self.trace.append(WorkflowTraceEvent(
            sequence=len(self.trace) + 1,
            node_id=location.step.id,
            node_type=location.step.type,
            path=location.path,
            status=status,
            elapsed_ms=self.elapsed_ms(),
            condition=condition,
            error=error,
        ))
        # [추가] 구독자가 받은 이벤트를 바꾸더라도 내부 trace는 영향을 받지 않게 한다.
        if self.on_event is not None:
            self.on_event(self.trace[-1].model_copy(deep=True))

    # [추가] 외부 응답 원문을 노출하지 않고 실패 종류와 노드 위치를 반환한다.
    def failure(
        self, code: WorkflowExecutionCode, message: str
    ) -> _ExecutionFailure:
        return _ExecutionFailure(WorkflowExecutionIssue(
            code=code,
            path=self.active.path if self.active else "$",
            node_id=self.active.step.id if self.active else None,
            message=message,
        ))

    # [추가] 정적 검증 후에도 실제 MCP 응답의 타입과 요청 대상 일치를 검사한다.
    async def read_monitoring(self, step: MonitoringStep) -> MonitoringSnapshot:
        try:
            raw = await self.mcp.call_tool(
                MONITORING_TOOL_NAME, {"system_name": step.system}
            )
        except Exception as error:
            raise self.failure(
                "MCP_ERROR", f"모니터링 MCP 조회에 실패했습니다 ({type(error).__name__})."
            ) from error
        try:
            snapshot = MonitoringSnapshot.model_validate(raw)
        except ValidationError as error:
            raise self.failure(
                "INVALID_MONITORING_RESULT",
                "모니터링 응답의 필수 필드·타입·상태를 확인하세요. 숫자는 유한한 값이어야 합니다.",
            ) from error
        if snapshot.system_name != step.system:
            raise self.failure(
                "INVALID_MONITORING_RESULT", "모니터링 응답이 요청한 시스템과 다릅니다."
            )
        return snapshot

    # [추가] 선택된 런북을 읽을 때만 서버를 탐색하고 탐색 결과는 현재 실행에서 재사용한다.
    async def read_runbook(self, incident_type: str) -> str:
        try:
            if self.runbook_location is None:
                server, template = await self.mcp.find_resource_template(RUNBOOK_TEMPLATE_NAME)
                if not isinstance(server, str) or not server or template != RUNBOOK_URI_TEMPLATE:
                    raise ValueError("허용된 런북 Resource 템플릿이 아닙니다.")
                self.runbook_location = (server, template)
            server, template = self.runbook_location
            body = await self.mcp.read_resource(
                template.format(incident_type=incident_type), server=server
            )
        except Exception as error:
            raise self.failure(
                "MCP_ERROR", f"런북 MCP 조회에 실패했습니다 ({type(error).__name__})."
            ) from error
        if not isinstance(body, str) or not body.strip():
            raise self.failure("INVALID_RUNBOOK_RESULT", "런북 응답은 비어 있지 않은 본문이어야 합니다.")
        # [추가] 기존 서버가 예외 대신 반환하는 '문서 없음' 응답도 실행 실패로 처리한다.
        if body.lstrip().splitlines()[0].strip().casefold() == "# runbook not found":
            raise self.failure("RUNBOOK_NOT_FOUND", "요청한 런북 문서가 MCP 서버에 없습니다.")
        return body

    # [추가] 조회가 성공하고 응답 검증이 끝난 경우에만 자료를 저장한다.
    async def run_action(self, step: ActionStep, path: str) -> None:
        self.active = _ActiveStep(step, path)
        self.emit(self.active, "started")
        if isinstance(step, MonitoringStep):
            data = await self.read_monitoring(step)
            self.monitoring[step.id] = data
        else:
            data = await self.read_runbook(step.incident_type)
        self.results.append(WorkflowActionResult(node_id=step.id, source=step.type, data=data))
        self.emit(self.active, "completed")
        self.active = None

    # [추가] 고정 비교 함수로 조건을 계산하며 누락·타입 오류를 false로 바꾸지 않는다.
    def evaluate_condition(self, step: IfStep) -> WorkflowConditionResult:
        condition = step.condition
        try:
            snapshot = self.monitoring[condition.ref]
            actual = getattr(snapshot, condition.field)
            matched = _COMPARATORS[condition.op](actual, condition.value)
            return WorkflowConditionResult(
                ref=condition.ref,
                field=condition.field,
                op=condition.op,
                expected=condition.value,
                actual=actual,
                result=matched,
                selected_branch="then_steps" if matched else "else_steps",
            )
        except (KeyError, AttributeError, TypeError, ValidationError) as error:
            raise self.failure("CONDITION_ERROR", "검증된 모니터링 결과로 조건을 계산할 수 없습니다.") from error

    # [추가] 조건 판단과 미선택 분기를 기록한 뒤 선택된 조회만 순서대로 실행한다.
    async def run_condition(self, step: IfStep, path: str) -> None:
        self.active = _ActiveStep(step, path)
        self.emit(self.active, "started")
        decision = self.evaluate_condition(step)
        self.emit(self.active, "completed", condition=decision)
        self.active = None
        skipped_branch = "else_steps" if decision.result else "then_steps"
        for index, action in enumerate(getattr(step, skipped_branch)):
            self.emit(_ActiveStep(action, f"{path}.{skipped_branch}[{index}]"), "skipped")
        for index, action in enumerate(getattr(step, decision.selected_branch)):
            await self.run_action(action, f"{path}.{decision.selected_branch}[{index}]")

    # [추가] 최상위 순차 실행을 유지하고 실패하면 예외를 통해 이후 노드 실행을 멈춘다.
    async def run(self, workflow: WorkflowDefinition) -> None:
        for index, step in enumerate(workflow.steps):
            path = f"steps[{index}]"
            if isinstance(step, IfStep):
                await self.run_condition(step, path)
            else:
                await self.run_action(step, path)

    # [추가] 완료된 자료와 trace를 복사해 외부 호출자에게 반환한다.
    def result(self, issue: WorkflowExecutionIssue | None = None) -> WorkflowExecutionResult:
        if issue is not None and self.active is not None:
            self.emit(self.active, "failed", error=issue)
        return WorkflowExecutionResult(
            status="failed" if issue else "completed",
            results=self.results,
            trace=self.trace,
            error=issue,
            elapsed_ms=self.elapsed_ms(),
        )


# [추가] 실행 직전 재검증하고 전체 조회에 하나의 비동기 시간 제한을 적용한다.
async def execute_workflow(
    payload: object,
    catalog: WorkflowCatalog,
    mcp: WorkflowMCP,
    *,
    timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    on_event: Callable[[WorkflowTraceEvent], None] | None = None,
) -> WorkflowExecutionResult:
    """검증·조회만 수행한다. 보고 생성·저장·재시도·사용자 취소 API는 포함하지 않는다.

    주입한 MCP 메서드는 비동기여야 한다. 외부 Task 취소는 전파하며, 호출별 오류는
    실패 결과로 반환한다. 후속 서비스는 조회와 보고를 합친 하나의 deadline을 둔다.
    """
    # [추가] 잘못된 제한 설정을 작업 실패로 숨기지 않고 호출자 오류로 구분한다.
    if type(timeout_seconds) not in (int, float):
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")
    try:
        timeout_value = float(timeout_seconds)
    except OverflowError as error:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.") from error
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")

    started_at = monotonic()
    validation = validate_workflow(payload, catalog)
    if not validation.valid:
        return WorkflowExecutionResult(
            status="failed",
            error=WorkflowExecutionIssue(
                code="VALIDATION_FAILED", path="$", message="워크플로 정적 검증에 실패했습니다."
            ),
            validation_errors=validation.errors,
            elapsed_ms=(monotonic() - started_at) * 1000,
        )

    runner = _WorkflowRunner(mcp, started_at, on_event)
    remaining = timeout_value - (monotonic() - started_at)
    if remaining <= 0:
        return runner.result(runner.failure("EXECUTION_TIMEOUT", "워크플로 실행 제한 시간을 초과했습니다.").issue)
    try:
        async with asyncio.timeout(remaining):
            await runner.run(validation.workflow)
    except _ExecutionFailure as failure:
        return runner.result(failure.issue)
    except TimeoutError:
        return runner.result(runner.failure("EXECUTION_TIMEOUT", "워크플로 실행 제한 시간을 초과했습니다.").issue)
    return runner.result()

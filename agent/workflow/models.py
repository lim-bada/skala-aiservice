# [추가] 조건부 진단 워크플로의 데이터 계약을 정의하는 신규 파일 전체.
"""워크플로 v1의 JSON 구조와 타입 계약.

순차 실행과 최상위 if/else만 표현한다. 자원 존재 여부와 참조의 실행 가능성은
이 모델과 별개인 정적 검증기에서 검사해야 한다.
"""
from typing import Annotated, Iterator, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)

# [추가] 노드 수·이름 길이·식별자 형식을 한곳에서 제한한다.
MAX_WORKFLOW_NODES = 6
MAX_WORKFLOW_NAME_LENGTH = 80
MAX_RESOURCE_NAME_LENGTH = 128
# [추가] 모델이 반환한 JSON 원문의 UTF-8 바이트 수를 제한한다.
MAX_WORKFLOW_JSON_BYTES = 16 * 1024
# [추가] 생성 요청의 원문 길이를 모델 호출 전에 제한한다.
MAX_WORKFLOW_QUERY_LENGTH = 2000
NODE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
RESOURCE_NAME_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"

# [추가] 모델과 카탈로그가 같은 필드·연산자·상태 허용 목록을 공유한다.
NumericField: TypeAlias = Literal["cpu_percent", "memory_percent"]
NumericOperator: TypeAlias = Literal[">", ">=", "<", "<=", "==", "!="]
StatusOperator: TypeAlias = Literal["==", "!="]
StatusValue: TypeAlias = Literal["healthy", "degraded", "down"]

# [추가] 노드 ID와 자원 이름에 경로·공백 등 허용하지 않은 형식이 들어오는 것을 막는다.
NodeId: TypeAlias = Annotated[
    str, StringConstraints(strict=True, pattern=NODE_ID_PATTERN)
]
ResourceName: TypeAlias = Annotated[
    str,
    StringConstraints(
        strict=True,
        max_length=MAX_RESOURCE_NAME_LENGTH,
        pattern=RESOURCE_NAME_PATTERN,
    ),
]


# [추가] 문자열이나 bool을 숫자로 바꾸지 않고 실제 JSON 숫자만 조건값으로 받는다.
def _require_json_number(value: object) -> object:
    # bool은 int의 하위 타입이다. 숫자처럼 변환하지 않고 JSON 숫자만 받는다.
    if type(value) not in (int, float):
        raise ValueError("조건 값은 문자열·불리언이 아닌 숫자여야 합니다.")
    return value


# [추가] 스키마 버전에서 True·1.0이 정수 1과 같은 값으로 처리되는 것을 막는다.
def _require_integer_version(value: object) -> object:
    # Literal[1]만으로는 True나 1.0도 1로 받아들일 수 있다.
    if type(value) is not int:
        raise ValueError("schema_version은 정수여야 합니다.")
    return value


# [추가] Pydantic의 타입 검사 전에 위의 엄격한 입력 검사를 수행한다.
NumericValue: TypeAlias = Annotated[
    StrictInt | StrictFloat, BeforeValidator(_require_json_number)
]
SchemaVersion: TypeAlias = Annotated[
    Literal[1], BeforeValidator(_require_integer_version)
]


# [추가] 모든 워크플로 모델에 공통 검증 정책을 적용한다.
class WorkflowModel(BaseModel):
    """암묵적 타입 변환·추가 필드·비유한 숫자를 허용하지 않는 기본 모델."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        allow_inf_nan=False,
        # [추가] 한 번 생성된 모델도 재검증해 이후 변경된 잘못된 값이 통과하지 않게 한다.
        revalidate_instances="always",
    )


# [추가] CPU·메모리 등 숫자 지표의 비교 조건을 표현한다.
class NumericCondition(WorkflowModel):
    ref: NodeId = Field(description="조건보다 앞에 있는 최상위 모니터링 노드 ID")
    field: NumericField
    op: NumericOperator
    value: NumericValue


# [추가] 상태 문자열에는 동등·비동등 비교만 허용한다.
class StatusCondition(WorkflowModel):
    ref: NodeId = Field(description="조건보다 앞에 있는 최상위 모니터링 노드 ID")
    field: Literal["status"]
    op: StatusOperator
    value: StatusValue


# [추가] field 값으로 숫자 조건과 상태 조건의 검증 모델을 선택한다.
Condition: TypeAlias = Annotated[
    NumericCondition | StatusCondition,
    Field(discriminator="field"),
]


# [추가] 시스템 모니터링 조회를 표현한다. 실제 등록 여부는 다음 정적 검증기에서 검사한다.
class MonitoringStep(WorkflowModel):
    id: NodeId
    type: Literal["monitoring"]
    system: ResourceName = Field(description="카탈로그에 등록된 시스템 이름")


# [추가] 장애 유형에 대응하는 런북 조회를 표현한다.
class RunbookStep(WorkflowModel):
    id: NodeId
    type: Literal["runbook"]
    incident_type: ResourceName = Field(description="카탈로그에 등록된 런북 유형")


# [추가] 조건의 분기 내부에는 조회 작업만 허용해 중첩 조건을 차단한다.
ActionStep: TypeAlias = Annotated[
    MonitoringStep | RunbookStep,
    Field(discriminator="type"),
]


# [추가] 조건식과 참·거짓 분기의 조회 작업을 하나의 노드로 묶는다.
class IfStep(WorkflowModel):
    id: NodeId
    type: Literal["if"]
    condition: Condition
    # 분기에는 조회 노드만 허용해 중첩 if와 재귀 실행 구조를 막는다.
    then_steps: list[ActionStep] = Field(max_length=MAX_WORKFLOW_NODES)
    else_steps: list[ActionStep] = Field(max_length=MAX_WORKFLOW_NODES)

    # [추가] 한쪽 빈 분기는 허용하되 양쪽 모두 비어 있는 무의미한 조건은 거부한다.
    @model_validator(mode="after")
    def require_action(self) -> "IfStep":
        if not self.then_steps and not self.else_steps:
            raise ValueError("조건의 두 분기를 모두 비울 수 없습니다.")
        return self


# [추가] 최상위 순차 목록은 조회 또는 조건 노드를 받을 수 있다.
WorkflowStep: TypeAlias = Annotated[
    MonitoringStep | RunbookStep | IfStep,
    Field(discriminator="type"),
]


# [추가] 절차 전체의 버전·이름·단계 목록을 정의하고 크기를 제한한다.
class WorkflowDefinition(WorkflowModel):
    """구조 검사가 끝난 절차. 실행 전 카탈로그·ID·참조 검증은 별도로 필요하다."""

    schema_version: SchemaVersion
    name: Annotated[
        str,
        StringConstraints(
            strict=True,
            strip_whitespace=True,
            min_length=1,
            max_length=MAX_WORKFLOW_NAME_LENGTH,
        ),
    ]
    steps: list[WorkflowStep] = Field(
        min_length=1,
        max_length=MAX_WORKFLOW_NODES,
    )

    # [추가] 정적 검사를 위해 실제 실행 여부와 무관하게 양쪽 분기의 노드를 방문한다.
    def iter_steps(self) -> Iterator[WorkflowStep]:
        """실행 여부와 무관하게 양쪽 분기를 포함한 모든 노드를 방문한다."""
        for step in self.steps:
            yield step
            if isinstance(step, IfStep):
                yield from step.then_steps
                yield from step.else_steps

    # [추가] if 자체와 미선택 분기까지 포함한 전체 노드 수를 계산한다.
    @property
    def node_count(self) -> int:
        return sum(1 for _ in self.iter_steps())

    # [추가] 최상위 목록 길이만 검사해서 분기 내부 노드를 빠뜨리는 것을 방지한다.
    @model_validator(mode="after")
    def enforce_node_limit(self) -> "WorkflowDefinition":
        if self.node_count > MAX_WORKFLOW_NODES:
            raise ValueError(
                "미선택 분기를 포함한 전체 노드는 "
                f"{MAX_WORKFLOW_NODES}개 이하여야 합니다."
            )
        return self


# [추가] API와 생성 재시도에서 분기할 수 있는 정적 검증 오류 코드를 정의한다.
WorkflowValidationCode: TypeAlias = Literal[
    "INVALID_JSON",
    "INPUT_TOO_LARGE",
    "INVALID_SCHEMA",
    "DUPLICATE_ID",
    "INVALID_REFERENCE",
    "UNKNOWN_SYSTEM",
    "UNKNOWN_RUNBOOK",
    # [추가] 구조는 유효하지만 요청에 명시된 순서·조건과 어긋난 후보의 수정 피드백이다.
    "INTENT_MISMATCH",
]


# [추가] 검증 실패의 종류·입력 위치·수정 안내를 JSON으로 전달한다.
class WorkflowValidationIssue(WorkflowModel):
    code: WorkflowValidationCode
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)


# [추가] 실행 가능한 새 모델 또는 오류 목록 중 하나만 반환하도록 결과를 제한한다.
class WorkflowValidationResult(WorkflowModel):
    valid: bool
    workflow: WorkflowDefinition | None = None
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)

    # [추가] 성공인데 오류가 있거나 실패인데 실행 모델이 남는 모순된 결과를 막는다.
    @model_validator(mode="after")
    def require_consistent_result(self) -> "WorkflowValidationResult":
        if self.valid:
            if self.workflow is None or self.errors:
                raise ValueError("검증 성공에는 워크플로가 필요하며 오류는 없어야 합니다.")
        elif self.workflow is not None or not self.errors:
            raise ValueError("검증 실패에는 오류가 필요하며 워크플로는 없어야 합니다.")
        return self


# [추가] MCP가 반환한 모니터링 스냅샷을 엄격히 검사하고 조건 계산에 재사용한다.
class MonitoringSnapshot(WorkflowModel):
    system_name: ResourceName
    status: StatusValue
    cpu_percent: NumericValue
    memory_percent: NumericValue
    last_checked: str = Field(min_length=1)
    alerts: list[str] = Field(default_factory=list)


# [추가] 조건의 입력값·판단 결과·선택 분기를 실행 기록에 남긴다.
class WorkflowConditionResult(WorkflowModel):
    ref: NodeId
    field: Literal["cpu_percent", "memory_percent", "status"]
    op: NumericOperator
    expected: NumericValue | StatusValue
    actual: NumericValue | StatusValue
    result: bool
    selected_branch: Literal["then_steps", "else_steps"]


# [추가] 정적 검증 실패와 조회·응답·시간 초과 실패를 구분한다.
WorkflowExecutionCode: TypeAlias = Literal[
    "VALIDATION_FAILED",
    "MCP_ERROR",
    "INVALID_MONITORING_RESULT",
    "INVALID_RUNBOOK_RESULT",
    "RUNBOOK_NOT_FOUND",
    "CONDITION_ERROR",
    "EXECUTION_TIMEOUT",
]


# [추가] 중단 원인과 마지막 실행 위치를 보고서·화면에 전달한다.
class WorkflowExecutionIssue(WorkflowModel):
    code: WorkflowExecutionCode
    path: str = Field(min_length=1)
    node_id: NodeId | None = None
    message: str = Field(min_length=1)


# [추가] 검증을 통과한 조회 자료만 실행 순서대로 저장한다.
class WorkflowActionResult(WorkflowModel):
    node_id: NodeId
    source: Literal["monitoring", "runbook"]
    data: MonitoringSnapshot | str

    # [추가] 조회 종류와 실제 자료의 형태가 어긋난 결과를 막는다.
    @model_validator(mode="after")
    def require_matching_data(self) -> "WorkflowActionResult":
        if self.source == "monitoring" and not isinstance(self.data, MonitoringSnapshot):
            raise ValueError("모니터링 결과에는 검증된 스냅샷이 필요합니다.")
        if self.source == "runbook" and (
            not isinstance(self.data, str) or not self.data.strip()
        ):
            raise ValueError("런북 결과에는 비어 있지 않은 본문이 필요합니다.")
        return self


# [추가] 시작·완료·건너뜀·실패와 조건 근거를 JSON으로 기록한다.
class WorkflowTraceEvent(WorkflowModel):
    sequence: int = Field(ge=1)
    node_id: NodeId
    node_type: Literal["monitoring", "runbook", "if"]
    path: str = Field(min_length=1)
    status: Literal["started", "completed", "skipped", "failed"]
    elapsed_ms: float = Field(ge=0)
    condition: WorkflowConditionResult | None = None
    error: WorkflowExecutionIssue | None = None


# [추가] 실행 성공·실패 모두 완료 자료와 trace를 보존하고 원인을 명시한다.
class WorkflowExecutionResult(WorkflowModel):
    status: Literal["completed", "failed"]
    results: list[WorkflowActionResult] = Field(default_factory=list)
    trace: list[WorkflowTraceEvent] = Field(default_factory=list)
    error: WorkflowExecutionIssue | None = None
    validation_errors: list[WorkflowValidationIssue] = Field(default_factory=list)
    elapsed_ms: float = Field(ge=0)

    # [추가] 실패를 성공으로 표시하거나 원인 없는 실패를 반환하는 것을 막는다.
    @model_validator(mode="after")
    def require_consistent_execution(self) -> "WorkflowExecutionResult":
        if self.status == "completed" and (self.error or self.validation_errors):
            raise ValueError("실행 성공에는 오류가 없어야 합니다.")
        if self.status == "failed" and self.error is None:
            raise ValueError("실행 실패에는 중단 원인이 필요합니다.")
        if self.validation_errors and (
            self.error is None or self.error.code != "VALIDATION_FAILED"
        ):
            raise ValueError("정적 검증 오류는 검증 실패 원인과 함께 반환해야 합니다.")
        return self

    # [추가] 기존 보고 노드의 collected 입력과 호환되는 독립적인 자료 목록을 만든다.
    def to_collected(self) -> list[dict]:
        return [
            {
                "source": item.source,
                "data": item.data.model_dump(mode="json")
                if isinstance(item.data, MonitoringSnapshot)
                else item.data,
            }
            for item in self.results
        ]


# [추가] 공급자의 입력·출력 토큰과 fake 추정치를 구분하고 미제공 값은 None으로 보존한다.
class WorkflowModelUsage(WorkflowModel):
    model: str = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    measurement: Literal["reported", "estimated", "unavailable"] = "unavailable"


# [추가] 어댑터는 파싱하지 않은 JSON 원문과 사용량·응답 종료 상태만 전달한다.
class WorkflowModelResponse(WorkflowModel):
    content: str
    usage: WorkflowModelUsage
    refusal: str | None = None
    complete: bool = True


# [추가] 지원하지 않는 요청의 이유만 허용하며 일부 단계로 임의 축소하지 않는다.
class WorkflowUnsupportedRequest(WorkflowModel):
    unsupported_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]


# [추가] 입력·지원 범위·모델 연동·정적 검증·시간 초과를 서로 다른 실패로 전달한다.
WorkflowCompilationCode: TypeAlias = Literal[
    "INVALID_QUERY",
    "UNSUPPORTED_REQUEST",
    "MODEL_REFUSED",
    "VALIDATION_FAILED",
    "MODEL_ERROR",
    "GENERATION_TIMEOUT",
]


# [추가] 생성 실패를 후속 API와 미리보기 화면에서 설명할 수 있게 구조화한다.
class WorkflowCompilationIssue(WorkflowModel):
    code: WorkflowCompilationCode
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)


# [추가] 실패한 호출과 수정 생성까지 시도별 사용량·검증 오류를 빠짐없이 기록한다.
class WorkflowCompilationAttempt(WorkflowModel):
    number: int = Field(ge=1, le=2)
    usage: WorkflowModelUsage | None = None
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)


# [추가] 유효한 실행 정의 또는 명시적인 생성 실패와 전체 시도 기록을 반환한다.
class WorkflowCompilationResult(WorkflowModel):
    valid: bool
    workflow: WorkflowDefinition | None = None
    error: WorkflowCompilationIssue | None = None
    errors: list[WorkflowValidationIssue] = Field(default_factory=list)
    attempts: list[WorkflowCompilationAttempt] = Field(default_factory=list, max_length=2)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0)

    # [추가] 검증되지 않은 정의가 성공 결과나 실패 결과에 남는 것을 막는다.
    @model_validator(mode="after")
    def require_consistent_compilation(self) -> "WorkflowCompilationResult":
        if self.valid:
            if self.workflow is None or self.error is not None or self.errors:
                raise ValueError("생성 성공에는 유효한 워크플로가 필요하며 오류는 없어야 합니다.")
        elif self.workflow is not None or self.error is None:
            raise ValueError("생성 실패에는 원인이 필요하며 워크플로는 없어야 합니다.")
        return self


# [추가] 보고 재작성의 사용량과 형식 검증 반려 이유를 시도별로 기록한다.
class WorkflowReportAttempt(WorkflowModel):
    number: int = Field(ge=1, le=3)
    usage: WorkflowModelUsage | None = None
    errors: list[str] = Field(default_factory=list)


# [추가] 조회 실패·보고 실패·전체 시간 초과의 단계와 원인을 구분한다.
class WorkflowRunIssue(WorkflowModel):
    code: Literal[
        "INVALID_QUERY",
        "EXECUTION_FAILED",
        "REPORT_ERROR",
        "REPORT_REFUSED",
        "REPORT_VALIDATION_FAILED",
        "WORKFLOW_TIMEOUT",
    ]
    stage: Literal["input", "execution", "report"]
    message: str = Field(min_length=1)


# [추가] 한 실행의 정의·조회·보고 결과를 묶고 보고 토큰을 생성 토큰과 구분한다.
class WorkflowRunResult(WorkflowModel):
    status: Literal["completed", "failed"]
    run_id: str = Field(min_length=1)
    definition: WorkflowDefinition | None = None
    definition_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    execution: WorkflowExecutionResult | None = None
    report_valid: bool = False
    final: str = ""
    draft: str = ""
    # [추가] 요약 실패와 관계없이 조회 성공 시 코드가 만든 사실 보고를 별도로 제공한다.
    facts_report: str = ""
    report_attempts: list[WorkflowReportAttempt] = Field(default_factory=list, max_length=3)
    error: WorkflowRunIssue | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0)
    events: list[dict] = Field(default_factory=list)

    # [추가] 조회·보고 검증이 모두 성공한 경우에만 확정 답변을 내보낸다.
    @model_validator(mode="after")
    def require_consistent_run(self) -> "WorkflowRunResult":
        if self.status == "completed":
            if (
                self.definition is None or self.definition_hash is None
                or self.execution is None or self.execution.status != "completed"
                or not self.report_valid or not self.final.strip() or self.error is not None
            ):
                raise ValueError("완료 결과에는 유효한 정의·조회 성공·검증된 보고가 필요합니다.")
        elif self.error is None or self.report_valid or self.final:
            raise ValueError("실패 결과에는 원인이 필요하며 확정 답변을 남길 수 없습니다.")
        return self


# [추가] 생성 초안과 재사용 절차의 저장·미리보기 계약을 정의한다.
class WorkflowRecord(WorkflowModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    name: Annotated[
        str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80)
    ]
    query: str = Field(min_length=1, max_length=MAX_WORKFLOW_QUERY_LENGTH)
    definition: WorkflowDefinition
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["draft", "saved"]
    created_at: str = Field(min_length=1)
    saved_at: str | None = None

    # [추가] 원문은 공백을 보존하되 내용이 없는 요청을 저장하지 않는다.
    @model_validator(mode="after")
    def require_query_content(self) -> "WorkflowRecord":
        if not self.query.strip():
            raise ValueError("저장할 요청에는 내용이 있어야 합니다.")
        return self


# [추가] 생성 API가 초안 미리보기와 모든 생성 시도·사용량을 함께 반환한다.
class WorkflowGenerated(WorkflowRecord):
    compilation: WorkflowCompilationResult

    # [추가] 생성 실패를 실행 가능한 저장 초안으로 반환하는 것을 차단한다.
    @model_validator(mode="after")
    def require_successful_generation(self) -> "WorkflowGenerated":
        if not self.compilation.valid:
            raise ValueError("생성 성공 결과만 초안과 함께 반환할 수 있습니다.")
        return self

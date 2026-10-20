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

MAX_WORKFLOW_NODES = 6
MAX_WORKFLOW_NAME_LENGTH = 80
MAX_RESOURCE_NAME_LENGTH = 128
NODE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
RESOURCE_NAME_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"

NumericField: TypeAlias = Literal["cpu_percent", "memory_percent"]
NumericOperator: TypeAlias = Literal[">", ">=", "<", "<=", "==", "!="]
StatusOperator: TypeAlias = Literal["==", "!="]
StatusValue: TypeAlias = Literal["healthy", "degraded", "down"]

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


def _require_json_number(value: object) -> object:
    # bool은 int의 하위 타입이다. 숫자처럼 변환하지 않고 JSON 숫자만 받는다.
    if type(value) not in (int, float):
        raise ValueError("조건 값은 문자열·불리언이 아닌 숫자여야 합니다.")
    return value


def _require_integer_version(value: object) -> object:
    # Literal[1]만으로는 True나 1.0도 1로 받아들일 수 있다.
    if type(value) is not int:
        raise ValueError("schema_version은 정수여야 합니다.")
    return value


NumericValue: TypeAlias = Annotated[
    StrictInt | StrictFloat, BeforeValidator(_require_json_number)
]
SchemaVersion: TypeAlias = Annotated[
    Literal[1], BeforeValidator(_require_integer_version)
]


class WorkflowModel(BaseModel):
    """암묵적 타입 변환·추가 필드·비유한 숫자를 허용하지 않는 기본 모델."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        allow_inf_nan=False,
    )


class NumericCondition(WorkflowModel):
    ref: NodeId = Field(description="조건보다 앞에 있는 최상위 모니터링 노드 ID")
    field: NumericField
    op: NumericOperator
    value: NumericValue


class StatusCondition(WorkflowModel):
    ref: NodeId = Field(description="조건보다 앞에 있는 최상위 모니터링 노드 ID")
    field: Literal["status"]
    op: StatusOperator
    value: StatusValue


Condition: TypeAlias = Annotated[
    NumericCondition | StatusCondition,
    Field(discriminator="field"),
]


class MonitoringStep(WorkflowModel):
    id: NodeId
    type: Literal["monitoring"]
    system: ResourceName = Field(description="카탈로그에 등록된 시스템 이름")


class RunbookStep(WorkflowModel):
    id: NodeId
    type: Literal["runbook"]
    incident_type: ResourceName = Field(description="카탈로그에 등록된 런북 유형")


ActionStep: TypeAlias = Annotated[
    MonitoringStep | RunbookStep,
    Field(discriminator="type"),
]


class IfStep(WorkflowModel):
    id: NodeId
    type: Literal["if"]
    condition: Condition
    # 분기에는 조회 노드만 허용해 중첩 if와 재귀 실행 구조를 막는다.
    then_steps: list[ActionStep] = Field(max_length=MAX_WORKFLOW_NODES)
    else_steps: list[ActionStep] = Field(max_length=MAX_WORKFLOW_NODES)

    @model_validator(mode="after")
    def require_action(self) -> "IfStep":
        if not self.then_steps and not self.else_steps:
            raise ValueError("조건의 두 분기를 모두 비울 수 없습니다.")
        return self


WorkflowStep: TypeAlias = Annotated[
    MonitoringStep | RunbookStep | IfStep,
    Field(discriminator="type"),
]


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

    def iter_steps(self) -> Iterator[WorkflowStep]:
        """실행 여부와 무관하게 양쪽 분기를 포함한 모든 노드를 방문한다."""
        for step in self.steps:
            yield step
            if isinstance(step, IfStep):
                yield from step.then_steps
                yield from step.else_steps

    @property
    def node_count(self) -> int:
        return sum(1 for _ in self.iter_steps())

    @model_validator(mode="after")
    def enforce_node_limit(self) -> "WorkflowDefinition":
        if self.node_count > MAX_WORKFLOW_NODES:
            raise ValueError(
                "미선택 분기를 포함한 전체 노드는 "
                f"{MAX_WORKFLOW_NODES}개 이하여야 합니다."
            )
        return self

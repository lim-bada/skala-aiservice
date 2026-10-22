# [추가] 실행 전 워크플로의 JSON·구조·자원·참조를 검사하는 신규 파일 전체.
"""LLM·MCP 호출 없이, 주입받은 카탈로그만으로 실행 정의를 정적 검증한다."""
import json
from dataclasses import dataclass
from typing import Iterator

from pydantic import ValidationError

from .catalog import WorkflowCatalog
from .models import (
    MAX_WORKFLOW_JSON_BYTES,
    IfStep,
    MonitoringStep,
    RunbookStep,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowValidationCode,
    WorkflowValidationIssue,
    WorkflowValidationResult,
)


# [추가] 실행 위치와 분기 내부 여부를 함께 보관해 참조 범위를 판단한다.
@dataclass(frozen=True)
class _LocatedStep:
    step: WorkflowStep
    path: str
    top_index: int | None


# [추가] 표준 JSON에 없는 NaN·Infinity를 Python JSON 파서가 허용하지 못하게 한다.
def _reject_constant(value: str) -> None:
    raise ValueError(f"표준 JSON에서 허용하지 않는 숫자입니다: {value}")


# [추가] 마지막 키로 덮어쓰지 않고 중복 키가 포함된 JSON 객체를 거부한다.
def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 객체에 중복된 키가 있습니다.")
        result[key] = value
    return result


# [추가] 입력 오류도 API에서 그대로 직렬화할 수 있는 일관된 실패 결과로 만든다.
def _failure(
    code: WorkflowValidationCode, message: str, path: str = "$"
) -> WorkflowValidationResult:
    return WorkflowValidationResult(
        valid=False,
        errors=[WorkflowValidationIssue(code=code, path=path, message=message)],
    )


# [추가] 내부 union 태그만 제거하고 사용자가 입력한 실제 필드명은 유지한다.
def _schema_path(location: tuple[str | int, ...]) -> str:
    parts: list[str | int] = []
    for index, part in enumerate(location):
        if (
            part in ("monitoring", "runbook", "if")
            and index >= 2
            and isinstance(location[index - 1], int)
            and location[index - 2] in ("steps", "then_steps", "else_steps")
        ):
            continue
        if (
            part in ("cpu_percent", "memory_percent", "status")
            and index >= 1
            and location[index - 1] == "condition"
        ):
            continue
        if part in ("int", "float") and index >= 1 and location[index - 1] == "value":
            continue
        parts.append(part)

    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif part.isidentifier():
            path += f".{part}" if path else part
        else:
            path += f"[{json.dumps(part, ensure_ascii=True)}]"
    return path or "$"


# [추가] Pydantic 오류에서 입력 내용 대신 위치와 수정에 필요한 설명만 추출한다.
def _schema_issues(error: ValidationError) -> list[WorkflowValidationIssue]:
    issues = []
    seen: set[tuple[str, str]] = set()
    messages = {
        "missing": "필수 필드가 없습니다.",
        "extra_forbidden": "허용하지 않는 추가 필드입니다.",
        "union_tag_invalid": "허용하지 않는 노드 유형 또는 조건 필드입니다.",
        "union_tag_not_found": "노드의 type 또는 조건의 field가 필요합니다.",
        "model_type": "워크플로와 각 노드는 올바른 객체 형식이어야 합니다.",
    }
    for detail in error.errors(include_input=False, include_url=False):
        path = _schema_path(detail["loc"])
        if detail["type"] == "value_error":
            message = str(detail.get("ctx", {}).get("error", detail["msg"]))
        else:
            message = messages.get(detail["type"], detail["msg"])
        if (path, message) not in seen:
            seen.add((path, message))
            issues.append(
                WorkflowValidationIssue(code="INVALID_SCHEMA", path=path, message=message)
            )
    return issues


# [추가] 실행될 분기와 무관하게 모든 노드를 방문하고 원본 JSON 위치를 부여한다.
def _locate_steps(workflow: WorkflowDefinition) -> Iterator[_LocatedStep]:
    for index, step in enumerate(workflow.steps):
        path = f"steps[{index}]"
        yield _LocatedStep(step, path, index)
        if isinstance(step, IfStep):
            for branch_name in ("then_steps", "else_steps"):
                for branch_index, action in enumerate(getattr(step, branch_name)):
                    yield _LocatedStep(
                        action, f"{path}.{branch_name}[{branch_index}]", None
                    )


# [추가] 전체 ID의 유일성, 실제 자원 등록 여부, 선행 모니터링 참조를 함께 검사한다.
def _semantic_issues(
    workflow: WorkflowDefinition, catalog: WorkflowCatalog
) -> list[WorkflowValidationIssue]:
    locations = list(_locate_steps(workflow))
    by_id: dict[str, _LocatedStep] = {}
    duplicates: set[str] = set()
    issues: list[WorkflowValidationIssue] = []

    # [추가] 먼저 전체 ID를 수집해 미래 참조와 분기 간 중복도 놓치지 않는다.
    for location in locations:
        node_id = location.step.id
        if node_id in by_id:
            duplicates.add(node_id)
            issues.append(WorkflowValidationIssue(
                code="DUPLICATE_ID",
                path=f"{location.path}.id",
                message=f"노드 ID '{node_id}'가 {by_id[node_id].path}.id와 중복됩니다.",
            ))
        else:
            by_id[node_id] = location

    # [추가] 미선택 분기의 자원까지 검증하고 독립적인 오류들을 한 번에 반환한다.
    for location in locations:
        step = location.step
        if isinstance(step, MonitoringStep) and step.system not in catalog.systems:
            issues.append(WorkflowValidationIssue(
                code="UNKNOWN_SYSTEM",
                path=f"{location.path}.system",
                message=f"카탈로그에 없는 시스템입니다: {step.system}",
            ))
        elif isinstance(step, RunbookStep) and step.incident_type not in catalog.incident_types:
            issues.append(WorkflowValidationIssue(
                code="UNKNOWN_RUNBOOK",
                path=f"{location.path}.incident_type",
                message=f"카탈로그에 없는 런북 유형입니다: {step.incident_type}",
            ))
        elif isinstance(step, IfStep):
            reference = step.condition.ref
            target = by_id.get(reference)
            reason = None
            if target is None:
                reason = f"참조 대상 '{reference}'가 없습니다."
            elif reference in duplicates:
                reason = f"참조 대상 '{reference}'의 ID가 중복되어 대상을 결정할 수 없습니다."
            elif not isinstance(target.step, MonitoringStep):
                reason = "조건은 모니터링 노드만 참조할 수 있습니다."
            elif target.top_index is None:
                reason = "분기 내부의 모니터링 결과는 조건에서 참조할 수 없습니다."
            elif target.top_index >= location.top_index:
                reason = "조건보다 앞에 있는 최상위 모니터링 노드만 참조할 수 있습니다."
            if reason is not None:
                issues.append(WorkflowValidationIssue(
                    code="INVALID_REFERENCE",
                    path=f"{location.path}.condition.ref",
                    message=reason,
                ))
    return issues


# [추가] 모든 입력을 재검증하고 성공한 경우에만 원본과 분리된 실행 정의를 반환한다.
def validate_workflow(
    payload: object, catalog: WorkflowCatalog
) -> WorkflowValidationResult:
    """dict·모델·JSON 문자열·UTF-8 bytes를 검사한다. 외부 조회나 실행은 하지 않는다.

    원문 크기 제한은 str·bytes에 적용한다. 생성기는 LLM 응답을 미리 파싱하지
    말고 원문으로 전달해야 하며, dict·모델 입력에도 구조·의미 검사는 적용한다.
    """
    if isinstance(payload, (str, bytes)):
        # [추가] JSON 파싱 전에 문자 수가 아닌 UTF-8 바이트 크기로 입력을 제한한다.
        try:
            raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        except UnicodeEncodeError:
            return _failure("INVALID_JSON", "JSON 문자열은 유효한 UTF-8이어야 합니다.")
        if len(raw) > MAX_WORKFLOW_JSON_BYTES:
            return _failure(
                "INPUT_TOO_LARGE", f"JSON 입력은 {MAX_WORKFLOW_JSON_BYTES}바이트 이하여야 합니다."
            )
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError):
            return _failure(
                "INVALID_JSON", "유효한 UTF-8 JSON이 필요합니다. 구문·중복 키·숫자 형식을 확인하세요."
            )

    # [추가] 인스턴스도 중첩 모델까지 재검증해 생성 후 변조되거나 공유된 값을 차단한다.
    try:
        workflow = WorkflowDefinition.model_validate(payload)
    except ValidationError as error:
        return WorkflowValidationResult(valid=False, errors=_schema_issues(error))

    issues = _semantic_issues(workflow, catalog)
    if issues:
        return WorkflowValidationResult(valid=False, errors=issues)
    return WorkflowValidationResult(valid=True, workflow=workflow)

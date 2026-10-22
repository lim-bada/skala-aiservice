# [추가] 자연어 생성·원문 검증·최대 한 번의 수정 생성을 묶는 신규 파일 전체.
"""MCP 실행 없이 검증된 초안만 만들며, 실패한 호출도 시도·사용량에 포함한다."""
import asyncio
import json
import math
from time import monotonic
from typing import Protocol

from langsmith import traceable
from pydantic import ValidationError

from .catalog import WorkflowCatalog
from .intent import definition_issues, query_issues
from .models import (
    MAX_WORKFLOW_JSON_BYTES,
    MAX_WORKFLOW_QUERY_LENGTH,
    WorkflowCompilationAttempt,
    WorkflowCompilationCode,
    WorkflowCompilationIssue,
    WorkflowCompilationResult,
    WorkflowDefinition,
    WorkflowModelResponse,
    WorkflowUnsupportedRequest,
    WorkflowValidationIssue,
)
from .prompting import workflow_response_schema
from .validator import validate_workflow

# [추가] 수정 생성을 포함한 호출 횟수와 전체 대기 시간을 제한한다.
MAX_COMPILATION_ATTEMPTS = 2
DEFAULT_COMPILATION_TIMEOUT_SECONDS = 120.0


# [추가] 공급자별 HTTP·fake 구현과 생성 제어 로직의 의존성을 분리한다.
class WorkflowCompilerLLM(Protocol):
    async def compile_workflow(
        self,
        query: str,
        catalog: dict,
        schema: dict,
        errors: list[dict] | None = None,
        previous_response: str | None = None,
    ) -> WorkflowModelResponse: ...


# [추가] 원문 한도를 넘기거나 UTF-8로 표현할 수 없는 후보를 다음 요청에 되돌려 보내지 않는다.
def _repair_context(raw: str) -> str | None:
    try:
        return raw if len(raw.encode("utf-8")) <= MAX_WORKFLOW_JSON_BYTES else None
    except UnicodeEncodeError:
        return None


# [추가] 원문 JSON 검사를 이미 통과한 객체만 명시적인 지원 범위 거절로 해석한다.
def _unsupported_reason(raw: str, errors: list[WorkflowValidationIssue]) -> str | None:
    if any(issue.code in ("INVALID_JSON", "INPUT_TOO_LARGE") for issue in errors):
        return None
    try:
        return WorkflowUnsupportedRequest.model_validate(json.loads(raw)).unsupported_reason
    except (ValidationError, ValueError, RecursionError):
        return None


# [추가] 호출 하나라도 카운트를 제공하지 않으면 해당 합계는 미확인(None)으로 남긴다.
def _token_total(attempts: list[WorkflowCompilationAttempt], field: str) -> int | None:
    values = [getattr(attempt.usage, field) if attempt.usage else None for attempt in attempts]
    if any(value is None for value in values):
        return None
    return sum(values)


# [추가] 성공·실패 모두 같은 시도 기록과 누적 사용량을 반환한다.
def _result(
    started_at: float,
    attempts: list[WorkflowCompilationAttempt],
    *,
    workflow: WorkflowDefinition | None = None,
    code: WorkflowCompilationCode | None = None,
    message: str = "",
    errors: list[WorkflowValidationIssue] | None = None,
) -> WorkflowCompilationResult:
    return WorkflowCompilationResult(
        valid=workflow is not None,
        workflow=workflow,
        error=WorkflowCompilationIssue(code=code, path="$", message=message) if code else None,
        errors=errors or [],
        attempts=attempts,
        input_tokens=_token_total(attempts, "input_tokens"),
        output_tokens=_token_total(attempts, "output_tokens"),
        total_tokens=_token_total(attempts, "total_tokens"),
        elapsed_ms=(monotonic() - started_at) * 1000,
    )


# [추가] 지원 문법만 생성하고 검증 실패 때만 한 번 더 생성한다. 도구 조회는 수행하지 않는다.
@traceable(run_type="chain", name="workflow.compile")
async def compile_workflow(
    query: object,
    catalog: WorkflowCatalog,
    llm: WorkflowCompilerLLM,
    *,
    timeout_seconds: float = DEFAULT_COMPILATION_TIMEOUT_SECONDS,
) -> WorkflowCompilationResult:
    """모델 출력 원문을 검사하며 필드 보정·기본 자원 치환·부분 실행을 하지 않는다.

    시간 제한은 두 번의 생성 전체에 적용한다. 통신 오류·지원 거절은 재시도하지
    않으며 외부 Task의 CancelledError는 호출자에게 전파한다.
    """
    # [추가] 잘못된 제한 설정은 사용자 요청 실패와 구분해 호출자에게 알린다.
    if type(timeout_seconds) not in (int, float):
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")
    try:
        timeout_value = float(timeout_seconds)
    except OverflowError as error:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.") from error
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ValueError("timeout_seconds는 양의 유한한 숫자여야 합니다.")

    started_at = monotonic()
    attempts: list[WorkflowCompilationAttempt] = []
    # [추가] 길이·공백·잘못된 유니코드를 검사해 부적절한 입력의 모델 호출을 막는다.
    valid_query = isinstance(query, str) and 0 < len(query) <= MAX_WORKFLOW_QUERY_LENGTH
    if valid_query:
        try:
            query.encode("utf-8")
            valid_query = bool(query.strip())
        except UnicodeEncodeError:
            valid_query = False
    if not valid_query:
        return _result(
            started_at, attempts, code="INVALID_QUERY",
            message=f"요청은 비어 있지 않은 유효한 문자열이며 {MAX_WORKFLOW_QUERY_LENGTH}자 이하여야 합니다.",
        )

    query = query.strip()
    # [추가] 명백한 미지원·모호 요청은 일부 읽기 작업으로 대체하지 않고 전체 거절한다.
    unsupported = query_issues(query, catalog)
    if unsupported:
        return _result(
            started_at, attempts, code="UNSUPPORTED_REQUEST",
            message=" ".join(issue.message for issue in unsupported),
        )
    previous_response = None
    last_errors: list[WorkflowValidationIssue] = []
    try:
        async with asyncio.timeout(timeout_value):
            for number in range(1, MAX_COMPILATION_ATTEMPTS + 1):
                # [추가] 응답이 오지 않은 호출도 기록해 비용·시도 횟수를 과소 표시하지 않는다.
                attempt = WorkflowCompilationAttempt(number=number)
                attempts.append(attempt)
                try:
                    # [추가] 매 시도 독립 자료를 전달해 어댑터의 변조가 검증 정책을 바꾸지 못하게 한다.
                    response = await llm.compile_workflow(
                        query,
                        catalog.to_dict(),
                        workflow_response_schema(),
                        errors=[issue.model_dump(mode="json") for issue in last_errors] or None,
                        previous_response=previous_response,
                    )
                    response = WorkflowModelResponse.model_validate(response)
                except Exception as error:
                    return _result(
                        started_at, attempts, code="MODEL_ERROR", errors=last_errors,
                        message=f"워크플로 모델 호출 또는 응답 처리에 실패했습니다 ({type(error).__name__}).",
                    )
                attempt.usage = response.usage

                # [추가] 공급자의 명시적인 응답 거절은 구조 수정을 요구하지 않고 종료한다.
                if response.refusal is not None:
                    return _result(
                        started_at, attempts, code="MODEL_REFUSED",
                        message=response.refusal.strip() or "모델이 워크플로 생성을 거절했습니다.",
                    )

                # [추가] JSON 파싱 전에 기존 검증기의 크기·중복 키·문법 검사를 그대로 적용한다.
                validation = validate_workflow(response.content, catalog)
                last_errors = list(validation.errors)
                if response.complete and validation.valid:
                    # [추가] 순차 지시의 임의 조건화·명시한 조회 누락·비교 경계 변경을 수정 대상으로 돌린다.
                    last_errors = [
                        WorkflowValidationIssue(code="INTENT_MISMATCH", path=issue.path, message=issue.message)
                        for issue in definition_issues(query, validation.workflow, catalog)
                    ]
                    if not last_errors:
                        return _result(started_at, attempts, workflow=validation.workflow)

                if not response.complete:
                    # [추가] 잘린 응답이 우연히 유효한 JSON이어도 완성된 절차로 승인하지 않는다.
                    last_errors.append(WorkflowValidationIssue(
                        code="INVALID_SCHEMA", path="$", message="모델 응답이 정상적으로 끝나지 않았습니다."
                    ))
                else:
                    reason = _unsupported_reason(response.content, last_errors)
                    if reason is not None:
                        return _result(
                            started_at, attempts, code="UNSUPPORTED_REQUEST", message=reason,
                        )
                attempt.errors = last_errors
                previous_response = _repair_context(response.content)
    except TimeoutError:
        return _result(
            started_at, attempts, code="GENERATION_TIMEOUT", errors=last_errors,
            message="워크플로 생성 제한 시간을 초과했습니다.",
        )

    return _result(
        started_at, attempts, code="VALIDATION_FAILED", errors=last_errors,
        message="두 번의 생성 후에도 유효한 워크플로를 만들지 못했습니다.",
    )

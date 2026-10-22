# [추가] 모든 생성 어댑터가 공유하는 프롬프트·응답 스키마를 구성하는 신규 파일 전체.
"""프롬프트 파일과 Pydantic 스키마를 재사용하며 사용자 데이터를 JSON으로 구분한다."""
import json
from pathlib import Path

from .models import WorkflowDefinition, WorkflowUnsupportedRequest

# [추가] 현재 작업 디렉터리와 무관하게 프로젝트의 프롬프트 파일을 찾는다.
_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "workflow_compile.txt"


# [추가] 정상 AST와 지원 범위 거절을 모두 구조화 출력으로 표현한다.
def workflow_response_schema() -> dict:
    workflow = WorkflowDefinition.model_json_schema()
    definitions = workflow.pop("$defs", {})
    return {
        "$defs": definitions,
        "anyOf": [workflow, WorkflowUnsupportedRequest.model_json_schema()],
    }


# [추가] 어댑터 간 규칙 차이를 없애고 수정 생성의 원문·오류도 지시와 분리한다.
def build_workflow_messages(
    query: str,
    catalog: dict,
    schema: dict,
    errors: list[dict] | None = None,
    previous_response: str | None = None,
) -> list[dict]:
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = json.dumps(
        {
            "query": query,
            "catalog": catalog,
            "response_schema": schema,
            "validation_errors": errors or [],
            "previous_response": previous_response,
        },
        ensure_ascii=False,
        allow_nan=False,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

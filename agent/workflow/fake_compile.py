# [추가] 새 파일 전체: 자연어 해석 성능과 구분되는 고정 fixture 기반 워크플로 생성 어댑터.
"""외부 모델 호출 없이 정확히 등록된 요청의 응답과 추정 사용량을 반환한다."""

import json
from pathlib import Path

from .models import WorkflowModelResponse, WorkflowModelUsage
from .prompting import build_workflow_messages


# [추가] 실행한 폴더와 관계없이 저장소 안의 생성 fixture를 찾는다.
FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "workflow_compile_cases.json"
)


# [추가] 캐시하지 않고 매번 역직렬화해 호출 간 응답 객체가 공유되지 않도록 한다.
def load_fake_workflow_cases() -> dict[str, dict]:
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(cases, dict) or not all(
        isinstance(query, str) and isinstance(response, dict)
        for query, response in cases.items()
    ):
        raise ValueError("워크플로 fake fixture는 요청 문자열과 응답 객체의 사전이어야 합니다.")
    return cases


# [추가] 정확히 일치하는 fixture만 선택하며 미등록 요청은 부분 계획으로 바꾸지 않는다.
def compile_fake_workflow(
    query: str,
    catalog: dict,
    schema: dict,
    model_name: str,
    errors: list[dict] | None = None,
    previous_response: str | None = None,
) -> WorkflowModelResponse:
    cases = load_fake_workflow_cases()
    response = cases.get(query)
    if response is None:
        response = {
            "unsupported_reason": (
                "fake fixture에 등록되지 않은 요청입니다. "
                "등록된 예시를 사용하거나 실제 모델로 자연어 생성 기능을 확인하세요."
            )
        }
    content = json.dumps(response, ensure_ascii=False)

    # [추가] 실제 어댑터와 동일한 입력 조립을 거쳐 수정 요청까지 사용량 추정에 포함한다.
    messages = build_workflow_messages(
        query=query,
        catalog=catalog,
        schema=schema,
        errors=errors,
        previous_response=previous_response,
    )
    prompt = json.dumps(messages, ensure_ascii=False)
    input_tokens = (len(prompt) + 3) // 4
    output_tokens = (len(content) + 3) // 4
    return WorkflowModelResponse(
        content=content,
        usage=WorkflowModelUsage(
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            measurement="estimated",
        ),
        complete=True,
    )

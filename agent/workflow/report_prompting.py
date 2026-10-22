# [추가] 실제 조회한 런북의 문단만 보고 모델에 전달하는 신규 파일 전체.
"""관측값·분기·조회 횟수는 서버가 출력하므로 모델 입력과 역할에서 제외한다."""
import json
from pathlib import Path

from .report_content import build_runbook_sources

# [추가] 서버의 작업 디렉터리와 관계없이 같은 프롬프트를 사용한다.
_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "workflow_report.txt"


# [추가] 기존 어댑터 계약은 유지하되 질문·모니터링·trace를 모델의 사실 작성 재료로 보내지 않는다.
def build_workflow_report_messages(
    query: str, collected: list[dict], trace: list[dict],
    attempt: int, feedback: str | None,
) -> list[dict]:
    system = _PROMPT_PATH.read_text(encoding="utf-8")
    user = json.dumps({
        "runbook_sources": build_runbook_sources(collected),
        "attempt": attempt,
        "validation_feedback": feedback,
    }, ensure_ascii=False, allow_nan=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]

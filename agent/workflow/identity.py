# [추가] 미리보기·실행·향후 저장소에서 공유할 실행 정의 해시를 계산하는 신규 파일 전체.
"""표시 이름과 무관하게 버전·실행 단계의 동일성을 SHA-256으로 확인한다."""
import hashlib
import json

from .models import WorkflowDefinition


# [추가] 키 순서를 정규화하고 실행에 영향을 주는 필드만 해시에 포함한다.
def workflow_definition_hash(definition: WorkflowDefinition) -> str:
    checked = WorkflowDefinition.model_validate(definition)
    payload = checked.model_dump(mode="json", include={"schema_version", "steps"})
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

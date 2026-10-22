# [추가] 새 파일 전체: 모델의 이름·임의 ID와 무관하게 절차 의미와 실제 호출 경로를 채점한다.
"""정답은 평가에만 사용한다. 생성기·실행기 입력이나 수정 피드백으로 전달하지 않는다."""
from collections import Counter
from copy import deepcopy

from agent.workflow.models import WorkflowDefinition


# [추가] 구조·전역 ID·선행 참조를 확인한 뒤 이름·ID를 제거하고 참조 위치를 보존한다.
def canonical_definition(payload: object) -> dict:
    definition = WorkflowDefinition.model_validate(payload)
    data = definition.model_dump(mode="json")
    ids = [step.id for step in definition.iter_steps()]
    if len(set(ids)) != len(ids):
        raise ValueError("중복 노드 ID")
    previous = {}
    steps = []
    for index, raw in enumerate(data["steps"]):
        step = deepcopy(raw)
        step.pop("id")
        if step["type"] == "monitoring":
            previous[raw["id"]] = index
        elif step["type"] == "if":
            ref = step["condition"]["ref"]
            if ref not in previous:
                raise ValueError("선행 최상위 모니터링을 참조하지 않음")
            step["condition"]["ref"] = previous[ref]
            # [추가] 반대 비교식과 양쪽 분기를 함께 뒤집은 논리적으로 같은 절차도 정답으로 본다.
            complementary = {"<": ">=", "<=": ">", "!=": "=="}
            if step["condition"]["op"] in complementary:
                step["condition"]["op"] = complementary[step["condition"]["op"]]
                step["then_steps"], step["else_steps"] = step["else_steps"], step["then_steps"]
            for branch in ("then_steps", "else_steps"):
                for child in step[branch]:
                    child.pop("id")
        steps.append(step)
    return {"schema_version": data["schema_version"], "steps": steps}


# [추가] 숫자 80·80.0은 같은 의미이며, bool·문자열 숫자는 모델 검증 단계에서 거부한다.
def score_definition(actual: object, expected: object) -> dict:
    try:
        wanted = canonical_definition(expected)
        got = canonical_definition(actual)
    except (ValueError, TypeError) as error:
        return {"exact": False, "errors": [type(error).__name__ + ": 유효한 절차가 아닙니다."]}
    equal = got == wanted
    return {"exact": equal, "errors": [] if equal else ["조회 순서·대상·조건·참조·양쪽 분기 중 정답과 다른 항목이 있습니다."]}


# [추가] 호출 시작 순서를 비교하고, 미허용·중복 호출과 요청 대상의 누락도 별도 집계한다.
def score_calls(actual: list[dict], expected: list[dict]) -> dict:
    def key(call):
        if call.get("source") not in ("monitoring", "runbook") or not isinstance(call.get("target"), str):
            raise ValueError("호출에는 source와 target이 필요합니다.")
        return call["source"], call["target"]

    got, wanted = [key(call) for call in actual], [key(call) for call in expected]
    wanted_targets = {target for source, target in wanted if source == "monitoring"}
    got_targets = {target for source, target in got if source == "monitoring"}
    extra = Counter(got) - Counter(wanted)
    return {
        "path_match": got == wanted,
        "call_multiset_match": Counter(got) == Counter(wanted),
        "forbidden_calls": sum(extra.values()),
        "target_coverage": len(got_targets & wanted_targets) / len(wanted_targets) if wanted_targets else None,
    }

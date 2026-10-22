# [추가] 실행 사실의 결정론적 출력과 런북 요약의 자료 참조 검증을 담당하는 신규 파일 전체.
"""관측·조건·조회 여부는 실행기가 결정하고, 모델은 실제 문서의 문단만 요약한다."""
import json
import re
from decimal import Decimal

from pydantic import Field, ValidationError

from .models import (
    IfStep, MonitoringSnapshot, MonitoringStep, WorkflowDefinition,
    WorkflowExecutionResult, WorkflowModel,
)


# [추가] 모델이 사실·분기·조치 완료 필드를 새로 작성할 수 없게 출력 구조를 제한한다.
class RunbookSummary(WorkflowModel):
    source_id: str = Field(pattern=r"^runbook_[0-9]{3}$")
    block_id: str = Field(pattern=r"^block_[0-9]{3}$")
    summary: str = Field(min_length=1, max_length=1200)


# [추가] 실제 읽은 런북마다 하나의 문단을 선택하고 요약하도록 허용한다.
class RunbookSummaryReport(WorkflowModel):
    summaries: list[RunbookSummary] = Field(min_length=1, max_length=6)


# [추가] 원문 문단을 서버에서 식별해 모델이 인용문·문서 제목을 만들어내지 못하게 한다.
def build_runbook_sources(collected: list[dict]) -> list[dict]:
    sources = []
    for item in collected:
        if item.get("source") != "runbook" or not isinstance(item.get("data"), str):
            continue
        body = item["data"].strip()
        if not body:
            continue
        lines = body.splitlines()
        title = lines[0].lstrip("# ")
        content = "\n".join(lines[1:]).strip() if lines[0].startswith("#") else body
        blocks = re.split(r"\n\s*\n", content or body)
        sources.append({
            "source_id": f"runbook_{len(sources) + 1:03d}",
            "title": title,
            "blocks": [
                {"block_id": f"block_{index + 1:03d}", "text": block}
                for index, block in enumerate(blocks)
            ],
        })
    return sources


# [추가] 같은 JSON 키를 덮어쓰는 모호한 응답도 보고 재작성 대상으로 돌린다.
def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("중복 JSON 키")
        result[key] = value
    return result


# [추가] 문서에 없는 수치를 요약에 추가하는 명백한 오류를 검사한다. 수치의 의미 동치까지 증명하지 않는다.
def _numbers(text: str) -> set[Decimal]:
    return {Decimal(match) for match in re.findall(r"[+-]?\d+(?:\.\d+)?", text)}


# [추가] 실제 문서·문단 참조, 중복·누락, 새 수치·모델 제어 표기를 재작성 사유로 반환한다.
def parse_runbook_summary(raw: str, sources: list[dict]) -> tuple[RunbookSummaryReport | None, list[str]]:
    if len(raw) > 12_000:
        return None, ["런북 요약 JSON은 12000자 이하여야 합니다."]
    try:
        report = RunbookSummaryReport.model_validate(json.loads(raw, object_pairs_hook=_unique_keys))
    except (ValueError, TypeError, ValidationError, RecursionError):
        return None, ["summaries 배열의 source_id, block_id, summary만 포함하는 유효한 JSON을 작성하세요."]
    inventory = {source["source_id"]: source for source in sources}
    seen = set()
    errors = []
    for item in report.summaries:
        if item.source_id in seen:
            errors.append(f"{item.source_id}의 요약이 중복됐습니다.")
        seen.add(item.source_id)
        source = inventory.get(item.source_id)
        blocks = {block["block_id"]: block["text"] for block in source["blocks"]} if source else {}
        evidence = blocks.get(item.block_id)
        if evidence is None:
            errors.append(f"{item.source_id}/{item.block_id}는 실제 조회한 문단이 아닙니다.")
        elif not _numbers(item.summary).issubset(_numbers(evidence)):
            errors.append(f"{item.source_id} 요약에 근거 문단에 없는 수치를 추가하지 마세요.")
        if not item.summary.strip() or re.search(r"/think\b|</?think>", item.summary, re.I):
            errors.append("빈 요약이나 모델 제어 표기를 보고에 포함하지 마세요.")
    if seen != set(inventory):
        errors.append("실제 조회한 각 런북을 빠짐없이 한 번씩 요약하세요.")
    return (None if errors else report), errors


# [추가] 임의의 개행·문자열을 자료로 표시하고 수치를 반올림하지 않는다.
def _value(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


# [추가] 단계 ID는 자료 연결에만 사용하고 표시 대상은 검증된 실행 정의에서 찾는다.
def _steps(definition: WorkflowDefinition) -> dict:
    result = {}
    for step in definition.steps:
        result[step.id] = step
        if isinstance(step, IfStep):
            result.update((child.id, child) for child in (*step.then_steps, *step.else_steps))
    return result


# [추가] 모델이 작성하지 않은 관측값·조건식·조회 횟수·건너뜀과 검증된 문단 요약을 합친다.
def render_execution_report(
    definition: WorkflowDefinition,
    execution: WorkflowExecutionResult,
    summary: RunbookSummaryReport | None = None,
) -> str:
    steps = _steps(definition)
    completed = [event for event in execution.trace if event.status == "completed" and event.node_type != "if"]
    # [추가] 결과와 완료 trace의 연결이 깨지면 잘못된 조회 횟수를 출력하지 않는다.
    if [(event.node_id, event.node_type) for event in completed] != [(item.node_id, item.source) for item in execution.results]:
        raise ValueError("완료 조회 기록과 수집 자료가 일치하지 않습니다.")
    lines = ["1) 관측 사실"]
    monitoring = [item for item in execution.results if isinstance(item.data, MonitoringSnapshot)]
    for item in monitoring:
        data = item.data
        lines.append(
            f"- {data.system_name}: 상태={data.status}, CPU={_value(data.cpu_percent)}%, "
            f"메모리={_value(data.memory_percent)}%, 알림 {len(data.alerts)}건 "
            f"{_value(data.alerts)}, 조회 시각={_value(data.last_checked)}"
        )
    if not monitoring:
        lines.append("- 이번 실행에서 모니터링을 조회하지 않았습니다.")
    books = [item for item in execution.results if item.source == "runbook"]
    lines += ["2) 실행 경로", f"- 완료한 조회: 모니터링 {len(monitoring)}회, 런북 {len(books)}회."]
    for event in execution.trace:
        if event.status == "completed" and event.condition is not None:
            condition = event.condition
            target = steps[condition.ref]
            field = {"status": "상태", "cpu_percent": "CPU 사용률", "memory_percent": "메모리 사용률"}[condition.field]
            lines.append(
                f"- 조건: {target.system} {field}, {_value(condition.actual)} {condition.op} "
                f"{_value(condition.expected)} → {'참' if condition.result else '거짓'}; "
                f"{'참' if condition.selected_branch == 'then_steps' else '거짓'} 분기를 선택했습니다."
            )
        elif event.status == "skipped":
            step = steps[event.node_id]
            target = step.system if isinstance(step, MonitoringStep) else f"{step.incident_type} 런북"
            lines.append(f"- 건너뜀: {target}; 이 단계는 조회하지 않았습니다.")
    sources = build_runbook_sources(execution.to_collected())
    if not books:
        lines.append("- 런북을 조회하지 않았습니다.")
    for source in sources:
        lines.append(f"- 조회한 런북 [{source['source_id']}]: {_value(source['title'])}")
    lines.append("3) 런북 요약·권고와 한계")
    if summary is not None:
        by_id = {source["source_id"]: source for source in sources}
        for item in summary.summaries:
            source = by_id[item.source_id]
            block = next(block for block in source["blocks"] if block["block_id"] == item.block_id)
            lines.append(f"- 런북 요약(모델 생성) [{item.source_id}/{item.block_id}]: {item.summary.strip()}")
            lines.append("  근거 원문(전제조건 포함):")
            lines.extend(f"  | {line}" for line in block["text"].splitlines())
        lines.append("- 위 권고는 문서에 기록된 절차이며, 전제조건의 충족 여부는 별도 확인이 필요합니다.")
    elif not books:
        lines.append("- 조회한 런북이 없어 런북 기반 요약·권고를 생성하지 않았습니다.")
    else:
        lines.append("- 이 사실 보고에는 모델의 런북 요약·권고를 포함하지 않았습니다. 조회한 원문을 별도로 확인하세요.")
    lines.append("- 재시작·롤백·확장 등 운영 조치는 실행하지 않았습니다.")
    lines.append("- 각 조회 시점의 자료만 확보했습니다. 지표의 지속 여부·근본 원인·배포 이력·조치 성공 여부는 확인하지 않았습니다.")
    return "\n".join(lines)

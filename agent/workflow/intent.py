# [추가] 새 파일 전체: 자연어에서 명확한 조회 제약만 추출해 생성 절차의 의도 변조를 차단한다.
"""Conservative checks for the explicitly supported query vocabulary.

This is not a general natural-language parser or proof of semantic equivalence.
It verifies named resources, readable comparisons and references, and rejects
unsupported operations before generation. Unreadable conditional requests need
clarification rather than an invented threshold or target. Independent static
AST/catalog validation is still required.

The six supported comparisons may be negated together with a complete swap
of both branches. These equivalent rewrites are normalized on a private copy.
Count words such as "twice" need explicit separate steps; unseen paraphrases,
pronouns and arbitrary clause scope are not proven correct by this module.
Conditions after repeated observations of the same system need clarification:
this limited parser cannot prove which observation occurrence was intended.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Literal

from .catalog import WorkflowCatalog
from .models import IfStep, MonitoringStep, RunbookStep, WorkflowDefinition


# [추가] 모델 계약을 변경하지 않고 컴파일러가 명시적으로 변환할 수 있는 오류 계약을 둔다.
@dataclass(frozen=True)
class IntentIssue:
    code: Literal["UNSUPPORTED_REQUEST", "INTENT_MISMATCH", "AMBIGUOUS_REQUEST"]
    path: str
    message: str


# [추가] 원문상의 위치를 보존해 비교 조건과 then/else의 자원 언급을 연결한다.
@dataclass(frozen=True)
class _Mention:
    name: str
    start: int
    end: int


# [추가] 확실히 해석한 비교만 저장하며 추측으로 임계값·참조 시스템을 채우지 않는다.
@dataclass(frozen=True)
class _Comparison:
    field: str
    op: str
    value: float | str
    system: str | None
    start: int
    end: int
    alternative_start: int | None = None
    alternative_end: int | None = None


# [추가] 명백한 별칭만 지원하고 새로운 카탈로그 항목에 임의 의미를 부여하지 않는다.
_RUNBOOK_ALIASES = {
    "cpu-spike": r"(?<![a-z_])cpu(?![a-z_])|프로세서",
    "memory-leak": r"메모리|(?<![a-z_])memory(?![a-z_])",
    "disk-full": r"디스크|(?<![a-z_])disk(?![a-z_])",
    "db-down": r"데이터베이스|(?<![a-z_])db(?![a-z_])",
    "high-latency": r"지연|(?<![a-z_])latency(?![a-z_])",
    "network-timeout": r"네트워크|타임아웃|(?<![a-z_])network(?![a-z_])",
}
_FIELD = re.compile(
    r"(?P<cpu>(?<![a-z_])cpu(?:_percent)?(?![a-z_])|프로세서)"
    r"|(?P<memory>메모리|(?<![a-z_])memory(?:_percent)?(?![a-z_]))",
    re.I,
)
_NUMBER = r"-?(?:\d+(?:\.\d+)?|\.\d+)"
_OPERATOR = (
    r">=|<=|==|!=|>|<|이상|이하|초과|미만|넘으면|넘을\s*경우|"
    r"보다\s*크면|보다\s*작으면|크면|작으면|같으면|같을\s*경우|"
    r"아니면|아닐\s*경우|다르면|정확히|이면|일\s*경우"
)
_NUMERIC_TEST = re.compile(
    r"^\s*(?:(?:사용률|사용량|점유율)(?:이|가|은|는)?\s*)?"
    r"(?:가|이|는|은)?\s*"
    r"(?:(?P<before>>=|<=|==|!=|>|<|정확히)\s*)?"
    rf"(?P<value>{_NUMBER})\s*(?:%|퍼센트)?\s*"
    r"(?:를|을|가|이(?!상|하)|와|과)?\s*"
    rf"(?P<after>{_OPERATOR})?",
    re.I,
)
_CONDITIONAL = re.compile(
    r"\b(?:if|when|unless)\b|조건|경우|"
    r"(?:이상|이하|초과|미만)(?:이|일|할|하)?\s*(?:면|때|경우)|넘으면|크면|작으면|아니면|"
    r"높으면|낮으면|많으면|적으면|좋으면|나쁘면|불안정하면|위험하면|같으면|다르면|일\s*때|"
    r"(?:healthy|degraded|down|정상)(?:이|라)?면|"
    r"(?<!아니)(?:이면|라면)",
    re.I,
)
_ELSE = re.compile(r"아니면|그렇지\s*않으면|그\s*외에는|넘지\s*않으면|초과하지\s*않으면|\belse\b|\botherwise\b", re.I)
_UNCONDITIONAL = re.compile(
    r"(?:조건|분기)(?:과|와|에|의)?\s*(?:관계|상관)?없이|"
    r"(?:조건|분기)(?:과|와|에)?\s*무관하게|"
    r"(?:마지막|이후|그\s*후)(?:에는|으로|에)?\s*항상|"
    r"(?:조건|분기)\s*(?:처리)?(?:가)?\s*끝난?\s*(?:뒤|후)|"
    r"어느\s*분기든\s*마친\s*(?:다음|뒤|후)"
)
_RUNBOOK_NOUN = re.compile(r"런북|\brunbooks?\b", re.I)
_CLAUSE_SPLIT = re.compile(
    r"[.;。\n]|(?:이면|라면|넘으면|높으면|낮으면|크면|작으면)|아니면|그렇지\s*않으면|"
    r"(?:이상|이하|초과|미만)(?:이|일|할|하)?\s*(?:면|때|경우)"
)
_ORDINAL = re.compile(
    r"(?P<first>첫\s*번째|첫째|첫\s*조회|처음\s*조회)|"
    r"(?P<second>두\s*번째|둘째|2\s*번째)|"
    r"(?P<third>세\s*번째|셋째|3\s*번째)|(?P<last>마지막)"
)
_COMPLEMENT = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!=", "!=": "=="}


# [추가] 정규식의 영문 식별자 경계를 한국어 조사와 구별한다.
def _named_mentions(text: str, names: tuple[str, ...]) -> list[_Mention]:
    result = []
    for name in names:
        for match in re.finditer(r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", text, re.I):
            result.append(_Mention(name, match.start(), match.end()))
    return sorted(result, key=lambda item: (item.start, item.end))


# [추가] 조건 속 CPU 단어를 CPU 런북 요청으로 잘못 보지 않도록 런북 명사 앞 절만 검사한다.
def _runbook_mentions(text: str, catalog: WorkflowCatalog) -> tuple[list[_Mention], bool]:
    result = _named_mentions(text, catalog.incident_types)
    unresolved = False
    previous_noun_end = 0
    for noun in _RUNBOOK_NOUN.finditer(text):
        # 정확한 자원 ID의 뒤에 붙은 '런북'도 이미 명시된 대상으로 인정한다.
        left_start = max(previous_noun_end, noun.start() - 55)
        left = text[left_start:noun.start()]
        boundaries = list(_CLAUSE_SPLIT.finditer(left))
        if boundaries:
            left_start += boundaries[-1].end()
            left = text[left_start:noun.start()]
        local = [item for item in result if left_start <= item.start < noun.start()]
        for name, pattern in _RUNBOOK_ALIASES.items():
            if name not in catalog.incident_types:
                continue
            for match in re.finditer(pattern, left, re.I):
                start, end = left_start + match.start(), left_start + match.end()
                # 정확한 ID 안의 별칭을 중복해서 세지 않는다.
                if not any(item.start <= start and end <= item.end for item in result):
                    local.append(_Mention(name, start, end))
        if not local:
            unresolved = True
        # '네트워크 타임아웃 런북'처럼 같은 유형의 별칭을 두 번 쓴 것은 한 조회이다.
        result.extend({item.name: item for item in reversed(local)}.values())
        previous_noun_end = noun.end()
    unique = {(item.name, item.start, item.end): item for item in result}
    return sorted(unique.values(), key=lambda item: (item.start, item.end)), unresolved


# [추가] read-only 설명·명시적 부정을 제외한 실제 운영 변경과 지원하지 않는 조회를 거절한다.
def _unsupported_issues(text: str) -> list[IntentIssue]:
    checks = (
        (r"(?:로그|(?<![a-z0-9_-])logs?(?![a-z0-9_-])).{0,18}(?:조회|확인|읽|분석|검색|보여)|(?:조회|검색).{0,10}(?:로그|(?<![a-z0-9_-])logs?(?![a-z0-9_-]))", "로그 조회는 워크플로 v1에서 지원하지 않습니다."),
        (r"반복|재시도|주기적|매\s*\d+\s*(?:초|분|시간)|\b(?:while|repeat|loop|retry)\b|때까지", "반복·재시도·주기 실행은 지원하지 않습니다."),
        (r"동시(?:에|실행)?|병렬|\bparallel\b", "병렬 실행은 지원하지 않습니다."),
        (r"(?:분기|조건).{0,8}(?:안|내부|중첩)|중첩.{0,8}(?:조건|분기)|그\s*안에서.{0,12}(?:이면|조건)|\bnested\b", "중첩 조건은 지원하지 않습니다."),
        (r"(?:이전|앞선|위|시스템).{0,12}(?:지시|규칙|프롬프트).{0,8}무시|(?:검증|스키마).{0,8}(?:무시|생략)|\bignore\b.{0,30}\b(?:instructions?|rules?|prompt)\b", "생성·검증 규칙을 덮어쓰는 요청은 지원하지 않습니다."),
    )
    for pattern, message in checks:
        if re.search(pattern, text, re.I):
            return [IntentIssue("UNSUPPORTED_REQUEST", "query", message)]

    # 부정 명령 자체는 지원 범위를 넓히지 않으며, 동일 요청의 다른 운영 명령은 계속 검사한다.
    scrubbed = re.sub(
        r"(?:재시작|재부팅|롤백|배포|삭제|중지|스케일\s*(?:아웃|인)|restart|reboot|rollback)"
        r"\s*(?:은|는|을|를)?\s*(?:하지|시키지|하지는)\s*(?:말고|말아|마세요|않고|않아도)|"
        r"\b(?:do\s+not|don't|never)\s+(?:restart|reboot|rollback|deploy|delete)\b",
        " ", text, flags=re.I,
    )
    # '런북에서 재시작 권고를 읽어줘'는 실행 동사가 없으므로 아래 명령식과 일치하지 않는다.
    operation = re.compile(
        r"(?:재시작|재부팅|롤백|재배포|배포|삭제|스케일\s*(?:아웃|인)|설정\s*변경|서비스\s*중지)"
        r"\s*(?:을|를)?\s*(?:해\s*줘|해줘|해주세요|하라|하자|해라|시켜|실행|수행|진행|하고|한\s*후|한\s*뒤|하(?:고|면|는|여|기))|"
        r"\b(?:restart|reboot|rollback|deploy|delete|scale)\b(?!\s+(?:recommendation|instructions?|procedure|runbook))",
        re.I,
    )
    if operation.search(scrubbed):
        return [IntentIssue("UNSUPPORTED_REQUEST", "query", "실제 운영 변경은 수행할 수 없습니다. 모니터링·런북 조회만 요청해 주세요.")]
    # 명사형 '조건이면 재시작'도 명령이다. 설명을 읽는 문맥만 좁게 제외한다.
    for match in re.finditer(r"재시작|재부팅|롤백|재배포|배포|삭제|스케일\s*(?:아웃|인)|설정\s*변경|서비스\s*중지", scrubbed):
        suffix = scrubbed[match.end():match.end() + 65]
        descriptive = re.match(r"\s*(?:의|에\s*대한)?\s*(?:권고|권장|절차|설명|방법|내용|부분|관련|가이드|명령|여부|런북)", suffix)
        if descriptive and re.search(r"읽|알려|보여|설명|확인|조회", suffix):
            continue
        return [IntentIssue("UNSUPPORTED_REQUEST", "query", "실제 운영 변경은 수행할 수 없습니다. 모니터링·런북 조회만 요청해 주세요.")]
    return []


# [추가] 표면 표현을 AST 연산자와 일치시키며 경계값을 임의로 느슨하게 바꾸지 않는다.
def _operator(word: str) -> str:
    word = re.sub(r"\s+", "", word)
    if word in {">=", "<=", ">", "<", "==", "!="}:
        return word
    if word == "이상":
        return ">="
    if word == "이하":
        return "<="
    if word in {"초과", "넘으면", "넘을경우", "보다크면", "크면"}:
        return ">"
    if word in {"미만", "보다작으면", "작으면"}:
        return "<"
    if word in {"아니면", "아닐경우", "다르면"}:
        return "!="
    return "=="


# [추가] '조건 없이 순서대로'라는 부정 지시를 조건 요청으로 잘못 분류하지 않는다.
def _has_condition(text: str) -> bool:
    positive = re.sub(
        r"조건(?:을|은|이)?\s*(?:없이|없고|추가하지\s*말고|사용하지\s*말고)|분기\s*없이",
        " ", text,
    )
    positive = _UNCONDITIONAL.sub(" ", positive)
    return _CONDITIONAL.search(positive) is not None


# [추가] 명시된 시스템 또는 조회 순번을 조건 참조로 연결하며 다중 후보를 추측하지 않는다.
def _reference(text: str, position: int, systems: list[_Mention]) -> str | None:
    prior = [item for item in systems if item.start < position]
    names = list(dict.fromkeys(item.name for item in prior))
    prefix = text[max(0, position - 65):position]
    ordinals = list(_ORDINAL.finditer(prefix))
    ordinal_target = None
    if ordinals and names:
        last = ordinals[-1]
        index = {"first": 0, "second": 1, "third": 2, "last": len(names) - 1}[last.lastgroup]
        if index < len(names):
            ordinal_target = names[index]
    if prior:
        nearest = prior[-1]
        gap = text[nearest.end:position]
        # 조건에 이름을 다시 적은 경우 또는 바로 해당 자원의 지표를 말한 경우.
        if len(gap) < 45 and not re.search(r"[.;。\n]|조회하고|조회한\s*(?:뒤|후)|확인하고", gap):
            return nearest.name if ordinal_target in {None, nearest.name} else None
    if ordinal_target is not None:
        return ordinal_target
    return names[0] if len(names) == 1 else None


# [추가] 숫자·상태 비교를 좁은 문법으로 읽고 해석 불가 조건은 명시적으로 남긴다.
def _comparisons(text: str, systems: list[_Mention]) -> list[_Comparison]:
    result = []
    fields = list(_FIELD.finditer(text))
    for index, field in enumerate(fields):
        stop = fields[index + 1].start() if index + 1 < len(fields) else len(text)
        candidate = text[field.end():min(stop, field.end() + 75)]
        match = _NUMERIC_TEST.match(candidate)
        if match is None or not (match["before"] or match["after"]):
            continue
        before, after = match["before"], match["after"]
        if before and after and after not in {"이면", "일 경우"} and _operator(before) != _operator(after):
            continue
        result.append(_Comparison(
            "cpu_percent" if field.lastgroup == "cpu" else "memory_percent",
            _operator(before or after), float(match["value"]),
            _reference(text, field.start(), systems), field.start(), field.end() + match.end(),
        ))
    for match in re.finditer(r"(?<![a-z_])(?P<value>healthy|degraded|down)(?![a-z_])|(?P<normal>정상)(?=\s*(?:상태|이|라|면|가))", text, re.I):
        suffix = text[match.end():match.end() + 20]
        prefix = text[max(0, match.start() - 25):match.start()]
        tail = re.match(r"\s*(?:상태)?\s*(?:가|이|와|과)?\s*(?P<op>아니면|아닐\s*경우|다르면|이면|라면|면|일\s*경우|일\s*때|==|!=)", suffix)
        preceding_op = re.search(r"(?:상태|status)\s*(?P<op>==|!=)\s*$", prefix)
        # 런북 제목 속 down 등은 상태 비교로 보지 않는다.
        if tail is None and preceding_op is None:
            continue
        value = match["value"].lower() if match["value"] else "healthy"
        marker = preceding_op["op"] if preceding_op is not None else tail["op"]
        op = "!=" if re.sub(r"\s+", "", marker) in {"아니면", "!=", "아닐경우", "다르면"} else "=="
        result.append(_Comparison("status", op, value, _reference(text, match.start(), systems), match.start(), match.end() + (tail.end() if tail else 0)))
    ordered = sorted(result, key=lambda item: item.start)
    collapsed: list[_Comparison] = []
    for item in ordered:
        if collapsed:
            previous = collapsed[-1]
            same_input = (previous.field, previous.system, previous.value) == (item.field, item.system, item.value)
            if same_input and _COMPLEMENT[previous.op] == item.op and _RUNBOOK_NOUN.search(text[previous.end:item.start]):
                collapsed[-1] = replace(previous, alternative_start=item.start, alternative_end=item.end)
                continue
        collapsed.append(item)
    return collapsed


# [추가] 같은 시스템의 여러 관측값은 이름만으로 참조를 증명할 수 없어 생성 전에 명확화를 요구한다.
def _repeated_query_reference(text: str, systems: list[_Mention], comparisons: list[_Comparison]) -> bool:
    for condition in comparisons:
        prior = [item for item in systems if item.end <= condition.start]
        actions = 0
        for index, item in enumerate(prior):
            if item.name != condition.system:
                continue
            stop = prior[index + 1].start if index + 1 < len(prior) else condition.start
            # '조회 결과'라는 명사형 재언급은 추가 조회 명령으로 세지 않는다.
            if re.search(r"(?:조회|확인)\s*(?:해|하|한)|\b(?:check|monitor|observe)\b", text[item.end:stop], re.I):
                actions += 1
        if actions > 1:
            return True
    return False


# [추가] 연산자 반전과 양분기 교환이 함께 일어난 경우만 원문 방향으로 돌려 기존 순서 검사에 사용한다.
def _normalised_definition(definition: WorkflowDefinition, comparisons: list[_Comparison]) -> WorkflowDefinition:
    payload = definition.model_dump(mode="json")
    conditions = [step for step in payload["steps"] if step["type"] == "if"]
    for actual, expected in zip(conditions, comparisons):
        condition = actual["condition"]
        if (
            condition["field"] == expected.field
            and condition["value"] == expected.value
            and condition["op"] == _COMPLEMENT[expected.op]
        ):
            condition["op"] = expected.op
            actual["then_steps"], actual["else_steps"] = actual["else_steps"], actual["then_steps"]
    return WorkflowDefinition.model_validate(payload)


# [추가] 생성 전에는 지원 범위와 해석 가능한 최소 정보를 확인한다.
def query_issues(query: str, catalog: WorkflowCatalog) -> list[IntentIssue]:
    text = query.lower()
    unsupported = _unsupported_issues(text)
    if unsupported:
        return unsupported
    systems = _named_mentions(text, catalog.systems)
    runbooks, unresolved = _runbook_mentions(text, catalog)
    known = set(catalog.systems) | set(catalog.incident_types)
    for match in re.finditer(r"(?<![a-z0-9_-])[a-z][a-z0-9]*(?:-[a-z0-9]+)+(?![a-z0-9_-])", text):
        if match.group() not in known:
            return [IntentIssue("UNSUPPORTED_REQUEST", "query", f"카탈로그에 없는 식별자입니다: {match.group()}")]
    if unresolved:
        return [IntentIssue("AMBIGUOUS_REQUEST", "query", "조회할 런북 유형을 명시해 주세요. 예: CPU 런북, 메모리 런북.")]
    if re.search(r"(?:두|세|네|\d+)\s*번\s*(?:조회|확인|읽)|\b(?:twice|thrice)\b", text):
        return [IntentIssue("AMBIGUOUS_REQUEST", "query", "횟수 표현 대신 각 조회 단계에 시스템·런북 이름을 각각 명시해 주세요.")]
    if not systems and not runbooks:
        return [IntentIssue("AMBIGUOUS_REQUEST", "query", "조회할 시스템 또는 런북을 명시해 주세요.")]
    if _has_condition(text):
        comparisons = _comparisons(text, systems)
        if not comparisons:
            return [IntentIssue("AMBIGUOUS_REQUEST", "query", "조건의 지표·비교 연산·수치 또는 상태를 명시해 주세요.")]
        # 하나를 읽었다고 나머지 모호한 조건을 무시하지 않는다.
        fields = list(_FIELD.finditer(text))
        for index, field in enumerate(fields):
            stop = fields[index + 1].start() if index + 1 < len(fields) else len(text)
            tail = text[field.end():min(stop, field.end() + 70)]
            noun = _RUNBOOK_NOUN.search(tail)
            if noun:
                tail = tail[:noun.start()]
            if _CONDITIONAL.search(tail) and not any(field.start() in {item.start, item.alternative_start} for item in comparisons):
                return [IntentIssue("AMBIGUOUS_REQUEST", "query", "모든 조건의 비교 연산과 기준값을 명시해 주세요.")]
        if any(item.system is None for item in comparisons):
            return [IntentIssue("AMBIGUOUS_REQUEST", "query", "각 조건이 어느 시스템의 조회 결과를 참조하는지 명시해 주세요.")]
        if _repeated_query_reference(text, systems, comparisons):
            return [IntentIssue("AMBIGUOUS_REQUEST", "query", "동일 시스템을 여러 번 조회한 뒤의 조건 참조는 지원하지 않습니다. 조건별로 별도의 요청을 작성해 주세요.")]
        # 하나의 if에 여러 비교를 결합하는 and/or와 중첩은 현재 AST로 보존할 수 없다.
        for first, second in zip(comparisons, comparisons[1:]):
            between = text[first.end:second.start]
            if not _RUNBOOK_NOUN.search(between) and re.search(r"\b(?:and|or)\b|(?:이)?고\s*$|또는\s*$|이면서\s*$", between):
                return [IntentIssue("UNSUPPORTED_REQUEST", "query", "한 조건의 AND·OR 결합은 지원하지 않습니다.")]
            if not _RUNBOOK_NOUN.search(between) and not _ELSE.search(between) and re.search(r"면", between):
                return [IntentIssue("UNSUPPORTED_REQUEST", "query", "중첩·복합 조건은 지원하지 않습니다.")]
    return []


# [추가] 생성된 AST의 자원·조건·참조·분기를 원문에서 확인한 제약과 대조한다.
def definition_issues(query: str, definition: WorkflowDefinition, catalog: WorkflowCatalog) -> list[IntentIssue]:
    initial = query_issues(query, catalog)
    if initial:
        return initial
    text = query.lower()
    systems = _named_mentions(text, catalog.systems)
    runbooks, _ = _runbook_mentions(text, catalog)
    expected_conditions = _comparisons(text, systems) if _has_condition(text) else []
    definition = _normalised_definition(definition, expected_conditions)
    issues: list[IntentIssue] = []
    expected_systems = list(dict.fromkeys(item.name for item in systems))
    expected_runbooks = [item.name for item in runbooks]
    steps = list(definition.iter_steps())
    actual_systems = [step.system for step in steps if isinstance(step, MonitoringStep)]
    actual_runbooks = [step.incident_type for step in steps if isinstance(step, RunbookStep)]
    if set(actual_systems) != set(expected_systems):
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "요청에 명시한 시스템이 누락되었거나 요청하지 않은 시스템이 추가되었습니다."))
    elif list(dict.fromkeys(actual_systems)) != expected_systems:
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "시스템의 명시된 조회 순서가 바뀌었습니다."))
    if actual_runbooks != expected_runbooks:
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "명시한 런북의 종류·조회 순서·횟수가 생성 절차와 다릅니다."))
    if any(count > Counter(item.name for item in systems)[name] for name, count in Counter(actual_systems).items()):
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "요청에 없는 추가 모니터링 조회가 삽입되었습니다."))
    actual_conditions = [(index, step) for index, step in enumerate(definition.steps) if isinstance(step, IfStep)]
    if len(actual_conditions) != len(expected_conditions):
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "요청의 조건 수와 생성 절차의 조건 수가 다릅니다. 순차 요청에 조건을 추가할 수 없습니다."))
    monitoring_by_id = {step.id: step.system for step in definition.steps if isinstance(step, MonitoringStep)}
    top_level_counts = Counter(step.system for step in definition.steps if isinstance(step, MonitoringStep))
    for index, step in actual_conditions:
        referenced_system = monitoring_by_id.get(step.condition.ref)
        if referenced_system is not None and top_level_counts[referenced_system] > 1:
            return [IntentIssue("AMBIGUOUS_REQUEST", f"steps[{index}].condition.ref", "동일 시스템의 복수 관측값 중 어느 조회를 참조했는지 이 검증기로 보장할 수 없습니다.")]
    branch_ranges: list[tuple[int, int]] = []
    for position, (expected, (index, step)) in enumerate(zip(expected_conditions, actual_conditions)):
        condition = step.condition
        path = f"steps[{index}].condition"
        if (condition.field, condition.op, condition.value) != (expected.field, expected.op, expected.value):
            issues.append(IntentIssue("INTENT_MISMATCH", path, "요청의 지표·비교 연산자·기준값이 변경되었습니다."))
        if monitoring_by_id.get(condition.ref) != expected.system:
            issues.append(IntentIssue("INTENT_MISMATCH", path + ".ref", "조건이 요청에서 지정한 시스템의 조회 결과를 참조하지 않습니다."))
        # 다음 조건 전까지 명시된 then/else 자원을 각각 보존한다.
        end = expected_conditions[position + 1].start if position + 1 < len(expected_conditions) else len(text)
        unconditional = _UNCONDITIONAL.search(text, expected.end, end)
        if unconditional:
            end = unconditional.start()
        otherwise = _ELSE.search(text, expected.end, end)
        else_start = otherwise.start() if otherwise else None
        else_end = otherwise.end() if otherwise else None
        if expected.alternative_start is not None and expected.alternative_start < end:
            else_start, else_end = expected.alternative_start, expected.alternative_end
        elif expected.op == "!=" and isinstance(expected.value, float):
            # '50이 아니면 A, 정확히 50이면 B'의 명시적인 보완 분기를 읽는다.
            number = re.escape(f"{expected.value:g}")
            equal = re.search(rf"(?:정확히\s*)?{number}\s*%?\s*(?:이)?면", text[expected.end:end])
            if equal:
                else_start = expected.end + equal.start()
                else_end = expected.end + equal.end()
        then_end = else_start if else_start is not None else end
        branch_ranges.append((expected.end, end))
        for branch, start, stop in (
            ("then_steps", expected.end, then_end),
            ("else_steps", else_end if else_end is not None else end, end),
        ):
            named = [item.name for item in runbooks if start <= item.start < stop]
            actual = [item.incident_type for item in getattr(step, branch) if isinstance(item, RunbookStep)]
            if actual != named:
                issues.append(IntentIssue("INTENT_MISMATCH", f"steps[{index}].{branch}", "조건의 참·거짓 분기에 지정한 런북 조회가 바뀌었습니다."))
            named_systems = [item.name for item in systems if start <= item.start < stop]
            actual_systems_in_branch = [item.system for item in getattr(step, branch) if isinstance(item, MonitoringStep)]
            if actual_systems_in_branch != named_systems:
                issues.append(IntentIssue("INTENT_MISMATCH", f"steps[{index}].{branch}", "조건의 참·거짓 분기에 지정한 모니터링 조회 범위가 바뀌었습니다."))
            expected_actions = sorted(
                [(item.start, "monitoring", item.name) for item in systems if start <= item.start < stop]
                + [(item.start, "runbook", item.name) for item in runbooks if start <= item.start < stop]
            )
            actual_actions = [(item.type, item.system if isinstance(item, MonitoringStep) else item.incident_type) for item in getattr(step, branch)]
            if actual_actions != [(kind, name) for _, kind, name in expected_actions]:
                issues.append(IntentIssue("INTENT_MISMATCH", f"steps[{index}].{branch}", "분기 안의 모니터링·런북 조회 순서가 바뀌었습니다."))
    # 조건 참조에서 같은 이름을 다시 쓴 것을 추가 조회로 세지 않는다.
    references: set[int] = set()
    for condition in expected_conditions:
        prior = [item for item in systems if item.end <= condition.start]
        if len(prior) >= 2:
            last = prior[-1]
            if any(item.name == last.name for item in prior[:-1]) and not re.search(r"조회|확인|monitor|check", text[last.end:condition.start]):
                references.add(last.start)
    expected_top = sorted(
        [(item.start, "monitoring", item.name) for item in systems if item.start not in references and not any(start <= item.start < stop for start, stop in branch_ranges)]
        + [(item.start, "runbook", item.name) for item in runbooks if not any(start <= item.start < stop for start, stop in branch_ranges)]
        + [(item.start, "if", "") for item in expected_conditions]
    )
    actual_top = [(item.type, item.system if isinstance(item, MonitoringStep) else item.incident_type if isinstance(item, RunbookStep) else "") for item in definition.steps]
    if actual_top != [(kind, name) for _, kind, name in expected_top]:
        issues.append(IntentIssue("INTENT_MISMATCH", "steps", "순차 조회·조건·무조건 후속 단계의 순서 또는 횟수가 바뀌었습니다."))
    return issues

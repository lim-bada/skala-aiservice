# [추가] 기존 보고 노드를 새 비동기 모델·실행 근거·시간 제한에 연결하는 신규 파일 전체.
"""형식 검증과 조회 전용 실행 범위의 명백한 모순 차단을 분리한다.

근거 검사는 운영 조치의 완료 주장 등 한정된 표현만 검사하며 전체 자연어의
사실성·수치·인과 관계를 보장하는 판정기가 아니다.
"""
import asyncio
import re
from copy import deepcopy
from time import monotonic
from typing import Protocol

from .models import WorkflowModelResponse, WorkflowRunIssue

# [추가] 보고 재작성과 출력 크기를 제한하며 최대 시도 도달을 승인 조건으로 삼지 않는다.
MAX_REPORT_ATTEMPTS = 3
MAX_REPORT_LENGTH = 12_000


# [추가] 실제 모델과 fake·테스트 모델을 동일한 비동기 보고 계약으로 주입한다.
class WorkflowReportLLM(Protocol):
    async def write_workflow(
        self, query: str, collected: list[dict], trace: list[dict],
        attempt: int, feedback: str | None, on_token=None,
    ) -> WorkflowModelResponse: ...


# [추가] 모델 거절·연동 실패·시간 초과를 보고 노드에서 구조화해 전달한다.
class WorkflowReportFailure(Exception):
    def __init__(self, issue: WorkflowRunIssue):
        super().__init__(issue.message)
        self.issue = issue


# [추가] 형식 검증과 사실·인과 검증을 혼동하지 않고 실패 이유를 재작성에 사용한다.
def validate_report(draft: str, *, complete: bool = True) -> list[str]:
    errors = []
    if not complete:
        errors.append("모델 응답이 정상적으로 끝나지 않았습니다.")
    if len(draft.strip()) < 30:
        errors.append("보고 본문은 앞뒤 공백을 제외하고 최소 30자여야 합니다.")
    if len(draft) > MAX_REPORT_LENGTH:
        errors.append(f"보고는 {MAX_REPORT_LENGTH}자 이하여야 합니다.")
    sections = list(re.finditer(r"(?<!\d)([123])\)", draft))
    # [추가] 개행·인라인 형식을 모두 받되 1) → 2) → 3)의 순서를 요구한다.
    expected = iter(("1", "2", "3"))
    target = next(expected)
    for section in sections:
        if section.group(1) == target:
            target = next(expected, None)
            if target is None:
                break
    if target is not None:
        errors.append("보고에 1) 관측, 2) 조건·자료, 3) 권고·한계를 순서대로 작성하세요.")
    return errors


# [추가] 실행기가 제공하지 않는 변경 조치의 명시적 완료 표현만 좁게 식별한다.
_MUTATION_KO = r"(?:재시작|재부팅|리부팅|롤백|스케일\s*(?:업|아웃|다운|인)|(?:오토)?스케일링|확장|축소|배포|복구|정상화)"
_PAST_KO = r"(?:했(?:습니다|다|음)|하였(?:습니다|다|음)|됐(?:습니다|다|음)|되었(?:습니다|다|음)|됨|함)"
_MUTATION_COMPLETION = re.compile(
    rf"{_MUTATION_KO}(?:{_PAST_KO}|(?:되었|됐|했)(?:으며|고)|"
    rf"(?:(?!{_MUTATION_KO})[^\n.!?;。]){{0,65}}?(?:(?:실행|수행|적용){_PAST_KO}|(?:완료|성공)(?:{_PAST_KO})?))"
    r"|\b(?:restarted|rebooted|redeployed|restored|recovered|rolled\s+back|scaled\s+(?:up|out|down|in))\b"
    r"|\b(?:restart|reboot|rollback|roll\s*back|scal(?:e|ing)|deployment|recovery|restoration)\b"
    r"[^\n.!?;。]{0,45}?\b(?:completed|succeeded|performed|executed|applied)\b",
    re.IGNORECASE,
)


# [추가] 권고·가정·부정·미확인과 완료 사실을 구분하되 다른 절의 부정으로 완료 주장을 덮지 않는다.
def _non_asserted_completion(claim: str, before: str, after: str) -> bool:
    # [추가] '미완료'는 완료 어절과 의미가 다르고 권고안 작성 완료는 운영 조치가 아니다.
    if re.search(r"(?:미|불)\s*$", before):
        return True
    if re.search(r"(?:권고|제안|추천)(?:안|사항)?(?:을|를)?\s*(?:작성|제시|정리|완료)", claim):
        return True
    if re.search(
        r"(?:(?:런북|문서)[^.!?;\n]{0,25}(?:조회|열람|읽기)|"
        r"(?:보고서|보고|요약)[^.!?;\n]{0,15}(?:작성|생성)|"
        r"(?:여부|이력|기록|절차)[^.!?;\n]{0,15}(?:조회|확인|분석|검토))[^.!?;\n]{0,8}완료",
        claim,
    ):
        return True
    if re.search(r"(?:하지|되지|하지는|되지는)\s*(?:않|못)|(?:미실행|미수행|미완료)", claim):
        return True
    # [추가] 완료 여부·가정·조건·필요성을 설명하는 구절은 실행 사실을 단정하지 않는다.
    if re.match(
        r"\s*(?:(?:되|하)?(?:지\s*(?:않|못))|(?:여부|시(?:에는|에|점)?|후에?)(?:\s|[는를가,])|"
        r"(?:했|됐|되었)?(?:다면|으면|더라도)|면\b|(?:가|는|를|을|이)?\s*(?:필요|권고|제안|추천)|"
        r"(?:한|된|고|다고|는|했다는|됐다는)?\s*(?:것으로\s*)?(?:가정|예시)|"
        r"(?:는|고|다고|했다는|됐다는)?\s*(?:사실|근거|증거|주장)(?:을|를|는|이|가)?\s*(?:없|확인하지|확인되지)|"
        r"(?:은|는|을|를|이|가)?\s*(?:확인|보장)(?:하지|되지)\s*(?:않|못)|"
        r"(?:는|다는)?\s*뜻이\s*아니|(?:고|다고)\s*(?:볼|말할|주장할)\s*수\s*없)",
        after,
    ):
        return True
    if re.search(r"\b(?:not|never|wasn['’]?t|weren['’]?t|isn['’]?t)\b[^.!?;\n]{0,25}\b(?:completed|succeeded|performed|executed|applied)\b", claim, re.I):
        return True
    if re.search(r"\b(?:no|not|never|haven['’]?t|hasn['’]?t|wasn['’]?t|weren['’]?t|isn['’]?t)\s+(?:(?:been|actually|yet)\s+)*$", before, re.I):
        return True
    if re.match(r"\s*(?:is\s+|was\s+|has\s+been\s+)?(?:unverified|not\s+verified|not\s+confirmed|unknown|if\b)", after, re.I):
        return True
    if re.search(r"\b(?:if|assuming|hypothetically)\b[^.!?;\n]{0,35}$", before, re.I):
        return True
    if re.search(r"\b(?:cannot\s+confirm|unconfirmed\s+whether|unknown\s+whether|recommend(?:ed)?[^.!?;\n]{0,25}\bbe)\b[^.!?;\n]{0,30}$", before, re.I):
        return True
    # [추가] 문서의 절차를 직접 인용하는 경우에는 인용 안의 과거형을 이 실행의 완료로 보지 않는다.
    if re.search(r"(?:런북|문서|예시|알림)(?:에는|에서|의|는|에)?[^.!?;\n]{0,15}[\"'“‘][^\"'”’]*$", before):
        return True
    return False


# [추가] 실제 조회한 대상의 현재값을 미수집이라고 명시적으로 부정하는 제한된 표현을 검사한다.
def _missing_fact_claims(draft: str, collected: list[dict]) -> list[str]:
    errors = []
    for item in collected:
        data = item.get("data")
        if item.get("source") != "monitoring" or not isinstance(data, dict):
            continue
        target = data.get("system_name")
        if not isinstance(target, str):
            continue
        # [추가] 대상이 명시된 현재 메모리·상태 부정만 검사해 다른 시스템·시계열 한계를 보존한다.
        fields = []
        if "memory_percent" in data:
            fields.append(r"(?:메모리(?:\s*사용률)?|memory(?:_percent)?)")
        if "status" in data:
            fields.append(r"(?:상태(?:\s*정보)?|status)")
        if not fields:
            continue
        absent = r"(?:조회|확인|수집|확보)(?:하지|되지)\s*(?:않|못)"
        explicit = re.compile(
            rf"{re.escape(target)}(?:의|는|에\s*대한)?\s*(?:{'|'.join(fields)})"
            rf"(?:은|는|을|를|가|이)?\s*(?:이번에\s*)?{absent}", re.I,
        )
        # [추가] 관측 CPU만 읽었다는 전체 범위 부정은 같은 스냅샷의 상태·메모리와 모순이다.
        cpu_only = re.compile(
            rf"{re.escape(target)}(?:의)?\s*CPU(?:\s*사용률)?\s*(?:외에?|이외에?)\s*"
            rf"다른\s*(?:성능\s*)?(?:지표|상태)(?:나\s*(?:지표|상태))?\s*(?:는|를|을|은)?\s*{absent}", re.I,
        )
        if explicit.search(draft) or cpu_only.search(draft):
            errors.append(
                f"{target}의 실제 모니터링 스냅샷에 포함된 현재값을 미수집이라고 쓰지 마세요. "
                "현재 상태·메모리와 원인·시계열의 미확인을 구분하세요."
            )
    return errors


# [추가] 조회 전용 실행의 명백한 허위 완료·일부 근거 부정을 재작성 사유로 반환한다.
def validate_grounded_report(draft: str, collected: list[dict], trace: list[dict]) -> list[str]:
    """한정된 한국어·영어 표현의 모순을 차단하며 일반 사실성 판정으로 사용하지 않는다.

    이 실행기는 monitoring/runbook/if만 지원하므로 trace의 사용자 지정 ID나
    'completed' 문자열은 변경 수행의 증거가 아니다. trace는 인터페이스 일관성을
    위해 받으며 변경 가능성을 ID에서 추론하지 않는다. 수치 전수 검증은 하지 않는다.
    """
    errors = []
    # [추가] 절 경계를 넘는 부정·권고가 별도 완료 주장에 영향을 주지 않게 국소 문맥만 읽는다.
    for segment in re.split(r"[\n.!?;。]|(?:그러나|하지만)\s*", draft):
        for match in _MUTATION_COMPLETION.finditer(segment):
            before, after = segment[max(0, match.start() - 65):match.start()], segment[match.end():match.end() + 65]
            if not _non_asserted_completion(match.group(), before, after):
                errors.append(
                    "조회 전용 실행에는 변경 도구와 수행 기록이 없습니다. 재시작·롤백·확장·배포·복구의 "
                    "완료를 주장하지 말고, 실제 조회와 향후 권고를 구분하세요."
                )
                break
        if errors:
            break
    errors.extend(_missing_fact_claims(draft, collected))
    return errors


# [추가] 한 번의 write_node 호출에만 사용하는 어댑터로 실행 간 상태 공유를 막는다.
class WorkflowReportBridge:
    def __init__(
        self, llm: WorkflowReportLLM, trace: list[dict], deadline: float, on_token=None,
    ):
        self.llm = llm
        self.trace = deepcopy(trace)
        self.deadline = deadline
        self.response: WorkflowModelResponse | None = None
        # [추가] 기존 노드의 토큰 이벤트에 실행 메타데이터를 붙여 전달할 수 있게 한다.
        self.on_token = on_token

    # [추가] 기존 write(query,collected,attempt,feedback,on_token) 계약을 그대로 제공한다.
    async def write(
        self, query: str, collected: list[dict], attempt: int,
        feedback: str | None, on_token=None,
    ) -> tuple[str, dict]:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise WorkflowReportFailure(WorkflowRunIssue(
                code="WORKFLOW_TIMEOUT", stage="report", message="보고 작성 전에 전체 실행 시간이 만료됐습니다."
            ))
        timer = asyncio.timeout(remaining)
        try:
            async with timer:
                response = await self.llm.write_workflow(
                    query, deepcopy(collected), deepcopy(self.trace), attempt, feedback,
                    on_token=self.on_token if self.on_token is not None else on_token,
                )
                self.response = WorkflowModelResponse.model_validate(response)
        except TimeoutError as error:
            raise WorkflowReportFailure(WorkflowRunIssue(
                code="WORKFLOW_TIMEOUT" if timer.expired() else "REPORT_ERROR", stage="report",
                message="조회와 보고를 합친 실행 제한 시간을 초과했습니다."
                if timer.expired() else "보고 모델 호출이 시간 초과 오류를 반환했습니다.",
            )) from error
        except Exception as error:
            raise WorkflowReportFailure(WorkflowRunIssue(
                code="REPORT_ERROR", stage="report",
                message=f"보고 모델 호출 또는 응답 처리에 실패했습니다 ({type(error).__name__}).",
            )) from error
        if self.response.refusal is not None:
            raise WorkflowReportFailure(WorkflowRunIssue(
                code="REPORT_REFUSED", stage="report",
                message=self.response.refusal.strip() or "모델이 보고 작성을 거절했습니다.",
            ))
        # [추가] usd는 기존 write_node의 _cost 호환값이며 공개 결과에 비용으로 표시하지 않는다.
        # 실제 보고 사용량은 response.usage로 별도 보존하며 새 경로에서 가상 USD를 계산하지 않는다.
        return self.response.content, {"usd": 0.0}

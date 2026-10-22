# [추가] 파일 전체: 읽기 전용 보고의 명백한 허위 완료 차단과 권고·부정 허용 범위를 검증한다.
"""실제 LLM을 호출하지 않고 제한된 검증 경계와 프롬프트의 사실 연결을 확인한다."""
import json
import unittest
from copy import deepcopy

from agent.workflow.report_prompting import build_workflow_report_messages
from agent.workflow.reporting import validate_grounded_report, validate_report


# [추가] 노드 이름은 동작과 다를 수 있으며 조건·자료의 실제 연결은 실행 순서에 따른다.
def inputs():
    collected = [
        {"source": "monitoring", "data": {
            "system_name": "payments-service", "status": "degraded",
            "cpu_percent": 91.0, "memory_percent": 61.0,
            "last_checked": "2026-10-21T00:00:00Z", "alerts": ["CPU 경고"],
        }},
        {"source": "runbook", "data": "# CPU 런북\nCPU가 90%를 넘고 지속되면 확장을 검토한다."},
    ]
    trace = [
        {"node_id": "restart_service", "node_type": "monitoring", "status": "started"},
        {"node_id": "restart_service", "node_type": "monitoring", "status": "completed"},
        {"node_id": "deploy_branch", "node_type": "if", "status": "completed", "condition": {
            "ref": "restart_service", "field": "cpu_percent", "actual": 91.0,
            "op": ">=", "expected": 80, "result": True, "selected_branch": "then_steps",
        }},
        {"node_id": "restore_done", "node_type": "runbook", "status": "skipped"},
        {"node_id": "rollback_done", "node_type": "runbook", "status": "completed"},
    ]
    return collected, trace


# [추가] 완결된 조치 주장과 권고·부정·가정이 섞여도 실행하지 않은 조치를 승인하지 않는다.
class ReadOnlyReportGroundingTests(unittest.TestCase):
    def setUp(self):
        self.collected, self.trace = inputs()

    def check(self, text):
        return validate_grounded_report(text, self.collected, self.trace)

    # [추가] 실제 평가에서 관측한 허위 재시작 문장과 명백한 완료 표현을 회귀 사례로 고정한다.
    def test_rejects_explicit_mutation_completion_claims(self):
        claims = [
            "그러나 이번 워크플로에서는 서비스를 재시작하는 조치가 실행되었다.",
            "서비스를 재시작했습니다.", "서비스 재시작을 수행했습니다.",
            "서버 재부팅 완료.", "배포를 롤백했다.", "롤백이 성공했다.",
            "인스턴스 확장을 완료했습니다.", "오토스케일링을 실행했다.",
            "새 버전을 배포했습니다.", "서비스를 복구했다.", "서비스가 정상화되었습니다.",
            "재시작은 수행하지 않았으나 롤백을 완료했다.",
            "재시작을 권고합니다. 확장은 완료했습니다.",
            "We restarted the service.", "The service was rebooted.",
            "Rollback completed.", "We successfully rolled back the deployment.",
            "The service was scaled out.", "Recovery succeeded.",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertTrue(self.check(claim), claim)

    # [추가] 런북 요약·향후 권고·미확인·조건문을 허위 완료로 잘못 차단하지 않는다.
    def test_allows_recommendations_negations_hypotheses_and_uncertainty(self):
        descriptions = [
            "재시작을 권고합니다.", "롤백은 권고이며 실행한 조치가 아니다.",
            "확장 수행이 필요합니다.", "서비스 재시작을 수행하지 않았습니다.",
            "재시작은 완료되지 않았습니다.", "재시작은 미완료 상태입니다.",
            "재시작 완료 여부는 확인하지 못했다.", "롤백 수행 여부 미확인.",
            "재시작을 완료했다면 상태를 다시 확인하세요.",
            "재시작 완료 후 서비스 상태를 확인하도록 런북이 권고한다.",
            "재시작 권고안을 작성 완료했습니다.",
            "확장을 권고하는 런북 조회를 완료했습니다.",
            "배포 이력 조회를 완료했습니다.", "복구 절차 확인을 완료했다.",
            "재시작 보고서 작성을 완료했다.",
            "확장 작업의 완료를 권고합니다.", "복구가 필요하며 성공 여부는 미확인입니다.",
            "런북에는 ‘재시작 완료’ 후 상태 확인이 필요하다고 적혀 있다.",
            "런북 조회를 완료했으나 재시작은 수행하지 않았다.",
            "실제 복구 성공 여부는 확인하지 않았습니다.",
            "재시작을 실행했다는 뜻이 아니다.",
            "재시작을 수행했다고 볼 수 없다.", "재시작을 실행했다는 증거가 없다.",
            "재시작 완료가 확인되지 않았다.", "복구 성공은 보장하지 않는다.",
            "조회한 결과 현재 상태는 healthy입니다.",
            "We did not restart the service.", "The service was not restarted.",
            "Restart has not been performed.", "Restart wasn't completed.",
            "We have not yet restarted the service.", "No restart was completed.",
            "We cannot confirm the service was restarted.",
            "We recommended that the service be restarted.",
            "If restarted, verify monitoring again.", "Recovery completed is unverified.",
            "We recommend restarting or scaling out the service.",
            "Rollback execution is recommended, not performed.",
            "The runbook read completed. No infrastructure changes were performed.",
        ]
        for description in descriptions:
            with self.subTest(description=description):
                self.assertEqual(self.check(description), [], description)

    # [추가] 임의 노드 ID·completed 상태·런북의 변경 지침은 실제 변경 증거로 인정하지 않는다.
    def test_node_names_and_query_do_not_authorize_changes(self):
        self.trace.append({"node_id": "restart_complete", "node_type": "runbook", "status": "completed"})
        self.assertTrue(self.check("요청에 따라 재시작을 완료했다."))
        self.assertEqual(self.check("런북 조회 완료. 재시작은 권고이며 실제 실행하지 않았다."), [])

    # [추가] 실제 조회한 동일 대상의 현재 메모리·상태 부정만 제한적으로 차단한다.
    def test_rejects_explicit_denial_of_collected_current_values(self):
        for text in [
            "payments-service의 메모리는 확인하지 않았다.",
            "payments-service 상태는 수집하지 못했습니다.",
            "payments-service의 CPU 사용률 외에 다른 성능 지표나 상태는 확인하지 않았다.",
        ]:
            with self.subTest(text=text):
                self.assertTrue(self.check(text), text)
        for text in [
            "cache-service의 상태는 조회하지 않았으므로 추가 확인이 필요하다.",
            "payments-service의 메모리 시계열은 확인하지 않았다.",
            "payments-service의 메모리 누수 원인은 확인하지 않았다.",
            "payments-service의 상태 변화 이력은 확인하지 않았다.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.check(text), [], text)

    # [추가] 일반 수치·근본 원인까지 검증한 것으로 오인하지 않도록 검사 범위를 명시한다.
    def test_no_generic_numeric_or_causal_fact_check_is_claimed(self):
        self.assertEqual(self.check("현재 CPU는 91%이며 런북 임계값 90%와는 다릅니다."), [])
        # 범위 밖 오류도 통과할 수 있으므로 보고 승인과 전면적인 사실성을 동일시할 수 없다.
        self.assertEqual(self.check("현재 CPU는 1%이고 배포가 모든 문제의 원인입니다."), [])

    # [추가] 기존 형식 검증 함수는 호출·결과 호환성을 유지하고 근거 검증과 별도로 사용할 수 있다.
    def test_existing_format_validation_is_unchanged(self):
        report = "1) CPU를 확인했습니다.\n2) 서비스를 재시작했습니다.\n3) 추가 원인 확인이 필요합니다."
        self.assertEqual(validate_report(report), [])
        self.assertTrue(self.check(report))


# [추가] 실행 사실을 요약 모델에 재전송하지 않고 실제 문서 문단만 전달하는 경계를 검증한다.
class ReadOnlyReportPromptTests(unittest.TestCase):
    def test_query_node_names_and_snapshots_are_not_model_evidence(self):
        collected, trace = inputs()
        originals = deepcopy((collected, trace))
        messages = build_workflow_report_messages("CPU가 높으면 재시작해줘.", collected, trace, 1, None)
        payload = json.loads(messages[1]["content"])
        for excluded in ("query", "trace", "collected", "read_only_execution_contract"):
            self.assertNotIn(excluded, payload)
        for excluded in ("restart_service", "rollback_done", "91.0", "재시작해줘"):
            self.assertNotIn(excluded, messages[1]["content"])
        self.assertEqual((collected, trace), originals)

    # [추가] 문서의 수치·기간을 유지한 완전한 문단을 근거로 전달한다.
    def test_actual_document_paragraph_is_preserved(self):
        collected, trace = inputs()
        payload = json.loads(build_workflow_report_messages("실행 요청", collected, trace, 1, None)[1]["content"])
        self.assertEqual(payload["runbook_sources"], [{
            "source_id": "runbook_001", "title": "CPU 런북",
            "blocks": [{"block_id": "block_001", "text": "CPU가 90%를 넘고 지속되면 확장을 검토한다."}],
        }])

    # [추가] 임의의 trace 이름으로 조회하지 않은 문서를 모델 입력에 만들지 않는다.
    def test_trace_cannot_invent_runbook_sources(self):
        collected, trace = inputs()
        payload = json.loads(build_workflow_report_messages("확인", collected[:1], trace, 1, None)[1]["content"])
        self.assertEqual(payload["runbook_sources"], [])


if __name__ == "__main__":
    unittest.main()

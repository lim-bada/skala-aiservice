# [추가] 사용자 보고 오류를 고정 관측값·실제 호출 기록으로 검증하는 신규 회귀 테스트 전체.
"""실제 모델 없이 수치·반대 조건·조회 생략·허위 자료 참조·스트림 노출을 확인한다."""
import json
import unittest
from copy import deepcopy

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.graph import build_workflow_graph, run_workflow
from agent.workflow.report_content import build_runbook_sources, parse_runbook_summary
from tests.test_workflow_execution import FakeMCP, conditional_definition, monitoring_snapshot
from tests.test_workflow_graph import FakeReportLLM, report_response


# [추가] 코드의 렌더러를 정답 생성에 사용하지 않고 사용자 사례의 정의를 독립적으로 준비한다.
def healthy_definition(op="!="):
    book = {"id": "cpu", "type": "runbook", "incident_type": "cpu-spike"}
    return {"schema_version": 1, "name": "정상이면 조회 종료", "steps": [
        {"id": "web", "type": "monitoring", "system": "web-01"},
        {"id": "check", "type": "if", "condition": {
            "ref": "web", "field": "status", "op": op, "value": "healthy",
        }, "then_steps": [book] if op == "!=" else [], "else_steps": [] if op == "!=" else [book]},
    ]}


# [추가] 실제 문서·문단 ID만 포함한 모델 요약을 테스트마다 제어한다.
def summary_json(text="런북은 CPU 사용량을 확인하도록 안내합니다.", **changes):
    return json.dumps({"summaries": [{
        "source_id": "runbook_001", "block_id": "block_001", "summary": text, **changes,
    }]}, ensure_ascii=False)


# [추가] 최종 문장뿐 아니라 MCP 호출과 모델 호출까지 일치해야 정상으로 판단한다.
class WorkflowReportFactsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.catalog = WorkflowCatalog(systems=("web-01", "payments-service"), incident_types=("cpu-spike", "memory-leak"))
        self.mcp = FakeMCP()
        self.mcp.snapshots["web-01"] = monitoring_snapshot("web-01", status="healthy", cpu_percent=31.125, memory_percent=46.625, alerts=[])

    # [추가] ==의 빈 참 분기와 !=의 빈 거짓 분기는 모두 런북·모델 호출 0회여야 한다.
    async def test_healthy_skip_uses_exact_facts_and_never_calls_report_model(self):
        for op, decision in (("==", "참"), ("!=", "거짓")):
            with self.subTest(op=op):
                mcp, llm = deepcopy(self.mcp), FakeReportLLM([RuntimeError("호출하면 실패")])
                result = await run_workflow(healthy_definition(op), self.catalog, mcp, llm, query="정상이면 추가 조회하지 마.")
                self.assertEqual(result.status, "completed")
                self.assertEqual([call[0] for call in mcp.calls], ["tool"])
                self.assertEqual(llm.calls, [])
                self.assertEqual(result.report_attempts, [])
                self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (0, 0, 0))
                for expected in ("상태=healthy", "CPU=31.125%", "메모리=46.625%", "알림 0건", "모니터링 1회, 런북 0회", f'"healthy" {op} "healthy" → {decision}', "런북을 조회하지 않았습니다"):
                    self.assertIn(expected, result.final)
                self.assertNotIn("런북 조회는 완료", result.final)
                self.assertNotIn("/think", result.final)

    # [추가] 비정상 상태에서 실제 읽은 런북의 요약·원문과 관측 사실을 구분해 표시한다.
    async def test_degraded_reads_runbook_and_binds_summary_to_original_block(self):
        self.mcp.snapshots["web-01"]["status"] = "degraded"
        llm = FakeReportLLM([report_response(summary_json())])
        result = await run_workflow(healthy_definition(), self.catalog, self.mcp, llm, query="조건부 확인")
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(sum(call[0] == "resource" for call in self.mcp.calls), 1)
        for text in ('"degraded" != "healthy" → 참', "모니터링 1회, 런북 1회", "런북 요약(모델 생성)", "근거 원문", "CPU 사용량을 확인하세요", "운영 조치는 실행하지 않았습니다"):
            self.assertIn(text, result.final)

    # [추가] 경계값의 분기·실제 조회와 출력 수치를 같은 입력으로 비교한다.
    async def test_cpu_boundary_values_preserve_operator_branch_and_call_target(self):
        for cpu, matched, incident in ((79.9, "거짓", "memory-leak"), (80.0, "참", "cpu-spike"), (80.1, "참", "cpu-spike")):
            with self.subTest(cpu=cpu):
                mcp = FakeMCP()
                mcp.snapshots["payments-service"]["cpu_percent"] = cpu
                llm = FakeReportLLM([report_response(summary_json("런북의 점검 절차를 확인하세요."))])
                result = await run_workflow(conditional_definition(), self.catalog, mcp, llm, query="CPU 80% 기준으로 확인")
                self.assertEqual(result.status, "completed")
                self.assertIn(f"CPU={cpu}%", result.final)
                self.assertIn(f"{cpu} >= 80 → {matched}", result.final)
                self.assertEqual([call[1] for call in mcp.calls if call[0] == "resource"], [f"runbook://incident-response/{incident}"])

    # [추가] 존재하지 않는 문서·문단·새 수치는 재작성시키고 거절한 원문은 답변 스트림에 싣지 않는다.
    async def test_invalid_evidence_and_numeric_claims_are_rewritten_without_leaking_tokens(self):
        bad_reports = [summary_json(source_id="runbook_099"), summary_json(block_id="block_099"), summary_json("CPU가 99%라고 합니다.")]
        for bad in bad_reports:
            with self.subTest(bad=bad):
                llm = FakeReportLLM([report_response(bad), report_response(summary_json())])
                graph = build_workflow_graph(self.catalog, FakeMCP(), llm)
                events = [event async for event in graph.astream({"payload": conditional_definition(), "query": "조회"}, stream_mode="custom")]
                self.assertEqual(len(llm.calls), 2)
                self.assertTrue(llm.calls[1]["feedback"])
                tokens = [event for event in events if event["type"] == "AnswerToken"]
                self.assertEqual(len(tokens), 1)
                self.assertEqual(tokens[0]["delta"], events[-1]["final"])
                self.assertNotIn('"summaries"', tokens[0]["delta"])
                self.assertNotIn("99%", tokens[0]["delta"])

    # [추가] 요약이 실패·거절·반복 반려돼도 성공한 조회 사실을 보존하고 잘못된 요약을 확정하지 않는다.
    async def test_failed_summary_keeps_fact_report_without_promoting_model_draft(self):
        cases = [
            ([report_response(summary_json("CPU는 99%입니다."))] * 3, "REPORT_VALIDATION_FAILED"),
            ([RuntimeError("model unavailable")], "REPORT_ERROR"),
            ([report_response(summary_json(), refusal="거절")], "REPORT_REFUSED"),
        ]
        for responses, code in cases:
            with self.subTest(code=code):
                result = await run_workflow(conditional_definition(), self.catalog, FakeMCP(), FakeReportLLM(responses), query="조회")
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error.code, code)
                self.assertEqual(result.execution.status, "completed")
                self.assertEqual(result.final, "")
                self.assertFalse(result.report_valid)
                self.assertIn("CPU=80.0%", result.facts_report)
                self.assertIn("모니터링 1회, 런북 1회", result.facts_report)
                self.assertNotIn("99%", result.facts_report)
                self.assertNotIn('"summaries"', result.facts_report)
                self.assertFalse(any(event["type"] == "AnswerToken" for event in result.events))

    # [추가] 조회 자체가 실패한 경우에는 성공한 사실 보고를 만들어내지 않는다.
    async def test_execution_failure_does_not_create_completed_fact_report(self):
        self.mcp.tool_error = RuntimeError("monitoring unavailable")
        result = await run_workflow(healthy_definition(), self.catalog, self.mcp, FakeReportLLM(), query="조회")
        self.assertEqual(result.error.stage, "execution")
        self.assertEqual(result.facts_report, "")

    # [추가] 코드 보고의 출력 한도 오류를 무한한 0회 모델 재시도나 그래프 예외로 만들지 않는다.
    async def test_facts_only_output_limit_is_terminal_without_model_retry(self):
        self.mcp.snapshots["web-01"]["alerts"] = ["긴 알림" * 4000]
        llm = FakeReportLLM()
        result = await run_workflow(healthy_definition(), self.catalog, self.mcp, llm, query="조회")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "REPORT_ERROR")
        self.assertEqual(llm.calls, [])
        self.assertEqual(result.report_attempts, [])


# [추가] 정상 표현의 허용과 구조 오류의 거절을 함께 검사해 근거 검사의 범위를 고정한다.
class RunbookSummaryContractTests(unittest.TestCase):
    def setUp(self):
        self.sources = build_runbook_sources([{"source": "runbook", "data": "# CPU\n\nCPU가 90%를 넘고 지속되면 확장을 검토한다.\n\n변경 전 승인이 필요하다."}])

    def test_reference_preserves_entire_condition_and_accepts_original_numbers(self):
        report, errors = parse_runbook_summary(summary_json("런북은 CPU가 90%를 넘고 지속되면 확장을 권고한다."), self.sources)
        self.assertEqual(errors, [])
        self.assertIsNotNone(report)
        self.assertEqual(self.sources[0]["blocks"][0]["text"], "CPU가 90%를 넘고 지속되면 확장을 검토한다.")

    def test_rejects_extra_fields_duplicate_sources_blank_and_control_tokens(self):
        valid = json.loads(summary_json())
        duplicate = {"summaries": valid["summaries"] * 2}
        for raw in (summary_json(actual=99), summary_json(" "), summary_json("요약 /think"), json.dumps(duplicate), '{"summaries": [], "summaries": []}', "[" * 2000 + "]" * 2000):
            with self.subTest(raw=raw):
                report, errors = parse_runbook_summary(raw, self.sources)
                self.assertIsNone(report)
                self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()

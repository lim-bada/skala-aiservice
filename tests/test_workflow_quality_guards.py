# [추가] 새 파일 전체: 의도·보고 근거 검사가 실제 컴파일·그래프의 승인 경로를 막는지 검증한다.
"""프롬프트 문구가 아니라 생성 재시도와 최종 답변의 공개 여부를 검사한다."""
import json
import unittest

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.compiler import compile_workflow
from agent.workflow.graph import run_workflow
from tests.test_workflow_compilation import FakeCompilerLLM, model_response
from tests.test_workflow_execution import FakeMCP, conditional_definition
from tests.test_workflow_graph import FakeReportLLM, VALID_REPORT, report_response


# [추가] 기존 실행 대역에 의도 불일치·허위 완료 응답을 주입해 경계 간 연결을 검사한다.
class WorkflowQualityGuardTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 서비스 데이터와 네트워크 없이 독립된 카탈로그를 준비한다.
    def setUp(self):
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak"),
        )

    # [추가] 실행할 수 없는 작업을 읽기로 대체할 기회 자체를 주지 않는다.
    async def test_unsupported_mutation_and_ambiguous_condition_never_call_model(self):
        for query in (
            "payments-service를 조회하고 CPU가 80 이상이면 서비스를 재시작해줘.",
            "payments-service의 CPU가 높으면 적당한 런북을 읽어줘.",
        ):
            with self.subTest(query=query):
                llm = FakeCompilerLLM()
                result = await compile_workflow(query, self.catalog, llm)
                self.assertFalse(result.valid)
                self.assertEqual(result.error.code, "UNSUPPORTED_REQUEST")
                self.assertEqual(llm.calls, [])
                self.assertEqual(result.total_tokens, 0)

    # [추가] 문법상 정상인 임의 if를 반려하고 한 번의 수정으로 순차 지시를 복구한다.
    async def test_valid_schema_with_invented_condition_requires_repair(self):
        query = "payments-service를 조회한 뒤 CPU 런북을 읽어줘."
        wrong = conditional_definition()
        wrong["steps"][1]["else_steps"] = []
        right = {"schema_version": 1, "name": "순차 조회", "steps": [
            {"id": "check", "type": "monitoring", "system": "payments-service"},
            {"id": "book", "type": "runbook", "incident_type": "cpu-spike"},
        ]}
        llm = FakeCompilerLLM([model_response(json.dumps(wrong)), model_response(json.dumps(right))])
        result = await compile_workflow(query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(result.workflow.model_dump(mode="json"), right)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(all(issue["code"] == "INTENT_MISMATCH" for issue in llm.calls[1]["errors"]))
        self.assertEqual(result.total_tokens, 240)

    # [추가] 수정 한도를 넘긴 의미 불일치는 실행·저장 가능한 정의를 반환하지 않는다.
    async def test_repeated_intent_mismatch_has_no_approved_definition(self):
        llm = FakeCompilerLLM([model_response(), model_response()])
        result = await compile_workflow("payments-service를 조회해줘.", self.catalog, llm)
        self.assertFalse(result.valid)
        self.assertIsNone(result.workflow)
        self.assertEqual(result.error.code, "VALIDATION_FAILED")
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(any(issue.code == "INTENT_MISMATCH" for issue in result.errors))

    # [추가] 동등한 반대 조건은 한 번에 승인하고 저장·해시 대상인 원본 AST를 보존한다.
    async def test_equivalent_inverse_is_approved_without_repair_or_ast_mutation(self):
        query = "payments-service가 healthy면 추가 조회하지 말고 아니면 메모리 런북을 읽어줘."
        inverse = conditional_definition()
        inverse["steps"][1]["condition"] = {
            "ref": "metrics_1", "field": "status", "op": "!=", "value": "healthy",
        }
        inverse["steps"][1]["then_steps"] = inverse["steps"][1]["else_steps"]
        inverse["steps"][1]["else_steps"] = []
        llm = FakeCompilerLLM([model_response(json.dumps(inverse))])
        result = await compile_workflow(query, self.catalog, llm)
        self.assertTrue(result.valid, result.errors)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result.workflow.model_dump(mode="json"), inverse)

    # [추가] 실제 조회는 한 번만 하고 허위 조치 완료 보고만 다시 작성한다.
    async def test_false_completion_is_rewritten_using_feedback(self):
        bad_data = json.loads(VALID_REPORT)
        bad_data["summaries"][0]["summary"] += " 그러나 이번 워크플로에서는 서비스를 재시작하는 조치가 실행되었다."
        bad = json.dumps(bad_data, ensure_ascii=False)
        llm = FakeReportLLM([report_response(bad), report_response()])
        mcp = FakeMCP()
        result = await run_workflow(conditional_definition(), self.catalog, mcp, llm,
                                    query="결제 서비스 점검")
        self.assertEqual(result.status, "completed")
        self.assertIn("런북 요약(모델 생성)", result.final)
        self.assertNotIn("조치가 실행되었다", result.final)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("조회 전용", llm.calls[1]["feedback"])
        self.assertTrue(result.report_attempts[0].errors)
        self.assertEqual(sum(call[0] == "tool" for call in mcp.calls), 1)

    # [추가] 세 번 모두 허위 완료이면 형식 검증 통과 여부와 관계없이 최종 답변을 비운다.
    async def test_repeated_false_completion_never_emits_final_answer(self):
        bad_data = json.loads(VALID_REPORT)
        bad_data["summaries"][0]["summary"] += " 서비스 재시작을 완료했습니다."
        bad = json.dumps(bad_data, ensure_ascii=False)
        llm = FakeReportLLM([report_response(bad) for _ in range(3)])
        result = await run_workflow(conditional_definition(), self.catalog, FakeMCP(), llm,
                                    query="결제 서비스 점검")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "REPORT_VALIDATION_FAILED")
        self.assertFalse(result.report_valid)
        self.assertEqual(result.final, "")
        self.assertEqual(len(result.report_attempts), 3)
        self.assertFalse(any(event["type"] == "FinalAnswer" for event in result.events))


if __name__ == "__main__":
    unittest.main()

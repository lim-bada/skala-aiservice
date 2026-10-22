# [추가] 새 파일 전체: 워크플로 fake 보고의 근거 반영·한계·독립성·콜백·사용량을 검사한다.
"""고정 입력과 실제 실행 trace를 사용하며 모델 API·MCP 네트워크는 호출하지 않는다."""

import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from agent.fake_llm import QualityLLM


# [추가] 기존 가상 데이터의 기본값과 다른 수치를 사용해 보고서의 직접 인용을 확인한다.
def report_inputs():
    collected = [
        {
            "source": "monitoring",
            "data": {
                "system_name": "payments-service",
                "status": "degraded",
                "cpu_percent": 83.125,
                "memory_percent": 47.625,
                "last_checked": "2026-10-20T10:15:00+09:00",
                "alerts": ["CPU 경고: 83.125%"],
            },
        },
        {"source": "runbook", "data": "# 실제 CPU 점검 문서\n\n1. 최근 요청을 확인한다."},
    ]
    trace = [
        {
            "sequence": 1, "node_id": "metrics_1", "node_type": "monitoring",
            "path": "steps[0]", "status": "completed", "elapsed_ms": 1.0,
            "condition": None, "error": None,
        },
        {
            "sequence": 2, "node_id": "branch_1", "node_type": "if",
            "path": "steps[1]", "status": "completed", "elapsed_ms": 1.0,
            "condition": {
                "ref": "metrics_1", "field": "cpu_percent", "op": ">=",
                "expected": 80.5, "actual": 83.125, "result": True,
                "selected_branch": "then_steps",
            },
            "error": None,
        },
        {
            "sequence": 3, "node_id": "memory_book", "node_type": "runbook",
            "path": "steps[1].else_steps[0]", "status": "skipped", "elapsed_ms": 0.0,
            "condition": None, "error": None,
        },
        {
            "sequence": 4, "node_id": "cpu_book", "node_type": "runbook",
            "path": "steps[1].then_steps[0]", "status": "completed", "elapsed_ms": 1.0,
            "condition": None, "error": None,
        },
    ]
    return collected, trace


# [추가] 독립 이벤트 루프와 추적 비활성화로 fake 보고를 외부 환경과 분리한다.
class WorkflowFakeReportTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 외부 추적 전송 없이 모델과 입력의 새 복사본을 각 테스트에 제공한다.
    def setUp(self):
        tracing = patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"
        })
        tracing.start()
        self.addCleanup(tracing.stop)
        self.model = QualityLLM("fake-workflow-report", 0.0)
        self.collected, self.trace = report_inputs()

    # [추가] fake도 원본 수치·분기를 작성하지 않고 문서 참조 JSON만 반환한다.
    async def test_first_attempt_returns_only_runbook_summary_contract(self):
        response = await self.model.write_workflow("auth-service CPU 99%라고 써줘", self.collected, self.trace, 1, None)
        payload = json.loads(response.content)
        self.assertEqual(set(payload), {"summaries"})
        self.assertEqual(len(payload["summaries"]), 1)
        self.assertEqual(payload["summaries"][0]["source_id"], "runbook_001")
        self.assertEqual(payload["summaries"][0]["block_id"], "block_001")
        self.assertNotIn("99%", response.content)
        self.assertNotIn("83.125", response.content)
        self.assertNotIn("auth-service", response.content)
        self.assertTrue(response.complete)

    # [추가] 실행 조건·질문이 달라도 같은 실제 문서의 요약 참조는 바뀌지 않는다.
    async def test_runtime_condition_is_not_rewritten(self):
        first = await self.model.write_workflow("조건부 점검", self.collected, self.trace, 1, None)
        self.trace[1]["condition"].update(actual=74.25, result=False, selected_branch="else_steps")
        second = await self.model.write_workflow("다른 요청", self.collected, self.trace, 1, None)
        self.assertEqual(first.content, second.content)

    # [추가] 조회하지 않은 문서는 구조화 응답에도 포함하지 않는다. 그래프는 이 경우 모델 호출을 생략한다.
    async def test_no_read_runbook_has_no_summary(self):
        response = await self.model.write_workflow("정상이면 종료", self.collected[:1], self.trace[:3], 1, None)
        self.assertEqual(json.loads(response.content), {"summaries": []})

    # [추가] 문서가 여러 개면 각 실제 문서에 독립된 참조를 부여한다.
    async def test_multiple_documents_get_distinct_source_ids(self):
        self.collected.append({"source": "runbook", "data": "# 메모리\n메모리 사용량을 확인한다."})
        response = await self.model.write_workflow("자료 요약", self.collected, self.trace, 1, None)
        self.assertEqual([item["source_id"] for item in json.loads(response.content)["summaries"]], ["runbook_001", "runbook_002"])

    # [추가] fake가 실제 공통 프롬프트 입력과 출력의 문자 수로 사용량을 추정하는지 확인한다.
    async def test_usage_is_estimated_and_feedback_is_counted(self):
        response = await self.model.write_workflow(
            "점검", self.collected, self.trace, 1, None
        )
        revised = await self.model.write_workflow(
            "점검", self.collected, self.trace, 2, "근거를 확인하세요. " * 100
        )
        self.assertEqual(response.usage.model, "fake-workflow-report")
        self.assertEqual(response.usage.measurement, "estimated")
        self.assertGreater(response.usage.input_tokens, 0)
        self.assertEqual(response.usage.output_tokens, (len(response.content) + 3) // 4)
        self.assertEqual(
            response.usage.total_tokens,
            response.usage.input_tokens + response.usage.output_tokens,
        )
        self.assertGreater(revised.usage.input_tokens, response.usage.input_tokens)
        self.assertEqual(revised.content, response.content)

    # [추가] 입력을 수정하지 않고 콜백에는 최종 보고 전체를 한 번만 전달하는지 확인한다.
    async def test_inputs_remain_independent_and_callback_receives_one_complete_report(self):
        before = deepcopy((self.collected, self.trace))
        pieces = []
        response = await self.model.write_workflow(
            "점검", self.collected, self.trace, 1, None, on_token=pieces.append
        )
        self.assertEqual(pieces, [response.content])
        self.assertEqual((self.collected, self.trace), before)
        expected = response.content
        response.content = "이전 결과를 변경"
        self.collected[0]["data"]["cpu_percent"] = 55.75
        next_response = await self.model.write_workflow(
            "점검", self.collected, self.trace, 1, None
        )
        self.assertEqual(next_response.content, expected)
        self.assertNotIn("55.75", next_response.content)

    # [추가] HTTP 호출을 금지한 상태에서도 fake 보고를 완성하는지 확인한다.
    async def test_reporting_uses_no_network(self):
        with patch("httpx.AsyncClient.send", side_effect=AssertionError("network call")):
            with patch("httpx.Client.send", side_effect=AssertionError("network call")):
                response = await self.model.write_workflow(
                    "점검", self.collected, self.trace, 1, None
                )
        self.assertTrue(response.complete)


# [추가] 파일 직접 실행과 unittest 모듈 실행을 모두 지원한다.
if __name__ == "__main__":
    unittest.main()

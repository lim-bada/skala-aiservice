# [추가] 새 파일 전체: 고정 fixture 생성의 유효성·거절·독립성·추정 사용량을 검사한다.
"""fake는 자연어 모델 성능 평가가 아닌 결정적인 연결·실행 테스트용이다."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from agent.fake_llm import FastLLM
from agent.workflow.catalog import load_catalog
from agent.workflow.compiler import compile_workflow
from agent.workflow.fake_compile import load_fake_workflow_cases
from agent.workflow.prompting import workflow_response_schema
from agent.workflow.validator import validate_workflow


# [추가] 정상·지원 거절 fixture를 실제 카탈로그 및 정적 검증기와 함께 확인한다.
class WorkflowFakeCompileTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 모델 API와 MCP 없이 실제 스키마·카탈로그 및 fake 어댑터를 준비한다.
    def setUp(self):
        # [추가] 개발자 환경의 추적 설정과 무관하게 이 단위 테스트는 외부 전송을 하지 않는다.
        tracing = patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"
        })
        tracing.start()
        self.addCleanup(tracing.stop)
        self.catalog = load_catalog()
        self.schema = workflow_response_schema()
        self.model = FastLLM("fake-workflow", 0.0)
        self.cases = load_fake_workflow_cases()

    # [추가] 모든 지원 예시가 실제 정적 검증을 통과한 실행 정의를 반환하는지 확인한다.
    async def test_all_supported_fixtures_pass_real_validator(self):
        supported_count = 0
        for query, expected in self.cases.items():
            if "unsupported_reason" in expected:
                continue
            supported_count += 1
            with self.subTest(query=query):
                response = await self.model.compile_workflow(
                    query, self.catalog.to_dict(), self.schema
                )
                self.assertEqual(json.loads(response.content), expected)
                result = validate_workflow(response.content, self.catalog)
                self.assertTrue(result.valid, result.errors)
                self.assertIsNotNone(result.workflow)
                self.assertTrue(response.complete)
                self.assertIsNone(response.refusal)
        self.assertEqual(supported_count, 3)

    # [추가] 실제 생성기와 연결해 정상 응답·지원 거절 모두 재생성 없이 끝나는지 확인한다.
    async def test_real_compiler_accepts_supported_cases_and_preserves_refusals(self):
        for query, expected in self.cases.items():
            with self.subTest(query=query):
                result = await compile_workflow(query, self.catalog, self.model)
                if "unsupported_reason" in expected:
                    # [추가] 명백한 미지원 요청은 모델 앞에서 거절하며 사용량은 0이다.
                    self.assertEqual(len(result.attempts), 0)
                    self.assertEqual(result.total_tokens, 0)
                    self.assertFalse(result.valid)
                    self.assertIsNone(result.workflow)
                    self.assertEqual(result.error.code, "UNSUPPORTED_REQUEST")
                    self.assertTrue(result.error.message)
                else:
                    self.assertEqual(len(result.attempts), 1)
                    self.assertEqual(result.attempts[0].usage.measurement, "estimated")
                    self.assertTrue(result.valid, result.errors)
                    self.assertEqual(result.workflow.model_dump(mode="json"), expected)

    # [추가] CPU 임계값·참거짓 런북을 fixture가 지정한 의미 그대로 표현하는지 확인한다.
    async def test_numeric_fixture_preserves_threshold_and_both_branches(self):
        query = next(query for query in self.cases if "80%" in query)
        response = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema
        )
        workflow = json.loads(response.content)
        self.assertEqual(workflow["steps"][0]["system"], "payments-service")
        branch = workflow["steps"][1]
        self.assertEqual(branch["condition"], {
            "ref": "payment_metrics", "field": "cpu_percent", "op": ">=", "value": 80
        })
        self.assertEqual(branch["then_steps"][0]["incident_type"], "cpu-spike")
        self.assertEqual(branch["else_steps"][0]["incident_type"], "memory-leak")

    # [추가] 정상 상태에서 불필요한 후속 조회를 넣지 않는 빈 분기 표현을 검사한다.
    async def test_healthy_fixture_keeps_empty_then_branch(self):
        query = next(query for query in self.cases if "healthy" in query)
        response = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema
        )
        branch = json.loads(response.content)["steps"][1]
        self.assertEqual(branch["condition"]["field"], "status")
        self.assertEqual(branch["condition"]["op"], "==")
        self.assertEqual(branch["condition"]["value"], "healthy")
        self.assertEqual(branch["then_steps"], [])
        self.assertEqual(branch["else_steps"][0]["incident_type"], "cpu-spike")

    # [추가] 반복 요청과 미등록 시스템을 일부 실행 가능한 계획으로 바꾸지 않는지 검사한다.
    async def test_unsupported_fixtures_only_return_explicit_reason(self):
        unsupported_count = 0
        for query, expected in self.cases.items():
            if "unsupported_reason" not in expected:
                continue
            unsupported_count += 1
            with self.subTest(query=query):
                response = await self.model.compile_workflow(
                    query, self.catalog.to_dict(), self.schema
                )
                self.assertEqual(json.loads(response.content), expected)
                self.assertEqual(set(expected), {"unsupported_reason"})
                self.assertTrue(expected["unsupported_reason"].strip())
        self.assertEqual(unsupported_count, 2)

    # [추가] 미등록 문장은 키워드나 유사도 추측으로 등록 예시에 연결하지 않는지 확인한다.
    async def test_unregistered_query_is_rejected_even_when_similar(self):
        known_query = next(iter(self.cases))
        response = await self.model.compile_workflow(
            known_query + " ", self.catalog.to_dict(), self.schema
        )
        payload = json.loads(response.content)
        self.assertEqual(set(payload), {"unsupported_reason"})
        self.assertIn("fixture에 등록되지 않은", payload["unsupported_reason"])

    # [추가] fixture 로딩과 응답 변경이 다음 호출에 영향을 주지 않는지 확인한다.
    async def test_fixture_and_response_objects_are_not_shared(self):
        query = next(iter(self.cases))
        self.cases[query]["steps"].clear()
        first = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema
        )
        expected_content = first.content
        first.content = "{}"
        first.usage.input_tokens = 0
        second = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema
        )
        self.assertEqual(second.content, expected_content)
        self.assertGreater(second.usage.input_tokens, 0)
        self.assertTrue(validate_workflow(second.content, self.catalog).valid)

    # [추가] 전역 작업 폴더 대신 helper 파일 기준으로 fixture를 찾는지 확인한다.
    async def test_fixture_loading_is_independent_of_current_directory(self):
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                cases = load_fake_workflow_cases()
                response = await self.model.compile_workflow(
                    next(iter(cases)), self.catalog.to_dict(), self.schema
                )
            finally:
                os.chdir(original_cwd)
        self.assertTrue(validate_workflow(response.content, self.catalog).valid)

    # [추가] 문자 수 기반 추정을 실제 측정 토큰과 구분하고 재시도 입력도 반영한다.
    async def test_usage_is_estimated_and_includes_retry_context(self):
        query = next(iter(self.cases))
        first = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema
        )
        retried = await self.model.compile_workflow(
            query, self.catalog.to_dict(), self.schema,
            errors=[{"code": "INVALID_SCHEMA", "path": "steps", "message": "수정 필요"}],
            previous_response="{" + ("x" * 500) + "}",
        )
        self.assertEqual(first.usage.model, "fake-workflow")
        self.assertEqual(first.usage.measurement, "estimated")
        self.assertEqual(
            first.usage.total_tokens,
            first.usage.input_tokens + first.usage.output_tokens,
        )
        self.assertGreater(first.usage.output_tokens, 0)
        self.assertGreater(retried.usage.input_tokens, first.usage.input_tokens)
        self.assertEqual(first.content, retried.content)

    # [추가] fixture를 읽는 경로에 네트워크 요청이 생기면 테스트를 즉시 실패시킨다.
    async def test_compile_does_not_use_network(self):
        query = next(iter(self.cases))
        with patch("httpx.AsyncClient.send", side_effect=AssertionError("network call")):
            with patch("httpx.Client.send", side_effect=AssertionError("network call")):
                response = await self.model.compile_workflow(
                    query, self.catalog.to_dict(), self.schema
                )
        self.assertTrue(validate_workflow(response.content, self.catalog).valid)


# [추가] 표준 unittest 명령 및 파일 직접 실행 모두 지원한다.
if __name__ == "__main__":
    unittest.main()

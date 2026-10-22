# [추가] 새 파일 전체: 자연어 컴파일의 입력·수정 한도·사용량·실패 처리를 외부 모델 없이 검증한다.
"""고정된 비동기 LLM 응답으로 검증을 통과한 워크플로만 생성되는지 확인한다."""

import asyncio
import json
import math
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.compiler import compile_workflow
from agent.workflow.models import (
    MAX_WORKFLOW_JSON_BYTES,
    WorkflowCompilationIssue,
    WorkflowCompilationResult,
    WorkflowDefinition,
    WorkflowModelResponse,
    WorkflowModelUsage,
    WorkflowValidationIssue,
)


# [추가] 실제 데이터 파일이나 다른 테스트 모듈에 의존하지 않는 기준 절차를 만든다.
def example_definition():
    return {
        "schema_version": 1,
        "name": "결제 서비스 조건부 점검",
        "steps": [
            {"id": "metrics_1", "type": "monitoring", "system": "payments-service"},
            {
                "id": "branch_1",
                "type": "if",
                "condition": {
                    "ref": "metrics_1", "field": "cpu_percent", "op": ">=", "value": 80
                },
                "then_steps": [
                    {"id": "cpu_book", "type": "runbook", "incident_type": "cpu-spike"}
                ],
                "else_steps": [
                    {"id": "memory_book", "type": "runbook", "incident_type": "memory-leak"}
                ],
            },
        ],
    }


# [추가] 유효 응답과 토큰 사용량을 테스트마다 독립적으로 생성한다.
def model_response(content=None, *, usage=None, refusal=None, complete=True):
    if content is None:
        content = json.dumps(example_definition(), ensure_ascii=False)
    return WorkflowModelResponse(
        content=content,
        usage=usage if usage is not None else WorkflowModelUsage(
            model="test-model", input_tokens=100, output_tokens=20, total_tokens=120,
            measurement="reported",
        ),
        refusal=refusal,
        complete=complete,
    )


# [추가] 모델 호출 인자·응답·예외·취소를 제어하는 비동기 대역을 정의한다.
class FakeCompilerLLM:
    # [추가] 호출마다 사용할 응답과 실패 주입 설정을 준비한다.
    def __init__(self, responses=None):
        self.responses = [model_response()] if responses is None else responses
        self.calls = []
        self.on_call = None
        self.block_on_calls = set()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    # [추가] 수정 피드백과 프롬프트 자료를 기록하고 실제 네트워크 없이 응답을 반환한다.
    async def compile_workflow(self, query, catalog, schema, errors=None, previous_response=None):
        number = len(self.calls) + 1
        self.calls.append({
            "query": query,
            "catalog": deepcopy(catalog),
            "schema": deepcopy(schema),
            "errors": deepcopy(errors),
            "previous_response": previous_response,
        })
        if self.on_call is not None:
            self.on_call(number, catalog, schema)
        if number in self.block_on_calls:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        await asyncio.sleep(0)
        response = self.responses[number - 1]
        if isinstance(response, BaseException):
            raise response
        return response


# [추가] 각 테스트가 독립 이벤트 루프에서 컴파일과 재시도·취소 계약을 검사한다.
class WorkflowCompilationTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 생성기가 사용할 명시적 카탈로그와 자연어 요청을 준비한다.
    def setUp(self):
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )
        self.query = "payments-service를 조회하고 CPU가 80 이상이면 CPU 런북, 아니면 메모리 런북을 확인해줘."

    # [추가] 실패 결과가 실행 정의를 남기지 않고 원인을 명시하는지 확인한다.
    def assert_failure(self, result, code):
        self.assertFalse(result.valid)
        self.assertIsNone(result.workflow)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, code)
        self.assertTrue(result.error.path)
        self.assertTrue(result.error.message)
        self.assertTrue(math.isfinite(result.elapsed_ms))
        self.assertGreaterEqual(result.elapsed_ms, 0)

    # [추가] 정상 응답은 검증된 절차와 한 번의 호출·사용량을 반환하는지 확인한다.
    async def test_valid_generation_returns_validated_definition_and_usage(self):
        llm = FakeCompilerLLM()
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertIsNone(result.error)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.workflow.model_dump(mode="json"), example_definition())
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual([attempt.number for attempt in result.attempts], [1])
        self.assertEqual(result.attempts[0].errors, [])
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (100, 20, 120))
        self.assertEqual(llm.calls[0]["query"], self.query)
        self.assertEqual(llm.calls[0]["catalog"], self.catalog.to_dict())
        expected = WorkflowDefinition.model_json_schema()
        definitions = expected.pop("$defs")
        self.assertEqual(llm.calls[0]["schema"]["$defs"], definitions)
        self.assertEqual(llm.calls[0]["schema"]["anyOf"][0], expected)
        self.assertEqual(llm.calls[0]["schema"]["anyOf"][1]["required"], ["unsupported_reason"])
        self.assertIn(llm.calls[0]["errors"], (None, []))
        self.assertIsNone(llm.calls[0]["previous_response"])

    # [추가] 타입·공백·길이·UTF-8이 잘못된 요청은 모델을 호출하지 않고 거부한다.
    async def test_invalid_queries_are_rejected_without_model_call(self):
        for query in (None, 123, True, [], {}, b"query", "", " \n\t ", "q" * 2001, "\ud800"):
            with self.subTest(query=query):
                llm = FakeCompilerLLM()
                result = await compile_workflow(query, self.catalog, llm)
                self.assert_failure(result, "INVALID_QUERY")
                self.assertEqual(result.attempts, [])
                self.assertEqual(llm.calls, [])
                self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (0, 0, 0))

    # [추가] 요청 길이는 공백 제거 전 원문 기준으로 검사하고 정확히 2,000자는 허용한다.
    async def test_query_length_boundary_uses_original_input(self):
        # [추가] 길이 경계만 검증하도록 반환 후보와 일치하는 요청에 공백을 붙인다.
        result = await compile_workflow(self.query.ljust(2000), self.catalog, FakeCompilerLLM())
        self.assertTrue(result.valid)
        llm = FakeCompilerLLM()
        result = await compile_workflow(" " * 1000 + "q" + " " * 1000, self.catalog, llm)
        self.assert_failure(result, "INVALID_QUERY")
        self.assertEqual(llm.calls, [])

    # [추가] 잘못된 시간 제한 설정은 모델을 호출하지 않고 호출자 오류로 알린다.
    async def test_invalid_timeout_configuration_raises_value_error(self):
        for timeout in (0, -1, True, None, "1", float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                llm = FakeCompilerLLM()
                with self.assertRaises(ValueError):
                    await compile_workflow(self.query, self.catalog, llm, timeout_seconds=timeout)
                self.assertEqual(llm.calls, [])

    # [추가] 첫 JSON 오류를 수정 요청에 전달하고 두 번째 정상 응답으로 완료한다.
    async def test_invalid_json_is_repaired_once_with_error_feedback(self):
        bad = "{not json"
        llm = FakeCompilerLLM([model_response(bad), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual([attempt.number for attempt in result.attempts], [1, 2])
        self.assertEqual(result.attempts[0].errors[0].code, "INVALID_JSON")
        self.assertEqual(result.attempts[1].errors, [])
        self.assertEqual(llm.calls[1]["previous_response"], bad)
        self.assertEqual(llm.calls[1]["errors"], [issue.model_dump(mode="json") for issue in result.attempts[0].errors])
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (200, 40, 240))

    # [추가] JSON 형식뿐 아니라 필드 구조·자원·참조 오류도 같은 한 번의 수정 경로를 사용한다.
    async def test_schema_resource_and_reference_errors_are_repaired_once(self):
        for kind in ("schema", "resource", "reference"):
            with self.subTest(kind=kind):
                payload = example_definition()
                if kind == "schema":
                    payload["steps"][1]["condition"]["value"] = "80"
                    expected = "INVALID_SCHEMA"
                elif kind == "resource":
                    payload["steps"][0]["system"] = "missing-service"
                    expected = "UNKNOWN_SYSTEM"
                else:
                    payload["steps"][1]["condition"]["ref"] = "missing"
                    expected = "INVALID_REFERENCE"
                llm = FakeCompilerLLM([model_response(json.dumps(payload)), model_response()])
                result = await compile_workflow(self.query, self.catalog, llm)
                self.assertTrue(result.valid)
                self.assertIn(expected, [issue.code for issue in result.attempts[0].errors])
                self.assertEqual(len(llm.calls), 2)

    # [추가] 두 번 모두 실패하면 세 번째 호출 없이 마지막 오류와 실패한 두 시도를 반환한다.
    async def test_second_invalid_generation_stops_without_third_call(self):
        payload = example_definition()
        payload["steps"][0]["system"] = "missing-service"
        llm = FakeCompilerLLM([model_response("broken"), model_response(json.dumps(payload)), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "VALIDATION_FAILED")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual([attempt.number for attempt in result.attempts], [1, 2])
        self.assertIn("UNKNOWN_SYSTEM", [issue.code for issue in result.errors])
        self.assertTrue(all(attempt.errors for attempt in result.attempts))

    # [추가] 코드 펜스를 임의로 제거해 유효 JSON처럼 실행하지 않고 검증 실패로 수정시킨다.
    async def test_markdown_wrapped_json_is_not_silently_executed(self):
        wrapped = "```json\n" + json.dumps(example_definition()) + "\n```"
        llm = FakeCompilerLLM([model_response(wrapped), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(result.attempts[0].errors[0].code, "INVALID_JSON")

    # [추가] 모델이 반환한 원문 JSON의 중복 키를 파싱 후 딕셔너리로 넘겨 숨기지 않는다.
    async def test_duplicate_json_keys_are_detected_before_repair(self):
        raw = json.dumps(example_definition()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        llm = FakeCompilerLLM([model_response(raw), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(result.attempts[0].errors[0].code, "INVALID_JSON")

    # [추가] 모델 응답도 동일한 16KB 제한을 적용하고 초과 원문은 수정 프롬프트에 재전송하지 않는다.
    async def test_oversized_model_json_cannot_bypass_limit_or_be_resent(self):
        raw = json.dumps(example_definition(), ensure_ascii=False)
        raw += " " * MAX_WORKFLOW_JSON_BYTES
        llm = FakeCompilerLLM([model_response(raw), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(result.attempts[0].errors[0].code, "INPUT_TOO_LARGE")
        self.assertIsNone(llm.calls[1]["previous_response"])

    # [추가] 정확히 16KB인 UTF-8 JSON은 원문 제한을 넘지 않았으므로 한 번에 성공한다.
    async def test_exact_json_byte_limit_is_allowed(self):
        raw = json.dumps(example_definition(), ensure_ascii=False)
        raw += " " * (MAX_WORKFLOW_JSON_BYTES - len(raw.encode("utf-8")))
        llm = FakeCompilerLLM([model_response(raw)])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(len(llm.calls), 1)

    # [추가] UTF-8로 표현할 수 없는 모델 응답은 오류로 처리하고 수정 요청에 넣지 않는다.
    async def test_invalid_utf8_response_is_not_resent_for_repair(self):
        llm = FakeCompilerLLM([model_response("\ud800"), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(result.attempts[0].errors[0].code, "INVALID_JSON")
        self.assertIsNone(llm.calls[1]["previous_response"])

    # [추가] 지원할 수 없는 요청의 명시적 JSON 거절은 수정 호출 없이 종료한다.
    async def test_explicit_unsupported_request_is_terminal(self):
        reason = "현재 지원 범위에서는 재시작 명령을 실행할 수 없습니다."
        llm = FakeCompilerLLM([model_response(json.dumps({"unsupported_reason": reason}))])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "UNSUPPORTED_REQUEST")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.total_tokens, 120)

    # [추가] 거절 JSON에 다른 필드가 섞이면 명시적 거절 계약을 충족하지 못해 검증 후 수정한다.
    async def test_unsupported_envelope_with_extra_keys_is_repaired(self):
        response = {"unsupported_reason": "지원하지 않는 명령입니다.", "steps": []}
        llm = FakeCompilerLLM([model_response(json.dumps(response)), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(len(llm.calls), 2)
        self.assertTrue(result.attempts[0].errors)

    # [추가] 거절 이유는 실제 비공백 문자열이며 500자 이하여야 한다.
    async def test_malformed_unsupported_reason_is_not_terminal(self):
        for reason in ("", " \n\t ", "r" * 501, None, 123):
            with self.subTest(reason=reason):
                llm = FakeCompilerLLM([model_response(json.dumps({"unsupported_reason": reason})), model_response()])
                result = await compile_workflow(self.query, self.catalog, llm)
                self.assertTrue(result.valid)
                self.assertEqual(len(llm.calls), 2)
                self.assertTrue(result.attempts[0].errors)

    # [추가] 모델 API의 거절 응답은 유효 절차가 함께 있어도 실행하거나 재시도하지 않는다.
    async def test_model_refusal_is_terminal_even_with_valid_json(self):
        llm = FakeCompilerLLM([model_response(refusal="요청을 수행할 수 없습니다.")])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "MODEL_REFUSED")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.total_tokens, 120)

    # [추가] 생성이 중간 종료된 응답은 JSON이 우연히 유효해도 완료된 계획으로 채택하지 않는다.
    async def test_incomplete_response_is_repaired_even_if_json_is_valid(self):
        llm = FakeCompilerLLM([model_response(complete=False), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("INVALID_SCHEMA", [issue.code for issue in result.attempts[0].errors])

    # [추가] 두 응답 모두 미완성이면 유효 JSON을 최종 결과로 통과시키지 않는다.
    async def test_two_incomplete_responses_fail_validation(self):
        llm = FakeCompilerLLM([model_response(complete=False), model_response(complete=False)])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "VALIDATION_FAILED")
        self.assertEqual(len(result.attempts), 2)

    # [추가] HTTP 등 모델 호출 예외는 JSON 수정 재시도 대상으로 삼지 않는다.
    async def test_model_exception_is_terminal_and_attempt_is_recorded(self):
        llm = FakeCompilerLLM([RuntimeError("model unavailable"), model_response()])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "MODEL_ERROR")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.attempts), 1)
        self.assertIsNone(result.attempts[0].usage)
        self.assertIsNone(result.total_tokens)

    # [추가] 어댑터 응답의 자료형이나 사용량 계약이 잘못됐을 때 예외 대신 모델 오류로 반환한다.
    async def test_invalid_model_response_contract_is_terminal(self):
        bad_response = model_response()
        bad_response.content = 123
        variants = (None, "raw model text", bad_response, {"content": "{}", "usage": {"model": "test", "total_tokens": "30"}})
        for response in variants:
            with self.subTest(response=response):
                llm = FakeCompilerLLM([response, model_response()])
                result = await compile_workflow(self.query, self.catalog, llm)
                self.assert_failure(result, "MODEL_ERROR")
                self.assertEqual(len(llm.calls), 1)
                self.assertEqual(len(result.attempts), 1)

    # [추가] 두 번째 모델 호출이 실패해도 첫 시도의 오류와 사용량 기록은 보존한다.
    async def test_exception_during_repair_keeps_first_attempt_usage(self):
        llm = FakeCompilerLLM([model_response("bad"), RuntimeError("model unavailable")])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assert_failure(result, "MODEL_ERROR")
        self.assertEqual(len(result.attempts), 2)
        self.assertEqual(result.attempts[0].usage.total_tokens, 120)
        self.assertTrue(result.attempts[0].errors)
        self.assertIsNone(result.attempts[1].usage)
        self.assertIsNone(result.total_tokens)

    # [추가] 전체 제한 시간 초과는 진행 중 호출을 취소하고 시작한 시도를 기록한다.
    async def test_generation_timeout_cancels_model_and_records_attempt(self):
        llm = FakeCompilerLLM()
        llm.block_on_calls = {1}
        result = await compile_workflow(self.query, self.catalog, llm, timeout_seconds=0.02)
        self.assert_failure(result, "GENERATION_TIMEOUT")
        self.assertTrue(llm.cancelled.is_set())
        self.assertEqual(len(result.attempts), 1)
        self.assertIsNone(result.attempts[0].usage)

    # [추가] 수정 중 시간 초과도 이전 모델 사용량을 지우지 않으며 진행 중 사용량은 알 수 없음으로 둔다.
    async def test_timeout_during_repair_preserves_prior_usage(self):
        llm = FakeCompilerLLM([model_response("bad"), model_response()])
        llm.block_on_calls = {2}
        result = await compile_workflow(self.query, self.catalog, llm, timeout_seconds=0.02)
        self.assert_failure(result, "GENERATION_TIMEOUT")
        self.assertEqual([attempt.number for attempt in result.attempts], [1, 2])
        self.assertEqual(result.attempts[0].usage.total_tokens, 120)
        self.assertIsNone(result.attempts[1].usage)
        self.assertIsNone(result.total_tokens)

    # [추가] 외부 취소를 모델 오류나 시간 초과로 바꾸지 않고 호출자에게 전파한다.
    async def test_external_cancellation_propagates(self):
        llm = FakeCompilerLLM()
        llm.block_on_calls = {1}
        task = asyncio.create_task(compile_workflow(self.query, self.catalog, llm))
        try:
            await asyncio.wait_for(llm.entered.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(llm.cancelled.is_set())
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 보고된 값과 추정값을 시도별로 보존하면서 알려진 토큰 수는 합산한다.
    async def test_reported_and_estimated_usage_are_summed_and_labelled_per_attempt(self):
        reported = WorkflowModelUsage(model="model-a", input_tokens=100, output_tokens=20, total_tokens=120, measurement="reported")
        estimated = WorkflowModelUsage(model="model-b", input_tokens=30, output_tokens=10, total_tokens=40, measurement="estimated")
        llm = FakeCompilerLLM([model_response("bad", usage=reported), model_response(usage=estimated)])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (130, 30, 160))
        self.assertEqual([attempt.usage.measurement for attempt in result.attempts], ["reported", "estimated"])
        self.assertEqual([attempt.usage.model for attempt in result.attempts], ["model-a", "model-b"])

    # [추가] 일부 시도의 값이 없으면 해당 합계만 None으로 두고 다른 항목은 정상 합산한다.
    async def test_partial_usage_is_unknown_per_field_without_inference(self):
        first = WorkflowModelUsage(model="first", input_tokens=100, output_tokens=20, total_tokens=120, measurement="reported")
        second = WorkflowModelUsage(model="second", input_tokens=None, output_tokens=10, total_tokens=None, measurement="unavailable")
        llm = FakeCompilerLLM([model_response("bad", usage=first), model_response(usage=second)])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertIsNone(result.input_tokens)
        self.assertEqual(result.output_tokens, 30)
        self.assertIsNone(result.total_tokens)

    # [추가] 사용량이 전혀 제공되지 않은 경우 임의의 0이나 추정치를 만들지 않는다.
    async def test_unavailable_usage_remains_none(self):
        usage = WorkflowModelUsage(model="test", measurement="unavailable")
        result = await compile_workflow(self.query, self.catalog, FakeCompilerLLM([model_response(usage=usage)]))
        self.assertTrue(result.valid)
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (None, None, None))

    # [추가] 모델에 전달한 카탈로그·스키마를 변조해도 실제 검증 정책과 다음 호출 자료는 유지한다.
    async def test_model_cannot_mutate_catalog_or_schema_across_attempts(self):
        invalid = example_definition()
        invalid["steps"][0]["system"] = "injected-service"
        llm = FakeCompilerLLM([model_response(json.dumps(invalid)), model_response()])

        # [추가] 첫 호출에서 전달 사본만 변조해 자원 추가 및 스키마 삭제를 시도한다.
        def mutate_first(number, catalog, schema):
            if number == 1:
                catalog["systems"].append("injected-service")
                catalog["condition_fields"].clear()
                schema.clear()

        llm.on_call = mutate_first
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        self.assertIn("UNKNOWN_SYSTEM", [issue.code for issue in result.attempts[0].errors])
        self.assertEqual(llm.calls[1]["catalog"], self.catalog.to_dict())
        self.assertEqual(llm.calls[1]["schema"], llm.calls[0]["schema"])
        self.assertNotIn("injected-service", self.catalog.systems)

    # [추가] 반환 후 어댑터가 원본 사용량 객체를 변경해도 이미 기록된 결과는 바뀌지 않는다.
    async def test_success_result_is_independent_of_adapter_response(self):
        response = model_response()
        llm = FakeCompilerLLM([response])
        result = await compile_workflow(self.query, self.catalog, llm)
        self.assertTrue(result.valid)
        response.usage.total_tokens = 999
        self.assertEqual(result.total_tokens, 120)
        self.assertEqual(result.attempts[0].usage.total_tokens, 120)
        self.assertIsNot(result.attempts[0].usage, response.usage)

    # [추가] 주입 카탈로그만 사용하고 실행기·MCP 작업을 수행하지 않은 채 계획 생성에서 종료한다.
    async def test_compilation_uses_injected_resources_without_execution_or_loading(self):
        payload = {
            "schema_version": 1, "name": "별도 시스템 점검",
            "steps": [{"id": "custom_metric", "type": "monitoring", "system": "custom-service"}],
        }
        catalog = WorkflowCatalog(systems=("custom-service",), incident_types=("custom-book",))
        llm = FakeCompilerLLM([model_response(json.dumps(payload))])
        with patch("agent.workflow.catalog.load_catalog", side_effect=AssertionError("카탈로그 재로딩 금지")):
            with patch("agent.workflow.executor.execute_workflow", new_callable=AsyncMock) as execute:
                result = await compile_workflow("custom-service를 조회해줘", catalog, llm)
                execute.assert_not_awaited()
        self.assertTrue(result.valid)
        self.assertEqual(result.workflow.steps[0].system, "custom-service")

    # [추가] 성공·검증 실패·입력 오류 결과 모두 API용 JSON으로 직렬화할 수 있는지 확인한다.
    async def test_success_and_failure_results_are_json_serializable(self):
        outcomes = [
            await compile_workflow(self.query, self.catalog, FakeCompilerLLM()),
            await compile_workflow(self.query, self.catalog, FakeCompilerLLM([model_response("bad"), model_response("bad")])),
            await compile_workflow("", self.catalog, FakeCompilerLLM()),
        ]
        for result in outcomes:
            with self.subTest(valid=result.valid, error=result.error):
                serialized = json.loads(result.model_dump_json())
                self.assertEqual(serialized["valid"], result.valid)
                self.assertEqual(len(serialized["attempts"]), len(result.attempts))
                self.assertEqual(serialized["total_tokens"], result.total_tokens)
                if result.error is not None:
                    self.assertEqual(serialized["error"]["code"], result.error.code)

    # [추가] 성공인데 정의가 없거나 실패인데 실행 정의가 남는 모순된 생성 결과를 거부한다.
    async def test_compilation_result_model_rejects_inconsistent_outcomes(self):
        workflow = WorkflowDefinition.model_validate(example_definition())
        error = WorkflowCompilationIssue(code="MODEL_ERROR", path="$", message="모델 오류")
        issue = WorkflowValidationIssue(code="INVALID_SCHEMA", path="$", message="형식 오류")
        inconsistent = (
            {"valid": True},
            {"valid": True, "workflow": workflow, "error": error},
            {"valid": True, "workflow": workflow, "errors": [issue]},
            {"valid": False},
            {"valid": False, "workflow": workflow, "error": error},
        )
        for payload in inconsistent:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                WorkflowCompilationResult(elapsed_ms=0.0, **payload)

    # [추가] 공급자 사용량도 문자열·bool·음수를 숫자로 보정하거나 잘못된 측정 구분을 허용하지 않는다.
    async def test_model_usage_rejects_invalid_counts_and_measurement(self):
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            for value in ("100", True, -1, 1.5):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    WorkflowModelUsage(model="test", **{field: value})
        with self.assertRaises(ValidationError):
            WorkflowModelUsage(model="test", measurement="unknown")

    # [추가] 동시에 생성하는 두 요청의 피드백·사용량·단계 목록이 서로 섞이지 않는다.
    async def test_concurrent_compilations_keep_attempts_and_usage_isolated(self):
        alternate = example_definition()
        alternate["steps"][0]["system"] = "auth-service"
        first_llm = FakeCompilerLLM([model_response("bad"), model_response()])
        second_llm = FakeCompilerLLM([model_response(json.dumps(alternate))])
        first, second = await asyncio.gather(
            compile_workflow(self.query, self.catalog, first_llm),
            # [추가] 격리 검증용 두 번째 요청도 주입한 조건부 후보의 의도를 보존한다.
            compile_workflow(self.query.replace("payments-service", "auth-service"), self.catalog, second_llm),
        )
        self.assertTrue(first.valid)
        self.assertTrue(second.valid)
        self.assertEqual(len(first.attempts), 2)
        self.assertEqual(len(second.attempts), 1)
        self.assertEqual(first.total_tokens, 240)
        self.assertEqual(second.total_tokens, 120)
        self.assertEqual(first.workflow.steps[0].system, "payments-service")
        self.assertEqual(second.workflow.steps[0].system, "auth-service")
        self.assertIsNot(first.attempts, second.attempts)
        self.assertIn(second_llm.calls[0]["errors"], (None, []))


# [추가] 이 파일을 직접 실행해도 비동기 표준 unittest 테스트를 사용할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

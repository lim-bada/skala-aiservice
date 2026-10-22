# [추가] 새 파일 전체: 조회·보고 그래프의 분기·재작성·시간 제한·실시간 이벤트를 외부 API 없이 검증한다.
"""실행 결과와 보고서 검증을 연결하되 실패한 초안을 최종 답변으로 내보내지 않는지 확인한다."""

import asyncio
import hashlib
import json
import math
import time
import unittest
from copy import deepcopy

from pydantic import ValidationError

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.executor import execute_workflow
from agent.workflow.graph import build_workflow_graph, run_workflow
from agent.workflow.models import (
    WorkflowModelResponse,
    WorkflowModelUsage,
    WorkflowReportAttempt,
    WorkflowRunIssue,
    WorkflowRunResult,
)
from tests.test_workflow_execution import FakeMCP, conditional_definition


# [추가] 형식 검사를 통과하는 고정 보고서를 제공하며 실제 모델 품질과 구분한다.
VALID_REPORT = json.dumps({"summaries": [{
    "source_id": "runbook_001", "block_id": "block_001",
    "summary": "런북은 CPU 사용량 확인 등 점검 절차를 안내합니다.",
}]}, ensure_ascii=False)


# [추가] 보고 응답의 본문·종료 상태·사용량을 독립적으로 제어한다.
def report_response(content=VALID_REPORT, *, usage=None, complete=True, refusal=None):
    return WorkflowModelResponse(
        content=content,
        usage=usage if usage is not None else WorkflowModelUsage(
            model="test-report-model", input_tokens=50, output_tokens=15,
            total_tokens=65, measurement="reported",
        ),
        complete=complete,
        refusal=refusal,
    )


# [추가] 보고 호출과 피드백을 기록하고 오류·시간 초과·입력 사본 변조를 재현한다.
class FakeReportLLM:
    # [추가] 호출별 응답과 제어용 이벤트를 준비한다.
    def __init__(self, responses=None):
        self.responses = [report_response()] if responses is None else responses
        self.calls = []
        self.on_call = None
        self.block_on_calls = set()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    # [추가] 실제 공급자와 같은 비동기 보고 인터페이스 및 토큰 콜백을 제공한다.
    async def write_workflow(self, query, collected, trace, attempt, feedback, on_token=None):
        number = len(self.calls) + 1
        self.calls.append({
            "query": query, "collected": deepcopy(collected), "trace": deepcopy(trace),
            "attempt": attempt, "feedback": feedback,
        })
        if self.on_call is not None:
            self.on_call(number, collected, trace)
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
        if on_token is not None and isinstance(response, WorkflowModelResponse) and isinstance(response.content, str):
            on_token(response.content)
        return response


# [추가] 조회를 완료시키지 않은 상태에서 custom 스트림이 이벤트를 내보내는지 관찰한다.
class GatedMCP(FakeMCP):
    # [추가] 테스트가 조회 완료 시점을 직접 결정하도록 해제 이벤트를 추가한다.
    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    # [추가] 이벤트를 기다리는 동안 그래프가 StepStarted를 전달할 수 있어야 한다.
    async def call_tool(self, name, arguments, server="ops"):
        self.entered.set()
        await self.release.wait()
        return await super().call_tool(name, arguments, server=server)


# [추가] 실행·보고 통합을 별도 이벤트 루프에서 검사한다.
class WorkflowGraphTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 데이터 파일이나 실제 모델을 사용하지 않는 의존성을 매번 생성한다.
    def setUp(self):
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )
        self.mcp = FakeMCP()
        self.report_llm = FakeReportLLM()
        self.query = "결제 시스템을 조건에 맞게 점검하고 결과를 보고해줘."

    # [추가] 보고 실패를 포함한 모든 실패가 최종 답변을 비워 두는지 확인한다.
    def assert_failure(self, result, code, stage):
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.report_valid)
        self.assertEqual(result.final, "")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, code)
        self.assertEqual(result.error.stage, stage)
        self.assertTrue(result.error.message)
        self.assertTrue(result.run_id)
        self.assertTrue(math.isfinite(result.elapsed_ms))
        self.assertGreaterEqual(result.elapsed_ms, 0)
        self.assertFalse(any(event["type"] == "FinalAnswer" for event in result.events))

    # [추가] MCP 결과·조건 trace를 보고 모델에 전달하고 검증된 본문만 최종 응답으로 확정한다.
    async def test_completed_run_passes_execution_evidence_to_report(self):
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.report_valid)
        self.assertIsNone(result.error)
        self.assertIn("모니터링 1회, 런북 1회", result.final)
        self.assertIn("런북 요약(모델 생성)", result.final)
        self.assertEqual(result.draft, result.final)
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(len(self.report_llm.calls), 1)
        self.assertEqual(self.report_llm.calls[0]["query"], self.query)
        self.assertEqual(self.report_llm.calls[0]["collected"], result.execution.to_collected())
        self.assertEqual(self.report_llm.calls[0]["trace"], [event.model_dump(mode="json") for event in result.execution.trace])
        self.assertEqual([attempt.number for attempt in result.report_attempts], [1])
        self.assertEqual(result.report_attempts[0].errors, [])
        self.assertEqual((result.input_tokens, result.output_tokens, result.total_tokens), (50, 15, 65))

    # [추가] 성공 이벤트의 메타데이터가 조사 전체에서 동일하고 최종 본문과 일치하는지 확인한다.
    async def test_events_include_metadata_and_selected_execution_path(self):
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query, workflow_id="saved-workflow-1")
        types = [event["type"] for event in result.events]
        self.assertEqual(types[0], "WorkflowStarted")
        for expected in ("StepStarted", "StepCompleted", "StepSkipped", "ConditionEvaluated", "ReportStarted", "DraftWritten", "ReportValidation", "FinalAnswer"):
            self.assertIn(expected, types)
        self.assertNotIn("WorkflowFailed", types)
        self.assertNotIn("AnswerToken", types)
        for event in result.events:
            self.assertEqual(event["run_id"], result.run_id)
            self.assertEqual(event["workflow_id"], "saved-workflow-1")
            self.assertEqual(event["definition_hash"], result.definition_hash)
        final = result.events[-1]
        self.assertEqual(final["type"], "FinalAnswer")
        self.assertEqual(final["final"], result.final)
        self.assertTrue(final["report_valid"])
        self.assertEqual(final["status"], "completed")

    # [추가] 잘못된 자연어 요청은 MCP 조회와 보고 모델 호출을 모두 막는다.
    async def test_invalid_query_stops_before_execution(self):
        for query in (None, 1, True, "", " \t\n ", "q" * 2001, "\ud800"):
            with self.subTest(query=query):
                mcp = FakeMCP()
                llm = FakeReportLLM()
                result = await run_workflow(conditional_definition(), self.catalog, mcp, llm, query=query)
                self.assert_failure(result, "INVALID_QUERY", "input")
                self.assertIsNone(result.execution)
                self.assertEqual(result.report_attempts, [])
                self.assertEqual(mcp.calls, [])
                self.assertEqual(llm.calls, [])

    # [추가] 정의의 정적 검증 실패를 기존 실행 오류와 함께 보존하고 보고는 생성하지 않는다.
    async def test_invalid_definition_preserves_execution_validation_errors(self):
        payload = conditional_definition()
        payload["steps"][0]["system"] = "missing-service"
        result = await run_workflow(payload, self.catalog, self.mcp, self.report_llm, query=self.query)
        self.assert_failure(result, "EXECUTION_FAILED", "execution")
        self.assertEqual(result.execution.error.code, "VALIDATION_FAILED")
        self.assertIn("UNKNOWN_SYSTEM", [issue.code for issue in result.execution.validation_errors])
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.report_llm.calls, [])

    # [추가] 조회 중 오류가 나면 부분 자료를 남기되 보고 모델을 호출하지 않는다.
    async def test_execution_failure_blocks_report_and_preserves_partial_results(self):
        self.mcp.resource_error = RuntimeError("resource unavailable")
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query)
        self.assert_failure(result, "EXECUTION_FAILED", "execution")
        self.assertEqual(result.execution.error.code, "MCP_ERROR")
        self.assertEqual([item.node_id for item in result.execution.results], ["metrics_1"])
        self.assertEqual(self.report_llm.calls, [])
        self.assertIn("StepFailed", [event["type"] for event in result.events])

    # [추가] 첫 보고서가 반려되면 피드백을 전달하되 MCP 조회는 다시 실행하지 않는다.
    async def test_invalid_first_report_is_rewritten_without_reexecuting_mcp(self):
        llm = FakeReportLLM([report_response("짧은 답변"), report_response()])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assertEqual(result.status, "completed")
        self.assertEqual([attempt.number for attempt in result.report_attempts], [1, 2])
        self.assertTrue(result.report_attempts[0].errors)
        self.assertEqual(result.report_attempts[1].errors, [])
        self.assertEqual([call["attempt"] for call in llm.calls], [1, 2])
        self.assertTrue(llm.calls[1]["feedback"])
        self.assertEqual([call[0] for call in self.mcp.calls].count("tool"), 1)
        self.assertEqual([call[0] for call in self.mcp.calls].count("resource"), 1)
        self.assertEqual(result.total_tokens, 130)

    # [추가] 예전 자유 형식 보고는 구조화 요약 계약을 충족하지 못하므로 반려한다.
    async def test_legacy_freeform_reports_require_structured_rewrite(self):
        invalid_reports = (
            "1) 첫 번째 항목만 있는 긴 보고서입니다. " * 5,
            "1) 관측 사실을 설명합니다. 3) 확인 사항입니다. 2) 대응 절차를 설명합니다.",
            "2) 먼저 대응합니다. 1) 다음에 관측 사실입니다. 3) 마지막 확인 사항입니다.",
        )
        for draft in invalid_reports:
            with self.subTest(draft=draft):
                llm = FakeReportLLM([report_response(draft), report_response()])
                result = await run_workflow(conditional_definition(), self.catalog, FakeMCP(), llm, query=self.query)
                self.assertEqual(result.status, "completed")
                self.assertEqual(len(result.report_attempts), 2)
                self.assertTrue(result.report_attempts[0].errors)

    # [추가] 세 번 모두 반려되면 강제 통과하지 않고 마지막 초안만 검토용으로 남긴다.
    async def test_three_invalid_reports_fail_without_forced_acceptance(self):
        llm = FakeReportLLM([report_response("첫 초안"), report_response("둘째 초안"), report_response("마지막 초안"), report_response()])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assert_failure(result, "REPORT_VALIDATION_FAILED", "report")
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual([attempt.number for attempt in result.report_attempts], [1, 2, 3])
        self.assertTrue(all(attempt.errors for attempt in result.report_attempts))
        self.assertEqual(result.draft, "마지막 초안")
        self.assertEqual(result.total_tokens, 195)
        self.assertEqual(result.execution.status, "completed")

    # [추가] 본문 형식이 맞더라도 공급자가 미완료로 표시한 보고서는 반려한다.
    async def test_incomplete_report_is_rewritten_even_when_format_is_valid(self):
        llm = FakeReportLLM([report_response(complete=False), report_response()])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.report_attempts), 2)
        self.assertTrue(result.report_attempts[0].errors)

    # [추가] 보고 생성 예외는 형식 수정 재시도로 숨기지 않고 즉시 종료한다.
    async def test_report_exception_is_terminal_and_attempt_is_recorded(self):
        llm = FakeReportLLM([RuntimeError("model unavailable"), report_response()])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assert_failure(result, "REPORT_ERROR", "report")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(result.report_attempts), 1)
        self.assertIsNone(result.report_attempts[0].usage)
        self.assertIsNone(result.total_tokens)
        self.assertEqual(result.execution.status, "completed")

    # [추가] 보고 응답 계약이 깨지면 잘못된 값을 최종 문자열로 바꾸지 않는다.
    async def test_invalid_report_response_contract_is_terminal(self):
        invalid = report_response()
        invalid.content = 123
        for response in (None, "raw text", invalid):
            with self.subTest(response=response):
                llm = FakeReportLLM([response, report_response()])
                result = await run_workflow(conditional_definition(), self.catalog, FakeMCP(), llm, query=self.query)
                self.assert_failure(result, "REPORT_ERROR", "report")
                self.assertEqual(len(llm.calls), 1)
                self.assertEqual(len(result.report_attempts), 1)

    # [추가] 공급자의 명시적인 보고 거절은 본문이 있어도 채택하거나 재시도하지 않는다.
    async def test_report_refusal_is_terminal_and_usage_is_preserved(self):
        llm = FakeReportLLM([report_response(refusal="보고서 생성을 거절했습니다."), report_response()])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assert_failure(result, "REPORT_REFUSED", "report")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(result.report_attempts[0].usage.total_tokens, 65)
        self.assertEqual(result.total_tokens, 65)

    # [추가] 재작성 중 모델 오류가 발생해도 앞선 사용량과 초안을 지우지 않는다.
    async def test_failure_during_rewrite_keeps_prior_draft_and_usage(self):
        llm = FakeReportLLM([report_response("검토할 짧은 초안"), RuntimeError("model unavailable")])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assert_failure(result, "REPORT_ERROR", "report")
        self.assertEqual(result.draft, "검토할 짧은 초안")
        self.assertEqual(len(result.report_attempts), 2)
        self.assertEqual(result.report_attempts[0].usage.total_tokens, 65)
        self.assertIsNone(result.report_attempts[1].usage)
        self.assertIsNone(result.total_tokens)

    # [추가] 조회 단계 시간 초과를 전체 워크플로 실패로 연결하고 보고 호출을 차단한다.
    async def test_timeout_during_execution_has_execution_stage(self):
        self.mcp.block_tool = True
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query, timeout_seconds=0.1)
        self.assert_failure(result, "WORKFLOW_TIMEOUT", "execution")
        self.assertEqual(result.execution.error.code, "EXECUTION_TIMEOUT")
        self.assertEqual(self.report_llm.calls, [])
        self.assertTrue(self.mcp.cancelled.is_set())

    # [추가] 보고 단계 시간 초과는 성공한 조회 자료와 진행 중인 보고 시도를 보존한다.
    async def test_timeout_during_report_has_report_stage_and_keeps_execution(self):
        self.report_llm.block_on_calls = {1}
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query, timeout_seconds=0.1)
        self.assert_failure(result, "WORKFLOW_TIMEOUT", "report")
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(len(result.execution.results), 2)
        self.assertEqual(len(result.report_attempts), 1)
        self.assertIsNone(result.report_attempts[0].usage)
        self.assertTrue(self.report_llm.cancelled.is_set())

    # [추가] 외부 취소는 조회 중이든 보고 중이든 실패 응답으로 삼키지 않는다.
    async def test_external_cancellation_propagates_from_both_stages(self):
        for stage in ("execution", "report"):
            with self.subTest(stage=stage):
                mcp = FakeMCP()
                llm = FakeReportLLM()
                if stage == "execution":
                    mcp.block_tool = True
                    entered = mcp.entered
                    cancelled = mcp.cancelled
                else:
                    llm.block_on_calls = {1}
                    entered = llm.entered
                    cancelled = llm.cancelled
                task = asyncio.create_task(run_workflow(conditional_definition(), self.catalog, mcp, llm, query=self.query))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(cancelled.is_set())
                finally:
                    if not task.done():
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task

    # [추가] 시간 제한은 양수 유한 숫자만 받고 잘못된 설정은 MCP 실행 전에 알린다.
    async def test_invalid_timeout_configuration_raises_value_error(self):
        for timeout in (0, -1, True, None, "1", float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query, timeout_seconds=timeout)
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.report_llm.calls, [])

    # [추가] 공급자가 일부 사용량을 제공하지 않으면 해당 보고 합계만 미확인으로 남긴다.
    async def test_report_usage_sums_known_fields_and_preserves_unknown_fields(self):
        partial = WorkflowModelUsage(model="partial", input_tokens=None, output_tokens=10, total_tokens=None, measurement="unavailable")
        llm = FakeReportLLM([report_response("짧은 초안"), report_response(usage=partial)])
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.input_tokens)
        self.assertEqual(result.output_tokens, 25)
        self.assertIsNone(result.total_tokens)
        self.assertEqual([attempt.usage.measurement for attempt in result.report_attempts], ["reported", "unavailable"])

    # [추가] 모델에 전달한 자료·trace의 변조가 실행 결과나 다음 재작성 입력을 바꾸지 못하게 한다.
    async def test_report_inputs_are_fresh_copies_for_each_attempt(self):
        llm = FakeReportLLM([report_response("짧은 초안"), report_response()])

        # [추가] 첫 보고 시도에서 전달 사본만 수정해 원본 상태 공유 여부를 검사한다.
        def mutate_first(number, collected, trace):
            if number == 1:
                collected[0]["data"]["cpu_percent"] = -999
                collected[0]["data"]["alerts"].clear()
                trace.clear()

        llm.on_call = mutate_first
        result = await run_workflow(conditional_definition(), self.catalog, self.mcp, llm, query=self.query)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.execution.results[0].data.cpu_percent, 80)
        self.assertTrue(result.execution.results[0].data.alerts)
        self.assertEqual(llm.calls[1]["collected"], llm.calls[0]["collected"])
        self.assertEqual(llm.calls[1]["trace"], llm.calls[0]["trace"])
        self.assertTrue(llm.calls[1]["trace"])

    # [추가] 정의 해시는 이름을 제외한 버전·단계만 정규화해 계산한다.
    async def test_definition_hash_is_canonical_and_excludes_name(self):
        payload = conditional_definition()
        first = await run_workflow(payload, self.catalog, self.mcp, self.report_llm, query=self.query)
        renamed = deepcopy(payload)
        renamed["name"] = "다른 이름의 동일 절차"
        second = await run_workflow(json.dumps(renamed, sort_keys=True), self.catalog, FakeMCP(), FakeReportLLM(), query=self.query)
        canonical = json.dumps({"schema_version": payload["schema_version"], "steps": payload["steps"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(first.definition_hash, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(first.definition_hash, second.definition_hash)
        changed = deepcopy(payload)
        changed["steps"][1]["condition"]["value"] = 81
        third = await run_workflow(changed, self.catalog, FakeMCP(), FakeReportLLM(), query=self.query)
        self.assertNotEqual(first.definition_hash, third.definition_hash)
        self.assertNotEqual(first.run_id, second.run_id)

    # [추가] compiled graph를 재사용해도 이전 최종 답변·시도·오류 상태를 새 실행에 물려주지 않는다.
    async def test_compiled_graph_resets_stale_state_before_execution(self):
        graph = build_workflow_graph(self.catalog, self.mcp, self.report_llm)
        state = await graph.ainvoke({
            "payload": conditional_definition(), "query": self.query,
            "final": "이전 실행의 최종 답변", "draft": "이전 실행의 초안",
            "report_valid": True, "events": [{"type": "StaleEvent"}],
            "report_attempts": [WorkflowReportAttempt(number=3, errors=["이전 검증 오류"])],
            "validation_attempts": 3, "validation_feedback": "이전 피드백",
            "error": WorkflowRunIssue(code="REPORT_ERROR", stage="report", message="이전 실행 오류"),
        })
        result = state["result"]
        self.assertEqual(result.status, "completed")
        self.assertIn("모니터링 1회, 런북 1회", result.final)
        self.assertIn("런북 요약(모델 생성)", result.final)
        self.assertIsNone(result.error)
        self.assertEqual([attempt.number for attempt in result.report_attempts], [1])
        self.assertNotIn("StaleEvent", [event["type"] for event in result.events])

    # [추가] MCP가 응답하기 전에 StepStarted가 custom 스트림에 도착해야 한다.
    async def test_step_started_streams_while_mcp_call_is_still_waiting(self):
        mcp = GatedMCP()
        graph = build_workflow_graph(self.catalog, mcp, self.report_llm)
        stream = graph.astream({"payload": conditional_definition(), "query": self.query}, stream_mode="custom")
        received = []
        try:
            async with asyncio.timeout(1):
                async for event in stream:
                    received.append(event)
                    if event["type"] == "StepStarted":
                        await mcp.entered.wait()
                        self.assertFalse(mcp.release.is_set())
                        self.assertFalse(any(item["type"] == "StepCompleted" for item in received))
                        break
            self.assertTrue(any(event["type"] == "StepStarted" for event in received))
            mcp.release.set()
            async with asyncio.timeout(1):
                async for event in stream:
                    received.append(event)
            self.assertIn("FinalAnswer", [event["type"] for event in received])
        finally:
            mcp.release.set()
            await stream.aclose()

    # [추가] 검증을 통과한 보고만 custom 스트림에 실리고 반려된 모델 JSON은 노출하지 않는다.
    async def test_only_validated_report_streams_with_attempt_and_metadata(self):
        llm = FakeReportLLM([report_response("짧은 초안"), report_response()])
        graph = build_workflow_graph(self.catalog, self.mcp, llm)
        events = [event async for event in graph.astream({
            "payload": conditional_definition(), "query": self.query,
            "workflow_id": "stream-workflow", "run_id": "stream-run",
        }, stream_mode="custom")]
        tokens = [event for event in events if event["type"] == "AnswerToken"]
        self.assertEqual([event["attempt"] for event in tokens], [2])
        self.assertEqual([event["delta"] for event in tokens], [events[-1]["final"]])
        self.assertNotIn("짧은 초안", tokens[0]["delta"])
        self.assertNotIn('"summaries"', tokens[0]["delta"])
        for event in tokens:
            self.assertEqual(event["run_id"], "stream-run")
            self.assertEqual(event["workflow_id"], "stream-workflow")
            self.assertTrue(event["definition_hash"])
        self.assertEqual([event["type"] for event in events].count("FinalAnswer"), 1)

    # [추가] 검증 직후 소비자가 이벤트 루프를 지연시켜도 기한이 지난 보고를 최종 승인하지 않는다.
    async def test_deadline_after_report_validation_prevents_final_answer(self):
        graph = build_workflow_graph(self.catalog, self.mcp, self.report_llm)
        received = []
        delayed = False
        async for event in graph.astream({
            "payload": conditional_definition(), "query": self.query, "timeout_seconds": 0.1,
        }, stream_mode="custom"):
            received.append(event)
            if event["type"] == "ReportValidation" and event["valid"] and not delayed:
                delayed = True
                # [추가] 테스트에서만 의도적으로 블로킹해 검증과 최종 확정 사이의 deadline 경계를 재현한다.
                time.sleep(0.15)
        self.assertTrue(delayed, "정상 검증 이후의 지연 상황을 실제로 재현해야 합니다.")
        self.assertFalse(any(event["type"] == "FinalAnswer" for event in received))
        terminal = received[-1]
        self.assertEqual(terminal["type"], "WorkflowFailed")
        self.assertEqual(terminal["status"], "failed")
        self.assertFalse(terminal["report_valid"])
        self.assertEqual(terminal["error"]["code"], "WORKFLOW_TIMEOUT")
        self.assertEqual(terminal["error"]["stage"], "report")
        self.assertEqual(terminal.get("final", ""), "")

    # [추가] 외부 이벤트 콜백이 받은 모델과 중첩 조건을 변경해도 실행기의 원본 trace는 보존한다.
    async def test_execution_event_callback_cannot_mutate_stored_trace(self):
        observed = []

        # [추가] 전달받은 이벤트 사본을 의도적으로 변조해 얕은 복사나 원본 노출을 검출한다.
        def mutate_event(event):
            observed.append(event)
            event.sequence = 999
            event.node_id = "mutated"
            event.path = "mutated"
            event.status = "failed"
            if event.condition is not None:
                event.condition.actual = -999

        result = await execute_workflow(
            conditional_definition(), self.catalog, self.mcp, on_event=mutate_event,
        )
        self.assertEqual(result.status, "completed")
        self.assertTrue(observed)
        self.assertTrue(all(event.sequence == 999 for event in observed))
        self.assertEqual([event.sequence for event in result.trace], list(range(1, len(result.trace) + 1)))
        self.assertTrue(all(event.node_id != "mutated" and event.path != "mutated" for event in result.trace))
        self.assertFalse(any(event.status == "failed" for event in result.trace))
        condition = next(event.condition for event in result.trace if event.condition is not None)
        self.assertEqual(condition.actual, 80)
        self.assertTrue(condition.result)

    # [추가] 두 조사를 동시에 실행해도 ID·자료·재작성 시도·메타데이터가 분리된다.
    async def test_concurrent_runs_keep_execution_and_reporting_state_isolated(self):
        first_payload = conditional_definition()
        second_payload = conditional_definition()
        second_payload["steps"][0]["system"] = "auth-service"
        first_llm = FakeReportLLM([report_response("짧은 초안"), report_response()])
        second_llm = FakeReportLLM()
        first, second = await asyncio.gather(
            run_workflow(first_payload, self.catalog, self.mcp, first_llm, query=self.query),
            run_workflow(second_payload, self.catalog, self.mcp, second_llm, query="인증 시스템 점검"),
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.execution.results[0].data.system_name, "payments-service")
        self.assertEqual(second.execution.results[0].data.system_name, "auth-service")
        self.assertEqual(len(first.report_attempts), 2)
        self.assertEqual(len(second.report_attempts), 1)
        self.assertEqual(first.total_tokens, 130)
        self.assertEqual(second.total_tokens, 65)
        self.assertTrue(all(event["run_id"] == first.run_id for event in first.events))
        self.assertTrue(all(event["run_id"] == second.run_id for event in second.events))

    # [추가] 성공·조회 실패·보고 실패 결과를 모두 API용 JSON으로 직렬화할 수 있다.
    async def test_success_and_failure_results_are_json_serializable(self):
        completed = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query)
        bad_mcp = FakeMCP()
        bad_mcp.tool_error = RuntimeError("unavailable")
        execution_failed = await run_workflow(conditional_definition(), self.catalog, bad_mcp, FakeReportLLM(), query=self.query)
        report_failed = await run_workflow(conditional_definition(), self.catalog, FakeMCP(), FakeReportLLM([RuntimeError("unavailable")]), query=self.query)
        for result in (completed, execution_failed, report_failed):
            with self.subTest(status=result.status, error=result.error):
                serialized = json.loads(result.model_dump_json())
                self.assertEqual(serialized["status"], result.status)
                self.assertEqual(serialized["final"], result.final)
                self.assertEqual(serialized["run_id"], result.run_id)
                self.assertEqual(len(serialized["events"]), len(result.events))

    # [추가] 조회·보고 검증이 끝나지 않은 결과나 확정 답변이 남은 실패를 완료로 표시하지 못하게 한다.
    async def test_run_result_rejects_inconsistent_completion_and_failure(self):
        completed = await run_workflow(conditional_definition(), self.catalog, self.mcp, self.report_llm, query=self.query)
        error = WorkflowRunIssue(code="REPORT_ERROR", stage="report", message="보고 오류")
        changes = (
            {"definition": None}, {"definition_hash": None}, {"execution": None},
            {"report_valid": False}, {"final": ""}, {"error": error},
            {"status": "failed", "error": error, "report_valid": False},
            {"status": "failed", "error": None, "report_valid": False, "final": ""},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                WorkflowRunResult.model_validate({**completed.model_dump(), **change})


# [추가] 파일을 직접 실행해도 비동기 표준 unittest 테스트를 사용할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

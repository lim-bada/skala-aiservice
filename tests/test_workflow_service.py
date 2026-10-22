# [추가] 새 파일 전체: 생성·SQLite 저장·미리보기·실행을 연결하는 서비스 계약을 외부 API 없이 검증한다.
"""실제 임시 SQLite와 주입한 모델·MCP 대역으로 저장 이후의 재검증과 실행을 확인한다."""

import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.workflow.catalog import CatalogError, WorkflowCatalog
from agent.workflow.service import WorkflowService, WorkflowServiceError
from agent.workflow.store import SqliteWorkflowStore
from tests.test_workflow_compilation import FakeCompilerLLM, model_response
from tests.test_workflow_execution import FakeMCP
from tests.test_workflow_graph import FakeReportLLM, report_response


# [추가] 카탈로그 변경·로딩 실패를 테스트 중 전환하고 새 로딩 횟수를 관찰한다.
class MutableCatalogLoader:
    # [추가] 실제 파일 대신 반환할 카탈로그 스냅샷을 준비한다.
    def __init__(self, catalog):
        self.current = catalog
        self.calls = 0

    # [추가] 호출마다 현재 설정을 반환하므로 이전 스냅샷을 재사용하는 오류를 검출한다.
    def __call__(self):
        self.calls += 1
        if isinstance(self.current, BaseException):
            raise self.current
        return self.current


# [추가] 일시적인 타임아웃과 영구 실패를 명시적으로 구분하는 MCP 대역이다.
class FailingMonitoringMCP(FakeMCP):
    # [추가] 실패 횟수와 예외 종류를 설정하고 실제 Tool 시도 수를 별도로 기록한다.
    def __init__(self, failures=1, exception_type=asyncio.TimeoutError):
        super().__init__()
        self.failures = failures
        self.exception_type = exception_type
        self.tool_attempts = 0

    # [추가] 지정한 횟수만 실패한 뒤 기존 고정 응답을 사용한다.
    async def call_tool(self, name, arguments, server="ops"):
        self.tool_attempts += 1
        if self.tool_attempts <= self.failures:
            raise self.exception_type("테스트용 모니터링 실패")
        return await super().call_tool(name, arguments, server=server)


# [추가] 저장소와 모델·MCP를 서비스에 연결한 실제 통합 흐름을 검증한다.
class WorkflowServiceTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 테스트마다 독립 DB와 충분한 고정 모델 응답을 제공한다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "workflows.db"
        self.store = SqliteWorkflowStore(self.db_path)
        self.addCleanup(self.store.close)
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )
        self.loader = MutableCatalogLoader(self.catalog)
        self.fast = FakeCompilerLLM([model_response() for _ in range(8)])
        self.quality = FakeReportLLM([report_response() for _ in range(8)])
        self.mcp = FakeMCP()
        self.query = "payments-service CPU가 80 이상이면 CPU 런북, 아니면 메모리 런북을 확인해줘."
        self.service = self.make_service()

    # [추가] 각 실패 조건에 필요한 의존성만 교체하면서 동일 저장소를 공유한다.
    def make_service(self, *, store=None, fast=None, quality=None, mcp=None, **options):
        return WorkflowService(
            self.store if store is None else store,
            self.fast if fast is None else fast,
            self.quality if quality is None else quality,
            self.mcp if mcp is None else mcp,
            catalog_loader=self.loader, **options,
        )

    # [추가] 서비스 오류의 HTTP 상태와 업무 코드가 함께 전달되는지 확인한다.
    async def assert_service_error(self, awaitable, status, code):
        with self.assertRaises(WorkflowServiceError) as caught:
            await awaitable
        error = caught.exception
        self.assertEqual(error.status_code, status)
        self.assertEqual(error.code, code)
        self.assertTrue(error.message)
        return error

    # [추가] 생성·초안 조회·이름 저장·목록·실행까지 이어져도 자연어를 다시 생성하지 않는다.
    async def test_generate_save_list_and_execute_complete_without_regeneration(self):
        generated = await self.service.generate(self.query)
        self.assertEqual(generated.status, "draft")
        self.assertTrue(generated.compilation.valid)
        self.assertEqual(generated.compilation.total_tokens, 120)
        self.assertEqual(len(self.fast.calls), 1)
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])
        loaded = await self.service.get_workflow(generated.workflow_id)
        self.assertEqual(loaded.definition_hash, generated.definition_hash)
        self.assertEqual(await self.service.list_workflows(), [])
        self.assertEqual(len(await self.service.list_workflows(include_drafts=True)), 1)
        saved = await self.service.save_workflow(generated.workflow_id, "팀 공용 결제 점검")
        self.assertEqual(saved.status, "saved")
        self.assertEqual(saved.name, "팀 공용 결제 점검")
        self.assertEqual(saved.definition.model_dump(), generated.definition.model_dump())
        self.assertEqual(saved.definition_hash, generated.definition_hash)
        self.assertEqual(saved.query, generated.query)
        self.assertIsNotNone(saved.saved_at)
        self.assertEqual([item.workflow_id for item in await self.service.list_workflows()], [saved.workflow_id])
        result = await self.service.execute(saved.workflow_id, saved.definition_hash)
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.report_valid)
        self.assertEqual(result.definition_hash, saved.definition_hash)
        self.assertEqual(len(self.fast.calls), 1)
        self.assertEqual(len(self.quality.calls), 1)
        self.assertEqual([item.node_id for item in result.execution.results], ["metrics_1", "cpu_book"])

    # [추가] 저장 목록의 기본 필터와 페이지 단위 조회가 서비스까지 유지된다.
    async def test_listing_filters_drafts_and_supports_pagination(self):
        records = [await self.service.generate(self.query) for _ in range(3)]
        await self.service.save_workflow(records[0].workflow_id, "첫 번째 저장")
        await self.service.save_workflow(records[1].workflow_id, "두 번째 저장")
        saved = await self.service.list_workflows()
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(record.status == "saved" for record in saved))
        all_records = await self.service.list_workflows(include_drafts=True)
        self.assertEqual(len(all_records), 3)
        page = await self.service.list_workflows(limit=1, offset=1, include_drafts=True)
        self.assertEqual([record.workflow_id for record in page], [all_records[1].workflow_id])

    # [추가] 카탈로그 조회·생성·실행 준비마다 새 목록을 읽고 단순 저장 조회에는 모델을 호출하지 않는다.
    async def test_catalog_is_reloaded_for_catalog_generation_and_preparation(self):
        initial = await self.service.get_catalog()
        self.assertEqual(self.loader.calls, 1)
        self.assertEqual(initial["systems"], list(self.catalog.systems))
        self.assertEqual(initial["incident_types"], list(self.catalog.incident_types))
        self.assertEqual(initial["condition_fields"], self.catalog.to_dict()["condition_fields"])
        self.assertIsInstance(initial["limits"], dict)
        self.assertEqual(initial["limits"]["max_nodes"], 6)
        self.assertEqual(initial["limits"]["max_query_length"], 2000)
        generated = await self.service.generate(self.query)
        self.assertEqual(self.loader.calls, 2)
        await self.service.get_workflow(generated.workflow_id)
        await self.service.save_workflow(generated.workflow_id, "표시 이름")
        await self.service.list_workflows()
        self.assertEqual(self.loader.calls, 2)
        await self.service.prepare_execution(generated.workflow_id, generated.definition_hash)
        self.assertEqual(self.loader.calls, 3)

    # [추가] 사용자에게 반환한 카탈로그 사본을 바꿔도 이후 생성·검증 정책은 변하지 않는다.
    async def test_catalog_payload_is_independent_of_loader_snapshot(self):
        payload = await self.service.get_catalog()
        payload["systems"].append("injected-service")
        payload["condition_fields"].clear()
        second = await self.service.get_catalog()
        self.assertEqual(second["systems"], list(self.catalog.systems))
        self.assertTrue(second["condition_fields"])
        self.assertNotIn("injected-service", self.catalog.systems)

    # [추가] 생성 실패는 HTTP 의미에 맞게 변환하고 불완전한 절차를 DB에 저장하지 않는다.
    async def test_generation_failures_map_codes_and_never_store_or_execute(self):
        cases = (
            ("", [model_response()], 422, "INVALID_QUERY"),
            (self.query, [model_response(json.dumps({"unsupported_reason": "지원하지 않는 명령입니다."}))], 422, "UNSUPPORTED_REQUEST"),
            (self.query, [model_response(refusal="생성을 거절했습니다.")], 422, "MODEL_REFUSED"),
            (self.query, [model_response("bad"), model_response("bad")], 422, "VALIDATION_FAILED"),
            (self.query, [RuntimeError("model unavailable")], 502, "MODEL_ERROR"),
        )
        for query, responses, status, code in cases:
            with self.subTest(code=code):
                fast = FakeCompilerLLM(responses)
                service = self.make_service(fast=fast)
                error = await self.assert_service_error(service.generate(query), status, code)
                self.assertIsInstance(error.details, dict)
                self.assertIn("compilation", error.details)
                self.assertFalse(error.details["compilation"]["valid"])
                self.assertEqual(await self.service.list_workflows(include_drafts=True), [])
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])

    # [추가] 검증에 실패한 생성도 두 호출의 토큰과 오류 기록을 예외 상세에 보존한다.
    async def test_failed_generation_preserves_compilation_usage(self):
        fast = FakeCompilerLLM([model_response("bad"), model_response("bad")])
        service = self.make_service(fast=fast)
        error = await self.assert_service_error(service.generate(self.query), 422, "VALIDATION_FAILED")
        compilation = error.details["compilation"]
        self.assertEqual(compilation["total_tokens"], 240)
        self.assertEqual([attempt["number"] for attempt in compilation["attempts"]], [1, 2])
        self.assertTrue(compilation["errors"])

    # [추가] 생성 제한 시간 초과는 진행 중 모델을 취소하고 초안을 저장하지 않는다.
    async def test_generation_timeout_maps_to_gateway_timeout(self):
        fast = FakeCompilerLLM()
        fast.block_on_calls = {1}
        service = self.make_service(fast=fast, generation_timeout_seconds=0.05)
        error = await self.assert_service_error(service.generate(self.query), 504, "GENERATION_TIMEOUT")
        self.assertTrue(fast.cancelled.is_set())
        self.assertEqual(len(error.details["compilation"]["attempts"]), 1)
        self.assertEqual(await self.service.list_workflows(include_drafts=True), [])

    # [추가] 없는 ID는 조회·저장·실행 준비 어디서든 동일한 404 오류로 처리한다.
    async def test_missing_workflow_is_not_found_without_model_or_mcp_calls(self):
        for action in (
            self.service.get_workflow("missing-id"),
            self.service.save_workflow("missing-id", "이름"),
            self.service.prepare_execution("missing-id", "0" * 64),
        ):
            await self.assert_service_error(action, 404, "WORKFLOW_NOT_FOUND")
        self.assertEqual(self.fast.calls, [])
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])

    # [추가] 해시가 다르거나 실제 64자리 해시 형식이 아니면 도구 호출 전에 거부한다.
    async def test_hash_mismatch_and_invalid_hash_block_execution(self):
        generated = await self.service.generate(self.query)
        for received_hash, status, code in (
            ("0" * 64, 409, "DEFINITION_HASH_MISMATCH"),
            ("invalid", 422, "INVALID_DEFINITION_HASH"),
            ("", 422, "INVALID_DEFINITION_HASH"),
            (None, 422, "INVALID_DEFINITION_HASH"),
            (123, 422, "INVALID_DEFINITION_HASH"),
        ):
            with self.subTest(received_hash=received_hash):
                await self.assert_service_error(
                    self.service.execute(generated.workflow_id, received_hash),
                    status, code,
                )
        self.assertEqual(len(self.fast.calls), 1)
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])

    # [추가] 생성 후 시스템이 카탈로그에서 제거되면 저장된 정의를 그대로 실행하지 않는다.
    async def test_deleted_system_blocks_preparation_as_catalog_changed(self):
        generated = await self.service.generate(self.query)
        self.loader.current = WorkflowCatalog(systems=("auth-service",), incident_types=self.catalog.incident_types)
        await self.assert_service_error(
            self.service.prepare_execution(generated.workflow_id, generated.definition_hash),
            409, "CATALOG_CHANGED",
        )
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])
        self.assertEqual(len(self.fast.calls), 1)

    # [추가] 현재 선택될 가능성이 낮은 분기라도 런북이 삭제되면 전체 정의를 재검증해 차단한다.
    async def test_deleted_runbook_in_unselected_branch_blocks_execution(self):
        generated = await self.service.generate(self.query)
        self.loader.current = WorkflowCatalog(systems=self.catalog.systems, incident_types=("cpu-spike", "db-down"))
        await self.assert_service_error(
            self.service.execute(generated.workflow_id, generated.definition_hash), 409, "CATALOG_CHANGED",
        )
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])

    # [추가] 카탈로그를 읽을 수 없을 때 조회·생성·실행 준비 모두 503으로 명시한다.
    async def test_catalog_loading_failure_is_service_unavailable(self):
        generated = await self.service.generate(self.query)
        self.loader.current = CatalogError("catalog unavailable")
        for action in (
            self.service.get_catalog(),
            self.service.generate(self.query),
            self.service.prepare_execution(generated.workflow_id, generated.definition_hash),
        ):
            await self.assert_service_error(action, 503, "CATALOG_UNAVAILABLE")
        self.assertEqual(len(self.fast.calls), 1)
        self.assertEqual(self.mcp.calls, [])

    # [추가] 저장소 운영 오류와 사용자에게 없는 절차를 요청한 경우를 구분한다.
    async def test_closed_store_is_reported_as_store_error(self):
        generated = await self.service.generate(self.query)
        self.store.close()
        await self.assert_service_error(self.service.get_workflow(generated.workflow_id), 500, "STORE_ERROR")
        self.assertEqual(self.mcp.calls, [])

    # [추가] DB의 JSON 손상은 사용자 요청이나 모델 오류와 구분하고 실행 전에 차단한다.
    async def test_corrupted_stored_definition_is_conflict_and_never_executes(self):
        generated = await self.service.generate(self.query)
        for definition_json, definition_hash in (
            ("{broken", generated.definition_hash),
            (generated.definition.model_dump_json(), "0" * 64),
        ):
            with self.subTest(definition_json=definition_json, definition_hash=definition_hash):
                with sqlite3.connect(self.db_path) as connection:
                    connection.execute(
                        "UPDATE workflows SET definition_json=?, definition_hash=? WHERE id=?",
                        (definition_json, definition_hash, generated.workflow_id),
                    )
                    connection.commit()
                await self.assert_service_error(
                    self.service.get_workflow(generated.workflow_id), 409, "STORED_WORKFLOW_INVALID",
                )
                await self.assert_service_error(
                    self.service.execute(generated.workflow_id, generated.definition_hash),
                    409, "STORED_WORKFLOW_INVALID",
                )
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.quality.calls, [])
        self.assertEqual(len(self.fast.calls), 1)

    # [추가] 같은 저장 절차를 재사용해도 새 관측값을 조회하여 반대 분기를 선택할 수 있다.
    async def test_repeated_execution_fetches_new_snapshot_without_regeneration(self):
        generated = await self.service.generate(self.query)
        first = await self.service.execute(generated.workflow_id, generated.definition_hash)
        self.mcp.snapshots["payments-service"]["cpu_percent"] = 79.9
        second = await self.service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual(first.execution.results[-1].node_id, "cpu_book")
        self.assertEqual(second.execution.results[-1].node_id, "memory_book")
        self.assertEqual(first.execution.results[0].data.cpu_percent, 80)
        self.assertEqual(second.execution.results[0].data.cpu_percent, 79.9)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.definition_hash, second.definition_hash)
        self.assertEqual(len(self.fast.calls), 1)

    # [추가] 실행 중 MCP 실패는 서비스 예외로 바꾸지 않고 조회 상세가 있는 실패 결과로 반환한다.
    async def test_runtime_failure_is_returned_as_run_result(self):
        generated = await self.service.generate(self.query)
        self.mcp.tool_error = RuntimeError("monitoring unavailable")
        result = await self.service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "EXECUTION_FAILED")
        self.assertEqual(result.execution.error.code, "MCP_ERROR")
        self.assertEqual(result.final, "")
        self.assertEqual(self.quality.calls, [])

    # [추가] 보고 실패 시에도 완료한 조회 자료와 시도별 사용량·검토용 초안은 유지한다.
    async def test_runtime_report_failure_keeps_prior_usage_and_draft(self):
        generated = await self.service.generate(self.query)
        quality = FakeReportLLM([report_response("짧은 보고 초안"), RuntimeError("model unavailable")])
        service = self.make_service(quality=quality)
        result = await service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "REPORT_ERROR")
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.draft, "짧은 보고 초안")
        self.assertEqual(result.report_attempts[0].usage.total_tokens, 65)
        self.assertIsNone(result.report_attempts[1].usage)
        self.assertIsNone(result.total_tokens)
        self.assertEqual(len(self.fast.calls), 1)

    # [추가] 기본 정책은 최초 MCP 타임아웃도 즉시 실패시켜 숨겨진 재시도를 하지 않는다.
    async def test_default_mcp_policy_is_fail_fast(self):
        generated = await self.service.generate(self.query)
        mcp = FailingMonitoringMCP()
        service = self.make_service(mcp=mcp)
        result = await service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "failed")
        self.assertEqual(mcp.tool_attempts, 1)
        self.assertEqual(self.quality.calls, [])

    # [추가] 명시한 MCP 재시도 정책은 타임아웃만 제한 횟수 내 재실행할 수 있다.
    async def test_configured_retry_recovers_synthetic_timeout(self):
        generated = await self.service.generate(self.query)
        mcp = FailingMonitoringMCP()
        service = self.make_service(mcp=mcp, mcp_attempts=3, mcp_retry_delay=0)
        result = await service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "completed")
        self.assertEqual(mcp.tool_attempts, 2)
        self.assertEqual(len(self.fast.calls), 1)
        self.assertEqual(len(self.quality.calls), 1)

    # [추가] 영구 MCP 오류는 재시도를 설정해도 반복하지 않는다.
    async def test_configured_retry_does_not_repeat_permanent_error(self):
        generated = await self.service.generate(self.query)
        mcp = FailingMonitoringMCP(exception_type=RuntimeError)
        service = self.make_service(mcp=mcp, mcp_attempts=3, mcp_retry_delay=0)
        result = await service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "failed")
        self.assertEqual(mcp.tool_attempts, 1)

    # [추가] 재시도 대기도 전체 실행 기한 안에 포함되어 무한 대기를 만들지 않는다.
    async def test_retry_delay_obeys_total_execution_deadline(self):
        generated = await self.service.generate(self.query)
        mcp = FailingMonitoringMCP(failures=3)
        service = self.make_service(mcp=mcp, mcp_attempts=3, mcp_retry_delay=0.3, execution_timeout_seconds=0.1)
        result = await service.execute(generated.workflow_id, generated.definition_hash)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.code, "WORKFLOW_TIMEOUT")
        self.assertEqual(result.error.stage, "execution")
        self.assertEqual(mcp.tool_attempts, 1)
        self.assertEqual(self.quality.calls, [])

    # [추가] 준비된 record 접근은 매번 독립 사본이며 스트림은 DB를 다시 읽지 않는다.
    async def test_prepared_stream_uses_immutable_snapshot_without_second_store_read(self):
        generated = await self.service.generate(self.query)
        prepared = await self.service.prepare_execution(generated.workflow_id, generated.definition_hash)
        changed = prepared.record
        changed.name = "변경 시도"
        changed.definition.steps[1].condition.value = 999
        self.assertEqual(prepared.record.definition.steps[1].condition.value, 80)
        # [추가] 준비 후 DB가 바뀌어도 이미 검증된 스트림 정의를 다른 내용으로 바꾸지 못하게 한다.
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE workflows SET definition_json=? WHERE id=?",
                ("{changed after preparation", generated.workflow_id),
            )
            connection.commit()
        with patch.object(self.store, "get", side_effect=AssertionError("준비 이후 DB 재조회 금지")):
            events = [event async for event in self.service.stream_prepared(prepared)]
        terminal = events[-1]
        self.assertEqual(terminal["type"], "FinalAnswer")
        self.assertEqual(terminal["result"]["status"], "completed")
        self.assertEqual(terminal["result"]["definition_hash"], generated.definition_hash)
        self.assertEqual(terminal["result"]["execution"]["results"][-1]["node_id"], "cpu_book")
        self.assertEqual(len(self.fast.calls), 1)

    # [추가] 스트림 소비 작업 취소가 실제 진행 중 MCP 호출까지 전파된다.
    async def test_stream_cancellation_propagates_to_active_mcp_call(self):
        generated = await self.service.generate(self.query)
        prepared = await self.service.prepare_execution(generated.workflow_id, generated.definition_hash)
        self.mcp.block_tool = True

        # [추가] 소비자가 취소될 때 서비스가 백그라운드 실행을 남기지 않는지 확인한다.
        async def consume():
            return [event async for event in self.service.stream_prepared(prepared)]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(self.mcp.entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.mcp.cancelled.is_set())
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 동기 SQLite의 생성·조회·저장·목록은 이벤트 루프 스레드 바깥에서 실행한다.
    async def test_sqlite_operations_run_outside_event_loop_thread(self):
        event_loop_thread = threading.get_ident()
        called_threads = {}

        # [추가] 실제 저장소 동작은 유지하면서 각 연산이 실행된 스레드만 기록한다.
        def record_thread(name, original):
            def wrapped(*args, **kwargs):
                called_threads[name] = threading.get_ident()
                return original(*args, **kwargs)
            return wrapped

        with patch.object(self.store, "create_draft", side_effect=record_thread("create_draft", self.store.create_draft)):
            generated = await self.service.generate(self.query)
        with patch.object(self.store, "get", side_effect=record_thread("get", self.store.get)):
            await self.service.get_workflow(generated.workflow_id)
        with patch.object(self.store, "save", side_effect=record_thread("save", self.store.save)):
            await self.service.save_workflow(generated.workflow_id, "스레드 확인")
        with patch.object(self.store, "list_workflows", side_effect=record_thread("list_workflows", self.store.list_workflows)):
            await self.service.list_workflows()
        self.assertEqual(set(called_threads), {"create_draft", "get", "save", "list_workflows"})
        self.assertTrue(all(thread != event_loop_thread for thread in called_threads.values()))

    # [추가] DB를 닫고 새 서비스로 열어도 저장한 절차를 모델 재생성 없이 실행한다.
    async def test_sqlite_reopen_preserves_saved_workflow_and_hash(self):
        generated = await self.service.generate(self.query)
        saved = await self.service.save_workflow(generated.workflow_id, "재시작 후 점검")
        self.store.close()
        reopened_store = SqliteWorkflowStore(self.db_path)
        self.addCleanup(reopened_store.close)
        fast = FakeCompilerLLM([])
        service = self.make_service(store=reopened_store, fast=fast)
        loaded = await service.get_workflow(saved.workflow_id)
        self.assertEqual(loaded.name, saved.name)
        self.assertEqual(loaded.definition_hash, saved.definition_hash)
        result = await service.execute(saved.workflow_id, saved.definition_hash)
        self.assertEqual(result.status, "completed")
        self.assertEqual(fast.calls, [])

    # [추가] 같은 저장 정의를 동시에 실행해도 실행 ID·진행 이벤트·보고 시도가 섞이지 않는다.
    async def test_concurrent_executions_keep_run_state_isolated(self):
        generated = await self.service.generate(self.query)
        first, second = await asyncio.gather(
            self.service.execute(generated.workflow_id, generated.definition_hash),
            self.service.execute(generated.workflow_id, generated.definition_hash),
        )
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertTrue(all(event["run_id"] == first.run_id for event in first.events))
        self.assertTrue(all(event["run_id"] == second.run_id for event in second.events))
        self.assertEqual(len(first.report_attempts), 1)
        self.assertEqual(len(second.report_attempts), 1)
        self.assertEqual(len(self.fast.calls), 1)


# [추가] 파일을 직접 실행해도 비동기 unittest 테스트를 사용할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

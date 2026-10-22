# [추가] 실제 서비스·임시 SQLite·모의 모델/MCP로 워크플로 API 계약을 검증하는 신규 파일 전체.
"""server.py와 .env를 읽지 않고 독립 FastAPI 앱에서 생성·재사용·실행·SSE를 확인한다."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from agent.fake_llm import FastLLM, QualityLLM
from agent.workflow.api import register_workflow_api
from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.graph import stream_workflow as actual_graph_stream
from agent.workflow.service import WorkflowService
from agent.workflow.store import SqliteWorkflowStore
from tests.test_workflow_execution import FakeMCP


# [추가] fake가 명시적으로 지원하는 요청을 사용해 자연어 모델 성능과 HTTP 동작을 구분한다.
_QUERY = "payments-service를 조회하고 CPU가 80% 이상이면 CPU 런북을, 아니면 메모리 런북을 확인해줘."


# [추가] 응답 전체의 SSE 프레임을 읽어 이벤트와 프로젝트 완료 신호를 분리한다.
def sse_payloads(text):
    frames = []
    for frame in text.split("\n\n"):
        if frame.startswith("data: "):
            data = frame[len("data: "):]
            frames.append(data if data == "[DONE]" else json.loads(data))
    return frames


# [추가] 라우터 대역 대신 실제 서비스와 저장소를 요청마다 연결해 경계 계약을 검사한다.
class WorkflowAPITests(unittest.IsolatedAsyncioTestCase):
    # [추가] 별도 DB와 고정 응답을 준비하고 추적·외부 네트워크를 차단한다.
    async def asyncSetUp(self):
        self.enterContext(patch.dict("os.environ", {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
        }))
        self.network = self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("실제 네트워크 접속 금지"),
        ))
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "workflow.db"
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )
        self.mcp = FakeMCP()
        self.fast_llm = FastLLM("api-test-fast", 0.1)
        self.quality_llm = QualityLLM("api-test-quality", 0.1)
        self.store = SqliteWorkflowStore(self.database_path)
        self.service = self.make_service()
        self.app = self.make_app(self.service)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://workflow.test",
        )

    # [추가] 클라이언트·파일을 해제해 테스트 사이에 저장 상태를 공유하지 않는다.
    async def asyncTearDown(self):
        await self.client.aclose()
        self.store.close()
        self.temporary.cleanup()
        self.network.assert_not_called()
        # [추가] 성공·실패·연결 종료 어떤 경로도 요청 전용 생산자를 남기지 않아야 한다.
        self.assertEqual([
            task for task in asyncio.all_tasks()
            if not task.done() and task.get_name() == "workflow-sse-producer"
        ], [])

    # [추가] 카탈로그 교체와 제한 시간만 주입해 같은 서비스의 정상·실패 경로를 검사한다.
    def make_service(self, **overrides):
        return WorkflowService(
            self.store, self.fast_llm, self.quality_llm, self.mcp,
            catalog_loader=lambda: self.catalog, **overrides,
        )

    # [추가] 실제 서버의 lifespan·환경 로딩 없이 API 등록 계약만 사용한다.
    def make_app(self, service):
        app = FastAPI()
        register_workflow_api(app)
        app.state.workflow_service = service
        return app

    # [추가] 여러 테스트에서 생성 API의 정상 응답과 저장된 초안을 확보한다.
    async def generate(self):
        response = await self.client.post("/workflows/generate", json={"query": _QUERY})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # [추가] 카탈로그→초안→미리보기→저장→목록이 같은 정의·hash를 유지하고 조회는 하지 않는다.
    async def test_catalog_generation_preview_save_and_listing(self):
        catalog = await self.client.get("/workflows/catalog")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["systems"], list(self.catalog.systems))
        generated = await self.generate()
        workflow_id = generated["workflow_id"]
        definition_hash = generated["definition_hash"]
        self.assertEqual(generated["status"], "draft")
        self.assertTrue(generated["compilation"]["valid"])
        self.assertEqual(generated["compilation"]["workflow"], generated["definition"])
        self.assertEqual(generated["compilation"]["attempts"][0]["usage"]["measurement"], "estimated")
        self.assertEqual(self.mcp.calls, [])

        preview = await self.client.get(f"/workflows/{workflow_id}")
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["definition"], generated["definition"])
        self.assertEqual(preview.json()["definition_hash"], definition_hash)
        hidden_drafts = await self.client.get("/workflows")
        self.assertEqual(hidden_drafts.json(), {"workflows": [], "limit": 20, "offset": 0})
        drafts = await self.client.get("/workflows", params={"include_drafts": True})
        self.assertEqual([item["workflow_id"] for item in drafts.json()["workflows"]], [workflow_id])

        saved = await self.client.post(f"/workflows/{workflow_id}/save", json={"name": "  결제 조건 점검  "})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["name"], "결제 조건 점검")
        self.assertEqual(saved.json()["status"], "saved")
        self.assertEqual(saved.json()["definition_hash"], definition_hash)
        self.assertEqual(saved.json()["definition"], generated["definition"])
        listed = await self.client.get("/workflows", params={"limit": 1, "offset": 0})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["limit"], 1)
        self.assertEqual([item["workflow_id"] for item in listed.json()["workflows"]], [workflow_id])
        self.assertEqual(self.mcp.calls, [])

    # [추가] 저장 정의로 실행한 실제 선택 분기와 보고 결과를 JSON으로 반환한다.
    async def test_execute_returns_report_and_selected_trace(self):
        generated = await self.generate()
        response = await self.client.post(
            f"/workflows/{generated['workflow_id']}/execute",
            json={"definition_hash": generated["definition_hash"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["report_valid"])
        self.assertEqual(result["definition"], generated["definition"])
        self.assertEqual(result["definition_hash"], generated["definition_hash"])
        self.assertTrue(result["final"])
        self.assertEqual(len(result["report_attempts"]), 1)
        condition = next(event["condition"] for event in result["execution"]["trace"] if event["condition"])
        self.assertEqual(condition["actual"], 80)
        self.assertTrue(condition["result"])
        self.assertEqual(condition["selected_branch"], "then_steps")
        self.assertEqual([call[1] for call in self.mcp.calls if call[0] == "resource"],
                         ["runbook://incident-response/cpu-spike"])
        self.assertEqual(len([call for call in self.mcp.calls if call[0] == "tool"]), 1)

    # [추가] 잘못된 hash와 변경된 카탈로그는 JSON·SSE 모두 조회 전에 HTTP 409로 거절한다.
    async def test_hash_and_catalog_conflicts_are_pre_execution_http_errors(self):
        generated = await self.generate()
        workflow_id = generated["workflow_id"]
        wrong_hash = "0" * 64 if generated["definition_hash"] != "0" * 64 else "1" * 64
        for suffix in ("execute", "execute/stream"):
            with self.subTest(conflict="hash", suffix=suffix):
                response = await self.client.post(
                    f"/workflows/{workflow_id}/{suffix}", json={"definition_hash": wrong_hash},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("error", response.json())
                self.assertNotIn("text/event-stream", response.headers.get("content-type", ""))
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"), incident_types=("cpu-spike",),
        )
        for suffix in ("execute", "execute/stream"):
            with self.subTest(conflict="catalog", suffix=suffix):
                response = await self.client.post(
                    f"/workflows/{workflow_id}/{suffix}",
                    json={"definition_hash": generated["definition_hash"]},
                )
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("error", response.json())
        self.assertEqual(self.mcp.calls, [])

    # [추가] 미지원 자연어는 422이며 실행 가능한 초안을 저장하지 않는다.
    async def test_unsupported_query_is_422_and_never_executes_mcp(self):
        response = await self.client.post("/workflows/generate", json={"query": "billing-service를 조회해줘."})
        self.assertEqual(response.status_code, 422, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "UNSUPPORTED_REQUEST")
        self.assertFalse(error["details"]["compilation"]["valid"])
        self.assertEqual(self.mcp.calls, [])
        listed = await self.client.get("/workflows", params={"include_drafts": True})
        self.assertEqual(listed.json()["workflows"], [])

    # [추가] 없는 ID는 조회·저장·실행·스트림에서 같은 404 오류 구조를 반환한다.
    async def test_unknown_workflow_is_404_on_all_id_routes(self):
        requests = [
            ("GET", "/workflows/missing-id", None),
            ("POST", "/workflows/missing-id/save", {"name": "새 이름"}),
            ("POST", "/workflows/missing-id/execute", {"definition_hash": "0" * 64}),
            ("POST", "/workflows/missing-id/execute/stream", {"definition_hash": "0" * 64}),
        ]
        for method, path, body in requests:
            with self.subTest(path=path):
                response = await self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertIn("code", response.json()["error"])
        self.assertEqual(self.mcp.calls, [])

    # [추가] API 입력의 타입·추가 필드·길이·페이지 범위는 서비스 실행 전에 거부한다.
    async def test_invalid_request_bodies_and_pagination_return_422(self):
        generated = await self.generate()
        workflow_id = generated["workflow_id"]
        cases = [
            ("/workflows/generate", {}),
            ("/workflows/generate", {"query": 1}),
            ("/workflows/generate", {"query": True}),
            ("/workflows/generate", {"query": ""}),
            ("/workflows/generate", {"query": "q" * 2001}),
            ("/workflows/generate", {"query": _QUERY, "extra": "ignored?"}),
            (f"/workflows/{workflow_id}/save", {"name": 1}),
            (f"/workflows/{workflow_id}/save", {"name": " \t "}),
            (f"/workflows/{workflow_id}/save", {"name": "n" * 81}),
            (f"/workflows/{workflow_id}/save", {"name": "이름", "query": "다른 실행"}),
            (f"/workflows/{workflow_id}/execute", {}),
            (f"/workflows/{workflow_id}/execute", {"definition_hash": 1}),
            (f"/workflows/{workflow_id}/execute", {"definition_hash": "A" * 64}),
            (f"/workflows/{workflow_id}/execute", {"definition_hash": "a" * 63}),
            (f"/workflows/{workflow_id}/execute", {"definition_hash": generated["definition_hash"], "definition": {}}),
            (f"/workflows/{workflow_id}/execute/stream", {"definition_hash": "invalid"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                response = await self.client.post(path, json=body)
                self.assertEqual(response.status_code, 422, response.text)
        for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "invalid"}, {"include_drafts": "invalid"}):
            with self.subTest(params=params):
                response = await self.client.get("/workflows", params=params)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.mcp.calls, [])

    # [추가] DB를 재연결해도 저장 정의를 재사용하고 최신 스냅샷으로 반대 분기를 선택한다.
    async def test_saved_workflow_reopens_and_uses_fresh_monitoring_snapshot(self):
        generated = await self.generate()
        workflow_id = generated["workflow_id"]
        body = {"definition_hash": generated["definition_hash"]}
        await self.client.post(f"/workflows/{workflow_id}/save", json={"name": "재사용 절차"})
        first = await self.client.post(f"/workflows/{workflow_id}/execute", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.store.close()
        self.store = SqliteWorkflowStore(self.database_path)
        self.service = self.make_service()
        self.app.state.workflow_service = self.service
        self.mcp.snapshots["payments-service"]["cpu_percent"] = 79.9
        self.mcp.calls.clear()
        loaded = await self.client.get(f"/workflows/{workflow_id}")
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["status"], "saved")
        self.assertEqual(loaded.json()["definition_hash"], generated["definition_hash"])
        second = await self.client.post(f"/workflows/{workflow_id}/execute", json=body)
        self.assertEqual(second.status_code, 200, second.text)
        condition = next(event["condition"] for event in second.json()["execution"]["trace"] if event["condition"])
        self.assertEqual(condition["actual"], 79.9)
        self.assertFalse(condition["result"])
        self.assertEqual(condition["selected_branch"], "else_steps")
        self.assertEqual([call[1] for call in self.mcp.calls if call[0] == "resource"],
                         ["runbook://incident-response/memory-leak"])
        self.assertNotEqual(first.json()["run_id"], second.json()["run_id"])

    # [추가] 조회 실패는 전체 실행 결과를 보존해 502로 반환하고 원문 예외는 숨긴다.
    async def test_runtime_failure_is_502_with_structured_run_result(self):
        generated = await self.generate()
        self.mcp.tool_error = RuntimeError("private runtime failure detail")
        response = await self.client.post(
            f"/workflows/{generated['workflow_id']}/execute",
            json={"definition_hash": generated["definition_hash"]},
        )
        self.assertEqual(response.status_code, 502, response.text)
        result = response.json()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "EXECUTION_FAILED")
        self.assertEqual(result["report_attempts"], [])
        self.assertFalse(result["report_valid"])
        self.assertEqual(result["final"], "")
        self.assertNotIn("private runtime failure detail", response.text)

    # [추가] JSON·SSE 모두 요약 실패를 유지하면서 코드로 작성한 조회 사실을 전달한다.
    async def test_report_failure_keeps_deterministic_facts_in_json_and_sse(self):
        generated = await self.generate()
        for suffix in ("/execute", "/execute/stream"):
            with self.subTest(suffix=suffix), patch.object(self.quality_llm, "write_workflow", side_effect=RuntimeError("model unavailable")):
                response = await self.client.post(
                    f"/workflows/{generated['workflow_id']}{suffix}",
                    json={"definition_hash": generated["definition_hash"]},
                )
                if suffix.endswith("stream"):
                    self.assertEqual(response.status_code, 200)
                    events = sse_payloads(response.text)[:-1]
                    self.assertFalse(any(event["type"] == "AnswerToken" for event in events))
                    self.assertEqual(events[-1]["type"], "WorkflowFailed")
                    result = events[-1]["result"]
                else:
                    self.assertEqual(response.status_code, 502)
                    result = response.json()
                self.assertEqual(result["error"]["stage"], "report")
                self.assertEqual(result["execution"]["status"], "completed")
                self.assertFalse(result["report_valid"])
                self.assertEqual(result["final"], "")
                self.assertIn("CPU=80.0%", result["facts_report"])
                self.assertIn("모니터링 1회, 런북 1회", result["facts_report"])

    # [추가] 실행 시간 초과를 504로 구분하고 진행 중인 MCP 대기를 취소한다.
    async def test_runtime_timeout_is_504_and_cancels_pending_mcp(self):
        generated = await self.generate()
        self.mcp.block_tool = True
        self.app.state.workflow_service = self.make_service(execution_timeout_seconds=0.03)
        response = await self.client.post(
            f"/workflows/{generated['workflow_id']}/execute",
            json={"definition_hash": generated["definition_hash"]},
        )
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()["error"]["code"], "WORKFLOW_TIMEOUT")
        self.assertTrue(self.mcp.cancelled.is_set())

    # [추가] POST 스트림이 진단을 한 번만 실행하고 최종 전체 결과와 완료 신호를 전달한다.
    async def test_stream_executes_once_and_returns_full_terminal_result(self):
        generated = await self.generate()
        response = await self.client.post(
            f"/workflows/{generated['workflow_id']}/execute/stream",
            json={"definition_hash": generated["definition_hash"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        payloads = sse_payloads(response.text)
        self.assertEqual(payloads[-1], "[DONE]")
        self.assertEqual(payloads.count("[DONE]"), 1)
        events = payloads[:-1]
        terminals = [event for event in events if event["type"] in ("FinalAnswer", "WorkflowFailed")]
        self.assertEqual(len(terminals), 1)
        result = terminals[0]["result"]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["report_valid"])
        self.assertTrue(result["final"])
        self.assertEqual(result["definition_hash"], generated["definition_hash"])
        self.assertEqual(terminals[0]["run_id"], result["run_id"])
        self.assertEqual(len([call for call in self.mcp.calls if call[0] == "tool"]), 1)
        self.assertEqual(len([call for call in self.mcp.calls if call[0] == "resource"]), 1)
        self.assertIn("관측", response.text)
        self.assertNotIn("\\uad00", response.text)

    # [추가] 응답 시작 후 실패는 HTTP 200의 실패 이벤트와 전체 실행 결과로 구분한다.
    async def test_stream_runtime_failure_has_terminal_result_and_done(self):
        generated = await self.generate()
        self.mcp.tool_error = RuntimeError("private stream failure detail")
        response = await self.client.post(
            f"/workflows/{generated['workflow_id']}/execute/stream",
            json={"definition_hash": generated["definition_hash"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payloads = sse_payloads(response.text)
        self.assertEqual(payloads[-1], "[DONE]")
        terminal = next(event for event in payloads[:-1] if event["type"] == "WorkflowFailed")
        self.assertEqual(terminal["result"]["status"], "failed")
        self.assertEqual(terminal["result"]["report_attempts"], [])
        self.assertEqual(terminal["result"]["final"], "")
        self.assertNotIn("private stream failure detail", response.text)

    # [추가] 서비스는 유지한 채 하위 그래프의 예기치 않은 오류를 주입해 공개 스트림 경계를 검사한다.
    async def test_unexpected_stream_error_is_redacted_and_generator_is_closed(self):
        generated = await self.generate()
        closed = asyncio.Event()

        # [추가] 정상 실패 결과조차 만들지 못한 오류도 API가 원문 예외 없이 알리도록 한다.
        async def broken_graph(*args, **kwargs):
            try:
                yield {
                    "type": "WorkflowStarted", "run_id": "unexpected-stream-run",
                    "workflow_id": generated["workflow_id"],
                    "definition_hash": generated["definition_hash"],
                }
                raise RuntimeError("private unexpected stream exception")
            finally:
                closed.set()

        with patch("agent.workflow.service.stream_workflow", new=broken_graph):
            response = await self.client.post(
                f"/workflows/{generated['workflow_id']}/execute/stream",
                json={"definition_hash": generated["definition_hash"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payloads = sse_payloads(response.text)
        self.assertEqual(payloads[-1], "[DONE]")
        self.assertEqual(payloads[-2]["type"], "WorkflowFailed")
        self.assertEqual(payloads[-2]["error"]["code"], "WORKFLOW_STREAM_ERROR")
        self.assertEqual(payloads[-2]["run_id"], "unexpected-stream-run")
        self.assertEqual(payloads[-2]["workflow_id"], generated["workflow_id"])
        self.assertEqual(payloads[-2]["definition_hash"], generated["definition_hash"])
        self.assertTrue(closed.is_set())
        self.assertNotIn("private unexpected stream exception", response.text)
        self.assertEqual(self.mcp.calls, [])

    # [추가] 첫 진행 이벤트도 만들지 못한 오류에는 준비된 저장 정의의 식별자를 붙인다.
    async def test_stream_error_before_first_event_keeps_prepared_workflow_metadata(self):
        generated = await self.generate()

        # [추가] 실행 ID 발급 이전의 제너레이터 실패를 재현한다.
        async def broken_graph(*args, **kwargs):
            raise RuntimeError("private failure before first event")
            yield  # pragma: no cover

        with patch("agent.workflow.service.stream_workflow", new=broken_graph):
            response = await self.client.post(
                f"/workflows/{generated['workflow_id']}/execute/stream",
                json={"definition_hash": generated["definition_hash"]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        payloads = sse_payloads(response.text)
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[-1], "[DONE]")
        failure = payloads[0]
        self.assertEqual(failure["type"], "WorkflowFailed")
        self.assertEqual(failure["workflow_id"], generated["workflow_id"])
        self.assertEqual(failure["definition_hash"], generated["definition_hash"])
        self.assertNotIn("run_id", failure)
        self.assertNotIn("private failure before first event", response.text)
        self.assertEqual(self.mcp.calls, [])

    # [추가] 확정 결과 뒤 중복 종료·진행 이벤트와 정리 오류가 와도 종료 결과는 한 번뿐이다.
    async def test_stream_preserves_first_terminal_result_when_cleanup_fails(self):
        generated = await self.generate()
        for failed in (False, True):
            with self.subTest(execution_failed=failed):
                self.mcp.tool_error = RuntimeError("known MCP failure") if failed else None
                closed = asyncio.Event()

                # [추가] 실제 그래프 결과를 전달한 뒤 잘못된 후속 이벤트·정리 실패만 주입한다.
                async def late_failure(*args, **kwargs):
                    try:
                        async for event in actual_graph_stream(*args, **kwargs):
                            yield event
                            if event["type"] in ("FinalAnswer", "WorkflowFailed"):
                                yield {"type": "WorkflowFailed", "error": "duplicate terminal must be hidden"}
                                yield {"type": "StepStarted", "node_id": "late_step_must_be_hidden"}
                        raise RuntimeError("private late cleanup failure")
                    finally:
                        closed.set()

                with patch("agent.workflow.service.stream_workflow", new=late_failure):
                    response = await self.client.post(
                        f"/workflows/{generated['workflow_id']}/execute/stream",
                        json={"definition_hash": generated["definition_hash"]},
                    )
                self.assertEqual(response.status_code, 200, response.text)
                payloads = sse_payloads(response.text)
                self.assertEqual(payloads[-1], "[DONE]")
                terminals = [
                    event for event in payloads[:-1]
                    if event["type"] in ("FinalAnswer", "WorkflowFailed")
                ]
                self.assertEqual(len(terminals), 1)
                self.assertEqual(terminals[0]["type"], "WorkflowFailed" if failed else "FinalAnswer")
                self.assertEqual(terminals[0]["result"]["status"], "failed" if failed else "completed")
                self.assertNotIn("duplicate terminal must be hidden", response.text)
                self.assertNotIn("late_step_must_be_hidden", response.text)
                self.assertNotIn("private late cleanup failure", response.text)
                self.assertTrue(closed.is_set())

    # [추가] 실행에 GET을 허용하지 않아 새로고침·자동 재접속으로 실행이 시작되지 않는다.
    async def test_get_execution_routes_are_405_and_do_not_execute(self):
        generated = await self.generate()
        for suffix in ("execute", "execute/stream"):
            response = await self.client.get(f"/workflows/{generated['workflow_id']}/{suffix}")
            self.assertEqual(response.status_code, 405, response.text)
        self.assertEqual(self.mcp.calls, [])

    # [추가] 서비스가 준비되지 않은 앱은 모든 정상 형태 요청에 구조화된 503을 반환한다.
    async def test_unavailable_service_is_503(self):
        self.app.state.workflow_service = None
        requests = [
            ("GET", "/workflows/catalog", None), ("GET", "/workflows", None),
            ("GET", "/workflows/missing-id", None),
            ("POST", "/workflows/generate", {"query": _QUERY}),
            ("POST", "/workflows/missing-id/save", {"name": "이름"}),
            ("POST", "/workflows/missing-id/execute", {"definition_hash": "a" * 64}),
            ("POST", "/workflows/missing-id/execute/stream", {"definition_hash": "a" * 64}),
        ]
        for method, path, body in requests:
            with self.subTest(path=path):
                response = await self.client.request(method, path, json=body)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "WORKFLOW_UNAVAILABLE")

    # [추가] ASGI 요청 취소가 SSE 제너레이터와 실제 서비스의 진행 중인 조회까지 정리한다.
    async def test_stream_request_cancellation_cleans_up_pending_execution(self):
        generated = await self.generate()
        self.mcp.block_tool = True
        body = json.dumps({"definition_hash": generated["definition_hash"]}).encode()
        path = f"/workflows/{generated['workflow_id']}/execute/stream"
        received_body = False
        waiting = asyncio.Event()
        step_sent = asyncio.Event()
        sent = []

        # [추가] 연결을 유지한 채 요청 취소를 주입해 ASGITransport의 응답 버퍼링을 피한다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await waiting.wait()
            return {"type": "http.disconnect"}

        # [추가] MCP가 대기 중인 시점의 StepStarted 전송을 관찰한다.
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and b"StepStarted" in message.get("body", b""):
                step_sent.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10000), "server": ("workflow.test", 80),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(self.mcp.entered.wait(), timeout=1)
            await asyncio.wait_for(step_sent.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.mcp.cancelled.is_set())
            self.assertFalse(any(b"FinalAnswer" in message.get("body", b"") for message in sent))
            self.assertFalse(any(b"[DONE]" in message.get("body", b"") for message in sent))
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 실제 ASGI disconnect가 요청을 끝내기 전에 조회 취소와 비동기 정리까지 완료한다.
    async def test_asgi_disconnect_waits_for_service_generator_cleanup(self):
        # [추가] 외부 조회 정리에 await가 필요한 경우도 level cancellation에서 보호되는지 검사한다.
        class CleanupMCP(FakeMCP):
            def __init__(self):
                super().__init__()
                self.cleaned = asyncio.Event()

            async def _block(self):
                try:
                    await super()._block()
                finally:
                    await asyncio.sleep(0)
                    self.cleaned.set()

        self.mcp = CleanupMCP()
        self.mcp.block_tool = True
        self.app.state.workflow_service = self.make_service()
        generated = await self.generate()
        body = json.dumps({"definition_hash": generated["definition_hash"]}).encode()
        path = f"/workflows/{generated['workflow_id']}/execute/stream"
        received_body = False
        disconnected = asyncio.Event()
        step_sent = asyncio.Event()
        sent = []

        # [추가] Starlette의 자체 disconnect 감시를 사용하며 직접 Task.cancel로 대체하지 않는다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if b"StepStarted" in message.get("body", b""):
                step_sent.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10001), "server": ("workflow.test", 80),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(self.mcp.entered.wait(), timeout=1)
            await asyncio.wait_for(step_sent.wait(), timeout=1)
            disconnected.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(self.mcp.cancelled.is_set())
            self.assertTrue(self.mcp.cleaned.is_set())
            self.assertFalse(any(b"FinalAnswer" in message.get("body", b"") for message in sent))
            self.assertFalse(any(b"[DONE]" in message.get("body", b"") for message in sent))
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 보고 모델 응답 대기 중 연결이 끊겨도 모델 취소·정리 후 생산자를 종료한다.
    async def test_asgi_disconnect_waits_for_report_cleanup(self):
        # [추가] 실제 모델 HTTP 대기와 유사한 취소·비동기 정리 계약을 가진 모의 보고 모델이다.
        class CleanupReportLLM(QualityLLM):
            def __init__(self):
                super().__init__("disconnect-report-test", 0.1)
                self.entered = asyncio.Event()
                self.cancelled = asyncio.Event()
                self.cleaned = asyncio.Event()

            async def write_workflow(self, query, collected, trace, attempt, feedback, on_token=None):
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
                finally:
                    await asyncio.sleep(0)
                    self.cleaned.set()

        self.quality_llm = CleanupReportLLM()
        self.app.state.workflow_service = self.make_service()
        generated = await self.generate()
        body = json.dumps({"definition_hash": generated["definition_hash"]}).encode()
        path = f"/workflows/{generated['workflow_id']}/execute/stream"
        received_body = False
        disconnected = asyncio.Event()
        report_started = asyncio.Event()
        sent = []

        # [추가] 도구 조회 완료 후 보고 작성이 시작된 시점에 실제 disconnect를 전달한다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if b"ReportStarted" in message.get("body", b""):
                report_started.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10002), "server": ("workflow.test", 80),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(self.quality_llm.entered.wait(), timeout=1)
            await asyncio.wait_for(report_started.wait(), timeout=1)
            disconnected.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(self.quality_llm.cancelled.is_set())
            self.assertTrue(self.quality_llm.cleaned.is_set())
            self.assertFalse(any(b"FinalAnswer" in message.get("body", b"") for message in sent))
            self.assertFalse(any(b"[DONE]" in message.get("body", b"") for message in sent))
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] body_iterator가 yield한 뒤 HTTP send에서 끊겨도 조회·생산자를 남기지 않는다.
    async def test_disconnect_during_http_send_closes_body_iterator_and_producer(self):
        generated = await self.generate()
        self.mcp.block_tool = True
        body = json.dumps({"definition_hash": generated["definition_hash"]}).encode()
        path = f"/workflows/{generated['workflow_id']}/execute/stream"
        received_body = False
        disconnected = asyncio.Event()
        sending = asyncio.Event()

        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        # [추가] 느린 브라우저로 전송이 대기 중인 시점의 연결 종료를 재현한다.
        async def send(message):
            if b"StepStarted" in message.get("body", b""):
                sending.set()
                await asyncio.Event().wait()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10003), "server": ("workflow.test", 80),
        }
        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            await asyncio.wait_for(sending.wait(), timeout=1)
            await asyncio.wait_for(self.mcp.entered.wait(), timeout=1)
            disconnected.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(self.mcp.cancelled.is_set())
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

# [추가] 새 파일 전체: 실제 자원을 열지 않고 서버 수명 관리의 정상·실패 정리를 검증한다.
"""환경 파일·DB·하위 프로세스·네트워크 없이 server.py의 조립과 종료 계약을 검사한다."""

import asyncio
import importlib
import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx


# [추가] 테스트마다 별도 서버 모듈을 사용해 전역 앱과 생성 횟수를 공유하지 않는다.
class WorkflowLifespanTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 동기 모델이 응답하지 않아도 실제 health·카탈로그·목록 API가 먼저 완료되는지 검증한다.
    async def _assert_model_wait_keeps_apis_responsive(self, node_name):
        from agent import graph as legacy_graph

        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        responses = {
            "route": ({"route": "incident", "needs_analysis": False}, {"usd": 0.0}),
            "plan": ({"tasks": [], "needs_analysis": False}, {"usd": 0.0}),
            "analyze": ("관측 자료 비교 결과입니다.", {"usd": 0.0}),
        }

        # [추가] 테스트 실패 때도 스레드를 남기지 않도록 유한한 대기를 사용한다.
        def slow_model(*args):
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=2)
            return responses[node_name]

        setattr(self.fast_llm, node_name, slow_model)
        self.mcp.find_prompt = AsyncMock(return_value="ops")
        self.mcp.get_prompt = AsyncMock(return_value="테스트용 조사 절차")
        self.workflow_store.list_workflows = Mock(return_value=[])
        async with self.server.lifespan(self.app):
            self.app.state.workflow_service.catalog_loader = lambda: self.catalog_module.WorkflowCatalog(
                systems=("web-01",), incident_types=("cpu-spike",),
            )
            state = {"query": "web-01 점검", "events": [], "collected": [],
                     "tasks": [{"agent": "analysis", "status": "pending"}]}
            function = {"route": legacy_graph.route_node, "plan": legacy_graph.plan_node,
                        "analyze": legacy_graph.analysis_node}[node_name]
            task = asyncio.create_task(function(state, self.app.state.deps))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app), base_url="http://test",
                ) as client:
                    results = await asyncio.wait_for(asyncio.gather(
                        client.get("/health"), client.get("/workflows/catalog"), client.get("/workflows"),
                    ), timeout=1)
                self.assertEqual([result.status_code for result in results], [200, 200, 200])
                self.assertFalse(task.done(), "모델 응답을 기다리지 않고 세 API가 먼저 응답해야 합니다.")
            finally:
                release.set()
                await asyncio.wait_for(task, timeout=2)
        self.assert_full_cleanup()

    # [추가] 라우팅·계획·분석 각각에서 같은 요청 정체 문제가 재발하지 않게 한다.
    async def test_slow_route_keeps_health_catalog_and_list_responsive(self):
        await self._assert_model_wait_keeps_apis_responsive("route")

    async def test_slow_plan_keeps_health_catalog_and_list_responsive(self):
        await self._assert_model_wait_keeps_apis_responsive("plan")

    async def test_slow_analysis_keeps_health_catalog_and_list_responsive(self):
        await self._assert_model_wait_keeps_apis_responsive("analyze")

    # [추가] import 시점부터 환경 파일·추적·실제 외부 자원 접근을 차단한다.
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
            "SUPERVISOR_VERSION": "v2",
        }, clear=True))
        self.dotenv_load = self.enterContext(patch("dotenv.load_dotenv", return_value=False))
        self.network = self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("실제 네트워크 접속 금지"),
        ))
        self.database = self.enterContext(patch(
            "sqlite3.connect", side_effect=AssertionError("실제 SQLite 연결 금지"),
        ))
        self.process = self.enterContext(patch(
            "subprocess.Popen", side_effect=AssertionError("실제 하위 프로세스 실행 금지"),
        ))
        self.async_process = self.enterContext(patch(
            "asyncio.create_subprocess_exec", side_effect=AssertionError("실제 비동기 프로세스 실행 금지"),
        ))
        self.events = []
        self.tracer_flush = self.enterContext(patch(
            "langchain_core.tracers.langchain.wait_for_all_tracers",
            side_effect=lambda: self.events.append("tracers.flush"),
        ))

        # [추가] 의존 모듈을 먼저 로딩해 타입 계약을 대역으로 바꾸지 않고 서버 조립만 대체한다.
        mcp_module = importlib.import_module("agent.mcp_client")
        session_module = importlib.import_module("agent.session_store")
        store_module = importlib.import_module("agent.workflow.store")
        graph_module = importlib.import_module("agent.graph")
        self.service_module = importlib.import_module("agent.workflow.service")
        self.catalog_module = importlib.import_module("agent.workflow.catalog")
        fake_module = importlib.import_module("agent.fake_llm")

        self.mcp = SimpleNamespace(
            start=AsyncMock(side_effect=lambda: self.events.append("mcp.start")),
            stop=AsyncMock(side_effect=lambda: self.events.append("mcp.stop")),
        )
        self.session_store = SimpleNamespace(
            close=Mock(side_effect=lambda: self.events.append("session.close")),
        )
        self.workflow_store = SimpleNamespace(
            close=Mock(side_effect=lambda: self.events.append("workflow.close")),
        )
        self.mcp_constructor = self.enterContext(patch.object(mcp_module, "MCPClient", return_value=self.mcp))
        self.session_constructor = self.enterContext(patch.object(
            session_module, "SqliteSessionStore", return_value=self.session_store,
        ))
        self.workflow_constructor = self.enterContext(patch.object(
            store_module, "SqliteWorkflowStore", return_value=self.workflow_store,
        ))
        self.graph = object()
        self.build_graph = self.enterContext(patch.object(graph_module, "build_graph", return_value=self.graph))

        # [추가] 실제 server.py를 별도 이름으로 등록해 import에 포함된 API 등록까지 검사한다.
        module_name = f"_workflow_lifespan_server_{id(self)}"
        path = Path(__file__).resolve().parents[1] / "server.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("테스트용 서버 모듈을 로딩하지 못했습니다.")
        self.server = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.server
        self.addCleanup(sys.modules.pop, module_name, None)
        spec.loader.exec_module(self.server)
        self.app = self.server.app
        self.fast_llm = fake_module.FastLLM("lifespan-fast", 0.0)
        self.quality_llm = fake_module.QualityLLM("lifespan-quality", 0.0)
        # [추가] 조립 대역과 별도로 실제 모델 선택 함수도 외부 호출 없이 검증한다.
        self.original_build_llms = self.server._build_llms
        self.build_llms = self.enterContext(patch.object(
            self.server, "_build_llms", return_value=("fake", self.fast_llm, self.quality_llm),
        ))

    # [추가] 각 경로의 성공·실패와 별개로 실제 외부 자원이 사용되지 않았는지 확인한다.
    def tearDown(self):
        self.dotenv_load.assert_called_once_with()
        self.network.assert_not_called()
        self.database.assert_not_called()
        self.process.assert_not_called()
        self.async_process.assert_not_called()

    # [추가] 초기화 완료 여부에 맞춰 확보된 자원이 역순으로 한 번씩 정리되는지 검사한다.
    def assert_full_cleanup(self):
        self.workflow_store.close.assert_called_once_with()
        self.session_store.close.assert_called_once_with()
        self.mcp.stop.assert_awaited_once_with()
        self.tracer_flush.assert_called_once_with()
        self.assertEqual(self.events[-4:], [
            "workflow.close", "session.close", "mcp.stop", "tracers.flush",
        ])
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 앱과 API 등록만으로 DB·모델·그래프·MCP 초기화가 시작되지 않도록 확인한다.
    def test_module_import_registers_routes_without_initializing_resources(self):
        self.mcp_constructor.assert_not_called()
        self.session_constructor.assert_not_called()
        self.workflow_constructor.assert_not_called()
        self.build_llms.assert_not_called()
        self.build_graph.assert_not_called()
        self.tracer_flush.assert_not_called()
        self.assertEqual(self.server.CREATION_COUNT, 0)
        routes = set(self.app.openapi()["paths"])
        self.assertTrue({"/health", "/chat", "/workflows/generate", "/workflows/{workflow_id}/execute"} <= routes)

    # [추가] 기존 v2 그래프·health를 유지하면서 같은 모델·MCP를 새 서비스가 공유한다.
    async def test_success_reuses_dependencies_and_closes_every_resource_once(self):
        async with self.server.lifespan(self.app):
            service = self.app.state.workflow_service
            self.assertIsInstance(service, self.service_module.WorkflowService)
            self.assertIs(service.store, self.workflow_store)
            self.assertIs(service.mcp, self.mcp)
            self.assertIs(service.fast_llm, self.fast_llm)
            self.assertIs(service.quality_llm, self.quality_llm)
            self.assertIs(self.app.state.deps.mcp, service.mcp)
            self.assertIs(self.app.state.deps.session_store, self.session_store)
            self.assertIs(self.app.state.graph, self.graph)
            self.build_graph.assert_called_once_with(self.app.state.deps)
            self.assertEqual(service.generation_timeout_seconds, 120.0)
            self.assertEqual(service.execution_timeout_seconds, 120.0)
            self.assertEqual(service.mcp_attempts, 3)
            self.assertEqual(service.mcp_retry_delay, 0.3)
            self.mcp.start.assert_awaited_once_with()
            self.mcp.stop.assert_not_awaited()
            self.session_store.close.assert_not_called()
            self.workflow_store.close.assert_not_called()
            health = await self.server.health()
            self.assertEqual(health, {
                "status": "ok", "instance_id": id(self.app.state.deps),
                "creation_count": 1, "supervisor_version": "v2", "llm_backend": "fake",
                "fast_llm": "lifespan-fast", "quality_llm": "lifespan-quality",
                "workflow_llm": "lifespan-fast",
            })
            self.assertEqual(await self.server.version(), {"supervisor_version": "v2"})
            # [추가] 실제 파일 대신 카탈로그 대역을 넣어 조립된 서비스를 호출해 본다.
            service.catalog_loader = lambda: self.catalog_module.WorkflowCatalog(
                systems=("payments-service",), incident_types=("cpu-spike",),
            )
            catalog = await service.get_catalog()
            self.assertEqual(catalog["systems"], ["payments-service"])
            self.assertEqual(catalog["limits"]["max_nodes"], 6)
        self.assert_full_cleanup()
        self.mcp_constructor.assert_called_once_with()
        self.session_constructor.assert_called_once_with()
        self.workflow_constructor.assert_called_once_with()
        self.build_llms.assert_called_once_with()

    # [추가] fake/OpenAI에서는 Ollama 전용 설정이 기존 생성 모델을 교체하지 않는다.
    def test_fake_and_openai_keep_existing_generation_model(self):
        with patch.dict(os.environ, {
            "OLLAMA_WORKFLOW_MODEL": "ollama-only-model",
            "OPENAI_API_KEY": "unit-test-not-a-real-key",
        }):
            for backend, expected_fast, expected_quality in (
                ("fake", "fast-model-stub", "quality-model-stub"),
                ("openai", "openai:gpt-4o-mini", "openai:gpt-4o"),
            ):
                with self.subTest(backend=backend), patch.dict(os.environ, {"LLM_BACKEND": backend}):
                    selected, fast, quality = self.original_build_llms()
                    generator = self.server._build_workflow_llm(selected, fast, quality)
                    self.assertEqual(selected, backend)
                    self.assertEqual(fast.name, expected_fast)
                    self.assertEqual(quality.name, expected_quality)
                    self.assertIs(generator, fast)

    # [추가] 별도 생성 설정이 없으면 일반 진단 fast는 0.6b를 유지하고 생성에는 보고 모델을 쓴다.
    def test_ollama_defaults_separate_generation_from_general_diagnosis(self):
        with patch.dict(os.environ, {"LLM_BACKEND": "ollama"}):
            backend, fast, quality = self.original_build_llms()
            generator = self.server._build_workflow_llm(backend, fast, quality)
        self.assertEqual(fast.name, "ollama:qwen3:0.6b")
        self.assertEqual(quality.name, "ollama:qwen3:8b")
        self.assertEqual(generator.name, "ollama:qwen3:8b")
        self.assertIsNot(generator, fast)
        self.assertIsNot(generator, quality)
        self.assertTrue(callable(generator.compile_workflow))

    # [추가] 명시한 워크플로 생성 모델은 보고 모델·일반 진단 모델 설정보다 우선한다.
    def test_explicit_ollama_workflow_model_has_priority(self):
        with patch.dict(os.environ, {
            "LLM_BACKEND": "ollama", "OLLAMA_FAST_MODEL": "general-fast",
            "OLLAMA_QUALITY_MODEL": "report-quality", "OLLAMA_WORKFLOW_MODEL": "workflow-compiler",
        }):
            backend, fast, quality = self.original_build_llms()
            generator = self.server._build_workflow_llm(backend, fast, quality)
        self.assertEqual((fast.model, quality.model, generator.model), (
            "general-fast", "report-quality", "workflow-compiler",
        ))

    # [추가] DI로 조립한 실제 보고 모델을 따라 환경의 오래된 이름으로 되돌리지 않는다.
    def test_ollama_generation_uses_actual_quality_model_and_ignores_blank_override(self):
        quality = SimpleNamespace(model="injected-quality")
        with patch.dict(os.environ, {"OLLAMA_QUALITY_MODEL": "stale-quality", "OLLAMA_WORKFLOW_MODEL": "  "}):
            generator = self.server._build_workflow_llm("ollama", self.fast_llm, quality)
        self.assertEqual(generator.name, "ollama:injected-quality")

    # [추가] 최소 DI 대역에 모델 속성이 없어도 명시된 quality 설정과 8b 기본값을 적용한다.
    def test_ollama_generation_quality_environment_and_final_default(self):
        for configured, expected in (("configured-quality", "configured-quality"), ("", "qwen3:8b")):
            with self.subTest(configured=configured), patch.dict(os.environ, {"OLLAMA_QUALITY_MODEL": configured}):
                generator = self.server._build_workflow_llm("ollama", self.fast_llm, self.quality_llm)
                self.assertEqual(generator.name, f"ollama:{expected}")

    # [추가] 실제 Ollama 어댑터 조립에서 워크플로 생성·보고·일반 진단과 health의 연결을 확인한다.
    async def test_ollama_lifespan_routes_generation_and_exposes_model_in_health(self):
        with patch.dict(os.environ, {"LLM_BACKEND": "ollama", "OLLAMA_WORKFLOW_MODEL": "workflow-compiler"}):
            backend, fast, quality = self.original_build_llms()
            self.build_llms.return_value = backend, fast, quality
            async with self.server.lifespan(self.app):
                service = self.app.state.workflow_service
                self.assertIs(self.app.state.deps.fast_llm, fast)
                self.assertIs(self.app.state.deps.quality_llm, quality)
                self.assertIs(service.quality_llm, quality)
                self.assertIs(service.fast_llm, self.app.state.workflow_llm)
                self.assertEqual(service.fast_llm.name, "ollama:workflow-compiler")
                self.assertIsNot(service.fast_llm, fast)
                health = await self.server.health()
                self.assertEqual(health["fast_llm"], "ollama:qwen3:0.6b")
                self.assertEqual(health["quality_llm"], "ollama:qwen3:8b")
                self.assertEqual(health["workflow_llm"], "ollama:workflow-compiler")
        self.assert_full_cleanup()

    # [추가] 새 생성 모델 조립이 실패해도 이미 시작한 MCP를 닫고 DB를 생성하지 않는다.
    async def test_workflow_model_failure_stops_mcp_before_opening_databases(self):
        with patch.object(self.server, "_build_workflow_llm", side_effect=RuntimeError("생성 모델 조립 실패")):
            with self.assertRaisesRegex(RuntimeError, "생성 모델 조립 실패"):
                async with self.server.lifespan(self.app):
                    self.fail("모델 조립 실패 뒤에는 서버가 요청을 받으면 안 됩니다.")
        self.mcp.stop.assert_awaited_once_with()
        self.session_constructor.assert_not_called()
        self.workflow_constructor.assert_not_called()
        self.tracer_flush.assert_called_once_with()
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 새 설정과 DB 경로가 서비스에 전달되며 실제 해당 경로는 열지 않는지 확인한다.
    async def test_workflow_environment_settings_are_applied(self):
        with patch.dict(os.environ, {
            "WORKFLOW_DB_PATH": "/not-opened/workflow.db",
            "WORKFLOW_GENERATION_TIMEOUT_SECONDS": "7.5",
            "WORKFLOW_EXECUTION_TIMEOUT_SECONDS": "8.25",
            "WORKFLOW_MCP_ATTEMPTS": "2",
            "WORKFLOW_MCP_RETRY_DELAY": "0.05",
        }):
            async with self.server.lifespan(self.app):
                service = self.app.state.workflow_service
                self.assertEqual(service.generation_timeout_seconds, 7.5)
                self.assertEqual(service.execution_timeout_seconds, 8.25)
                self.assertEqual(service.mcp_attempts, 2)
                self.assertEqual(service.mcp_retry_delay, 0.05)
                self.workflow_constructor.assert_called_once_with("/not-opened/workflow.db")
        self.assert_full_cleanup()

    # [추가] legacy v1에서는 기존 그래프 생성 분기를 건너뛰어도 새 서비스는 사용할 수 있다.
    async def test_v1_skips_graph_build_and_keeps_health_and_workflow_service(self):
        with patch.dict(os.environ, {"SUPERVISOR_VERSION": "v1"}):
            async with self.server.lifespan(self.app):
                self.assertIsNone(self.app.state.graph)
                self.build_graph.assert_not_called()
                self.assertEqual(self.app.state.deps.version, "v1")
                self.assertIs(self.app.state.workflow_service.mcp, self.mcp)
                self.assertEqual((await self.server.health())["supervisor_version"], "v1")
                self.assertEqual(await self.server.version(), {"supervisor_version": "v1"})
        self.assert_full_cleanup()

    # [추가] MCP 시작 자체가 실패해도 부분 시작 자원을 정리하고 DB 생성은 시도하지 않는다.
    async def test_mcp_start_failure_still_stops_mcp_without_opening_databases(self):
        self.mcp.start.side_effect = RuntimeError("MCP 시작 실패")
        with self.assertRaisesRegex(RuntimeError, "MCP 시작 실패"):
            async with self.server.lifespan(self.app):
                self.fail("초기화 실패 뒤에는 서버가 요청을 받으면 안 됩니다.")
        self.mcp.start.assert_awaited_once_with()
        self.mcp.stop.assert_awaited_once_with()
        self.build_llms.assert_not_called()
        self.session_constructor.assert_not_called()
        self.workflow_constructor.assert_not_called()
        self.session_store.close.assert_not_called()
        self.workflow_store.close.assert_not_called()
        self.tracer_flush.assert_called_once_with()
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 대화 DB 생성에 실패하면 이미 시작한 MCP만 정리한다.
    async def test_session_database_failure_cleans_mcp(self):
        self.session_constructor.side_effect = RuntimeError("대화 DB 초기화 실패")
        with self.assertRaisesRegex(RuntimeError, "대화 DB 초기화 실패"):
            async with self.server.lifespan(self.app):
                self.fail("초기화 실패 뒤에는 서버가 요청을 받으면 안 됩니다.")
        self.mcp.stop.assert_awaited_once_with()
        self.workflow_constructor.assert_not_called()
        self.session_store.close.assert_not_called()
        self.workflow_store.close.assert_not_called()
        self.tracer_flush.assert_called_once_with()
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 기존 그래프 생성 실패 시에도 대화 DB와 MCP를 되돌리고 새 DB는 열지 않는다.
    async def test_graph_build_failure_closes_session_and_mcp(self):
        self.build_graph.side_effect = RuntimeError("그래프 생성 실패")
        with self.assertRaisesRegex(RuntimeError, "그래프 생성 실패"):
            async with self.server.lifespan(self.app):
                self.fail("초기화 실패 뒤에는 서버가 요청을 받으면 안 됩니다.")
        self.session_store.close.assert_called_once_with()
        self.mcp.stop.assert_awaited_once_with()
        self.workflow_constructor.assert_not_called()
        self.workflow_store.close.assert_not_called()
        self.assertEqual(self.events[-3:], ["session.close", "mcp.stop", "tracers.flush"])
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 새 DB 생성에 실패하면 먼저 확보한 대화 DB와 MCP를 역순으로 정리한다.
    async def test_workflow_database_failure_closes_session_and_mcp(self):
        self.workflow_constructor.side_effect = RuntimeError("워크플로 DB 초기화 실패")
        with self.assertRaisesRegex(RuntimeError, "워크플로 DB 초기화 실패"):
            async with self.server.lifespan(self.app):
                self.fail("초기화 실패 뒤에는 서버가 요청을 받으면 안 됩니다.")
        self.session_store.close.assert_called_once_with()
        self.mcp.stop.assert_awaited_once_with()
        self.workflow_store.close.assert_not_called()
        self.tracer_flush.assert_called_once_with()
        self.assertEqual(self.events[-3:], ["session.close", "mcp.stop", "tracers.flush"])
        self.assertIsNone(self.app.state.workflow_service)

    # [추가] 설정값으로 실제 서비스 생성이 거부되면 새 DB까지 포함해 모두 정리한다.
    async def test_invalid_service_setting_closes_all_initialized_resources(self):
        with patch.dict(os.environ, {"WORKFLOW_MCP_ATTEMPTS": "0"}):
            with self.assertRaisesRegex(ValueError, "시도 횟수"):
                async with self.server.lifespan(self.app):
                    self.fail("유효하지 않은 설정으로 서비스가 시작되면 안 됩니다.")
        self.assert_full_cleanup()

    # [추가] 시작 완료 후 요청 처리 구간의 예외도 정상 종료와 같은 정리 경로를 따른다.
    async def test_exception_after_startup_clears_service_and_closes_resources(self):
        with self.assertRaisesRegex(RuntimeError, "요청 처리 구간 실패"):
            async with self.server.lifespan(self.app):
                self.assertIsNotNone(self.app.state.workflow_service)
                raise RuntimeError("요청 처리 구간 실패")
        self.assert_full_cleanup()


# [추가] 표준 unittest 명령과 파일 직접 실행으로 회귀 검증할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

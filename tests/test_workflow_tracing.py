# [추가] 외부 추적 전송 없이 workflow.run의 식별자 메타데이터 회귀를 검사하는 신규 파일 전체.
"""공개 실행 결과·진행 이벤트·루트 추적의 실행 식별자가 일치하는지 확인한다."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.graph import run_workflow
from tests.test_workflow_execution import FakeMCP, conditional_definition
from tests.test_workflow_graph import FakeReportLLM


# [추가] SDK 추적은 비활성화하고 현재 추적 객체만 대역으로 주입한다.
class WorkflowTracingTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 어떤 추적 경로도 실수로 네트워크를 사용하지 못하게 전송 진입점을 차단한다.
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
        }))
        self.requests = self.enterContext(patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("실제 LangSmith 전송 금지"),
        ))
        self.sync_http = self.enterContext(patch(
            "httpx.Client.send", side_effect=AssertionError("실제 HTTP 전송 금지"),
        ))
        self.async_http = self.enterContext(patch(
            "httpx.AsyncClient.send", side_effect=AssertionError("실제 비동기 HTTP 전송 금지"),
        ))
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )

    # [추가] 같은 root에는 공개 ID·hash를 붙이고 다른 이름의 상위 추적은 변경하지 않는다.
    async def test_root_metadata_matches_result_without_mutating_unrelated_parent(self):
        for name in ("workflow.run", "outer.request", None):
            with self.subTest(current_trace=name):
                current = SimpleNamespace(name=name, metadata={"existing": "preserved"}) if name else None
                with patch("agent.workflow.graph.get_current_run_tree", return_value=current):
                    result = await run_workflow(
                        conditional_definition(), self.catalog, FakeMCP(), FakeReportLLM(),
                        query="결제 시스템 조건부 점검", workflow_id="saved-workflow-1",
                    )
                self.assertEqual(result.status, "completed")
                self.assertTrue(result.run_id)
                self.assertTrue(result.definition_hash)
                metadata = {
                    "run_id": result.run_id,
                    "workflow_id": "saved-workflow-1",
                    "definition_hash": result.definition_hash,
                }
                if name == "workflow.run":
                    self.assertEqual(current.metadata, {"existing": "preserved", **metadata})
                elif current is not None:
                    self.assertEqual(current.metadata, {"existing": "preserved"})
                for event in result.events:
                    self.assertEqual({key: event[key] for key in metadata}, metadata)
        self.requests.assert_not_called()
        self.sync_http.assert_not_called()
        self.async_http.assert_not_called()

# [추가] 새 파일 전체: 조건부 실행기의 실제 분기·도구 호출·실패·취소 계약을 외부 API 없이 검증한다.
"""주입한 비동기 MCP 대역으로 워크플로 실행과 추적 결과를 재현한다."""

import asyncio
import json
import math
import unittest
from copy import deepcopy

from pydantic import ValidationError

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.executor import execute_workflow
from agent.workflow.models import (
    MonitoringSnapshot,
    WorkflowActionResult,
    WorkflowDefinition,
    WorkflowExecutionIssue,
    WorkflowExecutionResult,
    WorkflowValidationIssue,
)


# [추가] 실제 모니터링 도구와 동일한 필드를 가진 고정 스냅샷을 만든다.
def monitoring_snapshot(system="payments-service", **changes):
    return {
        "system_name": system,
        "status": "degraded",
        "cpu_percent": 80.0,
        "memory_percent": 47.5,
        "last_checked": "2026-10-20T09:00:00+09:00",
        "alerts": ["CPU 사용량 임계치 초과"],
        **changes,
    }


# [추가] 참·거짓 런북 조회를 갖는 절차를 각 테스트에 독립적으로 제공한다.
def conditional_definition():
    return {
        "schema_version": 1,
        "name": "결제 시스템 조건부 점검",
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


# [추가] 호출 순서와 인자를 기록하고 응답·예외·대기를 제어하는 비동기 MCP 대역이다.
class FakeMCP:
    # [추가] 실제 네트워크 없이 정상 응답과 테스트별 장애 조건을 준비한다.
    def __init__(self):
        self.calls = []
        self.snapshots = {
            "payments-service": monitoring_snapshot(),
            "auth-service": monitoring_snapshot("auth-service", cpu_percent=20.0),
            "web-01": monitoring_snapshot("web-01", cpu_percent=10.0),
        }
        self.runbooks = {
            "cpu-spike": "# CPU Spike\n\n1. CPU 사용량을 확인하세요.",
            "memory-leak": "# Memory Leak\n\n1. 메모리 사용량을 확인하세요.",
            "db-down": "# DB Down\n\n1. 연결 상태를 확인하세요.",
        }
        self.tool_error = None
        self.template_error = None
        self.resource_error = None
        self.template = ("ops", "runbook://incident-response/{incident_type}")
        self.block_tool = False
        self.block_resource = False
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    # [추가] 타임아웃과 취소를 긴 sleep 대신 완료되지 않는 이벤트 대기로 재현한다.
    async def _block(self):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    # [추가] 모니터링 요청 인자를 기록한 뒤 시스템별 고정 응답을 독립 복사해 반환한다.
    async def call_tool(self, name, arguments, server="ops"):
        self.calls.append(("tool", name, deepcopy(arguments), server))
        if self.block_tool:
            await self._block()
        if self.tool_error is not None:
            raise self.tool_error
        await asyncio.sleep(0)
        return deepcopy(self.snapshots[arguments["system_name"]])

    # [추가] 런북 템플릿의 탐색 횟수와 잘못된 탐색 결과 처리를 관찰할 수 있게 한다.
    async def find_resource_template(self, name):
        self.calls.append(("template", name))
        if self.template_error is not None:
            raise self.template_error
        await asyncio.sleep(0)
        return self.template

    # [추가] 조회한 URI·서버를 기록하고 문서 오류 또는 취소 상황을 주입한다.
    async def read_resource(self, uri, server="ops"):
        self.calls.append(("resource", uri, server))
        if self.block_resource:
            await self._block()
        if self.resource_error is not None:
            raise self.resource_error
        await asyncio.sleep(0)
        return self.runbooks[uri.rsplit("/", 1)[-1]]


# [추가] 각 테스트가 별도 이벤트 루프를 사용해 실행 상태와 취소를 격리한다.
class WorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 실제 데이터 파일 대신 고정 카탈로그와 독립 MCP 대역을 주입한다.
    def setUp(self):
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )
        self.mcp = FakeMCP()

    # [추가] 이벤트 번호와 시간 측정값이 API에 표시 가능한 일관된 값인지 확인한다.
    def assert_trace_contract(self, result):
        self.assertEqual([event.sequence for event in result.trace], list(range(1, len(result.trace) + 1)))
        self.assertTrue(math.isfinite(result.elapsed_ms))
        self.assertGreaterEqual(result.elapsed_ms, 0)
        for event in result.trace:
            self.assertTrue(math.isfinite(event.elapsed_ms))
            self.assertGreaterEqual(event.elapsed_ms, 0)
            self.assertIn(event.node_type, ("monitoring", "runbook", "if"))
            self.assertTrue(event.path.startswith("steps["))

    # [추가] 실패 코드·노드 위치·실패 이벤트가 함께 남아 원인을 화면에 표시할 수 있는지 확인한다.
    def assert_failure(self, result, code, node_id=None, path=None):
        self.assertEqual(result.status, "failed")
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.code, code)
        self.assertTrue(result.error.message)
        if node_id is not None:
            self.assertEqual(result.error.node_id, node_id)
            failures = [event for event in result.trace if event.status == "failed" and event.node_id == node_id]
            self.assertTrue(failures)
            self.assertEqual(failures[-1].error.code, code)
        if path is not None:
            self.assertEqual(result.error.path, path)
        self.assert_trace_contract(result)

    # [추가] 조건이 참일 때 참 분기의 런북만 조회하고 판단값을 기록하는지 확인한다.
    async def test_true_branch_executes_only_matching_runbook(self):
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.error)
        self.assertEqual(result.validation_errors, [])
        self.assertEqual([item.node_id for item in result.results], ["metrics_1", "cpu_book"])
        self.assertEqual(self.mcp.calls, [
            ("tool", "get_monitoring_status", {"system_name": "payments-service"}, "ops"),
            ("template", "get_incident_runbook"),
            ("resource", "runbook://incident-response/cpu-spike", "ops"),
        ])
        branch = next(event for event in result.trace if event.node_id == "branch_1" and event.status == "completed")
        self.assertEqual(branch.condition.ref, "metrics_1")
        self.assertEqual(branch.condition.field, "cpu_percent")
        self.assertEqual(branch.condition.op, ">=")
        self.assertEqual(branch.condition.expected, 80)
        self.assertEqual(branch.condition.actual, 80)
        self.assertIs(branch.condition.result, True)
        self.assertEqual(branch.condition.selected_branch, "then_steps")
        skipped = [event for event in result.trace if event.status == "skipped"]
        self.assertEqual([(event.node_id, event.path) for event in skipped], [("memory_book", "steps[1].else_steps[0]")])
        self.assert_trace_contract(result)

    # [추가] 조건이 거짓이면 반대 분기만 실행하고 참 분기는 건너뛰는지 확인한다.
    async def test_false_branch_executes_only_else_actions(self):
        self.mcp.snapshots["payments-service"]["cpu_percent"] = 79.9
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertEqual([item.node_id for item in result.results], ["metrics_1", "memory_book"])
        branch = next(event for event in result.trace if event.node_id == "branch_1" and event.status == "completed")
        self.assertIs(branch.condition.result, False)
        self.assertEqual(branch.condition.selected_branch, "else_steps")
        self.assertEqual([event.node_id for event in result.trace if event.status == "skipped"], ["cpu_book"])
        self.assertNotIn(("resource", "runbook://incident-response/cpu-spike", "ops"), self.mcp.calls)

    # [추가] 모든 숫자 비교 연산자가 임계값의 아래·동일·위에서 정확한 분기를 선택하는지 확인한다.
    async def test_all_numeric_operators_and_threshold_boundaries(self):
        expected = {
            ">": (False, False, True), ">=": (False, True, True),
            "<": (True, False, False), "<=": (True, True, False),
            "==": (False, True, False), "!=": (True, False, True),
        }
        for operator, outcomes in expected.items():
            for actual, wanted in zip((79.9, 80.0, 80.1), outcomes):
                with self.subTest(operator=operator, actual=actual):
                    payload = conditional_definition()
                    payload["steps"][1]["condition"]["op"] = operator
                    mcp = FakeMCP()
                    mcp.snapshots["payments-service"]["cpu_percent"] = actual
                    result = await execute_workflow(payload, self.catalog, mcp)
                    self.assertEqual(result.status, "completed")
                    condition = next(event.condition for event in result.trace if event.condition is not None)
                    self.assertEqual(condition.actual, actual)
                    self.assertIs(condition.result, wanted)
                    self.assertEqual(result.results[-1].node_id, "cpu_book" if wanted else "memory_book")

    # [추가] 메모리 조건은 CPU가 아닌 지정한 필드의 소수 값을 사용해야 한다.
    async def test_memory_condition_reads_requested_field(self):
        payload = conditional_definition()
        payload["steps"][1]["condition"].update(field="memory_percent", op="==", value=47.5)
        result = await execute_workflow(payload, self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        condition = next(event.condition for event in result.trace if event.condition is not None)
        self.assertEqual(condition.actual, 47.5)
        self.assertIs(condition.result, True)

    # [추가] 상태 비교는 문자열 동등·비동등 연산만 사용하고 결과를 그대로 기록한다.
    async def test_status_equality_and_inequality(self):
        for operator, wanted in (("==", True), ("!=", False)):
            with self.subTest(operator=operator):
                payload = conditional_definition()
                payload["steps"][1]["condition"] = {
                    "ref": "metrics_1", "field": "status", "op": operator, "value": "degraded"
                }
                result = await execute_workflow(payload, self.catalog, FakeMCP())
                self.assertEqual(result.status, "completed")
                condition = next(event.condition for event in result.trace if event.condition is not None)
                self.assertEqual(condition.actual, "degraded")
                self.assertIs(condition.result, wanted)

    # [추가] 선택된 분기가 비어 있으면 다른 분기를 대신 실행하지 않고 정상 종료한다.
    async def test_selected_empty_branch_completes_without_fallback(self):
        for branch, cpu in (("then_steps", 80.0), ("else_steps", 79.9)):
            with self.subTest(branch=branch):
                payload = conditional_definition()
                payload["steps"][1][branch] = []
                mcp = FakeMCP()
                mcp.snapshots["payments-service"]["cpu_percent"] = cpu
                result = await execute_workflow(payload, self.catalog, mcp)
                self.assertEqual(result.status, "completed")
                self.assertEqual([item.node_id for item in result.results], ["metrics_1"])
                self.assertEqual([call[0] for call in mcp.calls], ["tool"])

    # [추가] 조건 없는 순차 실행에서도 작성된 순서대로 호출·결과·이벤트를 남긴다.
    async def test_sequential_actions_preserve_order(self):
        payload = conditional_definition()
        payload["steps"] = [
            {"id": "first_book", "type": "runbook", "incident_type": "db-down"},
            payload["steps"][0],
            {"id": "auth_metric", "type": "monitoring", "system": "auth-service"},
        ]
        result = await execute_workflow(payload, self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertEqual([item.node_id for item in result.results], ["first_book", "metrics_1", "auth_metric"])
        self.assertEqual([event.node_id for event in result.trace if event.status == "completed"], ["first_book", "metrics_1", "auth_metric"])
        self.assertEqual([call[0] for call in self.mcp.calls], ["template", "resource", "tool", "tool"])
        self.assert_trace_contract(result)

    # [추가] 같은 모니터링 결과를 두 조건이 참조해도 불필요하게 다시 조회하지 않는다.
    async def test_reused_monitoring_reference_does_not_repeat_tool_call(self):
        payload = conditional_definition()
        second = deepcopy(payload["steps"][1])
        second["id"] = "branch_2"
        second["then_steps"][0].update(id="db_book", incident_type="db-down")
        second["else_steps"] = []
        payload["steps"].append(second)
        result = await execute_workflow(payload, self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertEqual([call[0] for call in self.mcp.calls].count("tool"), 1)
        self.assertEqual([item.node_id for item in result.results], ["metrics_1", "cpu_book", "db_book"])

    # [추가] 한 실행 안의 런북 조회는 템플릿을 한 번만 찾고 새 실행에서는 다시 찾는다.
    async def test_runbook_template_cache_is_scoped_to_one_execution(self):
        payload = conditional_definition()
        payload["steps"] = [
            {"id": "cpu_book", "type": "runbook", "incident_type": "cpu-spike"},
            {"id": "memory_book", "type": "runbook", "incident_type": "memory-leak"},
        ]
        first = await execute_workflow(payload, self.catalog, self.mcp)
        second = await execute_workflow(payload, self.catalog, self.mcp)
        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual([call[0] for call in self.mcp.calls].count("template"), 2)
        self.assertEqual([call[0] for call in self.mcp.calls].count("resource"), 4)

    # [추가] 미선택 분기의 런북을 실행하지 않을 때는 템플릿 탐색 자체도 하지 않는다.
    async def test_unselected_runbook_branch_never_discovers_template(self):
        payload = conditional_definition()
        payload["steps"][1]["then_steps"] = [{
            "id": "auth_metric", "type": "monitoring", "system": "auth-service"
        }]
        result = await execute_workflow(payload, self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertEqual([call[0] for call in self.mcp.calls], ["tool", "tool"])
        self.assertEqual([item.node_id for item in result.results], ["metrics_1", "auth_metric"])

    # [추가] 검증 실패 시 단 한 번의 MCP 호출도 없이 오류와 빈 실행 추적을 반환한다.
    async def test_validation_failure_blocks_all_mcp_calls(self):
        for change in ("missing_system", "future_reference", "malformed_json"):
            with self.subTest(change=change):
                payload = conditional_definition()
                if change == "missing_system":
                    payload["steps"][0]["system"] = "unknown-service"
                elif change == "future_reference":
                    payload["steps"][1]["condition"]["ref"] = "missing"
                else:
                    payload = "{bad"
                mcp = FakeMCP()
                result = await execute_workflow(payload, self.catalog, mcp)
                self.assert_failure(result, "VALIDATION_FAILED")
                self.assertTrue(result.validation_errors)
                self.assertEqual(result.trace, [])
                self.assertEqual(result.results, [])
                self.assertEqual(mcp.calls, [])

    # [추가] JSON·모델 입력도 동일한 검증과 실행 경로를 통과하는지 확인한다.
    async def test_json_and_model_inputs_execute(self):
        payload = conditional_definition()
        for candidate in (json.dumps(payload), WorkflowDefinition.model_validate(payload)):
            with self.subTest(input_type=type(candidate).__name__):
                result = await execute_workflow(candidate, self.catalog, FakeMCP())
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.results[-1].node_id, "cpu_book")

    # [추가] 모니터링 예외를 조건의 거짓으로 해석하지 않고 실행을 즉시 중단한다.
    async def test_monitoring_mcp_error_stops_before_condition(self):
        self.mcp.tool_error = RuntimeError("monitoring unavailable")
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assert_failure(result, "MCP_ERROR", "metrics_1", "steps[0]")
        self.assertEqual(result.results, [])
        self.assertEqual([call[0] for call in self.mcp.calls], ["tool"])
        self.assertFalse(any(event.node_id == "branch_1" for event in result.trace))

    # [추가] 필수 모니터링 필드가 없으면 정상 수치나 빈 문자열로 보정하지 않는다.
    async def test_missing_monitoring_fields_are_rejected(self):
        for field in ("system_name", "status", "cpu_percent", "memory_percent", "last_checked"):
            with self.subTest(field=field):
                mcp = FakeMCP()
                del mcp.snapshots["payments-service"][field]
                result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                self.assert_failure(result, "INVALID_MONITORING_RESULT", "metrics_1", "steps[0]")
                self.assertEqual([call[0] for call in mcp.calls], ["tool"])

    # [추가] 문자열 숫자·bool·NaN·Infinity는 유효한 관측 수치로 사용하지 않는다.
    async def test_invalid_monitoring_numeric_values_are_rejected(self):
        for field in ("cpu_percent", "memory_percent"):
            for value in ("80", True, None, float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    mcp = FakeMCP()
                    mcp.snapshots["payments-service"][field] = value
                    result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                    self.assert_failure(result, "INVALID_MONITORING_RESULT", "metrics_1")
                    self.assertEqual(result.results, [])

    # [추가] unknown 상태·다른 시스템 응답·비객체 응답을 진단 근거로 채택하지 않는다.
    async def test_unknown_status_wrong_system_and_non_object_are_rejected(self):
        variants = (
            monitoring_snapshot(status="unknown"),
            monitoring_snapshot("auth-service"),
            [], None, "monitoring result",
        )
        for snapshot in variants:
            with self.subTest(snapshot=snapshot):
                mcp = FakeMCP()
                mcp.snapshots["payments-service"] = snapshot
                result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                self.assert_failure(result, "INVALID_MONITORING_RESULT", "metrics_1")
                self.assertEqual(result.results, [])

    # [추가] 선택 사항인 alerts만 생략되면 빈 목록으로 보관해 기존 응답과 호환한다.
    async def test_missing_alerts_defaults_to_empty_list(self):
        del self.mcp.snapshots["payments-service"]["alerts"]
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.results[0].data.alerts, [])

    # [추가] 알림·조회 시각도 잘못된 자료형이나 빈 필수값을 정상 응답으로 보정하지 않는다.
    async def test_invalid_alerts_and_checked_time_are_rejected(self):
        changes = (
            {"alerts": "warning"}, {"alerts": [1]}, {"alerts": None},
            {"last_checked": ""}, {"last_checked": None}, {"last_checked": 123},
        )
        for change in changes:
            with self.subTest(change=change):
                mcp = FakeMCP()
                mcp.snapshots["payments-service"].update(change)
                result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                self.assert_failure(result, "INVALID_MONITORING_RESULT", "metrics_1")

    # [추가] 조건 평가는 완료됐더라도 선택된 자식 조회가 실패하면 전체 실행은 실패한다.
    async def test_child_failure_preserves_monitoring_and_completed_condition(self):
        self.mcp.resource_error = RuntimeError("runbook unavailable")
        payload = conditional_definition()
        payload["steps"].append({"id": "later_metric", "type": "monitoring", "system": "web-01"})
        result = await execute_workflow(payload, self.catalog, self.mcp)
        self.assert_failure(result, "MCP_ERROR", "cpu_book", "steps[1].then_steps[0]")
        self.assertEqual([item.node_id for item in result.results], ["metrics_1"])
        branch = next(event for event in result.trace if event.node_id == "branch_1" and event.status == "completed")
        child = next(event for event in result.trace if event.node_id == "cpu_book" and event.status == "started")
        self.assertLess(branch.sequence, child.sequence)
        self.assertTrue(branch.condition.result)
        self.assertFalse(any(event.node_id == "later_metric" for event in result.trace))
        self.assertFalse(any(call[0] == "tool" and call[2]["system_name"] == "web-01" for call in self.mcp.calls))

    # [추가] 런북 템플릿을 찾지 못한 경우 기존 모니터링만 보존하고 오류로 종료한다.
    async def test_template_lookup_error_is_mcp_error(self):
        self.mcp.template_error = LookupError("resource missing")
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assert_failure(result, "MCP_ERROR", "cpu_book")
        self.assertEqual([item.node_id for item in result.results], ["metrics_1"])
        self.assertEqual([call[0] for call in self.mcp.calls], ["tool", "template"])

    # [추가] 잘못된 MCP 템플릿 반환값이나 허용되지 않은 URI는 읽기 요청 전에 거부한다.
    async def test_invalid_template_response_is_rejected_before_read(self):
        for template in (None, (), ("ops",), ("ops", "file:///{incident_type}"), ("ops", "runbook://incident-response/no-placeholder")):
            with self.subTest(template=template):
                mcp = FakeMCP()
                mcp.template = template
                result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                self.assert_failure(result, "MCP_ERROR", "cpu_book")
                self.assertFalse(any(call[0] == "resource" for call in mcp.calls))

    # [추가] 비문자·빈 문서는 정상 런북으로 기록하지 않는다.
    async def test_empty_and_non_string_runbooks_are_rejected(self):
        for document in ("", " \n\t ", None, {}, ["instructions"], 123):
            with self.subTest(document=document):
                mcp = FakeMCP()
                mcp.runbooks["cpu-spike"] = document
                result = await execute_workflow(conditional_definition(), self.catalog, mcp)
                self.assert_failure(result, "INVALID_RUNBOOK_RESULT", "cpu_book")
                self.assertEqual([item.node_id for item in result.results], ["metrics_1"])

    # [추가] 기존 MCP가 반환하는 문서 없음 표제는 별도 실패 코드로 구분한다.
    async def test_runbook_not_found_marker_is_failure(self):
        self.mcp.runbooks["cpu-spike"] = "# Runbook Not Found\n\nNo runbook for incident type: cpu-spike"
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assert_failure(result, "RUNBOOK_NOT_FOUND", "cpu_book")
        self.assertEqual([item.source for item in result.results], ["monitoring"])

    # [추가] 일반 문서 본문의 문자열 언급은 문서 없음 표제로 오인하지 않는다.
    async def test_marker_mentioned_in_normal_document_is_not_failure(self):
        self.mcp.runbooks["cpu-spike"] = "# CPU Spike\n\nRunbook Not Found 메시지가 발생하면 경로를 확인하세요."
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.assertEqual(result.status, "completed")

    # [추가] 총 실행 제한을 넘으면 진행 중 MCP 요청을 취소하고 완료된 결과를 보존한다.
    async def test_execution_timeout_cancels_active_call_and_keeps_results(self):
        self.mcp.block_resource = True
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp, timeout_seconds=0.02)
        self.assert_failure(result, "EXECUTION_TIMEOUT", "cpu_book", "steps[1].then_steps[0]")
        self.assertTrue(self.mcp.cancelled.is_set())
        self.assertEqual([item.node_id for item in result.results], ["metrics_1"])

    # [추가] 시간 제한 설정은 양수 유한 숫자만 허용하고 잘못된 설정은 호출 전에 알린다.
    async def test_invalid_timeout_configuration_raises_value_error(self):
        for timeout in (0, -1, True, "1", None, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                mcp = FakeMCP()
                with self.assertRaises(ValueError):
                    await execute_workflow(conditional_definition(), self.catalog, mcp, timeout_seconds=timeout)
                self.assertEqual(mcp.calls, [])

    # [추가] 외부 작업 취소는 일반 MCP 실패로 삼키지 않고 호출자에게 그대로 전파한다.
    async def test_external_cancellation_propagates_cancelled_error(self):
        self.mcp.block_tool = True
        task = asyncio.create_task(execute_workflow(conditional_definition(), self.catalog, self.mcp))
        try:
            await asyncio.wait_for(self.mcp.entered.wait(), timeout=1.0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.mcp.cancelled.is_set())
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 기존 보고서 작성기가 사용하는 source/data 딕셔너리 형태로 변환되는지 확인한다.
    async def test_to_collected_preserves_existing_report_input_contract(self):
        result = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        collected = result.to_collected()
        self.assertEqual(collected, [
            {"source": "monitoring", "data": monitoring_snapshot()},
            {"source": "runbook", "data": self.mcp.runbooks["cpu-spike"]},
        ])
        collected[0]["data"]["alerts"].clear()
        self.assertTrue(result.results[0].data.alerts)

    # [추가] 결과·조건·오류·추적 이벤트를 JSON으로 직렬화해 API에 반환할 수 있는지 확인한다.
    async def test_completed_and_failed_results_are_json_serializable(self):
        completed = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        self.mcp.tool_error = RuntimeError("unavailable")
        failed = await execute_workflow(conditional_definition(), self.catalog, self.mcp)
        for result in (completed, failed):
            with self.subTest(status=result.status):
                serialized = json.loads(result.model_dump_json())
                self.assertEqual(serialized["status"], result.status)
                self.assertEqual(len(serialized["trace"]), len(result.trace))
                self.assertEqual(len(serialized["results"]), len(result.results))
                if result.error is not None:
                    self.assertEqual(serialized["error"]["code"], result.error.code)

    # [추가] 결과 자료의 source와 실제 데이터 형태가 어긋나거나 빈 런북이면 거부한다.
    async def test_action_result_model_rejects_mismatched_source_and_data(self):
        snapshot = MonitoringSnapshot.model_validate(monitoring_snapshot())
        for source, data in (("monitoring", "raw text"), ("runbook", snapshot), ("runbook", " \n ")):
            with self.subTest(source=source, data=data), self.assertRaises(ValidationError):
                WorkflowActionResult(node_id="metrics_1", source=source, data=data)

    # [추가] 성공·실패·검증 오류가 모순된 실행 결과를 만들어 외부에 전달하지 못하게 한다.
    async def test_execution_result_model_rejects_inconsistent_outcomes(self):
        error = WorkflowExecutionIssue(code="MCP_ERROR", path="steps[0]", node_id="metrics_1", message="조회 실패")
        issue = WorkflowValidationIssue(code="INVALID_SCHEMA", path="$", message="형식 오류")
        inconsistent = (
            {"status": "completed", "error": error},
            {"status": "completed", "validation_errors": [issue]},
            {"status": "failed"},
            {"status": "failed", "error": error, "validation_errors": [issue]},
        )
        for payload in inconsistent:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                WorkflowExecutionResult(elapsed_ms=0.0, **payload)

    # [추가] 두 실행이 같은 MCP를 사용해 교차 진행되어도 결과·조건·이벤트 번호가 섞이지 않는다.
    async def test_concurrent_executions_keep_state_and_trace_isolated(self):
        first = conditional_definition()
        second = conditional_definition()
        second["steps"][0]["system"] = "auth-service"
        original = deepcopy(first)
        first_result, second_result = await asyncio.gather(
            execute_workflow(first, self.catalog, self.mcp),
            execute_workflow(second, self.catalog, self.mcp),
        )
        self.assertEqual(first_result.status, "completed")
        self.assertEqual(second_result.status, "completed")
        self.assertEqual(first_result.results[-1].node_id, "cpu_book")
        self.assertEqual(second_result.results[-1].node_id, "memory_book")
        self.assertEqual(first_result.results[0].data.system_name, "payments-service")
        self.assertEqual(second_result.results[0].data.system_name, "auth-service")
        self.assertEqual(first, original)
        self.assertIsNot(first_result.trace, second_result.trace)
        self.assert_trace_contract(first_result)
        self.assert_trace_contract(second_result)


# [추가] 파일을 직접 실행할 때도 비동기 unittest 테스트를 동일하게 실행한다.
if __name__ == "__main__":
    unittest.main()

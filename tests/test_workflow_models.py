# [추가] 워크플로 데이터 모델의 정상 입력·경계값·거부 조건을 검증하는 신규 테스트 파일.
"""입력 경계와 조건부 AST의 구조 계약을 검증한다."""
import json
import unittest
from copy import deepcopy
from decimal import Decimal

from pydantic import ValidationError

from agent.workflow.models import (
    IfStep,
    MonitoringStep,
    NumericCondition,
    RunbookStep,
    StatusCondition,
    WorkflowDefinition,
)


# [추가] 실제 기획서와 같은 조건부 절차를 테스트마다 독립적으로 생성한다.
def example_definition():
    return {
        "schema_version": 1,
        "name": "결제 시스템 조건부 점검",
        "steps": [
            {
                "id": "metrics_1",
                "type": "monitoring",
                "system": "payments-service",
            },
            {
                "id": "branch_1",
                "type": "if",
                "condition": {
                    "ref": "metrics_1",
                    "field": "cpu_percent",
                    "op": ">=",
                    "value": 80,
                },
                "then_steps": [
                    {
                        "id": "cpu_book",
                        "type": "runbook",
                        "incident_type": "cpu-spike",
                    }
                ],
                "else_steps": [
                    {
                        "id": "memory_book",
                        "type": "runbook",
                        "incident_type": "memory-leak",
                    }
                ],
            },
        ],
    }


# [추가] 모델 타입·분기 구조·입력 제한을 외부 API 호출 없이 검증한다.
class WorkflowModelTests(unittest.TestCase):
    # [추가] JSON을 읽고 다시 내보내도 절차와 각 노드의 타입이 보존되는지 확인한다.
    def test_plan_example_round_trips_through_json(self):
        payload = example_definition()
        workflow = WorkflowDefinition.model_validate_json(json.dumps(payload))
        self.assertEqual(workflow.model_dump(mode="json"), payload)
        self.assertIsInstance(workflow.steps[0], MonitoringStep)
        self.assertIsInstance(workflow.steps[1], IfStep)
        self.assertIsInstance(workflow.steps[1].then_steps[0], RunbookStep)
        self.assertEqual(workflow.node_count, 4)
        self.assertEqual(
            [step.id for step in workflow.iter_steps()],
            ["metrics_1", "branch_1", "cpu_book", "memory_book"],
        )

    # [추가] 정수와 소수 임계값을 임의로 다른 타입으로 변환하지 않는지 확인한다.
    def test_integer_and_fractional_thresholds_remain_numbers(self):
        for value in (80, 79.9):
            with self.subTest(value=value):
                condition = NumericCondition(
                    ref="metrics_1", field="memory_percent", op=">", value=value
                )
                self.assertEqual(type(condition.value), type(value))
                self.assertEqual(condition.value, value)

    # [추가] 문자열·bool·Decimal·무한대·NaN이 조건값으로 통과하지 않는지 확인한다.
    def test_numeric_thresholds_reject_coercion_and_non_finite_values(self):
        for value in ("80", True, False, None, Decimal("80"), float("inf"),
                      float("-inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                NumericCondition(
                    ref="metrics_1", field="cpu_percent", op=">=", value=value
                )

    # [추가] 상태 조건의 허용값과 비교 연산 범위를 확인한다.
    def test_status_conditions_use_only_supported_values_and_operators(self):
        for status in ("healthy", "degraded", "down"):
            for op in ("==", "!="):
                condition = StatusCondition(
                    ref="metrics_1", field="status", op=op, value=status
                )
                self.assertEqual(condition.value, status)
        for changes in ({"op": ">"}, {"value": "unknown"}, {"value": True}):
            payload = {"ref": "metrics_1", "field": "status", "op": "==",
                       "value": "healthy", **changes}
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                StatusCondition.model_validate(payload)

    # [추가] 조건의 field에 따라 올바른 Pydantic 모델이 선택되는지 확인한다.
    def test_status_condition_is_selected_by_field(self):
        payload = example_definition()
        payload["steps"][1]["condition"] = {
            "ref": "metrics_1", "field": "status", "op": "!=", "value": "healthy"
        }
        workflow = WorkflowDefinition.model_validate(payload)
        self.assertIsInstance(workflow.steps[1].condition, StatusCondition)

    # [추가] '아니면 종료'는 지원하고 아무 동작도 없는 조건은 거부하는지 확인한다.
    def test_one_empty_branch_is_allowed_but_both_empty_are_rejected(self):
        for branch in ("then_steps", "else_steps"):
            payload = example_definition()
            payload["steps"][1][branch] = []
            self.assertEqual(WorkflowDefinition.model_validate(payload).node_count, 3)
        payload["steps"][1]["then_steps"] = []
        payload["steps"][1]["else_steps"] = []
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    # [추가] 초기 지원 범위를 벗어나는 중첩 if가 차단되는지 확인한다.
    def test_nested_condition_is_rejected(self):
        payload = example_definition()
        payload["steps"][1]["then_steps"] = [deepcopy(payload["steps"][1])]
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    # [추가] 미선택 분기를 포함해 6노드는 허용하고 7노드는 거부하는지 확인한다.
    def test_six_total_nodes_allowed_seven_rejected_including_branches(self):
        payload = example_definition()
        for index in range(2):
            payload["steps"][1]["else_steps"].append({
                "id": f"extra_{index}", "type": "runbook", "incident_type": "db-down"
            })
        self.assertEqual(WorkflowDefinition.model_validate(payload).node_count, 6)
        payload["steps"][1]["else_steps"].append({
            "id": "extra_2", "type": "runbook", "incident_type": "db-down"
        })
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    # [추가] 실행할 단계가 없는 절차가 생성되지 않게 확인한다.
    def test_empty_root_steps_are_rejected(self):
        payload = example_definition()
        payload["steps"] = []
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    # [추가] True·1.0·문자열과 미지원 버전이 버전 1로 처리되지 않는지 확인한다.
    def test_schema_version_does_not_accept_bool_or_numeric_coercion(self):
        for version in (True, False, 1.0, "1", 2, None):
            payload = example_definition()
            payload["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)

    # [추가] 허용하지 않은 필드나 작업 종류가 조용히 무시되지 않는지 확인한다.
    def test_additional_keys_and_unknown_node_types_are_rejected(self):
        for location in ("root", "step", "condition"):
            payload = example_definition()
            target = {"root": payload, "step": payload["steps"][0],
                      "condition": payload["steps"][1]["condition"]}[location]
            target["unexpected"] = "ignored instructions"
            with self.subTest(location=location), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)
        payload = example_definition()
        payload["steps"][0]["type"] = "shell"
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    # [추가] ID 규칙 위반과 자원 이름에 섞인 파일 경로 등을 차단하는지 확인한다.
    def test_invalid_identifiers_and_resource_paths_are_rejected(self):
        for node_id in ("", "Metrics", "../metrics", "a" * 33):
            payload = example_definition()
            payload["steps"][0]["id"] = node_id
            with self.subTest(node_id=node_id), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)
        for resource in ("", "../auth-service", "auth_service", "auth service"):
            payload = example_definition()
            payload["steps"][0]["system"] = resource
            with self.subTest(resource=resource), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)

    # [추가] 절차 이름의 공백 정리·필수값·길이 제한을 확인한다.
    def test_names_are_nonblank_and_bounded(self):
        for name in ("", "   ", "점" * 81, 123):
            payload = example_definition()
            payload["name"] = name
            with self.subTest(name=name), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)
        payload["name"] = "  점검  "
        self.assertEqual(WorkflowDefinition.model_validate(payload).name, "점검")

    # [추가] 향후 모델 프롬프트에 제공할 JSON Schema를 생성할 수 있는지 확인한다.
    def test_generated_json_schema_is_serializable(self):
        schema = WorkflowDefinition.model_json_schema()
        encoded = json.dumps(schema, ensure_ascii=False)
        self.assertIn('"discriminator"', encoded)
        self.assertEqual(schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()

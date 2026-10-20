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


class WorkflowModelTests(unittest.TestCase):
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

    def test_integer_and_fractional_thresholds_remain_numbers(self):
        for value in (80, 79.9):
            with self.subTest(value=value):
                condition = NumericCondition(
                    ref="metrics_1", field="memory_percent", op=">", value=value
                )
                self.assertEqual(type(condition.value), type(value))
                self.assertEqual(condition.value, value)

    def test_numeric_thresholds_reject_coercion_and_non_finite_values(self):
        for value in ("80", True, False, None, Decimal("80"), float("inf"),
                      float("-inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                NumericCondition(
                    ref="metrics_1", field="cpu_percent", op=">=", value=value
                )

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

    def test_status_condition_is_selected_by_field(self):
        payload = example_definition()
        payload["steps"][1]["condition"] = {
            "ref": "metrics_1", "field": "status", "op": "!=", "value": "healthy"
        }
        workflow = WorkflowDefinition.model_validate(payload)
        self.assertIsInstance(workflow.steps[1].condition, StatusCondition)

    def test_one_empty_branch_is_allowed_but_both_empty_are_rejected(self):
        for branch in ("then_steps", "else_steps"):
            payload = example_definition()
            payload["steps"][1][branch] = []
            self.assertEqual(WorkflowDefinition.model_validate(payload).node_count, 3)
        payload["steps"][1]["then_steps"] = []
        payload["steps"][1]["else_steps"] = []
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    def test_nested_condition_is_rejected(self):
        payload = example_definition()
        payload["steps"][1]["then_steps"] = [deepcopy(payload["steps"][1])]
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

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

    def test_empty_root_steps_are_rejected(self):
        payload = example_definition()
        payload["steps"] = []
        with self.assertRaises(ValidationError):
            WorkflowDefinition.model_validate(payload)

    def test_schema_version_does_not_accept_bool_or_numeric_coercion(self):
        for version in (True, False, 1.0, "1", 2, None):
            payload = example_definition()
            payload["schema_version"] = version
            with self.subTest(version=version), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)

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

    def test_names_are_nonblank_and_bounded(self):
        for name in ("", "   ", "점" * 81, 123):
            payload = example_definition()
            payload["name"] = name
            with self.subTest(name=name), self.assertRaises(ValidationError):
                WorkflowDefinition.model_validate(payload)
        payload["name"] = "  점검  "
        self.assertEqual(WorkflowDefinition.model_validate(payload).name, "점검")

    def test_generated_json_schema_is_serializable(self):
        schema = WorkflowDefinition.model_json_schema()
        encoded = json.dumps(schema, ensure_ascii=False)
        self.assertIn('"discriminator"', encoded)
        self.assertEqual(schema["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()

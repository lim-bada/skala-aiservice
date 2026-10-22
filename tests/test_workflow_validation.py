# [추가] 새 파일 전체: 워크플로 정적 검증의 입력 경계·참조 규칙·자원 검사를 외부 호출 없이 확인한다.
"""실행 전에 잘못된 워크플로를 거부하고 오류 위치를 전달하는 계약을 검증한다."""

import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

from agent.workflow.catalog import WorkflowCatalog
from agent.workflow.models import (
    MAX_WORKFLOW_JSON_BYTES,
    WorkflowDefinition,
    WorkflowValidationIssue,
    WorkflowValidationResult,
)
from agent.workflow.validator import validate_workflow


# [추가] 다른 테스트 모듈이나 실제 데이터 파일에 의존하지 않는 조건부 절차를 만든다.
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
                    {"id": "cpu_book", "type": "runbook", "incident_type": "cpu-spike"}
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


# [추가] 독립적인 카탈로그와 공통 판정을 통해 오류 코드·입력 경로·실행 차단을 검사한다.
class WorkflowValidationTests(unittest.TestCase):
    # [추가] 검증기가 실제 파일이나 MCP에 접근할 필요가 없도록 자원 목록을 직접 주입한다.
    def setUp(self):
        self.catalog = WorkflowCatalog(
            systems=("payments-service", "auth-service", "web-01"),
            incident_types=("cpu-spike", "memory-leak", "db-down"),
        )

    # [추가] 실패한 결과가 실행 가능한 모델을 남기지 않고 모든 오류에 설명을 제공하는지 확인한다.
    def assert_invalid(self, payload, code, path=None):
        result = validate_workflow(payload, self.catalog)
        self.assertFalse(result.valid)
        self.assertIsNone(result.workflow)
        self.assertTrue(result.errors)
        matches = [issue for issue in result.errors if issue.code == code]
        self.assertTrue(matches, [(issue.code, issue.path) for issue in result.errors])
        if path is not None:
            self.assertIn(path, [issue.path for issue in matches])
        for issue in result.errors:
            self.assertIsInstance(issue.message, str)
            self.assertTrue(issue.message.strip())
            self.assertTrue(issue.path)
        return result

    # [추가] 정상적인 계획은 실행용 모델과 빈 오류 목록을 반환하는지 확인한다.
    def test_valid_conditional_definition_returns_validated_workflow(self):
        payload = example_definition()
        result = validate_workflow(payload, self.catalog)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])
        self.assertIsInstance(result.workflow, WorkflowDefinition)
        self.assertEqual(result.workflow.model_dump(mode="json"), payload)

    # [추가] 조건 없이 조회를 나열한 절차와 같은 시스템의 서로 다른 조회 ID를 허용한다.
    def test_sequential_actions_and_repeated_system_are_allowed(self):
        payload = example_definition()
        payload["steps"] = [
            payload["steps"][0],
            {"id": "metrics_2", "type": "monitoring", "system": "payments-service"},
            {"id": "cpu_book", "type": "runbook", "incident_type": "cpu-spike"},
        ]
        self.assertTrue(validate_workflow(payload, self.catalog).valid)

    # [추가] 여러 조건이 앞서 실행된 같은 최상위 모니터링을 재사용할 수 있는지 확인한다.
    def test_multiple_conditions_may_reference_same_prior_monitor(self):
        payload = example_definition()
        second = deepcopy(payload["steps"][1])
        second["id"] = "branch_2"
        second["condition"] = {
            "ref": "metrics_1", "field": "status", "op": "!=", "value": "healthy"
        }
        second["then_steps"][0]["id"] = "second_book"
        second["else_steps"] = []
        payload["steps"].append(second)
        result = validate_workflow(payload, self.catalog)
        self.assertTrue(result.valid)
        self.assertEqual(result.workflow.node_count, 6)

    # [추가] dict·JSON 문자열·UTF-8 바이트·모델 입력이 같은 계약으로 검증되는지 확인한다.
    def test_supported_payload_formats_produce_same_definition(self):
        payload = example_definition()
        encoded = json.dumps(payload, ensure_ascii=False)
        variants = (payload, encoded, encoded.encode("utf-8"), WorkflowDefinition.model_validate(payload))
        for candidate in variants:
            with self.subTest(input_type=type(candidate).__name__):
                result = validate_workflow(candidate, self.catalog)
                self.assertTrue(result.valid)
                self.assertEqual(result.workflow.model_dump(mode="json"), payload)

    # [추가] API가 성공·실패 결과와 오류 정보를 추가 가공 없이 JSON으로 반환할 수 있는지 확인한다.
    def test_validation_results_are_json_serializable(self):
        for payload in (example_definition(), {"schema_version": True}):
            with self.subTest(payload=payload):
                result = validate_workflow(payload, self.catalog)
                serialized = json.loads(result.model_dump_json())
                self.assertEqual(serialized["valid"], result.valid)
                self.assertEqual(len(serialized["errors"]), len(result.errors))
                if not result.valid:
                    self.assertIsNone(serialized["workflow"])
                    self.assertEqual(
                        set(serialized["errors"][0]), {"code", "path", "message"}
                    )

    # [추가] 결과 모델이 성공 시 실행 정의만, 실패 시 오류만 담는 두 상태를 허용하는지 확인한다.
    def test_result_model_accepts_consistent_success_and_failure(self):
        workflow = WorkflowDefinition.model_validate(example_definition())
        issue = WorkflowValidationIssue(
            code="INVALID_SCHEMA", path="$", message="입력 구조를 확인하세요."
        )
        successful = WorkflowValidationResult(valid=True, workflow=workflow)
        failed = WorkflowValidationResult(valid=False, errors=[issue])
        self.assertEqual(successful.errors, [])
        self.assertIsNotNone(successful.workflow)
        self.assertIsNone(failed.workflow)
        self.assertEqual(len(failed.errors), 1)

    # [추가] 성공인데 오류가 있거나 실패인데 실행 정의가 남는 모순된 결과를 생성할 수 없다.
    def test_result_model_rejects_inconsistent_valid_workflow_and_errors(self):
        workflow = WorkflowDefinition.model_validate(example_definition())
        issue = WorkflowValidationIssue(
            code="INVALID_SCHEMA", path="$", message="입력 구조를 확인하세요."
        )
        inconsistent = (
            {"valid": True},
            {"valid": True, "workflow": workflow, "errors": [issue]},
            {"valid": False},
            {"valid": False, "workflow": workflow},
            {"valid": False, "workflow": workflow, "errors": [issue]},
        )
        for payload in inconsistent:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                WorkflowValidationResult.model_validate(payload)

    # [추가] 호출자가 가진 dict를 변경하지 않고 반환 모델과 중첩 목록까지 분리하는지 검사한다.
    def test_success_does_not_mutate_or_share_caller_dict(self):
        payload = example_definition()
        payload["name"] = "  점검  "
        original = deepcopy(payload)
        result = validate_workflow(payload, self.catalog)
        self.assertTrue(result.valid)
        self.assertEqual(payload, original)
        self.assertEqual(result.workflow.name, "점검")
        payload["steps"][1]["then_steps"][0]["incident_type"] = "db-down"
        self.assertEqual(result.workflow.steps[1].then_steps[0].incident_type, "cpu-spike")

    # [추가] 이미 만들어진 모델을 전달해도 실행용 결과는 독립적인 사본이어야 한다.
    def test_successful_model_input_is_defensively_copied(self):
        model = WorkflowDefinition.model_validate(example_definition())
        result = validate_workflow(model, self.catalog)
        self.assertTrue(result.valid)
        self.assertIsNot(result.workflow, model)
        self.assertIsNot(result.workflow.steps[1], model.steps[1])
        model.steps[1].then_steps[0].incident_type = "db-down"
        self.assertEqual(result.workflow.steps[1].then_steps[0].incident_type, "cpu-spike")

    # [추가] 실행 여부가 결정되지 않은 양쪽 분기의 자원을 모두 검사하고 오류를 함께 반환한다.
    def test_unknown_resources_are_collected_from_all_branches(self):
        payload = example_definition()
        payload["steps"][0]["system"] = "missing-service"
        payload["steps"][1]["then_steps"][0]["incident_type"] = "unknown-cpu"
        payload["steps"][1]["else_steps"][0] = {
            "id": "branch_metric", "type": "monitoring", "system": "missing-host"
        }
        result = self.assert_invalid(payload, "UNKNOWN_SYSTEM", "steps[0].system")
        errors = {(issue.code, issue.path) for issue in result.errors}
        self.assertIn(("UNKNOWN_RUNBOOK", "steps[1].then_steps[0].incident_type"), errors)
        self.assertIn(("UNKNOWN_SYSTEM", "steps[1].else_steps[0].system"), errors)

    # [추가] 자원 검사가 카탈로그를 자동 로딩하지 않고 전달받은 목록을 기준으로 동작하는지 확인한다.
    def test_resource_membership_uses_injected_catalog(self):
        payload = example_definition()
        other_catalog = WorkflowCatalog(systems=("other-service",), incident_types=("other-book",))
        result = validate_workflow(payload, other_catalog)
        self.assertFalse(result.valid)
        self.assertEqual(
            {issue.code for issue in result.errors}, {"UNKNOWN_SYSTEM", "UNKNOWN_RUNBOOK"}
        )

    # [추가] ID가 서로 다른 최상위 단계와 분기 사이에서도 전역으로 유일해야 한다.
    def test_duplicate_ids_across_root_and_branch_are_rejected(self):
        payload = example_definition()
        payload["steps"][1]["then_steps"][0]["id"] = "metrics_1"
        self.assert_invalid(payload, "DUPLICATE_ID", "steps[1].then_steps[0].id")

    # [추가] 동시에 실행되지 않는 양쪽 분기도 동일 ID를 재사용할 수 없도록 검사한다.
    def test_duplicate_ids_across_alternative_branches_are_rejected(self):
        payload = example_definition()
        payload["steps"][1]["else_steps"][0]["id"] = "cpu_book"
        self.assert_invalid(payload, "DUPLICATE_ID", "steps[1].else_steps[0].id")

    # [추가] 중복 ID가 조건의 참조 대상이면 어느 노드인지 모호하므로 참조도 거부한다.
    def test_reference_to_duplicate_id_is_ambiguous(self):
        payload = example_definition()
        payload["steps"].insert(1, {
            "id": "metrics_1", "type": "monitoring", "system": "auth-service"
        })
        result = self.assert_invalid(payload, "DUPLICATE_ID", "steps[1].id")
        self.assertIn(
            ("INVALID_REFERENCE", "steps[2].condition.ref"),
            {(issue.code, issue.path) for issue in result.errors},
        )

    # [추가] 존재하지 않는 단계·자기 자신·나중에 실행할 단계를 조건에서 참조하지 못하게 한다.
    def test_missing_self_and_future_references_are_rejected(self):
        for ref in ("missing", "branch_1", "future_metric"):
            with self.subTest(ref=ref):
                payload = example_definition()
                payload["steps"][1]["condition"]["ref"] = ref
                if ref == "future_metric":
                    payload["steps"].append({
                        "id": "future_metric", "type": "monitoring", "system": "web-01"
                    })
                self.assert_invalid(payload, "INVALID_REFERENCE", "steps[1].condition.ref")

    # [추가] 런북 조회가 먼저 실행되더라도 숫자·상태 조건의 데이터 소스로 사용할 수 없다.
    def test_prior_runbook_cannot_be_used_as_monitoring_reference(self):
        payload = example_definition()
        payload["steps"][0] = {
            "id": "metrics_1", "type": "runbook", "incident_type": "cpu-spike"
        }
        self.assert_invalid(payload, "INVALID_REFERENCE", "steps[1].condition.ref")

    # [추가] 앞선 if 자체도 모니터링 결과가 아니므로 다음 조건에서 참조할 수 없다.
    def test_prior_conditional_node_cannot_be_referenced(self):
        payload = example_definition()
        second = deepcopy(payload["steps"][1])
        second["id"] = "branch_2"
        second["condition"]["ref"] = "branch_1"
        second["then_steps"][0]["id"] = "second_book"
        second["else_steps"] = []
        payload["steps"].append(second)
        self.assert_invalid(payload, "INVALID_REFERENCE", "steps[2].condition.ref")

    # [추가] 분기에서 만든 값은 실행이 보장되지 않으므로 이후 최상위 조건에서 사용하지 않는다.
    def test_prior_branch_monitor_cannot_escape_its_branch(self):
        payload = example_definition()
        payload["steps"][1]["then_steps"] = [{
            "id": "branch_metric", "type": "monitoring", "system": "web-01"
        }]
        payload["steps"][1]["else_steps"] = []
        second = deepcopy(payload["steps"][1])
        second["id"] = "branch_2"
        second["condition"]["ref"] = "branch_metric"
        second["then_steps"] = [{
            "id": "second_book", "type": "runbook", "incident_type": "cpu-spike"
        }]
        payload["steps"].append(second)
        self.assert_invalid(payload, "INVALID_REFERENCE", "steps[2].condition.ref")

    # [추가] 현재 조건의 분기 내부 조회는 조건 평가 시점에 아직 실행되지 않으므로 거부한다.
    def test_current_branch_monitor_cannot_feed_its_parent_condition(self):
        payload = example_definition()
        payload["steps"][1]["condition"]["ref"] = "branch_metric"
        payload["steps"][1]["then_steps"] = [{
            "id": "branch_metric", "type": "monitoring", "system": "web-01"
        }]
        self.assert_invalid(payload, "INVALID_REFERENCE", "steps[1].condition.ref")

    # [추가] 구조 오류의 위치가 Pydantic 내부 union 태그 없이 사용자의 JSON 경로를 가리키는지 확인한다.
    def test_schema_error_paths_remove_virtual_union_tags(self):
        payload = example_definition()
        payload["steps"][0]["system"] = "../payments"
        payload["steps"][1]["condition"]["value"] = "80"
        payload["steps"][1]["then_steps"][0]["unexpected"] = True
        result = self.assert_invalid(payload, "INVALID_SCHEMA", "steps[0].system")
        paths = {issue.path for issue in result.errors}
        self.assertIn("steps[1].condition.value", paths)
        self.assertIn("steps[1].then_steps[0].unexpected", paths)
        self.assertTrue(all(issue.code == "INVALID_SCHEMA" for issue in result.errors))
        for path in paths:
            for virtual_tag in (".monitoring.", ".runbook.", ".if.", ".cpu_percent.", ".int", ".float"):
                self.assertNotIn(virtual_tag, path)

    # [추가] 실제 추가 필드명이 내부 태그와 같아도 오류 경로에서 그 필드를 지우지 않는다.
    def test_schema_paths_preserve_real_fields_named_like_union_tags(self):
        payload = example_definition()
        payload["steps"][0]["monitoring"] = "unexpected"
        payload["steps"][1]["condition"]["cpu_percent"] = "unexpected"
        result = self.assert_invalid(payload, "INVALID_SCHEMA", "steps[0].monitoring")
        self.assertIn(
            "steps[1].condition.cpu_percent", [issue.path for issue in result.errors]
        )

    # [추가] 허용하지 않은 노드 종류와 조건 필드를 입력 위치와 함께 거부한다.
    def test_unknown_node_and_condition_discriminators_are_schema_errors(self):
        payload = example_definition()
        payload["steps"][0]["type"] = "shell"
        self.assert_invalid(payload, "INVALID_SCHEMA", "steps[0]")
        payload = example_definition()
        payload["steps"][1]["condition"]["field"] = "disk_percent"
        self.assert_invalid(payload, "INVALID_SCHEMA", "steps[1].condition")

    # [추가] 숫자·상태 조건의 잘못된 타입과 허용하지 않은 연산자를 구조 오류로 반환한다.
    def test_malformed_conditions_are_rejected_before_semantic_checks(self):
        changes = (
            {"value": True}, {"value": None}, {"value": "80"}, {"op": "contains"},
            {"field": "status", "op": ">", "value": "healthy"},
            {"field": "status", "op": "==", "value": "unknown"},
        )
        for change in changes:
            with self.subTest(change=change):
                payload = example_definition()
                payload["steps"][1]["condition"].update(change)
                self.assert_invalid(payload, "INVALID_SCHEMA")

    # [추가] 최상위·노드의 추가 키와 불리언 버전이 자동 보정되지 않고 거부되는지 확인한다.
    def test_extra_keys_and_bool_version_are_schema_errors(self):
        for target in ("root", "step", "condition"):
            with self.subTest(target=target):
                payload = example_definition()
                locations = {
                    "root": (payload, "unexpected"),
                    "step": (payload["steps"][0], "steps[0].unexpected"),
                    "condition": (payload["steps"][1]["condition"], "steps[1].condition.unexpected"),
                }
                data, path = locations[target]
                data["unexpected"] = "ignored instructions"
                self.assert_invalid(payload, "INVALID_SCHEMA", path)
        payload = example_definition()
        payload["schema_version"] = True
        self.assert_invalid(payload, "INVALID_SCHEMA", "schema_version")

    # [추가] 잘못된 최상위 자료형은 예외를 전파하지 않고 루트 스키마 오류로 반환한다.
    def test_non_object_inputs_are_schema_errors(self):
        for payload in (None, [], [example_definition()], 1, True):
            with self.subTest(payload=payload):
                self.assert_invalid(payload, "INVALID_SCHEMA", "$")
        for encoded in ("null", "[]", '"text"', "1"):
            with self.subTest(encoded=encoded):
                self.assert_invalid(encoded, "INVALID_SCHEMA", "$")

    # [추가] 양쪽 분기를 포함한 노드 제한과 중첩 조건 제한이 검증기 진입점에도 적용된다.
    def test_node_limit_and_nested_conditions_are_schema_errors(self):
        payload = example_definition()
        for index in range(3):
            payload["steps"][1]["then_steps"].append({
                "id": f"extra_{index}", "type": "runbook", "incident_type": "db-down"
            })
        self.assert_invalid(payload, "INVALID_SCHEMA", "$")
        payload = example_definition()
        payload["steps"][1]["then_steps"] = [deepcopy(payload["steps"][1])]
        self.assert_invalid(payload, "INVALID_SCHEMA", "steps[1].then_steps[0]")

    # [추가] 생성 이후 타입이 변조된 모델 인스턴스가 구조 검사를 우회하지 못하도록 확인한다.
    def test_mutated_model_numeric_type_is_revalidated(self):
        model = WorkflowDefinition.model_validate(example_definition())
        model.steps[1].condition.value = "80"
        self.assert_invalid(model, "INVALID_SCHEMA", "steps[1].condition.value")

    # [추가] 모델 인스턴스의 참조가 변조돼도 의미 검사를 생략하지 않는지 확인한다.
    def test_mutated_model_reference_is_revalidated(self):
        model = WorkflowDefinition.model_validate(example_definition())
        model.steps[1].condition.ref = "missing"
        self.assert_invalid(model, "INVALID_REFERENCE", "steps[1].condition.ref")

    # [추가] JSON 구문 오류·잘못된 UTF-8·중복 키를 구분 가능한 입력 오류로 전달한다.
    def test_invalid_raw_json_is_rejected_without_exception(self):
        for raw in ("", "{broken", '{"steps": [}', b"\xff", b"\xe3\x81", "\ud800"):
            with self.subTest(raw=raw):
                self.assert_invalid(raw, "INVALID_JSON", "$")

    # [추가] Python JSON 파서가 기본 허용하는 비표준 숫자도 JSON 입력 오류로 거부한다.
    def test_non_standard_json_numeric_constants_are_rejected(self):
        raw = json.dumps(example_definition())
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                malformed = raw.replace('"value": 80', f'"value": {constant}')
                self.assert_invalid(malformed, "INVALID_JSON", "$")

    # [추가] JSON 중복 키는 마지막 값으로 덮어써 해석하지 않고 모든 깊이에서 거부한다.
    def test_duplicate_json_keys_are_rejected_at_root_and_nested_levels(self):
        encoded = json.dumps(example_definition(), ensure_ascii=False)
        duplicates = (
            encoded.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1'),
            encoded.replace('"ref": "metrics_1"', '"ref": "metrics_1", "ref": "metrics_1"'),
        )
        for raw in duplicates:
            with self.subTest(raw=raw):
                self.assert_invalid(raw, "INVALID_JSON", "$")

    # [추가] 정확히 허용된 바이트 수는 통과하고 1바이트 초과한 JSON은 파싱 전 차단한다.
    def test_raw_json_size_limit_is_inclusive_and_shared(self):
        raw = json.dumps(example_definition(), ensure_ascii=False).encode("utf-8")
        exact = raw + b" " * (MAX_WORKFLOW_JSON_BYTES - len(raw))
        self.assertEqual(len(exact), MAX_WORKFLOW_JSON_BYTES)
        self.assertTrue(validate_workflow(exact, self.catalog).valid)
        self.assert_invalid(exact + b" ", "INPUT_TOO_LARGE", "$")

    # [추가] 한글 JSON도 문자 수가 아닌 UTF-8 바이트 수로 크기 제한을 적용한다.
    def test_string_size_limit_counts_utf8_bytes(self):
        raw = json.dumps(example_definition(), ensure_ascii=False)
        oversized = raw + " " * (MAX_WORKFLOW_JSON_BYTES - len(raw))
        self.assertEqual(len(oversized), MAX_WORKFLOW_JSON_BYTES)
        self.assertGreater(len(oversized.encode("utf-8")), MAX_WORKFLOW_JSON_BYTES)
        self.assert_invalid(oversized, "INPUT_TOO_LARGE", "$")


# [추가] 이 파일만 실행해도 표준 unittest 실행기를 사용할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

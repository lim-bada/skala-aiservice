# [추가] 새 파일 전체: 이름·ID를 제외한 절차 의미, 숫자·분기·참조와 실제 호출 채점을 검증한다.
import json
import unittest
from collections import Counter
from copy import deepcopy

from agent.workflow.models import WorkflowDefinition
from eval.run_workflow_eval import CASE_PATH, FIXTURE_PATH, load_cases
from eval.scoring import canonical_definition, score_calls, score_definition


# [추가] 정답과 독립적으로 바꾼 후보를 이용해 채점기가 실제 의미 차이를 찾는지 확인한다.
class EvaluationScoringTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases()
        self.definition = deepcopy(self.cases[0]["expected_definition"])

    def test_suite_has_18_supported_6_unsupported_and_disjoint_splits(self):
        self.assertEqual(len(self.cases), 24)
        for split in ("dev", "holdout"):
            self.assertEqual(Counter(case["supported"] for case in self.cases if case["split"] == split), {True: 9, False: 3})
        self.assertEqual(sum(len(case["scenarios"]) for case in self.cases), 40)
        self.assertEqual(len({case["query"] for case in self.cases}), 24)

    def test_dataset_includes_all_operators_empty_branches_and_six_nodes(self):
        conditions = [step for case in self.cases if case["supported"] for step in case["expected_definition"]["steps"] if step["type"] == "if"]
        self.assertEqual({step["condition"]["op"] for step in conditions}, {">", ">=", "<", "<=", "==", "!="})
        self.assertTrue(any(not step["then_steps"] for step in conditions))
        self.assertTrue(any(not step["else_steps"] for step in conditions))
        self.assertTrue(any(WorkflowDefinition.model_validate(case['expected_definition']).node_count == 6 for case in self.cases if case['supported']))

    def test_name_ids_and_int_float_representation_do_not_change_semantics(self):
        candidate = deepcopy(self.definition)
        candidate["name"] = "변경된 표시 이름"
        candidate["steps"][0]["id"] = "renamed"
        candidate["steps"][1]["condition"].update(ref="renamed", value=80.0)
        candidate["steps"][1]["id"] = "renamed_branch"
        for branch in ("then_steps", "else_steps"):
            candidate["steps"][1][branch][0]["id"] += "_renamed"
        self.assertTrue(score_definition(candidate, self.definition)["exact"])

    def test_complementary_comparison_with_swapped_branches_is_equivalent(self):
        candidate = deepcopy(self.definition)
        condition = candidate["steps"][1]
        condition["condition"]["op"] = "<"
        condition["then_steps"], condition["else_steps"] = condition["else_steps"], condition["then_steps"]
        self.assertTrue(score_definition(candidate, self.definition)["exact"])

    def test_wrong_operator_threshold_and_branch_are_not_correct(self):
        for update in ({"op": ">"}, {"value": 81}, {"field": "memory_percent"}):
            candidate = deepcopy(self.definition)
            candidate["steps"][1]["condition"].update(update)
            self.assertFalse(score_definition(candidate, self.definition)["exact"])
        candidate = deepcopy(self.definition)
        candidate["steps"][1]["else_steps"] = []
        self.assertFalse(score_definition(candidate, self.definition)["exact"])

    def test_reference_to_other_prior_system_and_sequence_order_are_checked(self):
        case = next(case for case in self.cases if case["id"] == "holdout_ref_second")
        candidate = deepcopy(case["expected_definition"])
        candidate["steps"][2]["condition"]["ref"] = "auth"
        self.assertFalse(score_definition(candidate, case["expected_definition"])["exact"])
        sequential = next(case for case in self.cases if case["id"] == "dev_sequence")["expected_definition"]
        candidate = deepcopy(sequential)
        candidate["steps"].reverse()
        self.assertFalse(score_definition(candidate, sequential)["exact"])

    def test_duplicate_unknown_reference_bool_and_string_numbers_are_invalid(self):
        for value in (True, "80"):
            candidate = deepcopy(self.definition)
            candidate["steps"][1]["condition"]["value"] = value
            self.assertFalse(score_definition(candidate, self.definition)["exact"])
        candidate = deepcopy(self.definition)
        candidate["steps"][1]["id"] = "metrics"
        self.assertFalse(score_definition(candidate, self.definition)["exact"])
        candidate["steps"][1]["condition"]["ref"] = "future"
        with self.assertRaises(ValueError):
            canonical_definition(candidate)

    def test_call_order_duplicates_targets_and_unknown_sources(self):
        expected = [{"source": "monitoring", "target": "auth-service"}, {"source": "monitoring", "target": "web-01"}]
        score = score_calls(list(reversed(expected)), expected)
        self.assertFalse(score["path_match"])
        self.assertTrue(score["call_multiset_match"])
        self.assertEqual(score["target_coverage"], 1)
        score = score_calls(expected + [expected[0]], expected)
        self.assertEqual(score["forbidden_calls"], 1)
        self.assertEqual(score_calls(expected[:1], expected)["target_coverage"], .5)
        self.assertIsNone(score_calls([], [])["target_coverage"])
        with self.assertRaises(ValueError):
            score_calls([{"source": "restart", "target": "auth-service"}], [])

    def test_snapshots_and_query_limits_are_checked_before_execution(self):
        self.assertEqual(len(json.loads(FIXTURE_PATH.read_text())["snapshots"]), 6)
        self.assertEqual(load_cases(split="dev", limit=3), self.cases[:3])
        self.assertEqual(json.loads(CASE_PATH.read_text())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()

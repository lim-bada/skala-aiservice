# [추가] 새 파일 전체: 외부 API·기존 DB 없이 실제 그래프·실행기·임시 저장소로 평가기를 검증한다.
import argparse
import asyncio
import ast
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx

from agent.fake_llm import FastLLM, QualityLLM
from agent.workflow.catalog import load_catalog
from agent.workflow.executor import execute_workflow
from agent.workflow.graph import run_workflow
from agent.workflow.store import SqliteWorkflowStore
from eval.run_workflow_eval import evaluate, load_cases, main, make_models, semantic_scores
from eval.runtime import (
    ROOT, MeteredModel, RecordingFixtureMCP, fatal_provider_error, generate_saved,
    run_baseline, run_guard_checks, run_saved, usage_total,
)


# [추가] 동기·비동기 네트워크를 막고 평가 안에서만 독립된 로컬 자원을 사용한다.
class EvaluationRunnerTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 지원 시나리오 34개의 정답 AST를 실행해 새 보고 합성이 다중 조회·빈 분기에서도 동작하는지 검사한다.
    async def test_all_fixed_execution_scenarios_complete_with_fact_reports(self):
        checked = 0
        for case in self.cases:
            if not case["supported"]:
                continue
            for scenario in case["scenarios"]:
                with self.subTest(case=case["id"], scenario=scenario["id"]):
                    mcp = RecordingFixtureMCP(scenario["snapshots"])
                    result = await run_workflow(case["expected_definition"], load_catalog(), mcp, QualityLLM("fake-report", 0.0), query=case["query"])
                    self.assertEqual(result.status, "completed", result.error)
                    self.assertEqual(mcp.domain_calls, scenario["expected_calls"])
                    self.assertTrue(result.facts_report)
                    self.assertTrue(result.report_valid)
                    book_count = sum(item.source == "runbook" for item in result.execution.results)
                    self.assertIn(f"런북 {book_count}회", result.final)
                    if book_count == 0:
                        self.assertEqual(result.report_attempts, [])
                    checked += 1
        self.assertEqual(checked, 34)

    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}))
        self.network = self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("실제 네트워크 금지")))
        self.cases = load_cases()

    async def asyncTearDown(self):
        self.network.assert_not_called()

    async def test_all_supported_oracles_match_actual_executor_on_34_scenarios(self):
        count = 0
        for case in self.cases:
            if not case["supported"]:
                continue
            for scenario in case["scenarios"]:
                with self.subTest(case=case["id"], scenario=scenario["id"]):
                    mcp = RecordingFixtureMCP(scenario["snapshots"])
                    result = await execute_workflow(case["expected_definition"], load_catalog(), mcp)
                    self.assertEqual(result.status, "completed")
                    self.assertEqual(mcp.domain_calls, scenario["expected_calls"])
                    count += 1
        self.assertEqual(count, 34)

    async def test_fixed_monitoring_copies_and_discovery_counts_are_independent(self):
        mcp = RecordingFixtureMCP({})
        first = await mcp.call_tool("get_monitoring_status", {"system_name": "auth-service"})
        first["cpu_percent"] = 100
        second = await mcp.call_tool("get_monitoring_status", {"system_name": "auth-service"})
        self.assertNotEqual(second["cpu_percent"], 100)
        await mcp.find_prompt("incident_analysis_prompt")
        await mcp.get_prompt("incident_analysis_prompt", {"system_name": "auth-service", "symptom": "지연"})
        self.assertEqual(len(mcp.domain_calls), 2)
        self.assertEqual([event["source"] for event in mcp.events], ["monitoring", "monitoring", "discovery", "prompt"])
        self.assertTrue(all(event["completed_ms"] is not None for event in mcp.events))

    async def test_baseline_prompt_is_exact_original_mcp_prompt(self):
        # [추가] FastMCP 서버를 실행하지 않고 원본 함수 AST만 로드해 복사 문구의 동일성을 확인한다.
        source = ast.parse((ROOT / "mcp_server.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "incident_analysis_prompt")
        function.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        namespace = {}
        exec(compile(module, "original_mcp_prompt", "exec"), namespace)
        mcp = RecordingFixtureMCP({})
        text = await mcp.get_prompt("incident_analysis_prompt", {"system_name": "payments-service", "symptom": "CPU 급증"})
        self.assertEqual(text, namespace["incident_analysis_prompt"]("payments-service", "CPU 급증"))

    async def test_unchanged_baseline_incident_graph_uses_fixture_mcp(self):
        calls = []
        fast, quality = make_models("fake", calls)
        result = await run_baseline("payments-service CPU 급증 장애를 진단해줘.", {}, fast, quality)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["route"], "incident")
        self.assertIn({"source": "runbook", "target": "cpu-spike"}, result["calls"])
        self.assertEqual([call["method"] for call in calls], ["route", "plan", "write", "write"])
        self.assertTrue(any(event["operation"] == "get_prompt" for event in result["mcp_events"]))

    async def test_saved_reuse_changes_branch_without_recompilation(self):
        calls = []
        fast, quality = make_models("fake", calls)
        case = self.cases[0]
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowStore(Path(directory) / "definitions.db")
            try:
                compiled, record = await generate_saved(case["query"], fast, store)
                self.assertTrue(compiled.valid)
                saved_hash = record.definition_hash
                before, after = case["scenarios"][:2]
                first = await run_saved(store.get(record.workflow_id), case["query"], before["snapshots"], quality)
                second = await run_saved(store.get(record.workflow_id), case["query"], after["snapshots"], quality)
                self.assertEqual(first["calls"], before["expected_calls"])
                self.assertEqual(second["calls"], after["expected_calls"])
                self.assertEqual(store.get(record.workflow_id).definition_hash, saved_hash)
                self.assertEqual(sum(call["method"] == "compile_workflow" for call in calls), 1)
                self.assertNotEqual(first["result"]["run_id"], second["result"]["run_id"])
            finally:
                store.close()

    async def test_fake_does_not_use_expected_ast_as_model_response(self):
        calls = []
        fast, _ = make_models("fake", calls)
        case = next(case for case in self.cases if case["id"] == "dev_cpu_gt")
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteWorkflowStore(Path(directory) / "definitions.db")
            try:
                compiled, record = await generate_saved(case["query"], fast, store)
                self.assertFalse(compiled.valid)
                self.assertIsNone(record)
                self.assertFalse(semantic_scores(case, compiled, calls)["final_semantic"]["exact"])
            finally:
                store.close()

    async def test_12_invalid_asts_are_blocked_before_domain_calls(self):
        checks = await run_guard_checks()
        self.assertEqual((checks["total"], checks["blocked_before_mcp"]), (12, 12))
        self.assertTrue(all(case["domain_calls"] == 0 for case in checks["cases"]))

    async def test_failed_usage_is_unknown_and_error_artifacts_are_redacted(self):
        class FailedModel:
            name = "test-model"

            async def compile_workflow(self, *args, **kwargs):
                request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions", headers={"Authorization": "Bearer private-key"})
                response = httpx.Response(401, json={"error": "private error body"}, request=request)
                raise httpx.HTTPStatusError("private error body private-key", request=request, response=response)

        calls = []
        model = MeteredModel(FailedModel(), "openai", calls)
        with self.assertRaises(httpx.HTTPStatusError):
            await model.compile_workflow()
        self.assertEqual(fatal_provider_error(calls)["http_status"], 401)
        self.assertIsNone(usage_total(calls)["total_tokens"])
        self.assertNotIn("private", json.dumps(calls))
        self.assertEqual(usage_total([])["total_tokens"], 0)

    async def test_comparison_artifacts_and_reuse_probe_have_provenance(self):
        args = argparse.Namespace(backend="fake", mode="compare", report_mode="full", split="dev", reuse_first=True, timeout=10)
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            result = await evaluate(args, self.cases[:1], Path(directory))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["summary"]["paths"]["workflow"]["exact_paths"], 3)
            self.assertEqual(result["cases"][0]["saved_definition_hash"], result["scenarios"][0]["workflow"]["definition_hash"])
            self.assertEqual(result["summary"]["baseline_routes"], {"chat": 3})
            self.assertEqual(result["scenarios"][0]["reuse_probe"]["compile_calls"], 0)
            self.assertTrue((Path(directory) / "report.md").is_file())
            self.assertEqual(len(result["manifest"]["source_sha256"]), 64)
            self.assertEqual(len(result["manifest"]["runbook_sha256"]), 6)
            self.assertEqual(len(result["manifest"]["catalog_sha256"]), 64)
            self.assertEqual({call["arm"] for call in result["model_calls"]}, {"baseline", "workflow"})

    async def test_provider_failure_stops_after_first_call_without_false_refusal(self):
        class FailedModel:
            name = "test-model"

            async def compile_workflow(self, *args, **kwargs):
                raise httpx.ConnectError("private connection information")

        def models(backend, calls, context):
            return MeteredModel(FailedModel(), backend, calls, context), None

        args = argparse.Namespace(backend="fake", mode="compare", report_mode="full", split="dev", reuse_first=False, timeout=10)
        with tempfile.TemporaryDirectory() as directory, patch("eval.run_workflow_eval.make_models", side_effect=models), redirect_stdout(io.StringIO()):
            result = await evaluate(args, self.cases[:3], Path(directory))
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(len(result["model_calls"]), 1)
            self.assertEqual(result["summary"]["unsupported_explicitly_rejected"], 0)
            self.assertNotIn("private", (Path(directory) / "results.json").read_text())

    async def test_model_error_is_not_counted_as_correct_unsupported_rejection(self):
        class FailedModel:
            name = "test-model"

            async def compile_workflow(self, *args, **kwargs):
                raise ValueError("private failure")

        def models(backend, calls, context):
            return MeteredModel(FailedModel(), backend, calls, context), None

        case = next(case for case in self.cases if not case["supported"])
        args = argparse.Namespace(backend="fake", mode="workflow", report_mode="full", split="dev", reuse_first=False, timeout=10)
        # [추가] 사전 거절 검사와 별개로 모델 오류를 거절 정답으로 오인하지 않는 집계 경로를 검사한다.
        with tempfile.TemporaryDirectory() as directory, patch("eval.run_workflow_eval.make_models", side_effect=models), patch("agent.workflow.compiler.query_issues", return_value=[]), redirect_stdout(io.StringIO()):
            result = await evaluate(args, [case], Path(directory))
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["cases"][0]["scores"]["unsupported_correct"])

    async def test_none_report_mode_omits_real_report_model(self):
        class ForbiddenQuality:
            async def write(self, *args, **kwargs):
                raise AssertionError("보고 모델 호출 금지")

        result = await run_baseline("payments-service CPU 급증 장애", {}, FastLLM("fake", 0), ForbiddenQuality(), "none")
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["final"])


# [추가] 미리보기는 설정된 API 키가 있어도 모델·네트워크를 호출하지 않는다.
class EvaluationCLITests(unittest.TestCase):
    def test_preview_only_writes_reviewable_payload_without_model_call(self):
        with tempfile.TemporaryDirectory() as directory, patch("eval.run_workflow_eval.load_dotenv", return_value=False), patch("eval.run_workflow_eval.make_models", side_effect=AssertionError("모델 호출 금지")), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--backend", "openai", "--split", "all", "--preview-only", "--output-dir", directory]), 0)
            preview = json.loads((Path(directory) / "payload_preview.json").read_text())
            self.assertEqual(len(preview["compile_queries"]), 24)
            self.assertEqual(preview["destination"], "https://api.openai.com/v1/chat/completions")
            self.assertFalse((Path(directory) / "results.json").exists())


if __name__ == "__main__":
    unittest.main()

# [추가] 외부 호출 없이 보고 프롬프트의 사실 전달과 공급자별 비동기 보고 계약을 검증한다.
"""보고 모델의 원문·상태·실측 사용량·취소 전파를 MockTransport로 확인한다."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from agent.openai_llm import OPENAI_URL, OpenAIQualityLLM
from agent.ollama_llm import OLLAMA_URL, OllamaQualityLLM
from agent.workflow.report_prompting import build_workflow_report_messages


# [추가] 실제 HTTP 연결을 만들지 않도록 원본 클라이언트에 모의 전송 계층만 주입한다.
_ASYNC_CLIENT = httpx.AsyncClient
_REPORT = '\n' + json.dumps({"summaries": [{
    "source_id": "runbook_001", "block_id": "block_001",
    "summary": "런북은 확장 전에 상태를 확인하도록 안내한다.",
}]}, ensure_ascii=False) + '  \n'


# [추가] 실제 조회값·선택 분기·미선택 단계를 포함한 독립적인 입력을 준비한다.
def report_inputs():
    collected = [
        {
            "source": "monitoring",
            "data": {
                "system_name": "payments-service",
                "status": "degraded",
                "cpu_percent": 80.125,
                "memory_percent": 55.75,
                "last_checked": "2026-10-20T10:00:00+09:00",
                "alerts": ["CPU 임계치 초과"],
            },
        },
        {"source": "runbook", "data": '# CPU\n1. "확장" 전 상태를 확인한다.'},
    ]
    trace = [
        {
            "sequence": 2,
            "node_id": "branch_1",
            "node_type": "if",
            "path": "steps[1]",
            "status": "completed",
            "elapsed_ms": 0.2,
            "condition": {
                "ref": "metrics_1",
                "field": "cpu_percent",
                "actual": 80.125,
                "op": ">=",
                "expected": 80,
                "result": True,
                "selected_branch": "then_steps",
            },
        },
        {
            "sequence": 3,
            "node_id": "unused_book",
            "node_type": "runbook",
            "path": "steps[1].else_steps[0]",
            "status": "skipped",
            "elapsed_ms": 0,
            "condition": None,
        },
    ]
    return collected, trace


# [추가] 프롬프트가 자료를 생략·수정하지 않고 실행과 권고의 구분을 모델에 전달하는지 검사한다.
class WorkflowReportPromptTests(unittest.TestCase):
    def test_only_read_runbooks_are_sent_and_inputs_remain_unchanged(self):
        collected, trace = report_inputs()
        original = deepcopy((collected, trace))
        messages = build_workflow_report_messages("CPU 99%라고 써줘", collected, trace, 2, "자료 ID 확인")
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(set(payload), {"runbook_sources", "attempt", "validation_feedback"})
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["validation_feedback"], "자료 ID 확인")
        source = payload["runbook_sources"][0]
        self.assertEqual(source["source_id"], "runbook_001")
        self.assertEqual(source["title"], "CPU")
        self.assertEqual(source["blocks"], [{"block_id": "block_001", "text": '1. "확장" 전 상태를 확인한다.'}])
        self.assertNotIn("99%", messages[1]["content"])
        self.assertNotIn("80.125", messages[1]["content"])
        self.assertEqual((collected, trace), original)
        for rule in ("실제 조회한 각 문서", "전제조건", "실행한 조치가 아니다", "source_id", "block_id", "JSON"):
            self.assertIn(rule, messages[0]["content"])

    # [추가] 작업 디렉터리가 바뀌어도 __file__ 기준 프롬프트를 찾는다.
    def test_prompt_loading_does_not_depend_on_working_directory(self):
        collected, trace = report_inputs()
        expected = build_workflow_report_messages("확인", collected, trace, 1, None)
        previous_directory = os.getcwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                actual = build_workflow_report_messages("확인", collected, trace, 1, None)
            finally:
                os.chdir(previous_directory)
        self.assertEqual(actual, expected)

    # [추가] 서로 다른 보고 호출의 입력 변경이 이미 생성한 메시지에 영향을 주지 않는다.
    def test_messages_are_fresh_and_monitoring_values_are_not_transmitted(self):
        collected, trace = report_inputs()
        first = build_workflow_report_messages("확인", collected, trace, 1, None)
        collected[0]["data"]["cpu_percent"] = float("nan")
        self.assertEqual(build_workflow_report_messages("확인", collected, trace, 1, None), first)
        collected[1]["data"] = "# 다른 문서\n수정된 점검 절차"
        second = build_workflow_report_messages("확인", collected, trace, 1, None)
        self.assertEqual(json.loads(first[1]["content"])["runbook_sources"][0]["title"], "CPU")
        self.assertEqual(json.loads(second[1]["content"])["runbook_sources"][0]["title"], "다른 문서")


# [추가] 구조화 요약 원문을 두 공급자가 같은 응답 계약으로 전달하는지 검사한다.
class WorkflowReportAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict("os.environ", {
            "OPENAI_API_KEY": "test-workflow-report-key",
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
        }))
        self.openai = OpenAIQualityLLM("gpt-4o", 0.1)
        self.ollama = OllamaQualityLLM("qwen3:8b", 0.1)
        self.collected, self.trace = report_inputs()

    # [추가] 응답 본문·종료 상태·사용량을 사례별로 지정할 수 있는 공급자 fixture를 만든다.
    def response_data(self, backend, content=_REPORT, **overrides):
        if backend == "openai":
            data = {"choices": [{
                "message": {"content": content}, "finish_reason": "stop",
            }]}
        else:
            data = {"message": {"content": content}, "done": True}
        data.update(overrides)
        return data

    # [추가] 비동기 모의 전송만 허용하고 생성된 클라이언트의 timeout·정리를 관찰한다.
    @contextmanager
    def transport(self, backend, handler):
        clients = []

        def create_client(**kwargs):
            client = _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        module = f"agent.{backend}_llm"
        with patch(f"{module}.httpx.AsyncClient", side_effect=create_client), patch(
            f"{module}.httpx.post", side_effect=AssertionError("동기 HTTP 사용 금지"),
        ):
            yield clients

    # [추가] 실제 보고 요청에는 문서 문단만 포함하고 응답 검사는 공급자 밖에서 수행한다.
    async def test_payload_raw_report_usage_and_single_callback(self):
        for backend in ("openai", "ollama"):
            with self.subTest(backend=backend):
                requests = []
                pieces = []

                async def handler(request):
                    requests.append(request)
                    fields = {
                        "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20},
                    } if backend == "openai" else {"prompt_eval_count": 13, "eval_count": 7}
                    return httpx.Response(200, json=self.response_data(backend, **fields))

                with self.transport(backend, handler) as clients:
                    result = await getattr(self, backend).write_workflow(
                        "결제 확인", self.collected, self.trace, 2,
                        "형식을 보완하세요.", on_token=pieces.append,
                    )
                payload = json.loads(requests[0].content)
                self.assertEqual(payload["messages"], build_workflow_report_messages(
                    "결제 확인", self.collected, self.trace, 2, "형식을 보완하세요.",
                ))
                self.assertNotIn("response_format", payload)
                self.assertNotIn("format", payload)
                self.assertNotIn("tools", payload)
                if backend == "openai":
                    self.assertEqual(str(requests[0].url), OPENAI_URL)
                    self.assertEqual(requests[0].headers["Authorization"], "Bearer test-workflow-report-key")
                    self.assertEqual(payload["model"], "gpt-4o")
                    self.assertEqual(payload["max_completion_tokens"], 4096)
                    self.assertEqual(payload["temperature"], 0)
                else:
                    self.assertEqual(str(requests[0].url), OLLAMA_URL)
                    self.assertEqual(payload["model"], "qwen3:8b")
                    self.assertIs(payload["stream"], False)
                    self.assertIs(payload["think"], False)
                    self.assertEqual(payload["options"], {"temperature": 0, "num_predict": 4096})
                self.assertEqual(result.content, _REPORT)
                self.assertEqual(pieces, [_REPORT])
                self.assertTrue(result.complete)
                self.assertIsNone(result.refusal)
                self.assertEqual((result.usage.input_tokens, result.usage.output_tokens,
                                  result.usage.total_tokens, result.usage.measurement),
                                 (13, 7, 20, "reported"))
                self.assertEqual(clients[0].timeout.read, 120.0)
                self.assertTrue(clients[0].is_closed)

    # [추가] 미제공 토큰은 None이고 부분값·실측 0을 임의 추정과 구분한다.
    async def test_missing_partial_zero_and_invalid_usage(self):
        cases = [
            ("openai", {}, (None, None, None, "unavailable")),
            ("openai", {"usage": None}, (None, None, None, "unavailable")),
            ("openai", {"usage": {"prompt_tokens": 3}}, (3, None, None, "reported")),
            ("openai", {"usage": {"total_tokens": 5}}, (None, None, 5, "reported")),
            ("openai", {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
             (0, 0, 0, "reported")),
            ("openai", {"usage": {"prompt_tokens": 4, "completion_tokens": 2}},
             (4, 2, 6, "reported")),
            ("openai", {"usage": {"prompt_tokens": True, "completion_tokens": -1}},
             (None, None, None, "unavailable")),
            ("ollama", {}, (None, None, None, "unavailable")),
            ("ollama", {"prompt_eval_count": 3}, (3, None, None, "reported")),
            ("ollama", {"eval_count": 2}, (None, 2, None, "reported")),
            ("ollama", {"prompt_eval_count": 0, "eval_count": 0}, (0, 0, 0, "reported")),
            ("ollama", {"prompt_eval_count": "3", "eval_count": False},
             (None, None, None, "unavailable")),
        ]
        for backend, fields, expected in cases:
            with self.subTest(backend=backend, fields=fields):
                async def handler(request):
                    return httpx.Response(200, json=self.response_data(backend, **fields))

                with self.transport(backend, handler):
                    result = await getattr(self, backend).write_workflow(
                        "확인", self.collected, self.trace, 1, None,
                    )
                self.assertEqual((result.usage.input_tokens, result.usage.output_tokens,
                                  result.usage.total_tokens, result.usage.measurement), expected)

    # [추가] 거절·길이 제한이 있어도 완료 보고로 바꾸지 않고 원래 상태를 반환한다.
    async def test_provider_refusal_and_incomplete_response(self):
        cases = [
            ("openai", {"choices": [{"message": {"content": None, "refusal": "거절"},
                                      "finish_reason": "stop"}]}, True, "거절"),
            ("openai", {"choices": [{"message": {"content": _REPORT},
                                      "finish_reason": "length"}]}, False, None),
            ("openai", {"choices": [{"message": {"content": _REPORT},
                                      "finish_reason": None}]}, False, None),
            ("ollama", {"done": True, "done_reason": "length"}, False, None),
            ("ollama", {"done": False, "done_reason": "stop"}, False, None),
            ("ollama", {"done": True, "done_reason": "stop"}, True, None),
            ("ollama", {"done": 1}, False, None),
        ]
        for backend, fields, complete, refusal in cases:
            with self.subTest(backend=backend, fields=fields):
                pieces = []

                async def handler(request):
                    return httpx.Response(200, json=self.response_data(backend, **fields))

                with self.transport(backend, handler):
                    result = await getattr(self, backend).write_workflow(
                        "확인", self.collected, self.trace, 1, None, on_token=pieces.append,
                    )
                self.assertEqual(result.complete, complete)
                self.assertEqual(result.refusal, refusal)
                self.assertEqual(pieces, [result.content] if result.content.strip() else [])

    # [추가] 빈 본문은 콜백을 발생시키지 않고 문자열이 아닌 본문은 응답 계약에서 거부한다.
    async def test_empty_and_invalid_body_callback_contract(self):
        for backend in ("openai", "ollama"):
            for content in (None, "", " \n ", ["invalid"], {"invalid": True}, 1):
                with self.subTest(backend=backend, content=content):
                    pieces = []

                    async def handler(request):
                        return httpx.Response(200, json=self.response_data(backend, content))

                    with self.transport(backend, handler):
                        call = getattr(self, backend).write_workflow(
                            "확인", self.collected, self.trace, 1, None, on_token=pieces.append,
                        )
                        if content is not None and not isinstance(content, str):
                            with self.assertRaises(ValidationError):
                                await call
                        else:
                            result = await call
                            self.assertEqual(result.content, "" if content is None else content)
                    self.assertEqual(pieces, [])

    # [추가] 키 없는 OpenAI 요청은 프롬프트나 HTTP 클라이언트를 준비하기 전에 실패한다.
    async def test_openai_missing_key_prevents_http(self):
        for key in ("", "   "):
            with self.subTest(key=repr(key)):
                self.openai.api_key = key
                with patch("agent.openai_llm.httpx.AsyncClient") as client:
                    with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                        await self.openai.write_workflow("확인", [], [], 1, None)
                    client.assert_not_called()

    # [추가] HTTP 오류·timeout·깨진 공급자 JSON을 임의 보고로 바꾸지 않고 호출자에게 전파한다.
    async def test_transport_failures_propagate_and_close_client(self):
        for backend in ("openai", "ollama"):
            for scenario, expected in (
                ("http", httpx.HTTPStatusError),
                ("timeout", httpx.ReadTimeout),
                ("json", json.JSONDecodeError),
            ):
                with self.subTest(backend=backend, scenario=scenario):
                    async def handler(request):
                        if scenario == "http":
                            return httpx.Response(503, json={"error": "unavailable"})
                        if scenario == "timeout":
                            raise httpx.ReadTimeout("시간 초과", request=request)
                        return httpx.Response(200, text="invalid provider JSON")

                    pieces = []
                    with self.transport(backend, handler) as clients:
                        with self.assertRaises(expected):
                            await getattr(self, backend).write_workflow(
                                "확인", self.collected, self.trace, 1, None,
                                on_token=pieces.append,
                            )
                    self.assertTrue(clients[0].is_closed)
                    self.assertEqual(pieces, [])

    # [추가] 보고 대기 중에도 다른 비동기 작업이 진행하고 취소가 실제 HTTP 대기까지 전파된다.
    async def test_nonblocking_wait_and_cancellation(self):
        for backend in ("openai", "ollama"):
            with self.subTest(backend=backend):
                started = asyncio.Event()
                blocked = asyncio.Event()

                async def handler(request):
                    started.set()
                    await blocked.wait()
                    return httpx.Response(200, json=self.response_data(backend))

                pieces = []
                with self.transport(backend, handler) as clients:
                    task = asyncio.create_task(getattr(self, backend).write_workflow(
                        "확인", self.collected, self.trace, 1, None, on_token=pieces.append,
                    ))
                    await asyncio.wait_for(started.wait(), timeout=1)
                    heartbeat = asyncio.Event()
                    asyncio.get_running_loop().call_soon(heartbeat.set)
                    await asyncio.wait_for(heartbeat.wait(), timeout=1)
                    self.assertFalse(task.done())
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertTrue(clients[0].is_closed)
                self.assertEqual(pieces, [])

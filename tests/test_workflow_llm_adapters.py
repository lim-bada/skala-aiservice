# [추가] 외부 API 호출 없이 워크플로 어댑터의 비동기 HTTP·원문·사용량 계약을 검증한다.
"""OpenAI/Ollama 워크플로 생성 전용 어댑터의 경계 테스트."""
import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx

from agent.openai_llm import OPENAI_URL, OpenAIFastLLM
from agent.ollama_llm import OLLAMA_URL, OllamaFastLLM


# [추가] 패치 이전 클라이언트를 보관해 실제 소켓 대신 MockTransport만 사용한다.
_ASYNC_CLIENT = httpx.AsyncClient
_CATALOG = {"systems": ["payments-service"]}
_SCHEMA = {"type": "object", "properties": {"steps": {"type": "array"}}}
_MESSAGES = [
    {"role": "system", "content": "JSON 워크플로를 생성하세요."},
    {"role": "user", "content": "결제 시스템을 확인하세요."},
]


# [추가] 공급자별 응답 형식만 달리하고 공통 전송·취소 조건을 동일하게 검사한다.
class WorkflowLLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-workflow-key"}):
            self.openai = OpenAIFastLLM("gpt-4o-mini", 0.1)
        self.ollama = OllamaFastLLM("qwen3:0.6b", 0.1)

    # [추가] 공급자 응답은 기본 정상 종료로 만들고 각 사례에서 필요한 필드만 덮어쓴다.
    def response_data(self, backend, content='{"steps": []}', **overrides):
        if backend == "openai":
            data = {
                "choices": [{
                    "message": {"content": content},
                    "finish_reason": "stop",
                }],
            }
        else:
            data = {"message": {"content": content}, "done": True}
        data.update(overrides)
        return data

    # [추가] 전달된 HTTP 요청을 기록하고 공통 프롬프트 함수와 전송 자원을 교체한다.
    @contextmanager
    def transport(self, backend, handler):
        clients = []

        def create_client(**kwargs):
            client = _ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        module = f"agent.{backend}_llm"
        with patch(f"{module}.httpx.AsyncClient", side_effect=create_client), patch(
            f"{module}.build_workflow_messages", return_value=_MESSAGES,
        ) as prompting, patch(
            f"{module}.httpx.post", side_effect=AssertionError("동기 HTTP 사용 금지"),
        ):
            yield clients, prompting

    # [추가] OpenAI 요청의 JSON 모드·출력 제한·재작성 문맥·인증을 확인한다.
    async def test_openai_payload_and_reported_usage_preserve_raw_content(self):
        requests = []
        raw = '```json\n{"system":"unknown-service","steps":[]}\n```'

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=self.response_data(
                "openai", raw,
                usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            ))

        errors = [{"code": "UNKNOWN_SYSTEM", "path": "steps.0.system"}]
        previous = '{"broken":'
        with self.transport("openai", handler) as (clients, prompting):
            result = await self.openai.compile_workflow(
                "결제 서버 확인", _CATALOG, _SCHEMA, errors, previous,
            )
        request = requests[0]
        payload = json.loads(request.content)
        self.assertEqual(str(request.url), OPENAI_URL)
        self.assertEqual(request.headers["Authorization"], "Bearer test-workflow-key")
        self.assertEqual(payload, {
            "model": "gpt-4o-mini", "messages": _MESSAGES, "temperature": 0,
            "response_format": {"type": "json_object"}, "max_completion_tokens": 4096,
        })
        prompting.assert_called_once_with(
            "결제 서버 확인", _CATALOG, _SCHEMA,
            errors=errors, previous_response=previous,
        )
        self.assertEqual(result.content, raw)
        self.assertTrue(result.complete)
        self.assertIsNone(result.refusal)
        self.assertEqual(result.usage.model, "openai:gpt-4o-mini")
        self.assertEqual(result.usage.input_tokens, 11)
        self.assertEqual(result.usage.output_tokens, 7)
        self.assertEqual(result.usage.total_tokens, 18)
        self.assertEqual(result.usage.measurement, "reported")
        self.assertEqual(clients[0].timeout.connect, 120.0)
        self.assertTrue(clients[0].is_closed)

    # [추가] Ollama는 동일 스키마와 비추론·출력 제한 옵션을 사용하고 두 카운트를 합산한다.
    async def test_ollama_payload_and_reported_usage_preserve_raw_content(self):
        requests = []
        raw = "아직 JSON이 아닌 모델 원문"

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json=self.response_data(
                "ollama", raw, prompt_eval_count=13, eval_count=5, done_reason="stop",
            ))

        with self.transport("ollama", handler) as (clients, prompting):
            result = await self.ollama.compile_workflow("서버 확인", _CATALOG, _SCHEMA)
        self.assertEqual(str(requests[0].url), OLLAMA_URL)
        self.assertEqual(json.loads(requests[0].content), {
            "model": "qwen3:0.6b", "messages": _MESSAGES,
            "stream": False, "format": _SCHEMA, "think": False,
            "options": {"temperature": 0, "num_predict": 4096},
        })
        prompting.assert_called_once_with(
            "서버 확인", _CATALOG, _SCHEMA, errors=None, previous_response=None,
        )
        self.assertEqual(result.content, raw)
        self.assertTrue(result.complete)
        self.assertEqual(result.usage.model, "ollama:qwen3:0.6b")
        self.assertEqual(result.usage.input_tokens, 13)
        self.assertEqual(result.usage.output_tokens, 5)
        self.assertEqual(result.usage.total_tokens, 18)
        self.assertEqual(result.usage.measurement, "reported")
        self.assertEqual(clients[0].timeout.connect, 120.0)
        self.assertTrue(clients[0].is_closed)

    # [추가] 사용량이 누락·부분 보고되더라도 누락값을 0이나 임의 추정으로 채우지 않는다.
    async def test_usage_missing_partial_zero_and_invalid_counts(self):
        cases = [
            ("openai", {}, (None, None, None, "unavailable")),
            ("openai", {"usage": None}, (None, None, None, "unavailable")),
            ("openai", {"usage": {"prompt_tokens": 3}}, (3, None, None, "reported")),
            ("openai", {"usage": {"total_tokens": 9}}, (None, None, 9, "reported")),
            ("openai", {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
             (0, 0, 0, "reported")),
            ("openai", {"usage": {"prompt_tokens": 4, "completion_tokens": 2}},
             (4, 2, 6, "reported")),
            ("openai", {"usage": {"prompt_tokens": True, "completion_tokens": -1,
                                    "total_tokens": "4"}},
             (None, None, None, "unavailable")),
            ("ollama", {}, (None, None, None, "unavailable")),
            ("ollama", {"prompt_eval_count": 3}, (3, None, None, "reported")),
            ("ollama", {"eval_count": 2}, (None, 2, None, "reported")),
            ("ollama", {"prompt_eval_count": 0, "eval_count": 0}, (0, 0, 0, "reported")),
            ("ollama", {"prompt_eval_count": True, "eval_count": 2.5},
             (None, None, None, "unavailable")),
        ]
        for backend, fields, expected in cases:
            with self.subTest(backend=backend, fields=fields):
                async def handler(request):
                    return httpx.Response(200, json=self.response_data(backend, **fields))

                with self.transport(backend, handler):
                    result = await getattr(self, backend).compile_workflow(
                        "확인", _CATALOG, _SCHEMA,
                    )
                self.assertEqual((
                    result.usage.input_tokens, result.usage.output_tokens,
                    result.usage.total_tokens, result.usage.measurement,
                ), expected)

    # [추가] OpenAI의 거절과 길이 제한은 JSON 파싱에 숨기지 않고 상위 계층에 전달한다.
    async def test_openai_refusal_and_incomplete_finish_reasons(self):
        cases = [
            ("stop", None, True),
            ("length", None, False),
            ("content_filter", "요청을 처리할 수 없습니다.", False),
            (None, None, False),
        ]
        for reason, refusal, complete in cases:
            with self.subTest(reason=reason):
                data = {"choices": [{
                    "message": {"content": None, "refusal": refusal},
                    "finish_reason": reason,
                }]}

                async def handler(request):
                    return httpx.Response(200, json=data)

                with self.transport("openai", handler):
                    result = await self.openai.compile_workflow("확인", _CATALOG, _SCHEMA)
                self.assertEqual(result.content, "")
                self.assertEqual(result.refusal, refusal)
                self.assertEqual(result.complete, complete)

    # [추가] Ollama는 done=True와 정상 종료 사유를 모두 확인한다.
    async def test_ollama_incomplete_done_and_reasons(self):
        cases = [
            ({"done": True}, True),
            ({"done": True, "done_reason": "stop"}, True),
            ({"done": True, "done_reason": "length"}, False),
            ({"done": False, "done_reason": "stop"}, False),
            ({"done": 1, "done_reason": "stop"}, False),
            ({"done": None}, False),
            ({"done": True, "done_reason": None}, False),
        ]
        for fields, complete in cases:
            with self.subTest(fields=fields):
                async def handler(request):
                    return httpx.Response(200, json=self.response_data("ollama", **fields))

                with self.transport("ollama", handler):
                    result = await self.ollama.compile_workflow("확인", _CATALOG, _SCHEMA)
                self.assertEqual(result.complete, complete)

    # [추가] 인증 실패는 네트워크 자원 생성 전에 드러내 실제 요청을 보내지 않는다.
    async def test_missing_api_key_fails_before_http(self):
        for api_key in ("", "   "):
            with self.subTest(api_key=repr(api_key)):
                self.openai.api_key = api_key
                with patch("agent.openai_llm.httpx.AsyncClient") as client:
                    with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                        await self.openai.compile_workflow("확인", _CATALOG, _SCHEMA)
                    client.assert_not_called()

    # [추가] 공급자 오류·네트워크 시간 초과·비JSON 응답을 성공이나 가짜 결과로 바꾸지 않는다.
    async def test_transport_errors_propagate(self):
        for backend in ("openai", "ollama"):
            for scenario in ("http", "timeout", "invalid_json"):
                with self.subTest(backend=backend, scenario=scenario):
                    async def handler(request):
                        if scenario == "http":
                            return httpx.Response(503, json={"error": "unavailable"})
                        if scenario == "timeout":
                            raise httpx.ReadTimeout("응답 시간 초과", request=request)
                        return httpx.Response(200, text="not JSON")

                    expected = {
                        "http": httpx.HTTPStatusError,
                        "timeout": httpx.ReadTimeout,
                        "invalid_json": json.JSONDecodeError,
                    }[scenario]
                    with self.transport(backend, handler) as (clients, _):
                        with self.assertRaises(expected):
                            await getattr(self, backend).compile_workflow(
                                "확인", _CATALOG, _SCHEMA,
                            )
                    self.assertTrue(clients[0].is_closed)

    # [추가] 요청 대기 중에도 이벤트 루프가 진행하고 취소 시 HTTP 자원을 닫는지 검사한다.
    async def test_wait_is_nonblocking_and_cancellation_propagates(self):
        for backend in ("openai", "ollama"):
            with self.subTest(backend=backend):
                started = asyncio.Event()
                never_finishes = asyncio.Event()

                async def handler(request):
                    started.set()
                    await never_finishes.wait()
                    return httpx.Response(200, json=self.response_data(backend))

                with self.transport(backend, handler) as (clients, _):
                    task = asyncio.create_task(getattr(self, backend).compile_workflow(
                        "확인", _CATALOG, _SCHEMA,
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

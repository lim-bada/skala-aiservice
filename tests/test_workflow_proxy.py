# [추가] 실제 네트워크 없이 워크플로 프록시의 상태·원문·SSE·취소 계약을 검증하는 신규 파일 전체.
"""MockTransport와 별도 FastAPI upstream으로 기존 채팅과 새 고정 프록시를 확인한다."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import web_demo_server as proxy


# [추가] 패치 이전 클라이언트로 ASGI·모의 전송만 만들고 실제 소켓을 사용하지 않는다.
_ASYNC_CLIENT = httpx.AsyncClient
_BODY = b'{ "definition_hash": "' + b"a" * 64 + b'" }'


# [추가] UTF-8 분할·중도 오류·연결 대기와 aclose 호출을 재현하는 upstream 바이트 스트림이다.
class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None, *, block=False, close_error=False):
        self.chunks = chunks
        self.error = error
        self.block = block
        self.close_error = close_error
        self.closed = False
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    # [추가] 프록시가 본문을 텍스트로 재조립하지 않고 받은 바이트를 전달하는지 관찰한다.
    async def __aiter__(self):
        try:
            for chunk in self.chunks:
                await asyncio.sleep(0)
                yield chunk
            if self.block:
                self.entered.set()
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    # [추가] 연결 종료 여부와 정리 실패가 최종 결과에 미치는 영향을 검사한다.
    async def aclose(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("private cleanup error")


# [추가] 각 프록시 요청에 독립된 upstream 연결을 주입하고 앱과 자원을 실제로 실행한다.
class WorkflowProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict("os.environ", {
            "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false",
        }))
        self.network = self.enterContext(patch(
            "socket.socket.connect", side_effect=AssertionError("실제 소켓 연결 금지"),
        ))
        self.upstream_requests = []
        self.upstream_clients = []
        self.handler = None
        self.upstream_transport = httpx.MockTransport(self.dispatch)
        self.client = _ASYNC_CLIENT(
            transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy.test",
        )

        # [추가] 프록시가 만든 클라이언트만 가짜 upstream으로 연결한다.
        def create_client(**kwargs):
            client = _ASYNC_CLIENT(transport=self.upstream_transport, **kwargs)
            self.upstream_clients.append(client)
            return client

        self.enterContext(patch("web_demo_server.httpx.AsyncClient", side_effect=create_client))

    # [추가] 실패·취소 사례에도 테스트 자원을 남기지 않는다.
    async def asyncTearDown(self):
        await self.client.aclose()
        for client in self.upstream_clients:
            await client.aclose()
        self.network.assert_not_called()

    # [추가] 모의 HTTP 요청도 실제 method·URL·body를 기록한다.
    async def dispatch(self, request):
        self.upstream_requests.append(request)
        if self.handler is None:
            raise AssertionError("예상하지 않은 upstream 요청")
        return await self.handler(request)

    # [추가] 헤더 이후 전송 오류도 ASGI가 실제 보낸 바이트와 안전한 예외로 관찰한다.
    async def capture_stream_response(self):
        messages = []
        received_body = False
        waiting = asyncio.Event()

        # [추가] 요청 본문 뒤에는 연결을 유지해 서버 스트림의 종료를 기다린다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": _BODY, "more_body": False}
            await waiting.wait()
            return {"type": "http.disconnect"}

        # [추가] Response 시작과 전송 본문을 버퍼링 없이 수집한다.
        async def send(message):
            messages.append(message)

        path = "/workflows/id/execute/stream"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10002), "server": ("proxy.test", 80),
        }
        error = None
        try:
            await proxy.app(scope, receive, send)
        except Exception as caught:
            error = caught
        return messages, error

    # [추가] 정규 API 경로들이 요청 method·raw path·중복 쿼리·body를 그대로 전달하는지 검사한다.
    async def test_explicit_json_routes_preserve_method_path_query_body_and_upstream_status(self):
        raw_response = b'{ "error": {"code":"CHECK", "message":"upstream rejected"} }'

        async def handler(request):
            return httpx.Response(409, content=raw_response, headers={"content-type": "application/problem+json"})

        self.handler = handler
        routes = [
            ("GET", "/workflows/catalog"), ("POST", "/workflows/generate"),
            ("GET", "/workflows"), ("GET", "/workflows/saved-id"),
            ("POST", "/workflows/saved-id/save"), ("POST", "/workflows/saved-id/execute"),
        ]
        for method, path in routes:
            with self.subTest(path=path):
                query = "limit=2&limit=3&label=%ED%95%9C%EA%B8%80&offset=0"
                body = b'{ "query" : "request", "extra":true }' if method == "POST" else b""
                response = await self.client.request(
                    method, path + "?" + query, content=body,
                    headers={"content-type": "application/json; charset=utf-8"},
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.content, raw_response)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
                upstream = self.upstream_requests[-1]
                self.assertEqual(upstream.method, method)
                self.assertEqual(upstream.url.raw_path, (path + "?" + query).encode())
                self.assertEqual(upstream.content, body)
                self.assertEqual(upstream.headers["content-type"], "application/json; charset=utf-8")
        self.assertTrue(all(client.is_closed for client in self.upstream_clients))
        self.assertEqual(self.upstream_clients[0].timeout.connect, 5.0)
        self.assertEqual(self.upstream_clients[0].timeout.read, 140.0)

    # [추가] 별도 FastAPI 백엔드도 같은 ASGI 요청·상태 계약으로 연결한다.
    async def test_proxy_integrates_with_independent_fastapi_backend(self):
        backend = FastAPI()
        received = []

        @backend.post("/workflows/generate")
        async def generate(request: Request):
            body = await request.body()
            received.append((request.method, request.url.path, request.scope["query_string"], body))
            return JSONResponse({"workflow_id": "generated-id", "query": json.loads(body)["query"]}, status_code=201)

        self.upstream_transport = httpx.ASGITransport(app=backend)
        body = '{ "query": "결제 상태 확인" }'.encode()
        response = await self.client.post("/workflows/generate?source=ui&source=retry", content=body,
                                          headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json(), {"workflow_id": "generated-id", "query": "결제 상태 확인"})
        self.assertEqual(received, [("POST", "/workflows/generate", b"source=ui&source=retry", body)])

    # [추가] 도메인 오류·실행 실패·시간 초과의 JSON 상태와 원문을 바꾸지 않는다.
    async def test_json_statuses_and_bodies_are_preserved(self):
        for status in (200, 201, 404, 409, 422, 502, 504):
            with self.subTest(status=status):
                raw = json.dumps({"status": status, "message": "원문 유지"}, ensure_ascii=False).encode()

                async def handler(request):
                    return httpx.Response(status, content=raw, headers={"content-type": "application/json"})

                self.handler = handler
                response = await self.client.post("/workflows/id/execute", content=_BODY)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, raw)

    # [추가] HTML·깨진 JSON·잘못된 UTF-8·비표준 숫자를 정상 JSON API로 전달하지 않는다.
    async def test_invalid_upstream_json_or_content_type_becomes_redacted_502(self):
        for content_type, content in (
            ("text/html", b"private upstream html error"),
            ("application/json", b"private malformed JSON"),
            ("application/json", b'"\xff"'),
            ("application/json", b'{"value":NaN}'),
        ):
            with self.subTest(content_type=content_type, content=content):
                async def handler(request):
                    return httpx.Response(200, content=content, headers={"content-type": content_type})

                self.handler = handler
                response = await self.client.get("/workflows/catalog")
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json()["error"]["code"], "INVALID_UPSTREAM_RESPONSE")
                self.assertNotIn("private", response.text)

    # [추가] 헤더 수신 전 연결 오류·시간 초과는 JSON과 스트림 모두 502·504로 반환한다.
    async def test_initial_connection_errors_are_structured_http_errors(self):
        for path in ("/workflows/id/execute", "/workflows/id/execute/stream"):
            for exception, expected in ((httpx.ConnectError, 502), (httpx.ReadTimeout, 504)):
                with self.subTest(path=path, exception=exception):
                    async def handler(request):
                        raise exception("private transport failure", request=request)

                    self.handler = handler
                    response = await self.client.post(path, content=_BODY)
                    self.assertEqual(response.status_code, expected, response.text)
                    self.assertIn("error", response.json())
                    self.assertNotIn("private transport failure", response.text)
                    self.assertNotIn("text/event-stream", response.headers["content-type"])
                    self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 스트림의 사전 검증 오류는 응답 헤더 전에 JSON 상태·본문 그대로 반환한다.
    async def test_stream_preflight_preserves_error_status_body_and_closes_resources(self):
        for status in (404, 409, 422, 502, 504):
            with self.subTest(status=status):
                raw = json.dumps({"error": {"code": "PRECONDITION", "message": "실행 전 오류"}}, ensure_ascii=False).encode()
                stream = ChunkStream([raw[:4], raw[4:]])

                async def handler(request):
                    return httpx.Response(status, stream=stream, headers={"content-type": "application/json"})

                self.handler = handler
                response = await self.client.post("/workflows/id/execute/stream?mode=one", content=_BODY)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, raw)
                self.assertNotIn("text/event-stream", response.headers["content-type"])
                self.assertTrue(stream.closed)
                self.assertTrue(self.upstream_clients[-1].is_closed)
                self.assertEqual(self.upstream_requests[-1].url.raw_path, b"/workflows/id/execute/stream?mode=one")
                self.assertEqual(self.upstream_requests[-1].content, _BODY)

    # [추가] 성공 상태라도 JSON이나 HTML이 오면 SSE를 시작하지 않는다.
    async def test_stream_invalid_success_content_type_is_502(self):
        for content_type in ("application/json", "text/html"):
            with self.subTest(content_type=content_type):
                stream = ChunkStream([b"private unexpected body"])

                async def handler(request):
                    return httpx.Response(200, stream=stream, headers={"content-type": content_type})

                self.handler = handler
                response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(response.json()["error"]["code"], "INVALID_UPSTREAM_RESPONSE")
                self.assertNotIn("private", response.text)
                self.assertTrue(stream.closed)
                self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 한글·이모지를 한 바이트씩 분할해도 실제 upstream 프레임 원문이 변하지 않는다.
    async def test_stream_preserves_every_byte_across_utf8_and_frame_boundaries(self):
        raw = (
            'data: {"type":"AnswerToken","delta":"한글🙂"}\n\n'
            'data: {"type":"FinalAnswer","final":"진단 완료"}\n\n'
            'data: [DONE]\n\n'
        ).encode()
        stream = ChunkStream([raw[index:index + 1] for index in range(len(raw))])

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream; charset=utf-8"})

        self.handler = handler
        response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, raw)
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertTrue(stream.closed)
        self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 헤더 이후 연결이 끊기면 일반 HTTP 오류 대신 안전한 실패 프레임·완료를 보낸다.
    async def test_midstream_failure_is_redacted_sse_and_resources_close(self):
        first = b'data: {"type":"StepStarted","node_id":"metrics_1"}\n\n'
        stream = ChunkStream([first], httpx.ReadError("private midstream failure"))

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(first))
        events = [frame[6:] for frame in response.text.split("\n\n") if frame.startswith("data: ")]
        self.assertEqual(json.loads(events[-2])["type"], "WorkflowFailed")
        self.assertEqual(json.loads(events[-2])["error"]["code"], "UPSTREAM_STREAM_ERROR")
        self.assertEqual(events[-1], "[DONE]")
        self.assertNotIn("private midstream failure", response.text)
        self.assertTrue(stream.closed)
        self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 미완성 JSON·구분자 없는 최종 JSON을 합성 구분자로 성공 처리하지 않는다.
    async def test_midstream_incomplete_frame_propagates_redacted_error_without_synthesizing_frames(self):
        for partial in (
            b'data: {"type":"AnswerToken","delta":"unfinished',
            b'data: {"type":"FinalAnswer","final":"not delimited"}',
            b'data: {"type":"AnswerToken","delta":"\xed',
        ):
            with self.subTest(partial=partial):
                stream = ChunkStream([partial], httpx.ReadError("private partial frame failure"))

                async def handler(request):
                    return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

                self.handler = handler
                messages, error = await self.capture_stream_response()
                body = b"".join(message.get("body", b"") for message in messages)
                self.assertEqual(body, partial)
                self.assertIsInstance(error, RuntimeError)
                self.assertIn("완료 여부를 확인할 수 없습니다", str(error))
                self.assertNotIn("private partial frame failure", str(error))
                self.assertTrue(error.__suppress_context__)
                self.assertTrue(stream.closed)
                self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 정상 최종 결과 뒤의 전송 오류는 반대 결과를 추가하지 않고 완료만 보충한다.
    async def test_error_after_terminal_or_done_does_not_add_conflicting_terminal(self):
        for ending in (b"\n\n", b"\n\ndata: [DONE]\n\n", b"\r\n\r\ndata: [DONE]\r\n\r\n"):
            with self.subTest(ending=ending):
                raw = b'data: {"type":"FinalAnswer","final":"complete"}' + ending
                stream = ChunkStream([raw], httpx.ReadError("private late stream error"))

                async def handler(request):
                    return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

                self.handler = handler
                response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.content.startswith(raw))
                self.assertNotIn("WorkflowFailed", response.text)
                self.assertEqual(response.text.count("[DONE]"), 1)
                self.assertTrue(stream.closed)
                self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 큰 프레임은 관찰 메모리만 제한하고 정상 upstream 원문 중계는 유지한다.
    async def test_oversized_observer_frame_keeps_raw_relay_and_bounds_retained_memory(self):
        raw = b'data: {"type":"AnswerToken","delta":"' + b"x" * (proxy._MAX_OBSERVED_FRAME_BYTES + 1) + b'"}\n\ndata: [DONE]\n\n'
        observer = proxy._WorkflowTerminalObserver()
        observer.observe(raw)
        self.assertTrue(observer.overflow)
        self.assertEqual(observer.buffer, b"")
        stream = ChunkStream([raw])

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
        self.assertEqual(response.content, raw)
        self.assertTrue(stream.closed)

    # [추가] 관찰 불가 이후 전송 실패는 추정 종료 프레임을 만들지 않는다.
    async def test_failure_after_observer_overflow_propagates_redacted_unknown_outcome(self):
        raw = b"data: " + b"x" * (proxy._MAX_OBSERVED_FRAME_BYTES + 1)
        stream = ChunkStream([raw], httpx.ReadError("private overflow transport failure"))

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        messages, error = await self.capture_stream_response()
        self.assertEqual(b"".join(message.get("body", b"") for message in messages), raw)
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn("private overflow transport failure", str(error))
        self.assertTrue(stream.closed)
        self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 최종 프레임 뒤 aclose 오류가 나도 성공 본문을 실패 프레임으로 덮지 않는다.
    async def test_cleanup_error_does_not_change_completed_frames(self):
        raw = b'data: {"type":"FinalAnswer","final":"done"}\n\ndata: [DONE]\n\n'
        stream = ChunkStream([raw], close_error=True)

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        response = await self.client.post("/workflows/id/execute/stream", content=_BODY)
        self.assertEqual(response.content, raw)
        self.assertTrue(stream.closed)
        self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] GET 실행·정의되지 않은 중계·정적 경로는 backend를 호출하지 않는다.
    async def test_get_execution_and_unregistered_routes_do_not_proxy(self):
        for path in ("/workflows/id/execute", "/workflows/id/execute/stream"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 405, response.text)
        for path in ("/workflows/id/delete", "/proxy/http://example.com", "/web/unknown.js", "/web/%2e%2e/server.py"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.upstream_requests, [])

    # [추가] 화면 파일은 세 개의 고정 파일만 제공하고 쿼리로 다른 파일을 지정할 수 없다.
    async def test_static_assets_are_fixed_and_missing_assets_return_404(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            web = root / "web"
            web.mkdir()
            (root / "private.txt").write_text("private local file")
            with patch.object(proxy, "HERE", root):
                for filename in ("workflow.css", "workflow_stream.js", "workflow.js"):
                    content = f"/* {filename} fixed asset */"
                    (web / filename).write_text(content)
                    response = await self.client.get(f"/web/{filename}?file=../private.txt")
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.text, content)
                    self.assertNotIn("private local file", response.text)
                (web / "workflow.js").unlink()
                missing = await self.client.get("/web/workflow.js")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.json()["error"]["code"], "ASSET_NOT_FOUND")
        self.assertEqual(self.upstream_requests, [])

    # [추가] 기존 health·chat·GET chat stream의 경로와 동작을 유지한다.
    async def test_legacy_health_chat_and_chat_stream_keep_working(self):
        async def handler(request):
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            if request.url.path == "/chat":
                return httpx.Response(200, json={"reply": json.loads(request.content)["message"]})
            self.assertEqual(request.url.path, "/chat/stream")
            self.assertEqual(request.url.params["message"], "기존 채팅")
            self.assertEqual(request.url.params["thread_id"], "legacy-thread")
            return httpx.Response(200, stream=ChunkStream([b"data: [DONE]\n\n"]),
                                  headers={"content-type": "text/event-stream"})

        self.handler = handler
        health = await self.client.get("/health")
        self.assertEqual(health.json(), {"status": "ok"})
        chat = await self.client.post("/chat", json={"message": "기존 요청"})
        self.assertEqual(chat.json(), {"reply": "기존 요청"})
        stream = await self.client.get("/chat/stream", params={"message": "기존 채팅", "thread_id": "legacy-thread"})
        self.assertEqual(stream.content, b"data: [DONE]\n\n")

    # [추가] 상태 확인·일반 JSON 진단의 타임아웃과 연결 오류를 ASGI 500으로 바꾸지 않는다.
    async def test_legacy_json_proxy_maps_network_errors_without_private_details(self):
        for path in ("/health", "/chat"):
            for error_type, status, code in (
                (httpx.ReadTimeout, 504, "UPSTREAM_TIMEOUT"),
                (httpx.ConnectError, 502, "UPSTREAM_UNAVAILABLE"),
            ):
                with self.subTest(path=path, error=error_type.__name__):
                    async def handler(request):
                        raise error_type("private upstream details", request=request)

                    self.handler = handler
                    response = await (self.client.get(path) if path == "/health" else
                                      self.client.post(path, json={"message": "진단"}))
                    self.assertEqual(response.status_code, status)
                    self.assertEqual(response.json()["error"]["code"], code)
                    self.assertNotIn("private", response.text)
                    self.assertTrue(self.upstream_clients[-1].is_closed)

    # [추가] 잘못된 upstream JSON도 traceback 대신 정해진 오류로 표시한다.
    async def test_legacy_json_proxy_rejects_non_json_upstream(self):
        async def handler(request):
            return httpx.Response(200, text="private HTML page")

        self.handler = handler
        for path in ("/health", "/chat"):
            response = await (self.client.get(path) if path == "/health" else
                              self.client.post(path, json={"message": "진단"}))
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["error"]["code"], "INVALID_UPSTREAM_RESPONSE")
            self.assertNotIn("private", response.text)

    # [추가] 브라우저 요청 취소는 진행 중 upstream 읽기와 response·client 정리까지 전파된다.
    async def test_stream_request_cancellation_closes_upstream_response_and_client(self):
        stream = ChunkStream([b'data: {"type":"StepStarted"}\n\n'], block=True)

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        received_body = False
        waiting = asyncio.Event()
        started_sent = asyncio.Event()
        sent = []

        # [추가] ASGITransport의 전체 응답 버퍼링 대신 열린 브라우저 요청을 직접 유지한다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": _BODY, "more_body": False}
            await waiting.wait()
            return {"type": "http.disconnect"}

        # [추가] 첫 진행 프레임이 전달된 이후 연결 취소를 주입한다.
        async def send(message):
            sent.append(message)
            if b"StepStarted" in message.get("body", b""):
                started_sent.set()

        path = "/workflows/id/execute/stream"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10001), "server": ("proxy.test", 80),
        }
        task = asyncio.create_task(proxy.app(scope, receive, send))
        try:
            await asyncio.wait_for(stream.entered.wait(), timeout=1)
            await asyncio.wait_for(started_sent.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stream.cancelled.is_set())
            self.assertTrue(stream.closed)
            self.assertTrue(self.upstream_clients[-1].is_closed)
            self.assertFalse(any(b"WorkflowFailed" in message.get("body", b"") for message in sent))
            self.assertFalse(any(b"[DONE]" in message.get("body", b"") for message in sent))
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] 실제 ASGI disconnect의 AnyIO 취소 범위에서도 await가 필요한 연결 정리를 완료한다.
    async def test_asgi_disconnect_shields_upstream_cleanup(self):
        # [추가] 정리에 비동기 대기가 있어야 취소 범위에 의한 자원 누수를 재현할 수 있다.
        class AwaitCloseStream(ChunkStream):
            async def aclose(self):
                await asyncio.sleep(0)
                await super().aclose()

        stream = AwaitCloseStream([b'data: {"type":"StepStarted"}\n\n'], block=True)

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        received_body = False
        disconnected = asyncio.Event()
        step_sent = asyncio.Event()

        # [추가] 브라우저 연결 종료 이벤트를 Starlette에 전달해 자체 취소 경로를 실행한다.
        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": _BODY, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if b"StepStarted" in message.get("body", b""):
                step_sent.set()

        path = "/workflows/id/execute/stream"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10003), "server": ("proxy.test", 80),
        }
        task = asyncio.create_task(proxy.app(scope, receive, send))
        try:
            await asyncio.wait_for(stream.entered.wait(), timeout=1)
            await asyncio.wait_for(step_sent.wait(), timeout=1)
            disconnected.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(stream.cancelled.is_set())
            self.assertTrue(stream.closed)
            self.assertTrue(self.upstream_clients[-1].is_closed)
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    # [추가] relay가 yield한 뒤 브라우저 전송에서 끊겨도 upstream 응답·클라이언트를 닫는다.
    async def test_disconnect_during_http_send_closes_relay_and_upstream_resources(self):
        stream = ChunkStream([b'data: {"type":"StepStarted"}\n\n'], block=True)

        async def handler(request):
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        self.handler = handler
        received_body = False
        disconnected = asyncio.Event()
        sending = asyncio.Event()

        async def receive():
            nonlocal received_body
            if not received_body:
                received_body = True
                return {"type": "http.request", "body": _BODY, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        # [추가] send 대기 중 취소는 relay.__anext__ 내부 취소와 다른 정리 경계를 거친다.
        async def send(message):
            if b"StepStarted" in message.get("body", b""):
                sending.set()
                await asyncio.Event().wait()

        path = "/workflows/id/execute/stream"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 10004), "server": ("proxy.test", 80),
        }
        task = asyncio.create_task(proxy.app(scope, receive, send))
        try:
            await asyncio.wait_for(sending.wait(), timeout=1)
            disconnected.set()
            await asyncio.wait_for(task, timeout=1)
            self.assertTrue(stream.closed)
            self.assertTrue(self.upstream_clients[-1].is_closed)
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

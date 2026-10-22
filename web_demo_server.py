"""web_demo_server.py — 채팅과 조건부 진단 워크플로를 같은 출처에서 제공하는 데모.

# [수정] 기존 채팅 중계에 워크플로의 제한된 API·정적 파일 중계를 추가한다.
Vue 3(CDN 글로벌 빌드) 화면과 지정된 워크플로 정적 자산을 제공하고,
기존 채팅 및 /workflows의 명시된 API만 8100으로 중계한다.
워크플로 JSON 상태·본문과 SSE 응답 전 오류를 보존하며, 스트림 도중 취소는
upstream 연결까지 정리한다. 임의 URL·임의 정적 파일을 여는 프록시는 제공하지 않는다.

브라우저의 CORS 제약 없이 실제 에이전트와 대화할 수 있게 해주는 아주 얇은 레이어다.
server.py는 평소처럼(fake/ollama/openai 아무 백엔드로나) 8100에 미리 띄워 놓아야 한다.

실행:
    python3 web_demo_server.py        # http://localhost:8600 에서 채팅 화면 오픈
"""
# [추가] 워크플로 스트림 취소와 안전한 오류 JSON을 처리한다.
import asyncio
import json
from pathlib import Path

# [추가] Starlette의 disconnect 취소 범위에서도 upstream 정리 대기를 보호한다.
import anyio
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

BACKEND = "http://127.0.0.1:8100"
HERE = Path(__file__).parent

app = FastAPI()


@app.get("/")
async def index():
    return FileResponse(HERE / "web_chat.html")


@app.get("/health")
async def proxy_health():
    # [수정] 상태 확인 실패를 미처리 ASGI 500 대신 원인을 구분한 JSON으로 전달한다.
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{BACKEND}/health")
        return _workflow_json_response(r)
    except httpx.TimeoutException:
        return _workflow_proxy_error("UPSTREAM_TIMEOUT", "백엔드 상태 확인 시간이 초과됐습니다.", 504)
    except httpx.RequestError:
        return _workflow_proxy_error("UPSTREAM_UNAVAILABLE", "백엔드에 연결할 수 없습니다.", 502)


@app.post("/chat")
async def proxy_chat(req: Request):
    body = await req.body()
    # [수정] 일반 JSON 진단도 통신 실패가 서버 예외로 노출되지 않게 처리한다.
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{BACKEND}/chat", content=body,
                                  headers={"content-type": "application/json"})
        return _workflow_json_response(r)
    except httpx.TimeoutException:
        return _workflow_proxy_error("UPSTREAM_TIMEOUT", "백엔드 진단 응답 시간이 초과됐습니다.", 504)
    except httpx.RequestError:
        return _workflow_proxy_error("UPSTREAM_UNAVAILABLE", "백엔드에 연결할 수 없습니다.", 502)


@app.get("/chat/stream")
async def proxy_stream(message: str, thread_id: str = "web-demo"):
    async def relay():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET", f"{BACKEND}/chat/stream",
                params={"message": message, "thread_id": thread_id},
            ) as upstream:
                async for chunk in upstream.aiter_bytes():
                    yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")


# [추가] 백엔드의 120초 실행 제한과 사전 검증을 기다리되 연결 실패는 빨리 구분한다.
WORKFLOW_PROXY_TIMEOUT = httpx.Timeout(140.0, connect=5.0)
_WORKFLOW_TERMINALS = {"FinalAnswer", "WorkflowFailed"}
# [추가] 정상 raw 중계와 별개인 종료 관찰기가 과도한 프레임을 메모리에 쌓지 않게 한다.
_MAX_OBSERVED_FRAME_BYTES = 1024 * 1024


# [추가] 연결·형식 오류에 내부 URL·예외·응답 본문을 노출하지 않는 공통 오류를 반환한다.
def _workflow_proxy_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": None}},
        status_code=status_code,
    )


# [추가] 요청의 인코딩된 경로·중복 쿼리·JSON 원문을 다시 해석하지 않고 전달한다.
def _workflow_target(request: Request) -> httpx.URL:
    raw_path = request.scope["raw_path"]
    query = request.scope["query_string"]
    # [추가] HTTPX의 raw_path가 query를 덮어쓰므로 인코딩된 쿼리를 함께 전달한다.
    return httpx.URL(BACKEND).copy_with(raw_path=raw_path + (b"?" + query if query else b""))


# [추가] 고정 라우트라도 점 경로가 HTTP 클라이언트 정규화로 다른 API가 되지 못하게 한다.
def _workflow_path_allowed(request: Request) -> bool:
    return request.path_params.get("workflow_id") not in (".", "..")


# [추가] 요청 표현에 필요한 헤더만 전달하고 사용자 지정 upstream 주소는 받지 않는다.
def _workflow_headers(request: Request) -> dict[str, str]:
    return {
        key: request.headers[key]
        for key in ("content-type", "accept")
        if key in request.headers
    }


# [추가] JSON이 아닌 성공·실패 페이지를 JSON API처럼 전달하지 않고 502로 구분한다.
def _workflow_json_response(upstream: httpx.Response) -> Response:
    content_type = upstream.headers.get("content-type", "")
    mime = content_type.split(";", 1)[0].strip().lower()
    is_json = mime == "application/json" or (mime.startswith("application/") and mime.endswith("+json"))
    if not is_json:
        return _workflow_proxy_error("INVALID_UPSTREAM_RESPONSE", "백엔드 응답 형식을 확인할 수 없습니다.", 502)
    try:
        # [추가] 표준 JSON이 아닌 NaN·Infinity나 잘못된 UTF-8도 오류 응답으로 분리한다.
        def reject_constant(value):
            raise ValueError("비표준 JSON 숫자")

        json.loads(upstream.content.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        return _workflow_proxy_error("INVALID_UPSTREAM_RESPONSE", "백엔드 JSON 응답을 읽을 수 없습니다.", 502)
    return Response(
        content=upstream.content, status_code=upstream.status_code,
        headers={"Content-Type": content_type},
    )


# [추가] 모든 자원의 정리를 시도하되 정리 오류로 이미 확정한 결과를 바꾸지 않는다.
async def _close_workflow_upstream(upstream, client) -> None:
    cancelled = None
    # [추가] ASGI 2.3 disconnect의 level cancellation이 aclose 자체를 반복 취소하지 않게 한다.
    with anyio.CancelScope(shield=True):
        for resource in (upstream, client):
            if resource is not None:
                try:
                    await resource.aclose()
                except asyncio.CancelledError as error:
                    # [추가] 취소 중에도 나머지 자원 정리를 시도한 뒤 원래 취소를 전파한다.
                    cancelled = error
                except Exception:
                    # [추가] 전송 결과는 보존하고 다음 자원의 정리도 계속 시도한다.
                    pass
    if cancelled is not None:
        raise cancelled


# [추가] 일반 워크플로 API는 원문 body·쿼리와 백엔드 JSON 상태를 보존한다.
async def _proxy_workflow_json(request: Request):
    if not _workflow_path_allowed(request):
        return _workflow_proxy_error("WORKFLOW_NOT_FOUND", "워크플로 경로를 찾을 수 없습니다.", 404)
    try:
        async with httpx.AsyncClient(timeout=WORKFLOW_PROXY_TIMEOUT) as client:
            upstream = await client.request(
                request.method, _workflow_target(request), content=await request.body(),
                headers=_workflow_headers(request),
            )
        return _workflow_json_response(upstream)
    except httpx.TimeoutException:
        return _workflow_proxy_error("UPSTREAM_TIMEOUT", "백엔드 응답 제한 시간을 초과했습니다.", 504)
    except httpx.RequestError:
        return _workflow_proxy_error("UPSTREAM_UNAVAILABLE", "백엔드에 연결할 수 없습니다.", 502)


# [추가] 출력 바이트를 바꾸지 않고 완성된 SSE 프레임의 종료 여부만 옆에서 관찰한다.
class _WorkflowTerminalObserver:
    def __init__(self):
        self.buffer = b""
        self.terminal = False
        self.done = False
        self.overflow = False
        self.metadata = {}

    # [추가] UTF-8 문자가 청크 사이에서 잘려도 완성된 프레임에서만 JSON을 읽는다.
    def observe(self, chunk: bytes) -> None:
        if self.overflow:
            return
        self.buffer += chunk
        while True:
            boundaries = [
                (position, separator)
                for separator in (b"\n\n", b"\r\n\r\n")
                if (position := self.buffer.find(separator)) >= 0
            ]
            if not boundaries:
                if len(self.buffer) > _MAX_OBSERVED_FRAME_BYTES:
                    self.overflow = True
                    self.buffer = b""
                return
            position, separator = min(boundaries, key=lambda item: item[0])
            if position > _MAX_OBSERVED_FRAME_BYTES:
                self.overflow = True
                self.buffer = b""
                return
            frame, self.buffer = self.buffer[:position], self.buffer[position + len(separator):]
            data = b"\n".join(
                line[5:].lstrip(b" ") for line in frame.splitlines() if line.startswith(b"data:")
            )
            if data == b"[DONE]":
                self.done = True
                continue
            try:
                event = json.loads(data)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if isinstance(event, dict):
                for key in ("run_id", "workflow_id", "definition_hash"):
                    if isinstance(event.get(key), str) and event[key]:
                        self.metadata[key] = event[key]
                if event.get("type") in _WORKFLOW_TERMINALS:
                    self.terminal = True


# [추가] 진행 중 전송 실패를 SSE로 알리되 이미 받은 확정 결과와 완료 신호를 중복하지 않는다.
async def _relay_workflow_stream(upstream: httpx.Response, client: httpx.AsyncClient):
    observer = _WorkflowTerminalObserver()
    try:
        async for chunk in upstream.aiter_bytes():
            observer.observe(chunk)
            yield chunk
    except asyncio.CancelledError:
        raise
    except Exception:
        # [추가] 불완전 프레임에 구분자를 합성해 미확정 결과를 확정시키지 않는다.
        # 관찰 한도를 넘긴 경우도 결과를 추측하지 않고 reader가 완료 확인 불가로 처리하게 한다.
        if observer.buffer or observer.overflow:
            raise RuntimeError("백엔드 진행 스트림의 완료 여부를 확인할 수 없습니다.") from None
        if not observer.terminal and not observer.done:
            failure = {
                **observer.metadata,
                "type": "WorkflowFailed", "status": "failed", "report_valid": False,
                "error": {
                    "code": "UPSTREAM_STREAM_ERROR",
                    "message": "백엔드 진행 스트림 연결이 중단됐습니다.",
                },
            }
            yield ("data: " + json.dumps(failure, ensure_ascii=False) + "\n\n").encode("utf-8")
        if not observer.done:
            yield b"data: [DONE]\n\n"
    finally:
        await _close_workflow_upstream(upstream, client)


# [추가] 브라우저 전송 중 끊겨도 응답이 가진 relay를 닫아 upstream 연결을 해제한다.
class _WorkflowProxyStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()


# [추가] SSE를 시작하기 전에 upstream 상태와 콘텐츠 형식을 검사해 409·422 등을 보존한다.
async def _proxy_workflow_stream(request: Request):
    if not _workflow_path_allowed(request):
        return _workflow_proxy_error("WORKFLOW_NOT_FOUND", "워크플로 경로를 찾을 수 없습니다.", 404)
    client = httpx.AsyncClient(timeout=WORKFLOW_PROXY_TIMEOUT)
    upstream = None
    handed_off = False
    try:
        upstream = await client.send(client.build_request(
            request.method, _workflow_target(request), content=await request.body(),
            headers=_workflow_headers(request),
        ), stream=True)
        content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not 200 <= upstream.status_code < 300:
            await upstream.aread()
            return _workflow_json_response(upstream)
        if content_type != "text/event-stream":
            return _workflow_proxy_error("INVALID_UPSTREAM_RESPONSE", "백엔드 스트림 응답 형식을 확인할 수 없습니다.", 502)
        handed_off = True
        return _WorkflowProxyStreamingResponse(
            _relay_workflow_stream(upstream, client), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except httpx.TimeoutException:
        return _workflow_proxy_error("UPSTREAM_TIMEOUT", "백엔드 응답 제한 시간을 초과했습니다.", 504)
    except httpx.RequestError:
        return _workflow_proxy_error("UPSTREAM_UNAVAILABLE", "백엔드에 연결할 수 없습니다.", 502)
    finally:
        if not handed_off:
            await _close_workflow_upstream(upstream, client)


# [추가] 임의 경로를 받지 않고 알려진 워크플로 API의 메서드·경로만 등록한다.
app.add_api_route("/workflows/catalog", _proxy_workflow_json, methods=["GET"])
app.add_api_route("/workflows/generate", _proxy_workflow_json, methods=["POST"])
app.add_api_route("/workflows", _proxy_workflow_json, methods=["GET"])
app.add_api_route("/workflows/{workflow_id}", _proxy_workflow_json, methods=["GET"])
app.add_api_route("/workflows/{workflow_id}/save", _proxy_workflow_json, methods=["POST"])
app.add_api_route("/workflows/{workflow_id}/execute", _proxy_workflow_json, methods=["POST"])
app.add_api_route("/workflows/{workflow_id}/execute/stream", _proxy_workflow_stream, methods=["POST"])


# [추가] UI가 사용하는 세 파일만 고정 경로로 제공하고 파일이 없으면 404로 알린다.
def _workflow_asset(filename: str, media_type: str):
    path = HERE / "web" / filename
    if not path.is_file():
        return _workflow_proxy_error("ASSET_NOT_FOUND", "화면 파일을 찾을 수 없습니다.", 404)
    return FileResponse(path, media_type=media_type)


# [추가] 워크플로 전용 스타일을 같은 origin에서 제공한다.
@app.get("/web/workflow.css")
async def workflow_css():
    return _workflow_asset("workflow.css", "text/css")


# [추가] 바이트 단위 POST 스트림 해석기를 같은 origin에서 제공한다.
@app.get("/web/workflow_stream.js")
async def workflow_stream_js():
    return _workflow_asset("workflow_stream.js", "text/javascript")


# [추가] 워크플로 화면 로직을 같은 origin에서 제공한다.
@app.get("/web/workflow.js")
async def workflow_js():
    return _workflow_asset("workflow.js", "text/javascript")


if __name__ == "__main__":
    print("브라우저에서 http://localhost:8600 을 열어보세요 (server.py가 8100에 떠 있어야 합니다)")
    uvicorn.run(app, host="0.0.0.0", port=8600)

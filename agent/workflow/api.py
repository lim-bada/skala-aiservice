# [추가] 워크플로 생성·저장·실행·POST 스트림 API를 기존 채팅과 분리하는 신규 파일 전체.
"""준비 검증은 HTTP 응답 전에 수행하고 스트림 구독 하나가 실행 하나만 소비한다."""
import asyncio
import json
from typing import Annotated

# [추가] ASGI disconnect 취소 범위에서도 서비스 제너레이터 해제를 끝까지 기다린다.
import anyio
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .models import WorkflowGenerated, WorkflowRecord, WorkflowRunResult
from .service import WorkflowService, WorkflowServiceError


# [추가] API 입력에서 숫자→문자열 변환과 명세에 없는 추가 필드를 허용하지 않는다.
class _WorkflowRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


# [추가] 원문 길이를 모델 호출 전에 제한하고 지원 여부는 서비스 컴파일러에서 판단한다.
class WorkflowGenerateRequest(_WorkflowRequest):
    query: str = Field(min_length=1, max_length=2000)


# [추가] 저장 이름은 표시 전용이며 공백만 있는 이름을 거부한다.
class WorkflowSaveRequest(_WorkflowRequest):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
    ]


# [추가] 미리보기에서 받은 불변 정의 해시를 실행 전에 반드시 제출하게 한다.
class WorkflowExecuteRequest(_WorkflowRequest):
    definition_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


# [추가] lifespan에서 구성한 서비스를 꺼내며 준비되지 않은 서버를 503으로 구분한다.
def _workflow_service(request: Request) -> WorkflowService:
    service = getattr(request.app.state, "workflow_service", None)
    if service is None:
        raise WorkflowServiceError(
            status_code=503, code="WORKFLOW_UNAVAILABLE",
            message="워크플로 서비스를 사용할 수 없습니다.", details=None,
        )
    return service


# [추가] 정상·실행 실패 모두 같은 실행 결과 계약을 유지하면서 HTTP 상태만 구분한다.
def _execution_response(result: WorkflowRunResult):
    if result.status == "completed":
        return result
    status_code = 504 if result.error and result.error.code == "WORKFLOW_TIMEOUT" else 502
    return JSONResponse(status_code=status_code, content=jsonable_encoder(result))


# [추가] 이벤트 하나를 UTF-8 SSE 프레임 하나로 직렬화한다.
def _sse(event: dict) -> str:
    return "data: " + json.dumps(
        jsonable_encoder(event), ensure_ascii=False, allow_nan=False,
    ) + "\n\n"


# [추가] 서비스 제너레이터의 소비·close를 같은 Task에 묶어 ASGI level cancellation과 분리한다.
async def _produce_stream_events(source, queue: asyncio.Queue) -> None:
    try:
        try:
            async for event in source:
                await queue.put(("event", event))
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        # [추가] 취소 중 정리 오류가 빈 큐 소비자를 기다리며 요청 종료를 막지 않게 한다.
        if asyncio.current_task().cancelling():
            raise
        await queue.put(("error", error))
    else:
        await queue.put(("end", None))


# [추가] 요청 종료 후 남는 작업 없이 준비된 실행을 한 번만 소비하고 해제·취소를 전파한다.
async def _stream_events(service: WorkflowService, prepared):
    producer = None
    terminal_seen = False
    # [추가] 첫 이벤트 전 오류도 준비된 정의에 연결하고 이후 이벤트의 실행 ID를 보존한다.
    record = prepared.record
    metadata = {"workflow_id": record.workflow_id, "definition_hash": record.definition_hash}
    try:
        try:
            source = service.stream_prepared(prepared)
            # [추가] 큐 하나만 선행시키고 요청 종료 전에 반드시 cancel·join하는 요청 전용 Task다.
            queue = asyncio.Queue(maxsize=1)
            producer = asyncio.create_task(
                _produce_stream_events(source, queue), name="workflow-sse-producer",
            )
            while True:
                kind, event = await queue.get()
                if kind == "end":
                    break
                if kind == "error":
                    raise event
                # [추가] 확정 결과 이후의 중복 종료·진행 이벤트는 소비자 상태를 바꾸지 않는다.
                if terminal_seen:
                    continue
                if not isinstance(event, dict):
                    raise TypeError("워크플로 이벤트는 객체여야 합니다.")
                for key in ("run_id", "workflow_id", "definition_hash"):
                    if isinstance(event.get(key), str) and event[key]:
                        metadata[key] = event[key]
                frame = _sse(event)
                terminal_seen = event.get("type") in ("FinalAnswer", "WorkflowFailed")
                yield frame
        finally:
            if producer is not None:
                # [추가] Starlette의 반복 취소를 막고 생산자에 한 번만 취소를 전파한 뒤 종료를 기다린다.
                with anyio.CancelScope(shield=True):
                    cancelled_for_cleanup = not producer.done()
                    if cancelled_for_cleanup:
                        producer.cancel()
                    try:
                        await producer
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        # [추가] 취소 정리 중 예외가 원래 취소를 덮거나 원문 오류로 새지 않게 한다.
                        if not cancelled_for_cleanup:
                            raise
    except asyncio.CancelledError:
        raise
    except Exception:
        # [추가] 응답 헤더 전송 후에는 HTTP 오류 대신 원문 예외를 숨긴 실패 이벤트를 보낸다.
        # 이미 전달한 확정 결과가 있으면 정리 오류로 두 번째 종료 결과를 만들지 않는다.
        if not terminal_seen:
            yield _sse({
                **metadata,
                "type": "WorkflowFailed", "status": "failed", "report_valid": False,
                "error": {
                    "code": "WORKFLOW_STREAM_ERROR",
                    "message": "워크플로 진행 스트림을 처리하지 못했습니다.",
                },
            })
    yield "data: [DONE]\n\n"


# [추가] HTTP send 중 연결이 끊겨도 응답이 소유한 제너레이터를 요청 종료 전에 닫는다.
class _WorkflowStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                with anyio.CancelScope(shield=True):
                    await close()


# [추가] FastAPI 앱에 라우터와 서비스 오류 응답을 등록하고 server.py를 직접 불러오지 않는다.
def register_workflow_api(app: FastAPI) -> None:
    router = APIRouter(prefix="/workflows", tags=["workflows"])

    # [추가] 저장·카탈로그·모델 오류를 동일한 공개 오류 구조로 변환한다.
    @app.exception_handler(WorkflowServiceError)
    async def workflow_service_error_handler(request: Request, error: WorkflowServiceError):
        return JSONResponse(
            status_code=error.status_code,
            content=jsonable_encoder({"error": {
                "code": error.code, "message": error.message, "details": error.details,
            }}),
        )

    # [추가] 동적 카탈로그를 화면과 모델 입력에서 같은 형태로 조회한다.
    @router.get("/catalog")
    async def get_catalog(service: WorkflowService = Depends(_workflow_service)):
        return await service.get_catalog()

    # [추가] 자연어 생성은 MCP를 실행하지 않고 검증된 초안과 생성 사용량을 반환한다.
    @router.post("/generate", response_model=WorkflowGenerated, status_code=201)
    async def generate(
        body: WorkflowGenerateRequest, service: WorkflowService = Depends(_workflow_service),
    ):
        return await service.generate(body.query)

    # [추가] 기본 목록에는 저장한 절차만 제공하고 초안 포함 여부·페이지 범위를 명시한다.
    @router.get("")
    async def list_workflows(
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        include_drafts: bool = Query(default=False),
        service: WorkflowService = Depends(_workflow_service),
    ):
        return {
            "workflows": await service.list_workflows(
                limit=limit, offset=offset, include_drafts=include_drafts,
            ),
            "limit": limit, "offset": offset,
        }

    # [추가] 미리보기에서는 저장된 원본 정의·해시를 그대로 읽는다.
    @router.get("/{workflow_id}", response_model=WorkflowRecord)
    async def get_workflow(workflow_id: str, service: WorkflowService = Depends(_workflow_service)):
        return await service.get_workflow(workflow_id)

    # [추가] 표시 이름을 지정해 재사용 목록에 등록하되 실행 정의는 바꾸지 않는다.
    @router.post("/{workflow_id}/save", response_model=WorkflowRecord)
    async def save_workflow(
        workflow_id: str, body: WorkflowSaveRequest,
        service: WorkflowService = Depends(_workflow_service),
    ):
        return await service.save_workflow(workflow_id, body.name)

    # [추가] 조회와 보고를 한 번 실행하고 실패 종류에 따라 502·504를 반환한다.
    @router.post("/{workflow_id}/execute", response_model=WorkflowRunResult)
    async def execute_workflow(
        workflow_id: str, body: WorkflowExecuteRequest,
        service: WorkflowService = Depends(_workflow_service),
    ):
        return _execution_response(await service.execute(workflow_id, body.definition_hash))

    # [추가] 저장 정의·해시·현재 자원을 먼저 검사해 잘못된 실행은 스트림 시작 전에 거절한다.
    @router.post("/{workflow_id}/execute/stream")
    async def stream_workflow(
        workflow_id: str, body: WorkflowExecuteRequest,
        service: WorkflowService = Depends(_workflow_service),
    ):
        prepared = await service.prepare_execution(workflow_id, body.definition_hash)
        return _WorkflowStreamingResponse(
            _stream_events(service, prepared), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(router)

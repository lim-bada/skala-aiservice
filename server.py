"""all_total 통합 실습 — FastAPI 백엔드 (Wave 1·3·12·14 축 + 전체 조립).

- Wave 1  : lifespan에서 Agent 번들(MCP 연결·LLM·그래프)을 프로세스당 정확히 1회 생성,
            app.state로 모든 요청이 공유 (creation_count/instance_id로 증명)
- Wave 2  : mcp_server.py를 subprocess(stdio)로 기동해 연결 유지
- Wave 3  : GET /chat/stream — 그래프 실행 과정을 SSE 이벤트로 스트리밍
- Wave 12 : 대화 이력을 SQLite(session.db)에 저장 — 재시작해도 유지
- Wave 14 : SUPERVISOR_VERSION(v1/v2) 환경변수로 legacy/신형 파이프라인 즉시 전환
- LangSmith: 요청당 chat_request 루트 run 아래로 노드·LLM·MCP 호출이 계층 기록됨
"""
import asyncio  # [추가] 워크플로 DB 종료가 이벤트 루프를 막지 않도록 한다.
import json
import os
# [수정] 새 저장소를 포함해 초기화 중 실패한 자원도 역순으로 정리한다.
from contextlib import AsyncExitStack, asynccontextmanager

from dotenv import load_dotenv

load_dotenv()   # LANGCHAIN_TRACING_V2 / API_KEY / PROJECT — import보다 먼저

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel

from agent.deps import SupervisorDeps
from agent.fake_llm import FastLLM, QualityLLM
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.session_store import SqliteSessionStore
from agent.supervisor_v1 import run_v1
# [추가] 기존 채팅 경로와 별도로 워크플로 서비스와 전용 API를 등록한다.
from agent.workflow.api import register_workflow_api
from agent.workflow.service import WorkflowService
from agent.workflow.store import SqliteWorkflowStore

CREATION_COUNT = 0   # Wave 1 — 프로세스 내 Agent 번들 생성 횟수 (항상 1이어야 정상)


class ChatRequest(BaseModel):   # Wave 1 — Pydantic 요청 검증
    message: str
    thread_id: str = "default"


def _build_llms():
    """LLM_BACKEND=fake(기본, 결정론) | ollama(로컬 실모델) | openai(실제 OpenAI API)."""
    backend = os.environ.get("LLM_BACKEND", "fake")
    if backend == "ollama":
        from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
        fast = OllamaFastLLM(os.environ.get("OLLAMA_FAST_MODEL", "qwen3:0.6b"),
                             usd_per_1k_tokens=0.00015)
        quality = OllamaQualityLLM(os.environ.get("OLLAMA_QUALITY_MODEL", "qwen3:8b"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"),
                             usd_per_1k_tokens=0.00015)
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"),
                                   usd_per_1k_tokens=0.005)
        return backend, fast, quality
    fast = FastLLM("fast-model-stub", usd_per_1k_tokens=0.00015)
    quality = QualityLLM("quality-model-stub", usd_per_1k_tokens=0.005)
    return backend, fast, quality


# [추가] 로컬 워크플로의 엄격한 절차 생성을 일반 진단용 소형 모델과 분리한다.
def _build_workflow_llm(backend, fast_llm, quality_llm):
    """Ollama만 전용 생성 모델을 고르고 기존 fake/OpenAI 모델 선택은 유지한다."""
    if backend != "ollama":
        return fast_llm
    from agent.ollama_llm import OllamaFastLLM

    # [추가] 명시 설정을 우선하고, 없으면 실제 보고 모델의 설정을 따르며 빈 설정은 건너뛴다.
    configured = os.environ.get("OLLAMA_WORKFLOW_MODEL", "").strip()
    quality_model = getattr(quality_llm, "model", "") or os.environ.get("OLLAMA_QUALITY_MODEL", "")
    model = configured or quality_model.strip() or "qwen3:8b"
    # [추가] compile_workflow 계약은 Fast 어댑터가 제공하며 모델 크기는 별도로 선택한다.
    return OllamaFastLLM(model, usd_per_1k_tokens=0.00015)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CREATION_COUNT
    CREATION_COUNT += 1
    # [수정] 생성 중 예외와 정상 종료 모두에서 MCP·대화 DB·워크플로 DB를 정리한다.
    app.state.workflow_service = None
    try:
        async with AsyncExitStack() as resources:
            mcp = MCPClient()
            resources.push_async_callback(mcp.stop)
            await mcp.start()   # Wave 2 — MCP subprocess는 서버 수명 동안 딱 1개
            backend, fast_llm, quality_llm = _build_llms()
            app.state.llm_backend = backend
            # [추가] 일반 진단 의존성을 바꾸지 않고 워크플로 생성 모델만 별도로 조립한다.
            workflow_llm = _build_workflow_llm(backend, fast_llm, quality_llm)
            app.state.workflow_llm = workflow_llm
            session_store = SqliteSessionStore()
            resources.callback(session_store.close)
            deps = SupervisorDeps(
                fast_llm=fast_llm,
                quality_llm=quality_llm,
                mcp=mcp,
                session_store=session_store,
                version=os.environ.get("SUPERVISOR_VERSION", "v2"),
            )
            app.state.deps = deps
            app.state.graph = build_graph(deps) if deps.version == "v2" else None
            app.state.instance_id = id(deps)
            # [추가] 초안은 별도 DB에 저장하고 MCP·보고 모델과 전용 생성 모델을 연결한다.
            workflow_path = os.environ.get("WORKFLOW_DB_PATH")
            workflow_store = SqliteWorkflowStore(workflow_path) if workflow_path else SqliteWorkflowStore()
            resources.push_async_callback(asyncio.to_thread, workflow_store.close)
            app.state.workflow_service = WorkflowService(
                workflow_store, workflow_llm, quality_llm, mcp,
                generation_timeout_seconds=float(os.environ.get("WORKFLOW_GENERATION_TIMEOUT_SECONDS", "120")),
                execution_timeout_seconds=float(os.environ.get("WORKFLOW_EXECUTION_TIMEOUT_SECONDS", "120")),
                # [추가] 기존 타임아웃 실습을 포함한 일시적 Tool 오류만 최대 3회 시도한다.
                mcp_attempts=int(os.environ.get("WORKFLOW_MCP_ATTEMPTS", "3")),
                mcp_retry_delay=float(os.environ.get("WORKFLOW_MCP_RETRY_DELAY", "0.3")),
            )
            try:
                yield
            finally:
                app.state.workflow_service = None
    finally:
        try:   # 종료 전 LangSmith 배치 전송 플러시 — kill 시 run이 pending으로 남는 것 방지
            from langchain_core.tracers.langchain import wait_for_all_tracers
            wait_for_all_tracers()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)
# [추가] 조회·실행 API는 /workflows 하위에서 제공한다.
register_workflow_api(app)


@app.get("/health")
async def health():
    # [수정] 일반 진단 모델과 실제 워크플로 생성 모델을 함께 확인할 수 있게 한다.
    return {"status": "ok", "instance_id": app.state.instance_id,
            "creation_count": CREATION_COUNT,
            "supervisor_version": app.state.deps.version,
            "llm_backend": app.state.llm_backend,
            "fast_llm": app.state.deps.fast_llm.name,
            "quality_llm": app.state.deps.quality_llm.name,
            "workflow_llm": app.state.workflow_llm.name}


@app.get("/version")
async def version():
    return {"supervisor_version": app.state.deps.version}


def _metadata(result: dict, run_id: str | None) -> dict:
    return {
        "route": result.get("route"),
        "task_count": len(result.get("tasks", [])),
        "gate_removed": result.get("gate_removed", 0),
        "replan_rounds": result.get("replan_rounds", 0),
        "retry_count": result.get("retry_count", 0),
        "parallel_elapsed": result.get("parallel_elapsed"),
        "validation_attempts": result.get("validation_attempts", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "llm_backend": app.state.llm_backend,
        "supervisor_version": app.state.deps.version,
        "instance_id": app.state.instance_id,
        "langsmith_run_id": run_id,
    }


@traceable(name="chat_request")
async def _handle_chat(message: str, thread_id: str) -> tuple[dict, str | None]:
    deps = app.state.deps
    history = deps.session_store.load(thread_id)   # Wave 12
    if deps.version == "v2":
        result = await app.state.graph.ainvoke(
            {"query": message, "thread_id": thread_id, "history": history,
             "events": [], "cost_usd": 0.0})
    else:
        result = await run_v1(deps, message, thread_id, history)   # Wave 14
    deps.session_store.append(thread_id, "user", message)
    deps.session_store.append(thread_id, "assistant", result["final"])
    rt = get_current_run_tree()
    return result, (str(rt.id) if rt else None)


@app.post("/chat")
async def chat(req: ChatRequest):
    result, run_id = await _handle_chat(req.message, req.thread_id)
    return {"reply": result["final"], "events": result.get("events", []),
            "metadata": _metadata(result, run_id)}


@app.get("/chat/stream")
async def chat_stream(message: str, thread_id: str = "default"):
    """Wave 3 — 그래프의 노드별 진행 상황을 SSE로 스트리밍.

    이벤트마다 'data: {json}\\n\\n' 형태로 내려보내고 마지막에 data: [DONE] 센티널을
    보낸다 — 프론트엔드는 \\n\\n 경계로 이벤트를 분리해 파싱해야 한다(Wave 3b).
    """
    deps = app.state.deps

    async def gen():
        history = deps.session_store.load(thread_id)
        if deps.version != "v2":
            result = await run_v1(deps, message, thread_id, history)
            for ev in result["events"]:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': result['final']}, ensure_ascii=False)}\n\n"
        else:
            seen = 0
            final = ""
            last_prompt = None   # finalize까지 살아남은 최종 write/chat 호출의 실제 프롬프트
            # stream_mode를 리스트로 주면 (mode, payload) 튜플이 나온다 — "updates"는
            # 노드가 끝날 때마다의 상태 델타, "custom"은 write_node/chat_answer_node가
            # get_stream_writer()로 실행 중에 즉시 흘려보내는 토큰(AnswerToken) 이벤트다.
            async for mode, payload in app.state.graph.astream(
                    {"query": message, "thread_id": thread_id, "history": history,
                     "events": [], "cost_usd": 0.0}, stream_mode=["updates", "custom"]):
                if mode == "custom":
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    continue
                for node_name, delta in payload.items():
                    for ev in (delta.get("events") or [])[seen:]:
                        ev = {**ev, "node": node_name}
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    if delta.get("events"):
                        seen = len(delta["events"])
                    if delta.get("final"):
                        final = delta["final"]
                    if delta.get("last_prompt"):
                        last_prompt = delta["last_prompt"]
            yield f"data: {json.dumps({'type': 'FinalAnswer', 'reply': final, 'prompt': last_prompt}, ensure_ascii=False)}\n\n"
            deps.session_store.append(thread_id, "user", message)
            deps.session_store.append(thread_id, "assistant", final)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="warning")

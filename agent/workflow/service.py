# [추가] 생성·저장·미리보기·검증된 실행을 묶는 신규 서비스 파일 전체.
"""DB와 카탈로그 I/O를 스레드로 분리하고 저장 정의만 실행 진입점에 전달한다."""
import asyncio
import math
import re
from contextlib import aclosing
from dataclasses import dataclass
from typing import Callable

from pydantic import ValidationError

from agent.retry import with_retry

from .catalog import CatalogError, WorkflowCatalog, load_catalog
from .compiler import compile_workflow
from .executor import WorkflowMCP
from .graph import run_workflow, stream_workflow
from .models import (
    MAX_WORKFLOW_JSON_BYTES,
    MAX_WORKFLOW_NAME_LENGTH,
    MAX_WORKFLOW_NODES,
    MAX_WORKFLOW_QUERY_LENGTH,
    WorkflowGenerated,
    WorkflowRecord,
    WorkflowRunResult,
)
from .reporting import MAX_REPORT_ATTEMPTS
from .store import (
    SqliteWorkflowStore,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowStoreError,
)
from .validator import validate_workflow


# [추가] HTTP 응답으로 변환할 오류 코드·설명·검증 자료를 서비스 계층에 보존한다.
class WorkflowServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


# [추가] 선택적으로 기존 일시적 Tool 타임아웃 재시도 정책을 연결한다.
class WorkflowMCPAdapter:
    def __init__(self, mcp: WorkflowMCP, attempts: int, retry_delay: float):
        self.mcp = mcp
        self.attempts = attempts
        self.retry_delay = retry_delay

    async def call_tool(self, name: str, arguments: dict, server: str = "ops") -> dict:
        if self.attempts == 1:
            return await self.mcp.call_tool(name, arguments, server=server)
        # [추가] 읽기 Tool의 TimeoutError만 재시도하며 전체 실행 deadline이 대기까지 제한한다.
        result, _ = await with_retry(
            self.mcp.call_tool, name, arguments, server=server,
            retries=self.attempts, base_delay=self.retry_delay,
        )
        return result

    async def find_resource_template(self, template_name: str) -> tuple[str, str]:
        return await self.mcp.find_resource_template(template_name)

    async def read_resource(self, uri: str, server: str) -> str:
        return await self.mcp.read_resource(uri, server=server)


# [추가] 사전 검증 이후 외부 객체나 DB가 바뀌어도 실행 정의가 교체되지 않게 고정한다.
@dataclass(frozen=True)
class PreparedWorkflowExecution:
    _record_json: str
    catalog: WorkflowCatalog
    mcp: WorkflowMCP

    # [추가] 호출자가 미리보기 객체를 변경하더라도 내부 스냅샷은 그대로 유지한다.
    @property
    def record(self) -> WorkflowRecord:
        return WorkflowRecord.model_validate_json(self._record_json)


# [추가] 설정은 유효한 숫자만 허용하고 사용자 입력·모델 실패와 구분한다.
def _positive_timeout(value: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("시간 제한은 양의 유한한 숫자여야 합니다.")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("시간 제한은 양의 유한한 숫자여야 합니다.") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError("시간 제한은 양의 유한한 숫자여야 합니다.")
    return result


# [추가] API와 이후 화면·평가 도구가 같은 생성·저장·실행 정책을 사용한다.
class WorkflowService:
    def __init__(
        self, store: SqliteWorkflowStore, fast_llm, quality_llm, mcp: WorkflowMCP,
        *, catalog_loader: Callable[[], WorkflowCatalog] = load_catalog,
        generation_timeout_seconds: float = 120.0, execution_timeout_seconds: float = 120.0,
        mcp_attempts: int = 1, mcp_retry_delay: float = 0.3,
    ):
        self.store = store
        self.fast_llm = fast_llm
        self.quality_llm = quality_llm
        self.mcp = mcp
        self.catalog_loader = catalog_loader
        self.generation_timeout_seconds = _positive_timeout(generation_timeout_seconds)
        self.execution_timeout_seconds = _positive_timeout(execution_timeout_seconds)
        if type(mcp_attempts) is not int or not 1 <= mcp_attempts <= 3:
            raise ValueError("MCP Tool 시도 횟수는 1~3의 정수여야 합니다.")
        if type(mcp_retry_delay) not in (int, float):
            raise ValueError("MCP 재시도 대기는 0 이상의 유한한 숫자여야 합니다.")
        try:
            delay = float(mcp_retry_delay)
        except OverflowError as error:
            raise ValueError("MCP 재시도 대기는 0 이상의 유한한 숫자여야 합니다.") from error
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("MCP 재시도 대기는 0 이상의 유한한 숫자여야 합니다.")
        self.mcp_attempts = mcp_attempts
        self.mcp_retry_delay = delay

    # [추가] SQLite 읽기·쓰기 잠금과 디스크 대기가 이벤트 루프를 막지 않게 한다.
    async def _store_call(self, method, *args, **kwargs):
        try:
            return await asyncio.to_thread(method, *args, **kwargs)
        except WorkflowNotFoundError as error:
            raise WorkflowServiceError(404, "WORKFLOW_NOT_FOUND", "저장된 워크플로를 찾을 수 없습니다.") from error
        except WorkflowIntegrityError as error:
            raise WorkflowServiceError(
                409, "STORED_WORKFLOW_INVALID", "저장된 정의의 무결성을 확인할 수 없습니다."
            ) from error
        except WorkflowStoreError as error:
            raise WorkflowServiceError(500, "STORE_ERROR", "워크플로 저장소 작업에 실패했습니다.") from error
        except (ValueError, ValidationError) as error:
            raise WorkflowServiceError(422, "INVALID_WORKFLOW_DATA", "저장할 워크플로 데이터를 확인하세요.") from error

    # [추가] 호출마다 새 카탈로그를 읽어 저장 이후 삭제된 자원을 발견한다.
    async def _catalog(self) -> WorkflowCatalog:
        try:
            catalog = await asyncio.to_thread(self.catalog_loader)
            if not isinstance(catalog, WorkflowCatalog):
                raise CatalogError("카탈로그 로더가 잘못된 값을 반환했습니다.")
            return catalog
        except (CatalogError, OSError, ValueError) as error:
            raise WorkflowServiceError(
                503, "CATALOG_UNAVAILABLE", "현재 사용 가능한 진단 자원 목록을 읽을 수 없습니다."
            ) from error

    # [추가] 화면이 실제 허용 자원과 제한을 조회할 수 있게 한다.
    async def get_catalog(self) -> dict:
        catalog = await self._catalog()
        return {
            **catalog.to_dict(),
            "limits": {
                "max_nodes": MAX_WORKFLOW_NODES,
                "max_query_length": MAX_WORKFLOW_QUERY_LENGTH,
                "max_name_length": MAX_WORKFLOW_NAME_LENGTH,
                "max_json_bytes": MAX_WORKFLOW_JSON_BYTES,
                "max_report_attempts": MAX_REPORT_ATTEMPTS,
                "generation_timeout_seconds": self.generation_timeout_seconds,
                "execution_timeout_seconds": self.execution_timeout_seconds,
            },
        }

    # [추가] 유효한 생성 결과만 초안으로 저장하며 생성 단계에서는 MCP를 호출하지 않는다.
    async def generate(self, query: object) -> WorkflowGenerated:
        catalog = await self._catalog()
        compilation = await compile_workflow(
            query, catalog, self.fast_llm, timeout_seconds=self.generation_timeout_seconds,
        )
        if not compilation.valid:
            code = compilation.error.code
            status = 504 if code == "GENERATION_TIMEOUT" else 502 if code == "MODEL_ERROR" else 422
            raise WorkflowServiceError(
                status, code, compilation.error.message,
                details={"compilation": compilation.model_dump(mode="json")},
            )
        record = await self._store_call(self.store.create_draft, query, compilation.workflow)
        return WorkflowGenerated(**record.model_dump(), compilation=compilation)

    # [추가] 정의와 메타데이터는 매번 저장소 검사를 거친 독립 객체로 반환한다.
    async def get_workflow(self, workflow_id: str) -> WorkflowRecord:
        return await self._store_call(self.store.get, workflow_id)

    # [추가] 실행 JSON은 변경하지 않고 표시 이름과 재사용 상태만 저장한다.
    async def save_workflow(self, workflow_id: str, name: str) -> WorkflowRecord:
        return await self._store_call(self.store.save, workflow_id, name)

    # [추가] 기본 목록은 사용자가 저장한 절차이며 초안은 명시했을 때만 포함한다.
    async def list_workflows(
        self, *, limit: int = 20, offset: int = 0, include_drafts: bool = False,
    ) -> list[WorkflowRecord]:
        return await self._store_call(
            self.store.list_workflows, limit=limit, offset=offset, include_drafts=include_drafts,
        )

    # [추가] HTTP 스트림을 열기 전에 존재·미리보기 hash·현재 자원을 모두 확인한다.
    async def prepare_execution(
        self, workflow_id: str, definition_hash: str,
    ) -> PreparedWorkflowExecution:
        if not isinstance(definition_hash, str) or re.fullmatch(r"[a-f0-9]{64}", definition_hash) is None:
            raise WorkflowServiceError(422, "INVALID_DEFINITION_HASH", "유효한 실행 정의 해시가 필요합니다.")
        record = await self.get_workflow(workflow_id)
        if definition_hash != record.definition_hash:
            raise WorkflowServiceError(
                409, "DEFINITION_HASH_MISMATCH", "미리보기의 실행 정의와 저장된 정의가 다릅니다. 다시 조회하세요."
            )
        catalog = await self._catalog()
        validation = validate_workflow(record.definition, catalog)
        if not validation.valid:
            raise WorkflowServiceError(
                409, "CATALOG_CHANGED", "현재 카탈로그에서 실행할 수 없는 절차입니다. 다시 생성하세요.",
                details={"errors": [issue.model_dump(mode="json") for issue in validation.errors]},
            )
        return PreparedWorkflowExecution(
            record.model_dump_json(), catalog,
            WorkflowMCPAdapter(self.mcp, self.mcp_attempts, self.mcp_retry_delay),
        )

    # [추가] 사용자 입력이나 모델을 다시 실행하지 않고 검증된 저장 스냅샷만 실행한다.
    async def execute(self, workflow_id: str, definition_hash: str) -> WorkflowRunResult:
        prepared = await self.prepare_execution(workflow_id, definition_hash)
        record = prepared.record
        return await run_workflow(
            record.definition, prepared.catalog, prepared.mcp, self.quality_llm,
            query=record.query, timeout_seconds=self.execution_timeout_seconds,
            workflow_id=record.workflow_id,
        )

    # [추가] JSON 실행과 같은 검증 스냅샷·그래프를 POST 스트림에서 소비한다.
    async def stream_prepared(self, prepared: PreparedWorkflowExecution):
        record = prepared.record
        async with aclosing(stream_workflow(
            record.definition, prepared.catalog, prepared.mcp, self.quality_llm,
            query=record.query, timeout_seconds=self.execution_timeout_seconds,
            workflow_id=record.workflow_id,
        )) as events:
            async for event in events:
                yield event

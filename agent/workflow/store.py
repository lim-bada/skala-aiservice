# [추가] 새 파일 전체: 생성한 워크플로 초안과 재사용 절차를 SQLite에 영구 보관한다.
"""실행 정의는 불변으로 저장하고 표시 이름만 변경하는 스레드 안전 저장소."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .identity import workflow_definition_hash
from .models import MAX_WORKFLOW_JSON_BYTES, WorkflowDefinition, WorkflowRecord


# [추가] 실행한 폴더와 관계없이 기존 대화 DB와 분리된 프로젝트 루트 파일을 사용한다.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "workflow.db"
_COLUMNS = (
    "id", "name", "query", "schema_version", "definition_json", "definition_hash",
    "status", "created_at", "saved_at",
)
_SELECT = "SELECT " + ", ".join(_COLUMNS) + " FROM workflows"


# [추가] 없는 절차와 저장 계층의 장애·손상을 API에서 서로 구분하도록 한다.
class WorkflowNotFoundError(LookupError):
    def __init__(self, workflow_id: str):
        self.workflow_id = workflow_id
        super().__init__(f"워크플로를 찾을 수 없습니다: {workflow_id}")


# [추가] SQLite 연결·조회·트랜잭션 오류를 저장소 전용 예외로 전달한다.
class WorkflowStoreError(RuntimeError):
    pass


# [추가] 잘못된 저장 데이터를 실행 가능한 정의로 복원하지 않는 손상 전용 오류다.
class WorkflowIntegrityError(WorkflowStoreError):
    pass


# [추가] UTF-8로 표현할 수 없는 문자열을 DB에 보내기 전에 명시적으로 거부한다.
def _require_utf8(value: str, label: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"{label}은 유효한 UTF-8 문자열이어야 합니다.") from error


# [추가] JSON의 중복 키를 마지막 값으로 덮어쓰지 않고 저장 데이터 손상으로 다룬다.
def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("저장 JSON에 중복 키가 있습니다.")
        result[key] = value
    return result


# [추가] 표준 JSON에 없는 NaN·Infinity를 저장된 실행 조건으로 허용하지 않는다.
def _reject_constant(value: str) -> None:
    raise ValueError(f"저장 JSON에 유효하지 않은 숫자가 있습니다: {value}")


# [추가] 저장·생성 시각은 시간대가 있는 ISO 문자열로 보존한다.
def _validate_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("저장 시각에는 시간대가 필요합니다.")


# [추가] 서비스의 asyncio.to_thread 호출과 여러 저장소 인스턴스를 지원한다.
class SqliteWorkflowStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self._lock = RLock()
        self._conn: sqlite3.Connection | None = None
        connection = None
        try:
            connection = sqlite3.connect(
                db_path, check_same_thread=False, timeout=5.0
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS workflows ("
                    "id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL, query TEXT NOT NULL, "
                    "schema_version INTEGER NOT NULL, definition_json TEXT NOT NULL, "
                    "definition_hash TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN ('draft', 'saved')), "
                    "created_at TEXT NOT NULL, saved_at TEXT)"
                )
                # [추가] 같은 이름의 다른 테이블을 조용히 재사용하지 않고 초기화에 실패시킨다.
                info = connection.execute("PRAGMA table_info(workflows)").fetchall()
                if tuple(row["name"] for row in info) != _COLUMNS:
                    raise WorkflowIntegrityError("워크플로 저장 테이블의 컬럼이 올바르지 않습니다.")
                for row in info:
                    expected_type = "INTEGER" if row["name"] == "schema_version" else "TEXT"
                    if row["type"].upper() != expected_type:
                        raise WorkflowIntegrityError("워크플로 저장 테이블의 타입이 올바르지 않습니다.")
                if info[0]["pk"] != 1:
                    raise WorkflowIntegrityError("워크플로 저장 테이블의 기본 키가 올바르지 않습니다.")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS workflows_status_created "
                    "ON workflows(status, created_at DESC, id DESC)"
                )
            self._conn = connection
        except BaseException as error:
            # [추가] 연결 이후 초기화가 실패하면 불완전한 저장소와 열린 연결을 남기지 않는다.
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            if isinstance(error, sqlite3.Error):
                raise WorkflowStoreError("워크플로 저장소를 초기화하지 못했습니다.") from error
            raise

    # [추가] 종료된 연결의 작업을 SQLite 내부 예외 대신 일관된 저장소 오류로 처리한다.
    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise WorkflowStoreError("워크플로 저장소가 닫혀 있습니다.")
        return self._conn

    # [추가] 조회 키도 문자열로 제한하되 값 자체는 SQL 파라미터로만 전달한다.
    @staticmethod
    def _check_id(workflow_id: str) -> None:
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError("workflow_id는 비어 있지 않은 문자열이어야 합니다.")
        _require_utf8(workflow_id, "workflow_id")

    # [추가] JSON 구조·버전·해시·메타데이터를 매 조회마다 재검사하고 독립 모델로 만든다.
    @staticmethod
    def _record(row: sqlite3.Row) -> WorkflowRecord:
        try:
            if not isinstance(row["definition_json"], str):
                raise ValueError("실행 정의는 JSON 문자열이어야 합니다.")
            _require_utf8(row["definition_json"], "실행 정의")
            if len(row["definition_json"].encode("utf-8")) > MAX_WORKFLOW_JSON_BYTES:
                raise ValueError("저장된 실행 정의가 JSON 크기 제한을 초과했습니다.")
            payload = json.loads(
                row["definition_json"],
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            definition = WorkflowDefinition.model_validate(payload)
            if definition.model_dump(mode="json") != payload:
                raise ValueError("저장된 실행 정의를 암묵적으로 보정할 수 없습니다.")
            if type(row["schema_version"]) is not int or row["schema_version"] != definition.schema_version:
                raise ValueError("실행 정의의 스키마 버전이 일치하지 않습니다.")
            if workflow_definition_hash(definition) != row["definition_hash"]:
                raise ValueError("실행 정의 해시가 일치하지 않습니다.")
            record = WorkflowRecord(
                workflow_id=row["id"], name=row["name"], query=row["query"],
                definition=definition, definition_hash=row["definition_hash"],
                status=row["status"], created_at=row["created_at"], saved_at=row["saved_at"],
            )
            if record.name != row["name"]:
                raise ValueError("저장된 표시 이름이 정규화된 이름과 다릅니다.")
            for label, value in (("원문", record.query), ("표시 이름", record.name)):
                _require_utf8(value, label)
            _validate_timestamp(record.created_at)
            if record.saved_at is not None:
                _validate_timestamp(record.saved_at)
            if (record.status == "saved") != (record.saved_at is not None):
                raise ValueError("저장 상태와 저장 시각이 일치하지 않습니다.")
            return record
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise WorkflowIntegrityError("저장된 워크플로가 손상되어 읽을 수 없습니다.") from error

    # [추가] 하나의 트랜잭션에서 조회해 이름 변경 전에 기존 정의의 무결성을 검사한다.
    def _get(self, connection: sqlite3.Connection, workflow_id: str) -> WorkflowRecord:
        row = connection.execute(_SELECT + " WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise WorkflowNotFoundError(workflow_id)
        return self._record(row)

    # [추가] 검증된 정의의 새 복사본과 원문을 UUID 초안으로 저장한다. 자원 존재는 상위에서 검사한다.
    def create_draft(self, query: str, definition: WorkflowDefinition) -> WorkflowRecord:
        checked = WorkflowDefinition.model_validate(definition).model_copy(deep=True)
        record = WorkflowRecord(
            workflow_id=str(uuid4()), name=checked.name, query=query,
            definition=checked, definition_hash=workflow_definition_hash(checked),
            status="draft", created_at=datetime.now(timezone.utc).isoformat(),
        )
        _require_utf8(record.query, "원문")
        definition_json = json.dumps(
            checked.model_dump(mode="json"), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )
        _require_utf8(definition_json, "실행 정의")
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO workflows (" + ", ".join(_COLUMNS) + ") "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.workflow_id, record.name, record.query, checked.schema_version,
                            definition_json, record.definition_hash, record.status,
                            record.created_at, record.saved_at,
                        ),
                    )
                    return self._get(connection, record.workflow_id)
            except sqlite3.Error as error:
                raise WorkflowStoreError("워크플로 초안을 저장하지 못했습니다.") from error

    # [추가] 반환 모델을 캐시하지 않아 호출자가 변경해도 저장된 정의와 다른 조회에 영향이 없다.
    def get(self, workflow_id: str) -> WorkflowRecord:
        self._check_id(workflow_id)
        with self._lock:
            connection = self._connection()
            try:
                return self._get(connection, workflow_id)
            except sqlite3.Error as error:
                raise WorkflowStoreError("워크플로를 조회하지 못했습니다.") from error

    # [추가] 최초 생성 JSON과 해시는 그대로 두고 표시 이름·저장 상태·저장 시각만 변경한다.
    def save(self, workflow_id: str, name: str) -> WorkflowRecord:
        self._check_id(workflow_id)
        with self._lock:
            connection = self._connection()
            try:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    previous = self._get(connection, workflow_id)
                    values = previous.model_dump(mode="python")
                    values.update(name=name, status="saved", saved_at=datetime.now(timezone.utc).isoformat())
                    checked = WorkflowRecord.model_validate(values)
                    _require_utf8(checked.name, "표시 이름")
                    connection.execute(
                        "UPDATE workflows SET name=?, status=?, saved_at=? WHERE id=?",
                        (checked.name, checked.status, checked.saved_at, workflow_id),
                    )
                    return self._get(connection, workflow_id)
            except sqlite3.Error as error:
                raise WorkflowStoreError("워크플로를 저장 목록에 등록하지 못했습니다.") from error

    # [추가] 저장 목록을 기본으로 제한된 페이지만 읽고 생성 시각·ID로 순서를 고정한다.
    def list_workflows(
        self, *, limit: int = 20, offset: int = 0, include_drafts: bool = False
    ) -> list[WorkflowRecord]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit은 1~100의 정수여야 합니다.")
        if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
            raise ValueError("offset은 SQLite 범위 안의 0 이상 정수여야 합니다.")
        if type(include_drafts) is not bool:
            raise ValueError("include_drafts는 불리언이어야 합니다.")
        sql = _SELECT
        if not include_drafts:
            sql += " WHERE status='saved'"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        with self._lock:
            connection = self._connection()
            try:
                rows = connection.execute(sql, (limit, offset)).fetchall()
                return [self._record(row) for row in rows]
            except sqlite3.Error as error:
                raise WorkflowStoreError("워크플로 목록을 조회하지 못했습니다.") from error

    # [추가] 진행 중인 동기 작업과 경합하지 않도록 잠금 안에서 연결을 한 번만 닫는다.
    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                connection = self._conn
                self._conn = None
                try:
                    connection.close()
                except sqlite3.Error as error:
                    raise WorkflowStoreError("워크플로 저장소를 닫지 못했습니다.") from error

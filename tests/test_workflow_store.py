# [추가] 새 파일 전체: 워크플로 저장의 영속성·불변성·무결성·동시성과 종료 동작을 검증한다.
"""임시 SQLite 파일과 메모리 DB만 사용하며 프로젝트의 실제 저장 파일은 만들지 않는다."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from pydantic import ValidationError

from agent.workflow.identity import workflow_definition_hash
from agent.workflow.models import WorkflowDefinition
from agent.workflow.store import (
    DEFAULT_DB_PATH,
    SqliteWorkflowStore,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowStoreError,
)


# [추가] 저장소가 카탈로그 조회 없이 구조만 검사할 수 있는 독립 실행 정의를 준비한다.
def workflow_definition():
    return WorkflowDefinition.model_validate({
        "schema_version": 1,
        "name": "결제 조건부 점검",
        "steps": [
            {"id": "metrics", "type": "monitoring", "system": "payments-service"},
            {
                "id": "check_cpu", "type": "if",
                "condition": {"ref": "metrics", "field": "cpu_percent", "op": ">=", "value": 80},
                "then_steps": [{"id": "cpu_book", "type": "runbook", "incident_type": "cpu-spike"}],
                "else_steps": [],
            },
        ],
    })


# [추가] 테스트별 새 DB를 사용해 재오픈·손상 주입이 다른 사례에 영향을 주지 않게 한다.
class WorkflowStoreTests(unittest.IsolatedAsyncioTestCase):
    # [추가] 연결 종료 후 임시 디렉터리가 정리되도록 역순 정리 함수를 등록한다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "workflow.db"
        self.store = SqliteWorkflowStore(self.path)
        self.addCleanup(self.store.close)

    # [추가] 새 초안의 UUID·원문 보존·UTC 시각·해시와 초기 상태를 확인한다.
    def test_create_draft_preserves_query_and_generates_identity(self):
        definition = workflow_definition()
        query = "  결제 시스템을 점검해줘.\n"
        record = self.store.create_draft(query, definition)
        self.assertEqual(UUID(record.workflow_id).version, 4)
        self.assertEqual(record.query, query)
        self.assertEqual(record.name, definition.name)
        self.assertEqual(record.definition, definition)
        self.assertEqual(record.definition_hash, workflow_definition_hash(definition))
        self.assertEqual(record.status, "draft")
        self.assertIsNone(record.saved_at)
        self.assertEqual(datetime.fromisoformat(record.created_at).utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(self.store.get(record.workflow_id), record)

    # [추가] 표시 이름 변경이 실행 정의 원문이나 해시에 영향을 주지 않는지 확인한다.
    def test_save_renames_display_only_and_preserves_definition_bytes(self):
        record = self.store.create_draft("결제 점검", workflow_definition())
        with sqlite3.connect(self.path) as connection:
            original_json = connection.execute(
                "SELECT definition_json FROM workflows WHERE id=?", (record.workflow_id,)
            ).fetchone()[0]
        saved = self.store.save(record.workflow_id, "  재사용 점검  ")
        renamed = self.store.save(record.workflow_id, "다른 표시 이름")
        self.assertEqual(saved.name, "재사용 점검")
        self.assertEqual(renamed.name, "다른 표시 이름")
        self.assertEqual(renamed.status, "saved")
        self.assertIsNotNone(datetime.fromisoformat(renamed.saved_at).utcoffset())
        self.assertEqual(renamed.definition_hash, record.definition_hash)
        self.assertEqual(renamed.definition.name, "결제 조건부 점검")
        self.assertEqual(renamed.created_at, record.created_at)
        self.assertEqual(renamed.query, record.query)
        with sqlite3.connect(self.path) as connection:
            current_json = connection.execute(
                "SELECT definition_json FROM workflows WHERE id=?", (record.workflow_id,)
            ).fetchone()[0]
        self.assertEqual(current_json, original_json)

    # [추가] 파일 DB는 닫고 새 객체를 만들어도 저장 목록과 실행 정의를 보존한다.
    def test_file_database_survives_reopen(self):
        draft = self.store.create_draft("결제 점검", workflow_definition())
        saved = self.store.save(draft.workflow_id, "저장된 점검")
        self.store.close()
        reopened = SqliteWorkflowStore(str(self.path))
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(saved.workflow_id), saved)
        self.assertEqual(reopened.list_workflows(), [saved])

    # [추가] 메모리 DB도 같은 계약을 따르며 새 연결에는 이전 내용이 남지 않는지 확인한다.
    def test_memory_database_is_supported_and_not_persistent(self):
        first = SqliteWorkflowStore(":memory:")
        self.addCleanup(first.close)
        record = first.create_draft("점검", workflow_definition())
        self.assertEqual(first.get(record.workflow_id), record)
        first.close()
        second = SqliteWorkflowStore(":memory:")
        self.addCleanup(second.close)
        with self.assertRaises(WorkflowNotFoundError):
            second.get(record.workflow_id)

    # [추가] 입력 모델·반환 모델·목록 결과의 변경이 DB의 원본에 전파되지 않는지 확인한다.
    def test_input_and_all_return_values_are_independent(self):
        definition = workflow_definition()
        record = self.store.create_draft("결제 점검", definition)
        original_hash = record.definition_hash
        definition.steps.clear()
        record.definition.steps.clear()
        loaded = self.store.get(record.workflow_id)
        self.assertEqual(len(loaded.definition.steps), 2)
        self.assertEqual(loaded.definition_hash, original_hash)
        listed = self.store.list_workflows(include_drafts=True)
        listed[0].definition.steps[0].system = "auth-service"
        self.assertEqual(self.store.get(record.workflow_id).definition.steps[0].system, "payments-service")

    # [추가] 저장소는 SQL 문장처럼 보이는 원문·표시 이름도 데이터로만 바인딩한다.
    def test_sql_metacharacters_are_stored_as_values(self):
        query = "점검'); DROP TABLE workflows; --"
        record = self.store.create_draft(query, workflow_definition())
        name = "x'); DELETE FROM workflows; --"
        saved = self.store.save(record.workflow_id, name)
        self.assertEqual(saved.name, name)
        self.assertEqual(self.store.get(record.workflow_id).query, query)
        self.assertEqual(len(self.store.list_workflows()), 1)
        with self.assertRaises(WorkflowNotFoundError):
            self.store.get("' OR 1=1 --")

    # [추가] 기본 목록은 저장된 절차만 반환하고 초안 포함은 명시적으로 선택하게 한다.
    def test_listing_filters_and_paginates_with_stable_tie_order(self):
        drafts = [self.store.create_draft(f"점검 {index}", workflow_definition()) for index in range(4)]
        saved = [self.store.save(record.workflow_id, f"저장 {index}") for index, record in enumerate(drafts[:3])]
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE workflows SET created_at=?", ("2026-10-21T00:00:00+00:00",))
        expected_ids = sorted((record.workflow_id for record in saved), reverse=True)
        actual = self.store.list_workflows()
        self.assertEqual([record.workflow_id for record in actual], expected_ids)
        self.assertEqual([record.workflow_id for record in self.store.list_workflows(limit=1, offset=1)], expected_ids[1:2])
        self.assertEqual(len(self.store.list_workflows(include_drafts=True)), 4)
        self.assertEqual(self.store.list_workflows(offset=100), [])

    # [추가] 실행 정의·원문의 잘못된 타입·길이·빈 내용·UTF-8을 저장 전에 거부한다.
    def test_invalid_create_inputs_do_not_write_rows(self):
        for query in ("", " \n\t", "a" * 2001, None, True, "broken\ud800"):
            with self.subTest(query=repr(query)):
                with self.assertRaises((ValueError, ValidationError)):
                    self.store.create_draft(query, workflow_definition())
        changed = workflow_definition()
        changed.steps.clear()
        with self.assertRaises(ValidationError):
            self.store.create_draft("점검", changed)
        self.assertEqual(self.store.list_workflows(include_drafts=True), [])

    # [추가] 잘못된 새 이름은 트랜잭션을 되돌려 초안의 이전 상태를 유지한다.
    def test_invalid_save_name_rolls_back(self):
        record = self.store.create_draft("점검", workflow_definition())
        for name in ("", "  ", "a" * 81, None, 1, "broken\ud800"):
            with self.subTest(name=repr(name)):
                with self.assertRaises((ValueError, ValidationError)):
                    self.store.save(record.workflow_id, name)
                self.assertEqual(self.store.get(record.workflow_id), record)

    # [추가] bool을 정수로 취급하거나 과도한 페이지 범위를 SQLite에 보내지 않는다.
    def test_invalid_pagination_inputs_are_rejected(self):
        for kwargs in (
            {"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
            {"offset": -1}, {"offset": True}, {"offset": 1.5}, {"offset": 2**63},
            {"include_drafts": 1}, {"include_drafts": "true"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.store.list_workflows(**kwargs)

    # [추가] 없는 ID는 저장소 장애와 분리되며 조회와 이름 변경 양쪽에서 같은 오류를 낸다.
    def test_missing_workflow_raises_not_found(self):
        for action in (lambda: self.store.get("missing"), lambda: self.store.save("missing", "이름")):
            with self.assertRaises(WorkflowNotFoundError) as caught:
                action()
            self.assertEqual(caught.exception.workflow_id, "missing")

    # [추가] JSON·해시·버전·메타데이터 손상은 조회·수정·목록 모두에서 실행 정의를 차단한다.
    def test_corrupted_rows_fail_closed(self):
        bad_values = [
            ("definition_json", "{broken"),
            ("definition_json", "[]"),
            ("definition_json", '{"name":"x","name":"y"}'),
            ("definition_json", '{"schema_version":NaN}'),
            ("definition_json", " " * (16 * 1024 + 1)),
            ("definition_hash", "0" * 64),
            ("schema_version", 2),
            ("name", ""),
            ("name", " 이름 "),
            ("query", "   "),
            ("created_at", "not-a-time"),
            ("created_at", "2026-10-21T00:00:00"),
            ("saved_at", "bad-timestamp"),
            ("saved_at", None),
        ]
        for column, value in bad_values:
            with self.subTest(column=column, value=str(value)[:80]):
                draft = self.store.create_draft("점검", workflow_definition())
                record = self.store.save(draft.workflow_id, "저장")
                with sqlite3.connect(self.path) as connection:
                    connection.execute(f"UPDATE workflows SET {column}=? WHERE id=?", (value, record.workflow_id))
                with self.assertRaises(WorkflowIntegrityError):
                    self.store.get(record.workflow_id)
                with self.assertRaises(WorkflowIntegrityError):
                    self.store.save(record.workflow_id, "손상을 고치지 않는다")
                with self.assertRaises(WorkflowIntegrityError):
                    self.store.list_workflows()
                with sqlite3.connect(self.path) as connection:
                    connection.execute("DELETE FROM workflows WHERE id=?", (record.workflow_id,))

    # [추가] 구조는 유효해도 실행 대상이 바뀌어 원래 해시와 다르면 손상으로 판단한다.
    def test_valid_json_with_changed_steps_is_not_trusted(self):
        record = self.store.create_draft("점검", workflow_definition())
        payload = record.definition.model_dump(mode="json")
        payload["steps"][0]["system"] = "auth-service"
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE workflows SET definition_json=? WHERE id=?",
                (json.dumps(payload), record.workflow_id),
            )
        with self.assertRaises(WorkflowIntegrityError):
            self.store.get(record.workflow_id)

    # [추가] SQLite 작업 오류는 내부 예외 그대로가 아닌 저장소 오류로 전달한다.
    def test_sqlite_errors_are_wrapped(self):
        record = self.store.create_draft("점검", workflow_definition())
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE workflows")
        for action in (
            lambda: self.store.get(record.workflow_id),
            lambda: self.store.save(record.workflow_id, "이름"),
            lambda: self.store.list_workflows(),
            lambda: self.store.create_draft("점검", workflow_definition()),
        ):
            with self.assertRaises(WorkflowStoreError):
                action()

    # [추가] 연결 오류와 잘못된 기존 테이블에서 초기화가 실패하며 열린 연결을 남기지 않는다.
    def test_initialization_errors_cleanup_connections(self):
        with self.assertRaises(WorkflowStoreError):
            SqliteWorkflowStore(Path(self.temp.name))
        invalid_path = Path(self.temp.name) / "invalid.db"
        with sqlite3.connect(invalid_path) as connection:
            connection.execute("CREATE TABLE workflows(id TEXT)")
        real_connect = sqlite3.connect
        opened = []

        # [추가] 생성 중 확보한 실제 연결을 관찰해 예외 이후 닫혔는지 확인한다.
        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with patch("agent.workflow.store.sqlite3.connect", side_effect=connect):
            with self.assertRaises(WorkflowIntegrityError):
                SqliteWorkflowStore(invalid_path)
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")

    # [추가] close는 반복 호출 가능하고 이후의 모든 정상 작업은 명시적으로 거부한다.
    def test_close_is_idempotent_and_rejects_further_work(self):
        record = self.store.create_draft("점검", workflow_definition())
        self.store.close()
        self.store.close()
        for action in (
            lambda: self.store.get(record.workflow_id),
            lambda: self.store.save(record.workflow_id, "이름"),
            lambda: self.store.list_workflows(),
            lambda: self.store.create_draft("점검", workflow_definition()),
        ):
            with self.assertRaises(WorkflowStoreError):
                action()

    # [추가] 동일 연결에 대한 여러 to_thread 호출이 서로 다른 결과를 안전하게 보존한다.
    async def test_concurrent_to_thread_operations(self):
        # [추가] 서비스와 동일한 동기 메서드 호출 경로로 생성·저장·조회를 연속 실행한다.
        def create_save_get(index):
            record = self.store.create_draft(f"점검 {index}", workflow_definition())
            saved = self.store.save(record.workflow_id, f"절차 {index}")
            return self.store.get(saved.workflow_id)

        records = await asyncio.gather(*(
            asyncio.to_thread(create_save_get, index) for index in range(24)
        ))
        self.assertEqual(len({record.workflow_id for record in records}), 24)
        self.assertEqual({record.name for record in records}, {f"절차 {index}" for index in range(24)})
        self.assertEqual(len(self.store.list_workflows(limit=100)), 24)

    # [추가] 별도 연결의 저장소들도 WAL·대기 제한 아래에서 같은 파일에 동시에 기록한다.
    async def test_separate_store_instances_can_write_concurrently(self):
        other = SqliteWorkflowStore(self.path)
        self.addCleanup(other.close)
        records = await asyncio.gather(*(
            asyncio.to_thread(
                (self.store if index % 2 else other).create_draft,
                f"별도 연결 {index}", workflow_definition(),
            )
            for index in range(16)
        ))
        self.assertEqual(len({record.workflow_id for record in records}), 16)
        self.assertEqual(len(self.store.list_workflows(limit=100, include_drafts=True)), 16)

    # [추가] 기본 DB 위치는 모듈 기준 프로젝트 루트이며 일반 대화 DB와 분리한다.
    def test_default_path_is_project_relative(self):
        self.assertEqual(DEFAULT_DB_PATH, Path(__file__).resolve().parents[1] / "workflow.db")


# [추가] 표준 unittest 명령과 파일 직접 실행으로 검증할 수 있게 한다.
if __name__ == "__main__":
    unittest.main()

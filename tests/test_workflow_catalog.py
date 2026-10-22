# [추가] 새 파일 전체: 외부 호출 없이 카탈로그 로딩·오류·불변성·재로딩을 검증한다.
"""Local, deterministic tests for workflow resource catalog snapshots."""

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from agent.workflow.catalog import CatalogError, WorkflowCatalog, load_catalog


# [추가] 임시 데이터 디렉터리를 사용해 실제 프로젝트 데이터를 바꾸지 않고 검사한다.
class WorkflowCatalogTests(unittest.TestCase):
    # [추가] 각 테스트에 독립적인 시스템 목록과 두 개의 런북을 준비한다.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.monitoring_path = self.data_dir / "monitoring.json"
        self.runbooks_dir = self.data_dir / "runbooks"
        self.runbooks_dir.mkdir()
        self.write_monitoring({"web-01": {}, "auth-service": {}})
        (self.runbooks_dir / "memory-leak.md").write_text("# Memory\n", encoding="utf-8")
        (self.runbooks_dir / "cpu-spike.md").write_text("# CPU\n", encoding="utf-8")

    # [추가] 정상·비정상 JSON 입력을 테스트별로 간단히 구성하도록 공통 저장 함수를 둔다.
    def write_monitoring(self, data):
        self.monitoring_path.write_text(json.dumps(data), encoding="utf-8")

    # [추가] 목록 정렬·변경 금지와 조건 필드·연산자·상태 허용값을 확인한다.
    def test_loads_sorted_immutable_snapshot_and_condition_capabilities(self):
        catalog = load_catalog(self.data_dir)

        self.assertEqual(catalog.systems, ("auth-service", "web-01"))
        self.assertEqual(catalog.incident_types, ("cpu-spike", "memory-leak"))
        self.assertEqual(catalog.condition_fields, ("cpu_percent", "memory_percent", "status"))
        self.assertEqual(catalog.status_values, ("healthy", "degraded", "down"))
        self.assertEqual(set(catalog.numeric_operators), {">", ">=", "<", "<=", "==", "!="})
        self.assertEqual(set(catalog.status_operators), {"==", "!="})
        with self.assertRaises(FrozenInstanceError):
            catalog.systems = ("changed-service",)

    # [추가] API용 반환값은 JSON 직렬화 가능하며 원본 카탈로그와 분리되는지 검사한다.
    def test_payload_is_json_serializable_and_cannot_mutate_catalog(self):
        catalog = load_catalog(self.data_dir)
        payload = catalog.to_dict()

        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(payload["condition_fields"]["cpu_percent"]["type"], "number")
        self.assertEqual(payload["condition_fields"]["status"]["values"], list(catalog.status_values))
        payload["systems"].clear()
        payload["condition_fields"]["cpu_percent"]["operators"].clear()
        self.assertEqual(catalog.to_dict()["systems"], ["auth-service", "web-01"])
        self.assertIn(">=", catalog.to_dict()["condition_fields"]["cpu_percent"]["operators"])

    # [추가] 생성자에 전달했던 목록을 변경해도 스냅샷을 훼손하지 못하는지 검사한다.
    def test_constructor_copies_caller_owned_collections(self):
        systems = ["web-01", "auth-service"]
        catalog = WorkflowCatalog(systems=systems, incident_types=["cpu-spike"])
        systems.clear()

        self.assertEqual(catalog.systems, ("auth-service", "web-01"))

    # [추가] 파일 변경은 새 로드에만 반영되고 이전 스냅샷은 보존되는지 확인한다.
    def test_reload_reflects_resource_changes_without_changing_previous_snapshot(self):
        previous = load_catalog(self.data_dir)
        self.write_monitoring({"payments-service": {}})
        (self.runbooks_dir / "cpu-spike.md").unlink()
        (self.runbooks_dir / "disk-full.md").write_text("# Disk\n", encoding="utf-8")

        current = load_catalog(self.data_dir)

        self.assertEqual(previous.systems, ("auth-service", "web-01"))
        self.assertEqual(previous.incident_types, ("cpu-spike", "memory-leak"))
        self.assertEqual(current.systems, ("payments-service",))
        self.assertEqual(current.incident_types, ("disk-full", "memory-leak"))

    # [추가] 모니터링 파일이 없으면 대체 자원을 선택하지 않고 오류를 내는지 확인한다.
    def test_missing_monitoring_file_is_an_explicit_error(self):
        self.monitoring_path.unlink()
        with self.assertRaisesRegex(CatalogError, "monitoring.json"):
            load_catalog(self.data_dir)

    # [추가] 깨진 JSON을 명시적인 카탈로그 오류로 처리하는지 확인한다.
    def test_broken_json_is_an_explicit_error(self):
        self.monitoring_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "monitoring.json"):
            load_catalog(self.data_dir)

    # [추가] 시스템 딕셔너리가 아니거나 빈 목록인 데이터를 거부하는지 확인한다.
    def test_non_object_and_empty_monitoring_are_rejected(self):
        for data in ([], ["auth-service"], None, "auth-service", {}):
            with self.subTest(data=data):
                self.write_monitoring(data)
                with self.assertRaises(CatalogError):
                    load_catalog(self.data_dir)

    # [추가] 형식·경로·길이 규칙을 위반한 시스템 이름을 거부하는지 확인한다.
    def test_invalid_system_names_are_rejected(self):
        for name in ("", "Auth-service", "auth_service", "../auth", "auth--service", "a" * 129):
            with self.subTest(name=name):
                self.write_monitoring({name: {}})
                with self.assertRaisesRegex(CatalogError, "invalid resource name"):
                    load_catalog(self.data_dir)

    # [추가] 런북 디렉터리가 사라지면 원인을 알 수 있는 오류를 내는지 확인한다.
    def test_missing_runbook_directory_is_an_explicit_error(self):
        for path in self.runbooks_dir.iterdir():
            path.unlink()
        self.runbooks_dir.rmdir()
        with self.assertRaisesRegex(CatalogError, "runbooks"):
            load_catalog(self.data_dir)

    # [추가] 읽을 런북이 없거나 다른 확장자만 있을 때 실행 가능 자원으로 취급하지 않는다.
    def test_empty_runbook_catalog_is_rejected(self):
        for path in self.runbooks_dir.iterdir():
            path.unlink()
        (self.runbooks_dir / "README.txt").write_text("Ignored", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "at least one resource"):
            load_catalog(self.data_dir)

    # [추가] 잘못된 런북 식별자가 Markdown 파일명에 있어도 거부하는지 검사한다.
    def test_invalid_markdown_filename_is_rejected(self):
        # [추가] macOS의 대소문자 비구분 파일시스템을 고려해 기존 파일과 다른 이름을 쓴다.
        for name in ("BAD-book.md", "cpu_spike.md", "cpu spike.md", ".md"):
            with self.subTest(name=name):
                path = self.runbooks_dir / name
                path.write_text("# Invalid\n", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(CatalogError, "invalid resource name"):
                        load_catalog(self.data_dir)
                finally:
                    path.unlink()

    # [추가] 현재 작업 폴더를 바꿔도 모듈 기준 기본 데이터 경로를 사용하는지 확인한다.
    def test_default_catalog_is_independent_of_current_working_directory(self):
        original_cwd = Path.cwd()
        try:
            os.chdir(self.data_dir)
            catalog = load_catalog()
        finally:
            os.chdir(original_cwd)

        self.assertIn("payments-service", catalog.systems)
        self.assertIn("cpu-spike", catalog.incident_types)


# [추가] 테스트 파일을 직접 실행할 때도 표준 unittest 실행기를 사용한다.
if __name__ == "__main__":
    unittest.main()

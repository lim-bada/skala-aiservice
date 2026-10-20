"""Local, deterministic tests for workflow resource catalog snapshots."""

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from agent.workflow.catalog import CatalogError, WorkflowCatalog, load_catalog


class WorkflowCatalogTests(unittest.TestCase):
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

    def write_monitoring(self, data):
        self.monitoring_path.write_text(json.dumps(data), encoding="utf-8")

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

    def test_constructor_copies_caller_owned_collections(self):
        systems = ["web-01", "auth-service"]
        catalog = WorkflowCatalog(systems=systems, incident_types=["cpu-spike"])
        systems.clear()

        self.assertEqual(catalog.systems, ("auth-service", "web-01"))

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

    def test_missing_monitoring_file_is_an_explicit_error(self):
        self.monitoring_path.unlink()
        with self.assertRaisesRegex(CatalogError, "monitoring.json"):
            load_catalog(self.data_dir)

    def test_broken_json_is_an_explicit_error(self):
        self.monitoring_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "monitoring.json"):
            load_catalog(self.data_dir)

    def test_non_object_and_empty_monitoring_are_rejected(self):
        for data in ([], ["auth-service"], None, "auth-service", {}):
            with self.subTest(data=data):
                self.write_monitoring(data)
                with self.assertRaises(CatalogError):
                    load_catalog(self.data_dir)

    def test_invalid_system_names_are_rejected(self):
        for name in ("", "Auth-service", "auth_service", "../auth", "auth--service", "a" * 129):
            with self.subTest(name=name):
                self.write_monitoring({name: {}})
                with self.assertRaisesRegex(CatalogError, "invalid resource name"):
                    load_catalog(self.data_dir)

    def test_missing_runbook_directory_is_an_explicit_error(self):
        for path in self.runbooks_dir.iterdir():
            path.unlink()
        self.runbooks_dir.rmdir()
        with self.assertRaisesRegex(CatalogError, "runbooks"):
            load_catalog(self.data_dir)

    def test_empty_runbook_catalog_is_rejected(self):
        for path in self.runbooks_dir.iterdir():
            path.unlink()
        (self.runbooks_dir / "README.txt").write_text("Ignored", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "at least one resource"):
            load_catalog(self.data_dir)

    def test_invalid_markdown_filename_is_rejected(self):
        # Use a distinct name: common macOS volumes match paths case-insensitively.
        for name in ("BAD-book.md", "cpu_spike.md", "cpu spike.md", ".md"):
            with self.subTest(name=name):
                path = self.runbooks_dir / name
                path.write_text("# Invalid\n", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(CatalogError, "invalid resource name"):
                        load_catalog(self.data_dir)
                finally:
                    path.unlink()

    def test_default_catalog_is_independent_of_current_working_directory(self):
        original_cwd = Path.cwd()
        try:
            os.chdir(self.data_dir)
            catalog = load_catalog()
        finally:
            os.chdir(original_cwd)

        self.assertIn("payments-service", catalog.systems)
        self.assertIn("cpu-spike", catalog.incident_types)


if __name__ == "__main__":
    unittest.main()

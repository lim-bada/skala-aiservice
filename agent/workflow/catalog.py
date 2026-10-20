"""Available workflow resources, loaded from the project's local data files.

This module exposes resource names only. The model cannot choose file paths or
MCP tool names, and a missing catalog never falls back to a default system.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Iterable, get_args

from .models import (
    MAX_RESOURCE_NAME_LENGTH,
    RESOURCE_NAME_PATTERN,
    NumericField,
    NumericOperator,
    StatusOperator,
    StatusValue,
)


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_RESOURCE_NAME = re.compile(RESOURCE_NAME_PATTERN)


class CatalogError(ValueError):
    """The local workflow resource catalog is missing or invalid."""


def _resource_names(names: Iterable[str], source: str) -> tuple[str, ...]:
    """Validate and copy identifiers into a sorted, immutable snapshot."""
    if isinstance(names, (str, bytes)):
        raise CatalogError(f"{source}: expected a collection of resource names")

    result = []
    for name in names:
        if (
            not isinstance(name, str)
            or len(name) > MAX_RESOURCE_NAME_LENGTH
            or _RESOURCE_NAME.fullmatch(name) is None
        ):
            raise CatalogError(f"{source}: invalid resource name {name!r}")
        result.append(name)

    if not result:
        raise CatalogError(f"{source}: at least one resource is required")
    return tuple(sorted(set(result)))


@dataclass(frozen=True)
class WorkflowCatalog:
    """Immutable names and condition capabilities shared by all workflow stages."""

    systems: tuple[str, ...]
    incident_types: tuple[str, ...]

    numeric_fields: ClassVar[tuple[str, ...]] = get_args(NumericField)
    numeric_operators: ClassVar[tuple[str, ...]] = get_args(NumericOperator)
    status_operators: ClassVar[tuple[str, ...]] = get_args(StatusOperator)
    status_values: ClassVar[tuple[str, ...]] = get_args(StatusValue)
    condition_fields: ClassVar[tuple[str, ...]] = (*numeric_fields, "status")

    def __post_init__(self) -> None:
        # Copy caller-owned collections too, so mutation cannot alter a snapshot.
        object.__setattr__(self, "systems", _resource_names(self.systems, "systems"))
        object.__setattr__(
            self,
            "incident_types",
            _resource_names(self.incident_types, "incident_types"),
        )

    def to_dict(self) -> dict:
        """Return a fresh JSON-compatible payload for prompts and the catalog API."""
        fields = {
            field: {"type": "number", "operators": list(self.numeric_operators)}
            for field in self.numeric_fields
        }
        fields["status"] = {
            "type": "string",
            "operators": list(self.status_operators),
            "values": list(self.status_values),
        }
        return {
            "systems": list(self.systems),
            "incident_types": list(self.incident_types),
            "condition_fields": fields,
        }


def load_catalog(data_dir: Path | None = None) -> WorkflowCatalog:
    """Read a fresh catalog, independent of the process working directory.

    ``data_dir`` is the directory containing ``monitoring.json`` and ``runbooks``.
    It is an application/test configuration value, never a workflow AST field.
    Invalid or missing resources raise ``CatalogError`` before a plan can run.
    """
    directory = DEFAULT_DATA_DIR if data_dir is None else Path(data_dir)
    monitoring_path = directory / "monitoring.json"
    try:
        monitoring = json.loads(monitoring_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot read monitoring catalog {monitoring_path}: {exc}") from exc

    if not isinstance(monitoring, dict):
        raise CatalogError(f"{monitoring_path}: expected a JSON object of systems")
    systems = _resource_names(monitoring.keys(), str(monitoring_path))

    runbooks_dir = directory / "runbooks"
    try:
        incident_types = _resource_names(
            (
                path.stem
                for path in runbooks_dir.iterdir()
                if path.name.endswith(".md") and path.is_file()
            ),
            str(runbooks_dir),
        )
    except OSError as exc:
        raise CatalogError(f"Cannot read runbook catalog {runbooks_dir}: {exc}") from exc

    return WorkflowCatalog(systems=systems, incident_types=incident_types)

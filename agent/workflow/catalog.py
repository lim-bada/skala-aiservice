# [추가] 새 파일 전체: 워크플로에서 사용할 시스템·런북·조건 목록을 일관되게 제공한다.
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


# [추가] 실행 위치와 무관한 기본 데이터 경로와 모델에서 공유한 식별자 규칙을 준비한다.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_RESOURCE_NAME = re.compile(RESOURCE_NAME_PATTERN)


# [추가] 자원 목록을 읽거나 검증하지 못한 경우를 호출자가 구분하도록 전용 오류를 정의한다.
class CatalogError(ValueError):
    """The local workflow resource catalog is missing or invalid."""


# [추가] 자원 이름의 형식·길이와 빈 목록을 검사하고 정렬된 불변 목록으로 변환한다.
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


# [추가] 생성·검증·실행 단계가 함께 사용할 변경 불가능한 자원 스냅샷을 정의한다.
@dataclass(frozen=True)
class WorkflowCatalog:
    """Immutable names and condition capabilities shared by all workflow stages."""

    systems: tuple[str, ...]
    incident_types: tuple[str, ...]

    # [추가] 조건 스키마의 허용값을 재사용해 모델과 카탈로그의 규칙 불일치를 방지한다.
    numeric_fields: ClassVar[tuple[str, ...]] = get_args(NumericField)
    numeric_operators: ClassVar[tuple[str, ...]] = get_args(NumericOperator)
    status_operators: ClassVar[tuple[str, ...]] = get_args(StatusOperator)
    status_values: ClassVar[tuple[str, ...]] = get_args(StatusValue)
    condition_fields: ClassVar[tuple[str, ...]] = (*numeric_fields, "status")

    # [추가] 외부에서 전달한 가변 목록도 복사해 기존 스냅샷이 바뀌지 않도록 한다.
    def __post_init__(self) -> None:
        object.__setattr__(self, "systems", _resource_names(self.systems, "systems"))
        object.__setattr__(
            self,
            "incident_types",
            _resource_names(self.incident_types, "incident_types"),
        )

    # [추가] 프롬프트와 API에서 사용할 독립적인 JSON 직렬화용 목록을 만든다.
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


# [추가] 매 호출마다 현재 데이터 파일을 읽고, 실패를 기본 시스템 치환 없이 명시한다.
def load_catalog(data_dir: Path | None = None) -> WorkflowCatalog:
    """Read a fresh catalog, independent of the process working directory.

    ``data_dir`` is the directory containing ``monitoring.json`` and ``runbooks``.
    It is an application/test configuration value, never a workflow AST field.
    Invalid or missing resources raise ``CatalogError`` before a plan can run.
    """
    # [추가] 애플리케이션 또는 테스트에서 지정한 경로의 모니터링 시스템 목록을 확인한다.
    directory = DEFAULT_DATA_DIR if data_dir is None else Path(data_dir)
    monitoring_path = directory / "monitoring.json"
    try:
        monitoring = json.loads(monitoring_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot read monitoring catalog {monitoring_path}: {exc}") from exc

    if not isinstance(monitoring, dict):
        raise CatalogError(f"{monitoring_path}: expected a JSON object of systems")
    systems = _resource_names(monitoring.keys(), str(monitoring_path))

    # [추가] 실제 Markdown 런북의 파일명만 허용된 장애 유형으로 등록한다.
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

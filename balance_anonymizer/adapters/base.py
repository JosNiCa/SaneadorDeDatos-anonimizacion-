from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..models import AnonymizationPlan, DocumentSnapshot
from ..pseudonyms import Pseudonymizer


class AdapterError(RuntimeError):
    """Fallo de adaptador que no debe incorporar PII al reporte."""

    def __init__(
        self,
        code: str,
        *,
        diagnostic_stage: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.diagnostic_stage = diagnostic_stage
        self.diagnostic = diagnostic or {}


@dataclass
class AdapterOutput:
    temporary_path: Path
    profile: str
    substitutions: dict[str, int] = field(default_factory=dict)
    validation: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    snapshot: DocumentSnapshot | None = None


class BalanceAdapter(Protocol):
    name: str
    suffixes: tuple[str, ...]

    def discover(
        self,
        source: Path,
        pseudonymizer: Pseudonymizer,
        *,
        strict: bool,
    ) -> DocumentSnapshot: ...

    def apply(
        self,
        snapshot: DocumentSnapshot,
        plan: AnonymizationPlan,
        temporary_dir: Path,
        *,
        strict: bool,
    ) -> AdapterOutput: ...

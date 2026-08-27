"""Adaptadores de rechazo explícito para formatos heredados."""

from __future__ import annotations

from pathlib import Path

from ..models import AnonymizationPlan, DocumentSnapshot
from ..pseudonyms import Pseudonymizer
from .base import AdapterError, AdapterOutput


class LegacyXlsAdapter:
    """Reporta XLS binario sin convertirlo silenciosamente a OOXML."""

    name = "xls"
    suffixes = (".xls",)

    def discover(
        self,
        source: Path,
        pseudonymizer: Pseudonymizer,
        *,
        strict: bool,
    ) -> DocumentSnapshot:
        raise AdapterError("UNSUPPORTED_XLS")

    def apply(
        self,
        snapshot: DocumentSnapshot,
        plan: AnonymizationPlan,
        temporary_dir: Path,
        *,
        strict: bool,
    ) -> AdapterOutput:
        raise AdapterError("UNSUPPORTED_XLS")

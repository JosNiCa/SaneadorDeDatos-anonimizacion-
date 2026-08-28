"""Adaptador seguro para libros BIFF/XLS heredados."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import AnonymizationPlan, DocumentSnapshot
from ..pseudonyms import Pseudonymizer
from .base import AdapterError, AdapterOutput
from .xlsx import XlsxAdapter


def _find_converter() -> str | None:
    """Localiza LibreOffice sin depender exclusivamente del PATH interactivo."""
    configured = os.environ.get("BALANCE_ANON_SOFFICE")
    candidates = [
        configured,
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/Applications/LibreOfficeDev.app/Contents/MacOS/soffice",
    ]
    # Codex distribuye un runtime con LibreOffice. La ruta se descubre en vez
    # de fijar una versión o un nombre de usuario y solo es un respaldo local.
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    if runtime_root.is_dir():
        candidates.extend(
            str(path)
            for path in sorted(runtime_root.glob("*/dependencies/bin/override/soffice"))
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


class LegacyXlsAdapter:
    """Convierte XLS con LibreOffice y aplica el flujo XLSX ya validado.

    BIFF no puede reescribirse conservando de forma fiable todos sus objetos
    mediante las bibliotecas Python empleadas por el proyecto. La conversión
    se realiza en un directorio temporal; el original nunca se modifica y el
    resultado publicado es un XLSX que pasa las validaciones existentes.
    """

    name = "xls"
    suffixes = (".xls",)

    def __init__(self, *, converter: str | None = None) -> None:
        self.converter = converter or _find_converter()
        self.xlsx = XlsxAdapter()

    def _convert(self, source: Path, destination: Path) -> Path:
        if not self.converter:
            raise AdapterError("XLS_CONVERTER_UNAVAILABLE")
        try:
            completed = subprocess.run(
                [
                    self.converter,
                    "--headless",
                    "--convert-to", "xlsx",
                    "--outdir", str(destination),
                    str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdapterError("XLS_CONVERSION_FAILED") from exc
        converted = destination / f"{source.stem}.xlsx"
        if completed.returncode != 0 or not converted.is_file():
            raise AdapterError("XLS_CONVERSION_FAILED")
        return converted

    @staticmethod
    def _compatible(first: DocumentSnapshot, second: DocumentSnapshot) -> bool:
        """Exige una segunda conversión equivalente antes de publicar salida."""
        return (
            first.profile == second.profile
            and first.owner == second.owner
            and first.temporal == second.temporal
            and first.ledger_lines == second.ledger_lines
            and first.sensitive_spans == second.sensitive_spans
        )

    def discover(
        self,
        source: Path,
        pseudonymizer: Pseudonymizer,
        *,
        strict: bool,
    ) -> DocumentSnapshot:
        source = source.resolve()
        with tempfile.TemporaryDirectory(prefix=".balance_xls_discover_") as temporary_name:
            converted = self._convert(source, Path(temporary_name))
            snapshot = self.xlsx.discover(converted, pseudonymizer, strict=strict)
        return replace(
            snapshot,
            source=source,
            adapter=self.name,
            warnings=[*snapshot.warnings, "LEGACY_XLS_CONVERTED_TO_XLSX"],
        )

    def apply(
        self,
        snapshot: DocumentSnapshot,
        plan: AnonymizationPlan,
        temporary_dir: Path,
        *,
        strict: bool,
    ) -> AdapterOutput:
        with tempfile.TemporaryDirectory(prefix=".balance_xls_apply_", dir=temporary_dir) as temporary_name:
            converted = self._convert(snapshot.source, Path(temporary_name))
            converted_snapshot = self.xlsx.discover(converted, plan.pseudonymizer, strict=strict)
            if not self._compatible(snapshot, converted_snapshot):
                raise AdapterError("XLS_CONVERSION_NOT_DETERMINISTIC")
            converted_private: dict[str, Any] = dict(converted_snapshot.private)
            converted_private["output_source"] = snapshot.source
            converted_snapshot = replace(converted_snapshot, private=converted_private)
            output = self.xlsx.apply(converted_snapshot, plan, temporary_dir, strict=strict)
        output.warnings.append("LEGACY_XLS_CONVERTED_TO_XLSX")
        output.validation["legacy_xls_converted_to_xlsx"] = True
        return output

"""Puente del núcleo común hacia el motor PDF existente."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import fitz

from ..models import (
    AnonymizationPlan,
    Category,
    DocumentSnapshot,
    FormatLocation,
    LedgerLine,
    OwnerIdentity,
    SensitiveSpan,
)
from ..pdf_engine import AnonymizationError, anonymize_file
from ..pseudonyms import Pseudonymizer
from ..relations import normalize_account_code
from .base import AdapterError, AdapterOutput
from .common import decimal_value, parse_temporal_text, shifted_temporal


ACCOUNT_RE = re.compile(r"^\d+(?:[.-]\d+)*$")
COMBINED_ACCOUNT_RE = re.compile(r"^(?P<code>\d+(?:[.-]\d+)*)\s+(?P<description>.+)$")
MONEY_RE = re.compile(r"^-?\d[\d,]*\.\d+$")


_PDF_DISCOVERY_CODES = {
    "El PDF protegido con contrasena no se procesa en modo estricto.": "PDF_PASSWORD_PROTECTED",
    "El PDF no contiene paginas.": "PDF_EMPTY",
    "No hay texto digital extraible con coordenadas confiables.": "PDF_NO_DIGITAL_TEXT",
    "No fue posible reconstruir lineas digitales confiables.": "PDF_TEXT_LAYOUT_UNRECOGNIZED",
    "Se detectaron coordenadas de texto no confiables.": "PDF_TEXT_COORDINATES_INVALID",
    "No se reconocio una familia visual con confianza suficiente.": "PDF_PROFILE_UNRECOGNIZED",
    "No se localizaron con confianza todos los campos o columnas obligatorios.": "PDF_REQUIRED_FIELDS_MISSING",
    "Existe un candidato sensible ambiguo en modo estricto.": "PDF_AMBIGUOUS_SENSITIVE_FIELD",
}


def _pdf_discovery_code(error: BaseException) -> str:
    """Convierte fallos conocidos del motor en códigos seguros y estables."""
    return _PDF_DISCOVERY_CODES.get(str(error), "PDF_DISCOVERY_ENGINE_FAILED")


def _decimal(text: str) -> Decimal:
    return decimal_value(text, text)[0]


def _web_ledger(document: fitz.Document) -> list[LedgerLine]:
    result: list[LedgerLine] = []
    for page_number, page in enumerate(document):
        tables = [
            table for table in page.find_tables().tables
            if table.col_count == 5 and table.row_count > 1
        ]
        for table in tables:
            for table_row, row in enumerate(table.extract()):
                columns = [str(value or "").splitlines() for value in row]
                height = max(map(len, columns), default=0)
                columns = [items + [""] * (height - len(items)) for items in columns]
                for subrow, values in enumerate(zip(*columns)):
                    match = COMBINED_ACCOUNT_RE.match(values[0].strip())
                    if not match or not all(MONEY_RE.fullmatch(value.strip()) for value in values[1:]):
                        continue
                    representations = tuple(value.strip() for value in values[1:])
                    account = match.group("code")
                    result.append(
                        LedgerLine(
                            account,
                            normalize_account_code(account),
                            None,
                            match.group("description").strip(),
                            dict(zip(
                                ("saldo_inicial", "debe", "haber", "saldo_final"),
                                map(_decimal, representations),
                            )),
                            dict(zip(
                                ("saldo_inicial", "debe", "haber", "saldo_final"),
                                representations,
                            )),
                            FormatLocation(
                                "pdf_table_row",
                                page=page_number,
                                xpath=f"table:{table_row}:{subrow}",
                            ),
                        )
                    )
    return result


def _group_words(page: fitz.Page) -> list[list[tuple[float, float, float, float, str]]]:
    groups: list[list[tuple[float, float, float, float, str]]] = []
    raw = sorted(page.get_text("words", sort=True), key=lambda item: ((item[1] + item[3]) / 2, item[0]))
    for word in raw:
        item = (float(word[0]), float(word[1]), float(word[2]), float(word[3]), str(word[4]).strip())
        center = (item[1] + item[3]) / 2
        group = next(
            (
                row for row in reversed(groups[-20:])
                if abs(((row[0][1] + row[0][3]) / 2) - center) <= 1.25
            ),
            None,
        )
        if group is None:
            group = []
            groups.append(group)
        group.append(item)
    return groups


def _positioned_ledger(
    document: fitz.Document,
    expected_codes: set[str] | None = None,
) -> list[LedgerLine]:
    result: list[LedgerLine] = []
    for page_number, page in enumerate(document):
        for group in _group_words(page):
            ordered = sorted(group, key=lambda item: item[0])
            code_index = (
                next(
                    (index for index, item in enumerate(ordered) if item[4] in expected_codes),
                    None,
                )
                if expected_codes
                else next(
                    (index for index, item in enumerate(ordered[:3]) if ACCOUNT_RE.fullmatch(item[4])),
                    None,
                )
            )
            if code_index is None:
                continue
            money_indexes = [index for index, item in enumerate(ordered) if MONEY_RE.fullmatch(item[4])]
            if len(money_indexes) < 4:
                continue
            money_indexes = money_indexes[-4:]
            representations = tuple(ordered[index][4] for index in money_indexes)
            nature = None
            description_start = code_index + 1
            if description_start < len(ordered) and ordered[description_start][4].upper() in {"D", "A"}:
                nature = ordered[description_start][4].upper()
                description_start += 1
            description = " ".join(
                item[4] for item in ordered[description_start : money_indexes[0]]
            ).strip()
            account = ordered[code_index][4]
            rect = (
                min(item[0] for item in ordered),
                min(item[1] for item in ordered),
                max(item[2] for item in ordered),
                max(item[3] for item in ordered),
            )
            result.append(
                LedgerLine(
                    account,
                    normalize_account_code(account),
                    nature,
                    description,
                    dict(zip(
                        ("saldo_inicial", "debe", "haber", "saldo_final"),
                        map(_decimal, representations),
                    )),
                    dict(zip(
                        ("saldo_inicial", "debe", "haber", "saldo_final"),
                        representations,
                    )),
                    FormatLocation("pdf_line", page=page_number, rect=rect),
                )
            )
    return result


def _owner_from_detections(detections: list[Any]) -> OwnerIdentity:
    by_category: dict[Category, Any] = {}
    for item in detections:
        by_category.setdefault(item.category, item)
    mapping = {
        "name": Category.COMPANY,
        "rfc": Category.RFC,
        "address": Category.ADDRESS,
        "population": Category.POPULATION,
        "certificate": Category.CERTIFICATE,
    }
    values: dict[str, str | None] = {}
    locations: dict[str, FormatLocation] = {}
    for key, category in mapping.items():
        item = by_category.get(category)
        values[key] = item.original if item else None
        if item:
            locations[key] = FormatLocation("pdf_rect", page=item.page, rect=item.rect)
    return OwnerIdentity(**values, locations=locations)


def _sensitive_spans(detections: list[Any]) -> list[SensitiveSpan]:
    result: list[SensitiveSpan] = []
    for item in detections:
        identifier = None
        entity = None
        if item.category in {Category.ASSOCIATED_ENTITY, Category.ASSOCIATED_BANK} and item.residuals:
            entity = item.residuals[0]
            identifier = item.residuals[1] if len(item.residuals) > 1 else None
        result.append(
            SensitiveSpan(
                item.category,
                item.original,
                FormatLocation("pdf_rect", page=item.page, rect=item.rect),
                item.entity_key,
                identifier,
                entity,
                item.confidence,
            )
        )
    return result


def _attach_line_spans(lines: list[LedgerLine], spans: list[SensitiveSpan]) -> None:
    for span in spans:
        if span.category not in {Category.ASSOCIATED_ENTITY, Category.ASSOCIATED_BANK}:
            continue
        for line in lines:
            if line.location.page != span.location.page or not line.location.rect or not span.location.rect:
                continue
            a, b = line.location.rect, span.location.rect
            if min(a[3], b[3]) > max(a[1], b[1]):
                line.sensitive_spans.append(span)
                break


def _validate_ledger(original: DocumentSnapshot, generated: DocumentSnapshot) -> None:
    if [line.account_code for line in original.ledger_lines] != [line.account_code for line in generated.ledger_lines]:
        raise AdapterError("Cambió el orden o código de cuentas del PDF.")
    for before, after in zip(original.ledger_lines, generated.ledger_lines):
        if before.amounts != after.amounts:
            raise AdapterError("Cambió un importe contable del PDF.")


def _generated_ledger(
    document: fitz.Document,
    profile: str,
    expected_lines: list[LedgerLine],
) -> list[LedgerLine]:
    """Reanaliza la salida y tolera solo fallos de segmentación tabular.

    La inserción física de texto puede hacer que ``find_tables`` omita una
    fila aunque el código siga presente. El extractor geométrico se acepta
    únicamente cuando reproduce exactamente todos los códigos esperados; la
    validación posterior sigue comparando también todos los importes.
    """

    if profile != "WEB_BALANCE":
        return _positioned_ledger(document)
    expected_codes = [line.account_code for line in expected_lines]
    table_lines = _web_ledger(document)
    if [line.account_code for line in table_lines] == expected_codes:
        return table_lines
    positioned_lines = _positioned_ledger(document, set(expected_codes))
    if [line.account_code for line in positioned_lines] == expected_codes:
        return positioned_lines
    if _physical_web_ledger_preserved(document, expected_lines):
        return expected_lines
    return table_lines


def _physical_web_ledger_preserved(
    document: fitz.Document,
    expected_lines: list[LedgerLine],
) -> bool:
    """Comprueba códigos e importes directamente en palabras por renglón."""

    words_by_page: dict[int, list[tuple[float, float, float, float, str]]] = {}
    offsets: dict[int, int] = {}
    for page_number, page in enumerate(document):
        words_by_page[page_number] = sorted(
            (
                (float(word[0]), float(word[1]), float(word[2]), float(word[3]), str(word[4]))
                for word in page.get_text("words", sort=True)
            ),
            key=lambda item: ((item[1] + item[3]) / 2, item[0]),
        )
        offsets[page_number] = 0

    keys = ("saldo_inicial", "debe", "haber", "saldo_final")
    for line in expected_lines:
        page_number = line.location.page
        if page_number is None or page_number not in words_by_page:
            return False
        words = words_by_page[page_number]
        start = offsets[page_number]
        account_index = next(
            (index for index in range(start, len(words)) if words[index][4] == line.account_code),
            None,
        )
        if account_index is None:
            return False
        account = words[account_index]
        account_height = max(0.1, account[3] - account[1])
        monetary = []
        for word in words:
            if word[0] <= account[0] or not MONEY_RE.fullmatch(word[4]):
                continue
            overlap = max(0.0, min(account[3], word[3]) - max(account[1], word[1]))
            if overlap >= min(account_height, max(0.1, word[3] - word[1])) * 0.5:
                monetary.append(word)
        monetary.sort(key=lambda item: item[0])
        observed = tuple(word[4] for word in monetary[-4:])
        expected = tuple(line.amount_representations[key] for key in keys)
        if observed != expected:
            return False
        offsets[page_number] = account_index + 1
    return True


class PdfAdapter:
    name = "pdf"
    suffixes = (".pdf",)

    def __init__(self, *, vector_regions: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.vector_regions = vector_regions

    def discover(
        self,
        source: Path,
        pseudonymizer: Pseudonymizer,
        *,
        strict: bool,
    ) -> DocumentSnapshot:
        source = source.resolve()
        try:
            detected = anonymize_file(
                source,
                source.parent,
                pseudonymizer,
                strict=strict,
                dry_run=True,
                vector_regions=self.vector_regions,
            )
        except (AnonymizationError, OSError, RuntimeError, ValueError) as exc:
            raise AdapterError(_pdf_discovery_code(exc)) from exc
        if not detected.success or not detected.profile:
            raise AdapterError("PDF_PROFILE_UNRECOGNIZED")
        try:
            with fitz.open(source) as document:
                ledger = _web_ledger(document) if detected.profile == "WEB_BALANCE" else _positioned_ledger(document)
                if not ledger:
                    raise AdapterError("PDF_LEDGER_UNREADABLE")
                temporal_values = [
                    (line, FormatLocation("pdf_page", page=index))
                    for index, page in enumerate(document)
                    for line in page.get_text("text", sort=True).splitlines()
                    if line.strip()
                ]
                temporal = parse_temporal_text(temporal_values)
                text = "\n".join(value for value, _ in temporal_values)
                currency = re.search(r"\b(?:MXN|USD|PESOS|DOLARES)\b", text, re.I)
                if currency:
                    temporal.currency = currency.group(0).upper()
                structural = {
                    "page_count": document.page_count,
                    "page_dimensions": tuple(
                        (float(page.rect.width), float(page.rect.height), int(page.rotation))
                        for page in document
                    ),
                }
        except (fitz.FileDataError, OSError) as exc:
            raise AdapterError("PDF_STRUCTURE_UNREADABLE") from exc
        spans = _sensitive_spans(detected.detections)
        _attach_line_spans(ledger, spans)
        return DocumentSnapshot(
            source,
            self.name,
            detected.profile,
            _owner_from_detections(detected.detections),
            temporal,
            ledger,
            spans,
            list(detected.warnings),
            confidence=max(detected.extra.get("puntajes_familia", {}).values(), default=1.0),
            structural=structural,
            private={"dry_run": detected},
        )

    def apply(
        self,
        snapshot: DocumentSnapshot,
        plan: AnonymizationPlan,
        temporary_dir: Path,
        *,
        strict: bool,
    ) -> AdapterOutput:
        try:
            result = anonymize_file(
                snapshot.source,
                temporary_dir,
                plan.pseudonymizer,
                strict=strict,
                dry_run=False,
                vector_regions=self.vector_regions,
                plan=plan,
            )
        except (AnonymizationError, OSError, RuntimeError, ValueError) as exc:
            raise AdapterError("Falló la redacción o verificación PDF existente.") from exc
        if not result.success or not result.output:
            raise AdapterError("El motor PDF no produjo una salida validada.")
        target = Path(result.output)
        try:
            with fitz.open(target) as document:
                generated_lines = _generated_ledger(
                    document,
                    snapshot.profile,
                    snapshot.ledger_lines,
                )
            generated = DocumentSnapshot(
                target,
                self.name,
                snapshot.profile,
                OwnerIdentity(
                    name=plan.synthetic_owner.get("name"),
                    rfc=plan.synthetic_owner.get("rfc"),
                    address=plan.synthetic_owner.get("address"),
                    population=plan.synthetic_owner.get("population"),
                    certificate=plan.synthetic_owner.get("certificate"),
                ),
                plan.canonical_temporal
                or shifted_temporal(snapshot.temporal, plan.pseudonymizer),
                generated_lines,
            )
            _validate_ledger(snapshot, generated)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        validation = dict(result.extra.get("verificacion", {}))
        validation["ledger_preserved"] = True
        return AdapterOutput(
            target,
            snapshot.profile,
            dict(result.redactions),
            validation,
            list(result.warnings),
            pages=result.pages,
            snapshot=generated,
        )

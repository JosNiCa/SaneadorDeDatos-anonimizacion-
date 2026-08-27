"""Detección común no posicional para XLSX y metadatos canónicos."""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from ..models import Category, FormatLocation, SensitiveSpan, TemporalMetadata
from ..pseudonyms import Pseudonymizer, canonical_association_key, normalize


RFC_RE = re.compile(r"(?<![A-Z0-9&Ñ])([A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3})(?![A-Z0-9])", re.I)
EXPLICIT_IDENTIFIER_RE = re.compile(
    r"\b(?P<prefix>CTA\.?|CUENTA|CLABE|TARJETA|(?:N[ÚU]M(?:ERO)?\.?\s+DE\s+)?CLIENTE)"
    r"(?P<separator>\s*[:.#-]?\s*)(?P<identifier>\d(?:[\d .-]*\d)?)(?!\d)",
    re.I,
)
GENERIC_IDENTIFIER_RE = re.compile(r"(?<![\d,.])\d(?:[\d-]*\d){2,}(?![\d,.])")
LEGAL_RE = re.compile(
    r"\b(?:S\.?\s*A\.?|S\.?\s+DE\s+R\.?\s*L\.?|A\.?\s*C\.?|S\.?\s*C\.?)\b",
    re.I,
)
BANK_RE = re.compile(r"\b(?:BANCO|BANCOS|BANCARIA|BANCARIO|HSBC|BBVA|BANAMEX|SANTANDER|BANORTE)\b", re.I)

MONTHS = {
    "ENE": 1,
    "ENERO": 1,
    "FEB": 2,
    "FEBRERO": 2,
    "MAR": 3,
    "MARZO": 3,
    "ABR": 4,
    "ABRIL": 4,
    "MAY": 5,
    "MAYO": 5,
    "JUN": 6,
    "JUNIO": 6,
    "JUL": 7,
    "JULIO": 7,
    "AGO": 8,
    "AGOSTO": 8,
    "SEP": 9,
    "SEPT": 9,
    "SEPTIEMBRE": 9,
    "OCT": 10,
    "OCTUBRE": 10,
    "NOV": 11,
    "NOVIEMBRE": 11,
    "DIC": 12,
    "DICIEMBRE": 12,
}
MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})(?P<s1>[-/.])"
    r"(?P<month>\d{1,2}|[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,12})(?P<s2>[-/.])"
    r"(?P<year>\d{2,4})(?!\d)",
    re.I,
)
YEAR_MONTH_RE = re.compile(r"(?<!\d)(?P<year>\d{4})(?P<sep>[-/])(?P<month>\d{1,2})(?!\d)")


def decimal_value(value: object, representation: str | None = None) -> tuple[Decimal, str]:
    text = representation if representation is not None else str(value)
    compact = text.strip().replace(",", "")
    if compact in {"", "-"}:
        return Decimal(0), text
    try:
        return Decimal(compact), text
    except InvalidOperation as exc:
        raise ValueError("Importe decimal no compatible.") from exc


def detect_description_span(text: str, location: FormatLocation) -> SensitiveSpan | None:
    rfc_match = RFC_RE.search(text)
    if rfc_match:
        rfc = rfc_match.group(1)
        entity = (text[: rfc_match.start()] + " " + text[rfc_match.end() :]).strip(" :-;,.\t")
        if sum(char.isalpha() for char in entity) >= 3:
            return SensitiveSpan(
                Category.ASSOCIATED_ENTITY,
                text,
                location,
                entity_key=normalize(rfc),
                identifier=rfc,
                entity_text=entity,
                confidence=0.995,
            )

    explicit = EXPLICIT_IDENTIFIER_RE.search(text)
    if explicit and len(re.sub(r"\D", "", explicit.group("identifier"))) > 2:
        identifier = explicit.group("identifier").strip()
        entity = text[: explicit.start()].strip(" :-;,.\t")
        if not entity:
            entity = text[explicit.end() :].strip(" :-;,.\t")
        if sum(char.isalpha() for char in entity) >= 3:
            category = Category.ASSOCIATED_BANK if BANK_RE.search(entity) else Category.ASSOCIATED_ENTITY
            return SensitiveSpan(
                category,
                text,
                location,
                entity_key=canonical_association_key(entity, identifier),
                identifier=identifier,
                entity_text=entity,
                confidence=0.99,
            )

    for match in GENERIC_IDENTIFIER_RE.finditer(text):
        identifier = match.group(0)
        digits = re.sub(r"\D", "", identifier)
        if len(digits) <= 2 or re.fullmatch(r"(?:19|20)\d{2}", digits):
            continue
        entity = (text[: match.start()] + " " + text[match.end() :]).strip(" :-;,.\t")
        words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}", entity)
        if sum(char.isalpha() for char in entity) < 7 or len(words) < 2:
            continue
        category = Category.ASSOCIATED_BANK if BANK_RE.search(entity) else Category.ASSOCIATED_ENTITY
        return SensitiveSpan(
            category,
            text,
            location,
            entity_key=canonical_association_key(entity, identifier),
            identifier=identifier,
            entity_text=entity,
            confidence=0.92,
        )
    return None


def replacement_for_description(span: SensitiveSpan, pseudo: Pseudonymizer) -> str:
    entity = span.entity_text or "ENTIDAD"
    identifier = span.identifier or ""
    key = span.entity_key or canonical_association_key(entity, identifier)
    if RFC_RE.fullmatch(identifier):
        replacement_identifier = pseudo.rfc(identifier, key)
        replacement_entity = (
            pseudo.person(key)
            if len(re.sub(r"\W", "", identifier)) == 13
            else pseudo.company(key)
        )
    else:
        replacement_identifier = pseudo.numeric_identifier(identifier, key)
        if span.category == Category.ASSOCIATED_BANK:
            replacement_entity = pseudo.bank(key)
        elif LEGAL_RE.search(entity):
            replacement_entity = pseudo.company(key)
        else:
            replacement_entity = pseudo.person(key)
    value = span.original.replace(entity, replacement_entity, 1)
    return value.replace(identifier, replacement_identifier, 1)


def parse_temporal_text(values: list[tuple[str, FormatLocation]]) -> TemporalMetadata:
    result = TemporalMetadata()
    date_candidates: list[tuple[date, str, FormatLocation]] = []
    for text, location in values:
        normalized = normalize(text)
        year_month = YEAR_MONTH_RE.search(text)
        if year_month and any(word in normalized for word in ("PERIODO", "EJERCICIO")):
            result.year = int(year_month.group("year"))
            result.month = int(year_month.group("month"))
            result.representations.setdefault("period", year_month.group(0))
            result.locations.setdefault("period", location)
        exercise = re.search(r"EJERCICIO\s*:?\s*(\d{4}).*PER[IÍ]ODO\s*:?\s*(\d{1,2})", text, re.I)
        if exercise:
            result.year, result.month = int(exercise.group(1)), int(exercise.group(2))
            result.representations.setdefault("period", exercise.group(0))
            result.locations.setdefault("period", location)
        for match in DATE_RE.finditer(text):
            month_text = normalize(match.group("month")).rstrip(".")
            month = int(month_text) if month_text.isdigit() else MONTHS.get(month_text)
            if month is None:
                continue
            year = int(match.group("year"))
            if year < 100:
                year += 2000 if year < 70 else 1900
            day = int(match.group("day"))
            try:
                parsed = date(year, month, day)
            except ValueError:
                parsed = date(year, month, min(day, calendar.monthrange(year, month)[1]))
            date_candidates.append((parsed, match.group(0), location))
            if any(word in normalized for word in ("IMPRESION", "CREACION", "FECHA:")):
                result.print_date = parsed
                result.representations.setdefault("print_date", match.group(0))
                result.locations.setdefault("print_date", location)
    period_dates = [item for item in date_candidates if item[0] != result.print_date]
    if period_dates:
        result.period_start = period_dates[0][0]
        result.period_end = period_dates[-1][0]
        result.year, result.month = result.period_start.year, result.period_start.month
        result.representations.setdefault("period_start", period_dates[0][1])
        result.representations.setdefault("period_end", period_dates[-1][1])
        result.locations.setdefault("period_start", period_dates[0][2])
        result.locations.setdefault("period_end", period_dates[-1][2])
    return result


def format_date_like(template: str, value: date) -> str:
    match = DATE_RE.search(template)
    if not match:
        raise ValueError("Formato de fecha no compatible.")
    month_source = match.group("month")
    if month_source.isdigit():
        month = str(value.month).zfill(len(month_source))
    else:
        name = MONTH_NAMES[value.month - 1]
        if len(month_source.rstrip(".")) <= 4:
            name = name[:3]
        if month_source.isupper():
            name = name.upper()
        elif month_source[:1].isupper():
            name = name.capitalize()
        month = name + ("." if month_source.endswith(".") else "")
    year = str(value.year) if len(match.group("year")) == 4 else f"{value.year % 100:02d}"
    replacement = (
        f"{str(value.day).zfill(len(match.group('day')))}"
        f"{match.group('s1')}{month}{match.group('s2')}{year}"
    )
    return template[: match.start()] + replacement + template[match.end() :]


def shifted_temporal(source: TemporalMetadata, pseudo: Pseudonymizer) -> TemporalMetadata:
    """Materializa la política mensual común sin conservar representaciones PII."""

    result = TemporalMetadata(currency=source.currency)
    if source.period_start:
        result.period_start = pseudo.shift_date(source.period_start)
    if source.period_end:
        result.period_end = pseudo.shift_date(source.period_end)
    if source.print_date:
        result.print_date = pseudo.shift_date(source.print_date)
    if result.period_start:
        result.year, result.month = result.period_start.year, result.period_start.month
    elif source.year and source.month:
        shifted = pseudo.shift_date(date(source.year, source.month, 15))
        result.year, result.month = shifted.year, shifted.month
    return result

"""Deteccion conservadora de PII a partir de anclas y coordenadas reales."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import Category, Detection, ProfileResult, WordBox
from .pseudonyms import Pseudonymizer, normalize


_RFC_RE = re.compile(r"\b[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?|\d{1,2}/[A-Za-záéíóúñ]+/\d{2,4}|\d{1,2}/\d{1,2}/\d{2,4})\b",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(r"(?<!\d)(?:\d+(?:[-/]\d+){2,})(?!\d)")


@dataclass(frozen=True)
class Line:
    page: int
    words: tuple[WordBox, ...]

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def y0(self) -> float:
        return min(word.y0 for word in self.words)

    @property
    def x0(self) -> float:
        return min(word.x0 for word in self.words)

    def rect(self, words: Iterable[WordBox] | None = None) -> tuple[float, float, float, float]:
        selected = tuple(words or self.words)
        return (
            min(word.x0 for word in selected),
            min(word.y0 for word in selected),
            max(word.x1 for word in selected),
            max(word.y1 for word in selected),
        )


@dataclass(frozen=True)
class PageLayout:
    number: int
    width: float
    height: float
    lines: tuple[Line, ...]


def layout_from_page(page_number: int, page: Any) -> PageLayout:
    """Crea una estructura de lineas usando el modo `words` de PyMuPDF."""

    raw_words = page.get_text("words", sort=True)
    words = tuple(
        WordBox(
            page=page_number,
            x0=float(item[0]),
            y0=float(item[1]),
            x1=float(item[2]),
            y1=float(item[3]),
            text=str(item[4]),
            block=int(item[5]),
            line=int(item[6]),
            word=int(item[7]),
        )
        for item in raw_words
        if str(item[4]).strip()
    )
    grouped: dict[tuple[int, int], list[WordBox]] = {}
    for word in words:
        grouped.setdefault((word.block, word.line), []).append(word)
    lines = tuple(
        Line(page_number, tuple(sorted(group, key=lambda item: item.word)))
        for _, group in sorted(grouped.items(), key=lambda item: (min(word.y0 for word in item[1]), min(word.x0 for word in item[1])))
    )
    return PageLayout(page_number, float(page.rect.width), float(page.rect.height), lines)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[A-Z0-9Ñ]+", normalize(value))


def _line_has(line: Line, phrase: str) -> bool:
    source = _tokens(line.text)
    target = _tokens(phrase)
    return any(source[index : index + len(target)] == target for index in range(len(source)))


def _find_phrase_words(line: Line, phrase: str) -> tuple[WordBox, ...] | None:
    target = _tokens(phrase)
    flattened: list[tuple[str, int]] = []
    for word_index, word in enumerate(line.words):
        flattened.extend((token, word_index) for token in _tokens(word.text))
    for index in range(len(flattened) - len(target) + 1):
        if [token for token, _ in flattened[index : index + len(target)]] == target:
            first = flattened[index][1]
            last = flattened[index + len(target) - 1][1]
            return line.words[first : last + 1]
    return None


def _words_after(line: Line, phrase: str) -> tuple[WordBox, ...]:
    phrase_words = _find_phrase_words(line, phrase)
    if not phrase_words:
        return ()
    last = line.words.index(phrase_words[-1])
    values = line.words[last + 1 :]
    while values and not re.search(r"[A-Za-z0-9]", values[0].text):
        values = values[1:]
    return values


def _rect_for_range(line: Line, start: int, end: int) -> tuple[WordBox, ...]:
    """Convierte un rango de caracteres del texto unido de una linea en palabras."""

    selected: list[WordBox] = []
    cursor = 0
    for word in line.words:
        word_start, word_end = cursor, cursor + len(word.text)
        if word_start < end and word_end > start:
            selected.append(word)
        cursor = word_end + 1
    return tuple(selected)


def _match_detections(
    line: Line,
    regex: re.Pattern[str],
    category: Category,
    profile: str,
    entity_key: str | None = None,
) -> list[Detection]:
    matches: list[Detection] = []
    for match in regex.finditer(line.text):
        words = _rect_for_range(line, match.start(), match.end())
        if words:
            matches.append(
                Detection(category, line.page, line.rect(words), match.group(), None, profile, entity_key)
            )
    return matches


def _label_value_detection(
    line: Line,
    label: str,
    category: Category,
    profile: str,
    entity_key: str | None = None,
    value_regex: re.Pattern[str] | None = None,
) -> Detection | None:
    values = _words_after(line, label)
    if not values:
        return None
    text = " ".join(word.text for word in values).strip(" :")
    if value_regex:
        match = value_regex.search(text)
        if not match:
            return None
        temporary = Line(line.page, values)
        selected = _rect_for_range(temporary, match.start(), match.end())
        if not selected:
            return None
        text = match.group()
        values = selected
    if not text:
        return None
    return Detection(category, line.page, line.rect(values), text, None, profile, entity_key)


def _non_empty_following(lines: tuple[Line, ...], index: int) -> Line | None:
    return lines[index + 1] if index + 1 < len(lines) and lines[index + 1].words else None


def _is_company_candidate(line: Line) -> bool:
    text = line.text.strip()
    plain = normalize(text)
    letters = sum(char.isalpha() for char in text)
    upper = sum(char.isupper() for char in text)
    return (
        len(text) >= 10
        and letters >= 6
        and upper / max(letters, 1) >= 0.65
        and not re.search(r"\b(BALANZA|DIRECCION|POBLACION|REG\.?\s*FED|RFC|CEDULA|FECHA|DEBE|HABER|SALDO)\b", plain)
        and not _DATE_RE.search(text)
    )


def _dedupe(detections: Iterable[Detection]) -> list[Detection]:
    seen: set[tuple[Any, ...]] = set()
    result: list[Detection] = []
    for item in detections:
        key = (item.category, item.page, tuple(round(value, 1) for value in item.rect), item.original)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _profile_1(layouts: list[PageLayout]) -> ProfileResult:
    profile = "tipo_1"
    all_lines = tuple(line for layout in layouts for line in layout.lines)
    anchors = sum(
        any(_line_has(line, phrase) for line in all_lines)
        for phrase in ("NOMBRE RAZON SOCIAL", "RFC", "FECHA CREACION", "EJERCICIO", "PERIODO")
    )
    if anchors < 3:
        return ProfileResult(profile, 0.0)
    detections: list[Detection] = []
    companies: list[Detection] = []
    rfcs: list[Detection] = []
    for layout in layouts:
        for line_index, line in enumerate(layout.lines):
            if _line_has(line, "NOMBRE RAZON SOCIAL"):
                item = _label_value_detection(line, "NOMBRE RAZON SOCIAL", Category.COMPANY, profile)
                if item is None:
                    following = _non_empty_following(layout.lines, line_index)
                    if following:
                        item = Detection(Category.COMPANY, line.page, following.rect(), following.text, None, profile)
                if item:
                    companies.append(item)
            if _line_has(line, "RFC"):
                item = _label_value_detection(line, "RFC", Category.RFC, profile, value_regex=_RFC_RE)
                if item:
                    rfcs.append(item)
            if _line_has(line, "FECHA CREACION"):
                item = _label_value_detection(line, "FECHA CREACION", Category.CREATION_DATE, profile, value_regex=_DATE_RE)
                if item:
                    detections.append(item)
            if _line_has(line, "EJERCICIO"):
                for match in re.finditer(r"\b\d{4}\b", line.text):
                    words = _rect_for_range(line, match.start(), match.end())
                    detections.append(Detection(Category.EXERCISE_PERIOD, line.page, line.rect(words), match.group(), None, profile))
                    break
            if _line_has(line, "PERIODO"):
                after_period = _words_after(line, "PERIODO")
                for word in after_period:
                    match = re.search(r"\b\d{1,2}\b", word.text)
                    if match:
                        detections.append(Detection(Category.EXERCISE_PERIOD, line.page, line.rect((word,)), match.group(), None, profile))
                        break
    if not companies or not rfcs:
        return ProfileResult(profile, 0.55, warnings=["Campos obligatorios de tipo 1 incompletos."])
    # Tipo 1 liga cada razon al RFC de su cabecera; se toma el mas cercano en lectura.
    for company in companies:
        closest = min(rfcs, key=lambda item: abs(item.page - company.page) * 10000 + abs(item.rect[1] - company.rect[1]))
        company.entity_key = normalize(closest.original)
        closest.entity_key = normalize(closest.original)
    for rfc in rfcs:
        rfc.entity_key = normalize(rfc.original)
    detections.extend(companies)
    detections.extend(rfcs)
    return ProfileResult(profile, 0.70 + anchors * 0.06, _dedupe(detections))


def _header_company_and_context(layout: PageLayout, profile: str) -> tuple[Detection | None, Detection | None, Detection | None]:
    """Identifica nombre, direccion y poblacion no etiquetados de la cabecera tipo 2."""

    candidates = [line for line in layout.lines if line.y0 < layout.height * 0.42]
    address_line = next((line for line in candidates if "#" in line.text and sum(char.isdigit() for char in line.text) >= 1), None)
    population_line = next(
        (line for line in candidates if re.fullmatch(r"[A-ZÁÉÍÓÚÑ .'-]+,\s*[A-ZÁÉÍÓÚÑ]{2,4}", line.text.strip())),
        None,
    )
    cutoff = address_line.y0 if address_line else (population_line.y0 if population_line else layout.height * 0.35)
    company_line = next((line for line in candidates if line.y0 < cutoff and _is_company_candidate(line)), None)
    company = Detection(Category.COMPANY, layout.number, company_line.rect(), company_line.text, None, profile) if company_line else None
    address = Detection(Category.ADDRESS, layout.number, address_line.rect(), address_line.text, None, profile) if address_line else None
    population = (
        Detection(Category.POPULATION, layout.number, population_line.rect(), population_line.text, None, profile)
        if population_line
        else None
    )
    return company, address, population


def _profile_2(layouts: list[PageLayout]) -> ProfileResult:
    profile = "tipo_2"
    all_lines = tuple(line for layout in layouts for line in layout.lines)
    anchors = sum(
        any(_line_has(line, phrase) for line in all_lines)
        for phrase in ("REG FED", "DIRECCION", "POBLACION", "CEDULA", "BALANZA DE COMPROBACION AL")
    )
    if anchors < 2 or not any(_RFC_RE.search(line.text) for line in all_lines):
        return ProfileResult(profile, 0.0)
    detections: list[Detection] = []
    companies: list[Detection] = []
    rfcs: list[Detection] = []
    for layout in layouts:
        contextual_company, contextual_address, contextual_population = _header_company_and_context(layout, profile)
        if contextual_company:
            companies.append(contextual_company)
        if contextual_address:
            detections.append(contextual_address)
        if contextual_population:
            detections.append(contextual_population)
        for line_index, line in enumerate(layout.lines):
            if _line_has(line, "REG FED"):
                rfc_items = _match_detections(line, _RFC_RE, Category.RFC, profile)
                if not rfc_items:
                    following = _non_empty_following(layout.lines, line_index)
                    if following:
                        rfc_items = _match_detections(following, _RFC_RE, Category.RFC, profile)
                rfcs.extend(rfc_items)
            if _line_has(line, "DIRECCION"):
                item = _label_value_detection(line, "DIRECCION", Category.ADDRESS, profile)
                if item:
                    detections.append(item)
            if _line_has(line, "POBLACION"):
                item = _label_value_detection(line, "POBLACION", Category.POPULATION, profile)
                if item:
                    detections.append(item)
            if _line_has(line, "CEDULA"):
                item = _label_value_detection(line, "CEDULA", Category.CERTIFICATE, profile)
                if item:
                    detections.append(item)
            if _line_has(line, "BALANZA DE COMPROBACION AL"):
                detections.extend(_match_detections(line, _DATE_RE, Category.HEADER_DATE, profile))
    if not companies or not rfcs:
        return ProfileResult(profile, 0.55, warnings=["No se pudo ligar razon social y RFC del tipo 2."])
    for company in companies:
        closest = min(rfcs, key=lambda item: abs(item.page - company.page) * 10000 + abs(item.rect[1] - company.rect[1]))
        company.entity_key = normalize(closest.original)
    for rfc in rfcs:
        rfc.entity_key = normalize(rfc.original)
    entity_by_page = {item.page: item.entity_key for item in rfcs}
    for item in detections:
        if item.category in (Category.ADDRESS, Category.POPULATION, Category.CERTIFICATE):
            item.entity_key = entity_by_page.get(item.page) or normalize(rfcs[0].original)
    detections.extend(companies)
    detections.extend(rfcs)
    return ProfileResult(profile, 0.70 + anchors * 0.06, _dedupe(detections))


def _profile_3(layouts: list[PageLayout]) -> ProfileResult:
    profile = "tipo_3"
    logo_lines = [line for layout in layouts for line in layout.lines if _find_phrase_words(line, "CONTPAQ I")]
    if not logo_lines:
        return ProfileResult(profile, 0.0)
    detections: list[Detection] = []
    companies: list[Detection] = []
    for layout in layouts:
        page_logo_lines = [line for line in layout.lines if _find_phrase_words(line, "CONTPAQ I")]
        for line in page_logo_lines:
            words = _find_phrase_words(line, "CONTPAQ I")
            assert words
            detections.append(Detection(Category.TEXT_LOGO, line.page, line.rect(words), " ".join(word.text for word in words), None, profile, redact_only=True))
        logo_bottom = max((line.y0 for line in page_logo_lines), default=0.0)
        header_candidates = [
            line
            for line in layout.lines
            if logo_bottom <= line.y0 < layout.height * 0.38 and _is_company_candidate(line)
        ]
        if header_candidates:
            company_line = header_candidates[0]
            entity_key = normalize(company_line.text)
            companies.append(Detection(Category.COMPANY, layout.number, company_line.rect(), company_line.text, None, profile, entity_key))
        for line in layout.lines:
            line_normal = normalize(line.text)
            if "BALANZA DE COMPROBACION" in line_normal or re.search(r"\b(FECHA|SUBIDA|GENERACION|GENERADO|EMISION)\b", line_normal):
                detections.extend(_match_detections(line, _DATE_RE, Category.HEADER_DATE, profile))
    if not companies:
        return ProfileResult(profile, 0.65, warnings=["No se pudo identificar razon social de cabecera tipo 3."])
    detections.extend(companies)
    return ProfileResult(profile, 0.95, _dedupe(detections))


def _profile_4(layouts: list[PageLayout]) -> ProfileResult:
    profile = "tipo_4"
    detections: list[Detection] = []
    for layout in layouts:
        for line in layout.lines:
            normalized = normalize(line.text)
            if not re.search(r"\b(BANCO|BANC|CUENTA|CHEQUE)\w*\b", normalized):
                continue
            for item in _match_detections(line, _ACCOUNT_RE, Category.BANK_ACCOUNT, profile):
                digit_count = sum(char.isdigit() for char in item.original)
                if digit_count >= 9 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.original):
                    item.entity_key = normalize(item.original)
                    detections.append(item)
    return ProfileResult(profile, 0.90 if detections else 0.0, _dedupe(detections))


def _materialize_replacements(detections: list[Detection], pseudonymizer: Pseudonymizer) -> None:
    """Completa reemplazos solo en memoria antes de la redaccion fisica."""

    exercise_by_page: dict[tuple[int, str], list[Detection]] = {}
    for item in detections:
        if item.category == Category.EXERCISE_PERIOD:
            exercise_by_page.setdefault((item.page, item.profile), []).append(item)
            continue
        key = item.entity_key or normalize(item.original)
        if item.category == Category.COMPANY:
            item.replacement = pseudonymizer.company(key)
        elif item.category == Category.RFC:
            item.replacement = pseudonymizer.rfc(item.original, key)
        elif item.category == Category.ADDRESS:
            item.replacement = pseudonymizer.address(key)
        elif item.category == Category.POPULATION:
            item.replacement = pseudonymizer.population(key)
        elif item.category == Category.CERTIFICATE:
            item.replacement = pseudonymizer.certificate(item.original, key)
        elif item.category == Category.BANK_ACCOUNT:
            item.replacement = pseudonymizer.bank_account(item.original)
        elif item.category in (Category.CREATION_DATE, Category.HEADER_DATE):
            item.replacement = pseudonymizer.replace_date(item.original)
    for group in exercise_by_page.values():
        years = [item for item in group if re.fullmatch(r"\d{4}", item.original)]
        periods = [item for item in group if re.fullmatch(r"\d{1,2}", item.original)]
        for year, period in zip(years, periods):
            shifted_year, shifted_period = pseudonymizer.exercise_and_period(year.original, period.original)
            year.replacement, period.replacement = shifted_year, shifted_period


def detect_document(layouts: list[PageLayout], pseudonymizer: Pseudonymizer) -> ProfileResult | None:
    """Selecciona un perfil por anclas y agrega cuentas bancarias en cualquier perfil."""

    candidates = [_profile_1(layouts), _profile_2(layouts), _profile_3(layouts)]
    primary = max(candidates, key=lambda result: result.confidence)
    bank = _profile_4(layouts)
    if primary.confidence < 0.85 and bank.confidence < 0.85:
        return None
    if primary.confidence < 0.85:
        primary = bank
    elif bank.detections:
        primary.detections.extend(bank.detections)
    primary.detections = _dedupe(primary.detections)
    _materialize_replacements(primary.detections, pseudonymizer)
    return primary

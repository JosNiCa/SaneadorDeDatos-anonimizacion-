"""Deteccion posicional y conservadora para balanzas digitales.

La familia visual solo describe la maqueta. Los detectores de identidad,
fechas, logos y asociaciones en descripciones se ejecutan de forma
independiente y pueden activarse simultaneamente.
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Sequence, TypeVar

from .models import Category, Detection, GlyphBox, ProfileResult, TextStyle, WordBox
from .pseudonyms import Pseudonymizer, canonical_association_key, normalize


WEB_BALANCE = "WEB_BALANCE"
CLASSIC_BALANCE = "CLASSIC_BALANCE"
CONTALINK_BALANCE = "CONTALINK_BALANCE"
CONTPAQ_BALANCE = "CONTPAQ_BALANCE"
FAMILIES = (WEB_BALANCE, CLASSIC_BALANCE, CONTALINK_BALANCE, CONTPAQ_BALANCE)

_STRICT_RFC_RE = re.compile(r"[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}", re.IGNORECASE)
_RELAXED_RFC_RE = re.compile(r"[A-Z&Ñ]{3,5}\d{6}[A-Z0-9]{3}", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}[-/.](?:\d{1,2}|[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,12})[-/.]\d{2,4})"
    r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?)?(?!\d)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_ACCOUNT_CODE_RE = re.compile(r"^\d+(?:[.\-]\d+)*(?:\s|$)")
_MONEY_WORD_RE = re.compile(r"^-?\d[\d,]*\.\d{2}$")
_BANK_CONTEXT_RE = re.compile(r"\b(?:BANCO|BANCOS|BANCARIA|BANCARIO|CTA|CUENTA|CLABE|CHEQUE)\b")
_GENERIC_USER_VALUES = {
    "ADMIN", "ADMINISTRADOR", "ADMINISTRADORA", "DEFAULT", "GENERICO",
    "GENERICA", "SISTEMA", "SYSTEM", "USUARIO",
}
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:S\.?\s*A\.?\s*(?:DE\s*C\.?\s*V\.?)?|"
    r"S\.?\s*DE\s*R\.?\s*L\.?(?:\s*DE\s*C\.?\s*V\.?)?|"
    r"A\.?\s*C\.?|S\.?\s*C\.?)\s*$",
    re.IGNORECASE,
)


def _compact(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.upper())
    return "".join(
        char for char in decomposed
        if unicodedata.category(char) != "Mn" and char.isalnum()
    )


def _rect(items: Sequence[WordBox | GlyphBox]) -> tuple[float, float, float, float]:
    return (
        min(item.x0 for item in items), min(item.y0 for item in items),
        max(item.x1 for item in items), max(item.y1 for item in items),
    )


def _intersects(a: Sequence[float], b: Sequence[float], tolerance: float = 0.0) -> bool:
    return not (
        a[2] <= b[0] + tolerance or b[2] <= a[0] + tolerance
        or a[3] <= b[1] + tolerance or b[3] <= a[1] + tolerance
    )


def _style(items: Sequence[WordBox | GlyphBox]) -> TextStyle:
    if not items:
        return TextStyle()
    sizes = [item.size for item in items if item.size > 0]
    size = statistics.median(sizes) if sizes else 9.0
    exemplar = max(items, key=lambda item: (getattr(item, "size", 0.0), item.x1 - item.x0))
    return TextStyle(
        size, getattr(exemplar, "font", "") or "helv",
        getattr(exemplar, "color", 0), getattr(exemplar, "flags", 0),
    )


@dataclass(frozen=True)
class ImagePlacement:
    page: int
    xref: int
    rect: tuple[float, float, float, float]
    pixel_width: int
    pixel_height: int


@dataclass(frozen=True)
class Selection:
    text: str
    glyphs: tuple[GlyphBox, ...]

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return _rect(self.glyphs)

    @property
    def style(self) -> TextStyle:
        return _style(self.glyphs)


@dataclass(frozen=True)
class Anchor:
    phrase: str
    selection: Selection


@dataclass(frozen=True)
class Line:
    page: int
    words: tuple[WordBox, ...]
    glyphs: tuple[GlyphBox, ...] = ()

    @property
    def text(self) -> str:
        return _glyph_text(self.glyphs)[0].strip() if self.glyphs else " ".join(word.text for word in self.words)

    @property
    def y0(self) -> float:
        return min(item.y0 for item in self.glyphs or self.words)

    @property
    def y1(self) -> float:
        return max(item.y1 for item in self.glyphs or self.words)

    @property
    def x0(self) -> float:
        return min(item.x0 for item in self.glyphs or self.words)

    @property
    def x1(self) -> float:
        return max(item.x1 for item in self.glyphs or self.words)

    def rect(self, items: Iterable[WordBox | GlyphBox] | None = None) -> tuple[float, float, float, float]:
        selected = tuple(items or self.glyphs or self.words)
        return _rect(selected)


@dataclass(frozen=True)
class PageLayout:
    number: int
    width: float
    height: float
    words: tuple[WordBox, ...]
    lines: tuple[Line, ...]
    images: tuple[ImagePlacement, ...] = ()


@dataclass(frozen=True)
class ColumnBounds:
    page: int
    header_y: float
    description_left: float
    description_right: float
    account_left: float
    account_right: float
    table_bottom: float
    shared_account_description: bool = False


@dataclass
class DescriptionRow:
    page: int
    y0: float
    y1: float
    account_code: str
    text: str
    words: tuple[WordBox, ...]
    bounds: ColumnBounds
    parent_bank_text: str | None = None

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return _rect(self.words)

    @property
    def style(self) -> TextStyle:
        return _style(self.words)


def _glyph_text(glyphs: Sequence[GlyphBox]) -> tuple[str, list[GlyphBox | None]]:
    """Reconstruye una linea y conserva un mapa caracter -> glifo real."""
    ordered = sorted(glyphs, key=lambda item: (item.x0, item.y0))
    chunks: list[str] = []
    mapping: list[GlyphBox | None] = []
    previous: GlyphBox | None = None
    for glyph in ordered:
        if previous is not None and glyph.x0 - previous.x1 > max(0.8, min(previous.size, glyph.size) * 0.12):
            chunks.append(" ")
            mapping.append(None)
        chunks.append(glyph.text)
        mapping.extend([glyph] * len(glyph.text))
        previous = glyph
    return "".join(chunks), mapping


def _near_duplicate(
    a: WordBox | GlyphBox,
    b: WordBox | GlyphBox,
    normalized_a: str | None = None,
    normalized_b: str | None = None,
) -> bool:
    return (
        (normalized_a if normalized_a is not None else normalize(a.text))
        == (normalized_b if normalized_b is not None else normalize(b.text))
        and max(abs(a.x0 - b.x0), abs(a.y0 - b.y0), abs(a.x1 - b.x1), abs(a.y1 - b.y1)) <= 0.8
    )


Positioned = TypeVar("Positioned", WordBox, GlyphBox)


def _dedupe_positioned(items: Sequence[Positioned]) -> tuple[Positioned, ...]:
    # La deduplicación compara hasta cien elementos previos. Normalizar cada
    # texto dentro de ese ciclo multiplicaba el coste por el número de glifos
    # de una página; se conserva exactamente la misma ventana geométrica,
    # pero cada texto se normaliza una sola vez.
    kept: list[tuple[Positioned, str]] = []
    for item in sorted(items, key=lambda value: (value.y0, value.x0, value.y1, value.x1)):
        normalized_item = normalize(item.text)
        if any(
            _near_duplicate(item, prior, normalized_item, normalized_prior)
            for prior, normalized_prior in kept[-100:]
        ):
            continue
        kept.append((item, normalized_item))
    return tuple(item for item, _ in kept)


def _extract_glyphs(page_number: int, page: Any) -> tuple[GlyphBox, ...]:
    glyphs: list[GlyphBox] = []
    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = float(span.get("size", 0.0))
                font = str(span.get("font", ""))
                color = int(span.get("color", 0))
                flags = int(span.get("flags", 0))
                for char in span.get("chars", []):
                    value = str(char.get("c", ""))
                    box = char.get("bbox")
                    if not value or not box or len(box) != 4:
                        continue
                    glyphs.append(GlyphBox(
                        page_number, float(box[0]), float(box[1]), float(box[2]), float(box[3]),
                        value, size, font, color, flags,
                    ))
    return _dedupe_positioned(glyphs)


def _word_style(word: Sequence[Any], glyphs: Sequence[GlyphBox]) -> TextStyle:
    box = tuple(float(value) for value in word[:4])
    candidates = [
        glyph for glyph in glyphs
        if _intersects(box, (glyph.x0, glyph.y0, glyph.x1, glyph.y1), -0.05)
    ]
    return _style(candidates)


def _group_visual_lines(page_number: int, words: Sequence[WordBox], glyphs: Sequence[GlyphBox]) -> tuple[Line, ...]:
    rows: list[list[WordBox]] = []
    for word in sorted(words, key=lambda item: ((item.y0 + item.y1) / 2.0, item.x0)):
        center = (word.y0 + word.y1) / 2.0
        best: list[WordBox] | None = None
        distance = math.inf
        for row in rows[-10:]:
            row_center = statistics.mean((item.y0 + item.y1) / 2.0 for item in row)
            row_y0 = min(item.y0 for item in row)
            row_y1 = max(item.y1 for item in row)
            overlap = max(0.0, min(word.y1, row_y1) - max(word.y0, row_y0))
            min_height = min(word.y1 - word.y0, row_y1 - row_y0)
            delta = abs(center - row_center)
            if (overlap >= min_height * 0.4 or delta <= max(1.35, min_height * 0.18)) and delta < distance:
                best, distance = row, delta
        if best is None:
            rows.append([word])
        else:
            best.append(word)

    lines: list[Line] = []
    for row in rows:
        ordered = tuple(sorted(row, key=lambda item: item.x0))
        y0 = min(item.y0 for item in ordered) - 0.8
        y1 = max(item.y1 for item in ordered) + 0.8
        row_glyphs = tuple(sorted(
            (glyph for glyph in glyphs if y0 <= (glyph.y0 + glyph.y1) / 2.0 <= y1),
            key=lambda item: item.x0,
        ))
        lines.append(Line(page_number, ordered, row_glyphs))
    return tuple(sorted(lines, key=lambda line: (line.y0, line.x0)))


def layout_from_page(page_number: int, page: Any, *, textpage: Any | None = None) -> PageLayout:
    """Construye lineas visuales por baseline usando words y estilos/spans reales."""
    raw_words = page.get_text("words", textpage=textpage, sort=True)
    glyphs = _extract_glyphs(page_number, page)
    words: list[WordBox] = []
    for item in raw_words:
        if not str(item[4]).strip():
            continue
        style = _word_style(item, glyphs)
        words.append(WordBox(
            page_number, float(item[0]), float(item[1]), float(item[2]), float(item[3]),
            str(item[4]), int(item[5]), int(item[6]), int(item[7]),
            style.size, style.font, style.color, style.flags,
        ))
    positioned = _dedupe_positioned(words)
    images: list[ImagePlacement] = []
    for image in page.get_images(full=True):
        xref = int(image[0])
        for rect in page.get_image_rects(xref):
            images.append(ImagePlacement(
                page_number, xref, (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)),
                int(image[2]), int(image[3]),
            ))
    return PageLayout(
        page_number, float(page.rect.width), float(page.rect.height), positioned,
        _group_visual_lines(page_number, positioned, glyphs), tuple(images),
    )


def _compact_map(line: Line) -> tuple[str, list[GlyphBox]]:
    compact: list[str] = []
    mapping: list[GlyphBox] = []
    for glyph in sorted(line.glyphs, key=lambda item: item.x0):
        for char in _compact(glyph.text):
            compact.append(char)
            mapping.append(glyph)
    return "".join(compact), mapping


def _find_anchor(line: Line, phrase: str) -> Anchor | None:
    source, mapping = _compact_map(line)
    target = _compact(phrase)
    if not target:
        return None
    start = source.find(target)
    if start < 0:
        return None
    chosen: list[GlyphBox] = []
    for glyph in mapping[start:start + len(target)]:
        if not chosen or glyph is not chosen[-1]:
            chosen.append(glyph)
    return Anchor(phrase, Selection(_glyph_text(chosen)[0].strip(), tuple(chosen)))


def _anchors(layout: PageLayout, phrase: str) -> list[tuple[Line, Anchor]]:
    return [(line, anchor) for line in layout.lines if (anchor := _find_anchor(line, phrase))]


def _document_has(layouts: Sequence[PageLayout], phrase: str) -> bool:
    return any(_find_anchor(line, phrase) for layout in layouts for line in layout.lines)


def _selection_from_glyphs(glyphs: Iterable[GlyphBox]) -> Selection | None:
    chosen = tuple(sorted(
        (glyph for glyph in glyphs if glyph.text.strip() and glyph.text not in ":;"),
        key=lambda item: item.x0,
    ))
    if not chosen:
        return None
    text = _glyph_text(chosen)[0].strip(" :;\t")
    return Selection(text, chosen) if text else None


def _selection_after(line: Line, anchor: Anchor, *, before: Anchor | None = None) -> Selection | None:
    right = anchor.selection.rect[2]
    boundary = before.selection.rect[0] if before else math.inf
    return _selection_from_glyphs(
        glyph for glyph in line.glyphs
        if glyph.x0 >= right - 0.15 and glyph.x1 <= boundary + 0.15
    )


def _selection_between(line: Line, left: float, right: float) -> Selection | None:
    return _selection_from_glyphs(
        glyph for glyph in line.glyphs
        if glyph.x0 >= left - 0.15 and glyph.x1 <= right + 0.15
    )


def _regex_selections(line: Line, regex: re.Pattern[str]) -> list[Selection]:
    text, mapping = _glyph_text(line.glyphs)
    result: list[Selection] = []
    for match in regex.finditer(text):
        glyphs: list[GlyphBox] = []
        for glyph in mapping[match.start():match.end()]:
            if glyph is not None and (not glyphs or glyph is not glyphs[-1]):
                glyphs.append(glyph)
        if glyphs:
            result.append(Selection(match.group().strip(), tuple(glyphs)))
    return result


def _score_families(layouts: Sequence[PageLayout]) -> dict[str, float]:
    anchor_sets = {
        WEB_BALANCE: (
            ("FECHA CREACION", 2.0), ("NOMBRE RAZON SOCIAL", 2.5),
            ("EJERCICIO", 1.5), ("PERIODO", 1.5), ("BALANZA DE COMPROBACION", 1.0),
        ),
        CLASSIC_BALANCE: (
            ("REG FED", 2.0), ("DIRECCION", 1.0), ("POBLACION", 1.0),
            ("CEDULA", 0.75), ("TIPOS DE MONEDAS", 1.5),
            ("NO DE CUENTA", 1.0), ("DESCRIPCION", 1.0),
        ),
        CONTALINK_BALANCE: (
            ("FECHA DE IMPRESION", 2.0), ("TIPO MONEDA", 2.0),
            ("TIPOS DE MONEDAS", 1.5),
            ("NATURALEZA", 1.5), ("PERIODO", 1.0), ("PAGINA", 0.5),
            ("BALANZA DE COMPROBACION", 0.5),
        ),
        CONTPAQ_BALANCE: (
            ("CONTPAQ I", 3.0), ("SALDOS INICIALES", 1.5),
            ("SALDOS ACTUALES", 1.5), ("CARGOS", 0.75),
            ("ABONOS", 0.75), ("HOJA", 1.0),
        ),
    }
    scores: dict[str, float] = {}
    for family, anchors in anchor_sets.items():
        obtained = sum(weight for phrase, weight in anchors if _document_has(layouts, phrase))
        scores[family] = round(obtained / sum(weight for _, weight in anchors), 4)
    return scores


def _choose_family(layouts: Sequence[PageLayout]) -> tuple[str | None, dict[str, float]]:
    scores = _score_families(layouts)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < 0.50:
        return None, scores
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.10:
        return None, scores
    return ranked[0][0], scores


def _anchor_line(layout: PageLayout, *phrases: str) -> tuple[Line, Anchor] | None:
    for phrase in phrases:
        matches = _anchors(layout, phrase)
        if matches:
            return matches[0]
    return None


def _table_bottom(layout: PageLayout) -> float:
    footer_y = [
        line.y0 for line in layout.lines
        if _find_anchor(line, "USUARIO") or _find_anchor(line, "PAGINA")
    ]
    return min(footer_y) - 1.0 if footer_y else layout.height - 12.0


def _infer_page_columns(layout: PageLayout, family: str) -> ColumnBounds | None:
    numeric = _anchor_line(layout, "SALDOS INICIALES", "SALDO INICIAL")
    account = _anchor_line(layout, "NO DE CUENTA", "CUENTA")
    if numeric is None or account is None:
        if family == WEB_BALANCE:
            return _infer_web_columns_from_rows(layout)
        return None
    numeric_line, numeric_anchor = numeric
    _, account_anchor = account
    description = _anchor_line(layout, "DESCRIPCION", "NOMBRE")
    nature = _anchor_line(layout, "NATURALEZA")
    table_bottom = _table_bottom(layout)
    description_right = numeric_anchor.selection.rect[0] - 1.0
    account_left = max(0.0, account_anchor.selection.rect[0] - 3.0)
    shared = family == WEB_BALANCE
    if shared:
        description_left = account_left
        account_right = description_right
    elif description and (family in (CONTALINK_BALANCE, CONTPAQ_BALANCE) or nature):
        description_left = max(account_anchor.selection.rect[2] + 1.0, description[1].selection.rect[0] - 2.0)
        account_right = description_left - 1.0
    else:
        code_rights: list[float] = []
        for line in layout.lines:
            if line.y0 <= numeric_line.y1 + 1.0 or line.y0 >= table_bottom:
                continue
            for word in line.words:
                if word.x0 <= layout.width * 0.28 and _ACCOUNT_CODE_RE.match(word.text.strip() + " "):
                    code_rights.append(word.x1)
                    break
        inferred_right = max(code_rights, default=account_anchor.selection.rect[2])
        account_right = max(account_anchor.selection.rect[2], inferred_right) + 2.0
        description_left = account_right
    if shared:
        valid = account_left == description_left < description_right
    else:
        valid = account_left < description_left < description_right
    if not valid:
        return None
    return ColumnBounds(
        layout.number, numeric_line.y1, description_left, description_right,
        account_left, account_right, table_bottom, shared,
    )


def _infer_web_columns_from_rows(layout: PageLayout) -> ColumnBounds | None:
    """Respaldo geométrico para WEB cuando el encabezado está fragmentado.

    Solo se acepta si al menos tres renglones presentan un código contable y
    cuatro importes decimales con la misma posición horizontal. Así no se
    convierte una página narrativa en una tabla por heurística laxa.
    """
    candidates: list[tuple[float, float, float]] = []
    table_bottom = _table_bottom(layout)
    for line in layout.lines:
        if line.y0 >= table_bottom:
            continue
        words = sorted(line.words, key=lambda item: item.x0)
        account = next(
            (
                word for word in words
                if word.x0 <= layout.width * 0.40
                and _ACCOUNT_CODE_RE.match(word.text.strip() + " ")
            ),
            None,
        )
        monetary = [word for word in words if _MONEY_WORD_RE.fullmatch(word.text.strip())]
        if account is None or len(monetary) < 4:
            continue
        first_amount = monetary[-4]
        if first_amount.x0 <= account.x1 + 8.0:
            continue
        candidates.append((account.x0, first_amount.x0, line.y0))
    if len(candidates) < 3:
        return None
    account_lefts = [item[0] for item in candidates]
    amount_lefts = [item[1] for item in candidates]
    # Un desvío mayor a 2% del ancho indica que no hay una cuadrícula estable.
    if (
        max(account_lefts) - min(account_lefts) > layout.width * 0.02
        or max(amount_lefts) - min(amount_lefts) > layout.width * 0.02
    ):
        return None
    account_left = statistics.median(account_lefts)
    description_right = statistics.median(amount_lefts) - 1.0
    if not 0.0 <= account_left < description_right <= layout.width:
        return None
    return ColumnBounds(
        layout.number,
        max(0.0, min(item[2] for item in candidates) - 1.0),
        account_left,
        description_right,
        account_left,
        description_right,
        table_bottom,
        True,
    )


def infer_columns(layouts: Sequence[PageLayout], family: str) -> dict[int, ColumnBounds]:
    """Infiere encabezados y hereda limites solo a paginas de continuacion."""
    result: dict[int, ColumnBounds] = {}
    last: ColumnBounds | None = None
    last_size: tuple[float, float] | None = None
    for layout in layouts:
        current = _infer_page_columns(layout, family)
        if current:
            last = current
            last_size = (layout.width, layout.height)
            result[layout.number] = current
        elif last and last_size == (layout.width, layout.height):
            result[layout.number] = ColumnBounds(
                layout.number, 0.0, last.description_left, last.description_right,
                last.account_left, last.account_right, _table_bottom(layout),
                last.shared_account_description,
            )
    return result


def _account_code(line: Line, bounds: ColumnBounds) -> str:
    text = " ".join(
        word.text.strip() for word in line.words
        if word.x0 >= bounds.account_left - 2.0 and word.x1 <= bounds.account_right + 2.0
    )
    match = _ACCOUNT_CODE_RE.match(text + " ")
    return match.group().strip() if match else ""


def _description_rows(layouts: Sequence[PageLayout], columns: dict[int, ColumnBounds]) -> list[DescriptionRow]:
    rows: list[DescriptionRow] = []
    for layout in layouts:
        bounds = columns.get(layout.number)
        if not bounds:
            continue
        for line in layout.lines:
            if line.y0 <= bounds.header_y + 0.5 or line.y0 >= bounds.table_bottom:
                continue
            account_code = _account_code(line, bounds)
            words = tuple(
                word for word in line.words
                if bounds.description_left <= (word.x0 + word.x1) / 2.0 < bounds.description_right
            )
            if bounds.shared_account_description and words and account_code:
                words = tuple(word for word in words if word.text.strip() != account_code)
            if not words:
                continue
            text = " ".join(word.text for word in words).strip()
            if text:
                rows.append(DescriptionRow(
                    layout.number, line.y0, line.y1, account_code, text, words, bounds,
                ))

    previous: DescriptionRow | None = None
    bank_roots: list[tuple[str, str, int]] = []
    for row in rows:
        normalized = normalize(row.text)
        if _BANK_CONTEXT_RE.search(normalized) and not _identifier_candidates(row.text):
            bank_roots.append((row.account_code, row.text, row.page))
        inherited: str | None = None
        if previous and previous.page == row.page and _BANK_CONTEXT_RE.search(normalize(previous.text)):
            inherited = previous.text
        if not inherited and row.account_code:
            compact_code = re.sub(r"\D", "", row.account_code)
            for root_code, root_text, root_page in reversed(bank_roots):
                root_compact = re.sub(r"\D", "", root_code)
                if root_page == row.page and root_compact and compact_code.startswith(root_compact) and compact_code != root_compact:
                    inherited = root_text
                    break
        row.parent_bank_text = inherited
        previous = row
    return rows


def _candidate_rfc(text: str) -> tuple[str, bool] | None:
    normalized = normalize(text)
    strict = list(_STRICT_RFC_RE.finditer(normalized))
    if strict:
        match = max(
            strict,
            key=lambda item: sum(char.isalpha() for char in normalized[:item.start()] + normalized[item.end():]),
        )
        return match.group(), False
    relaxed = [match for match in _RELAXED_RFC_RE.finditer(normalized) if len(match.group()) == 14]
    return (relaxed[0].group(), True) if relaxed else None


def _entity_without_identifier(text: str, identifier: str) -> str:
    result = re.sub(re.escape(identifier), " ", text, count=1, flags=re.IGNORECASE)
    result = re.sub(r"\bR\.?\s*F\.?\s*C\.?\b", " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip(" :-;,.")


def _identifier_candidates(text: str) -> list[str]:
    normalized = normalize(text)
    excluded_spans = [
        match.span()
        for pattern in (
            # Importes con agrupacion de millares o decimales monetarios.
            re.compile(r"(?<![A-Z0-9])[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![A-Z0-9])"),
            re.compile(r"(?<![A-Z0-9])[+-]?\d+\.\d{2}(?![A-Z0-9])"),
            re.compile(r"(?<![A-Z0-9])\d+(?:\.\d+)?\s*%(?![A-Z0-9])"),
        )
        for match in pattern.finditer(normalized)
    ]
    candidates: list[str] = []
    for match in re.finditer(r"(?<![A-Z0-9])\d(?:[\d\s\-/]*\d)?(?![A-Z0-9])", normalized):
        if any(match.start() < end and start < match.end() for start, end in excluded_spans):
            continue
        value = match.group().strip()
        digits = re.sub(r"\D", "", value)
        if len(digits) < 3 or _DATE_RE.fullmatch(value) or _TIME_RE.fullmatch(value):
            continue
        if "." in value or "," in value or "%" in value:
            continue
        candidates.append(value)
    return candidates


def _legal_suffix(entity: str) -> str:
    match = _LEGAL_SUFFIX_RE.search(entity)
    return match.group().strip() if match else ""


def _company_alternatives(
    pseudo: Pseudonymizer, key: str, *, physical: bool, suffix: str = "",
) -> tuple[str, ...]:
    method = getattr(pseudo, "person_alternatives" if physical else "company_alternatives", None)
    if callable(method):
        try:
            values = method(key, suffix) if not physical else method(key)
        except TypeError:
            values = method(key)
        result = tuple(dict.fromkeys(str(value) for value in values if value))
        if result:
            return result
    return (pseudo.company(key),)


def _bank_pair(
    pseudo: Pseudonymizer, bank_text: str, identifier: str, key: str,
) -> tuple[str, tuple[str, ...]]:
    method = getattr(pseudo, "bank_pair", None) or getattr(pseudo, "bank_and_identifier", None)
    if callable(method):
        try:
            pair = method(bank_text, identifier, key)
        except TypeError:
            pair = method(identifier, key)
        if isinstance(pair, tuple) and len(pair) == 2:
            replacement = f"{pair[0]} {pair[1]}"
        else:
            replacement = str(pair)
    else:
        bank_method = getattr(pseudo, "bank", None)
        bank = bank_method(key) if callable(bank_method) else f"BANCO FICTICIO {pseudo.token('bank', key, 6)}"
        try:
            account = pseudo.bank_account(identifier, key)
        except TypeError:
            account = pseudo.bank_account(identifier)
        replacement = f"{bank} {account}"
    alternatives_method = getattr(pseudo, "bank_alternatives", None)
    bank_alternatives = tuple(alternatives_method(key)) if callable(alternatives_method) else ()
    identifier_replacement = replacement.split()[-1]
    return replacement, tuple(f"{value} {identifier_replacement}" for value in bank_alternatives if value)


def _new_detection(
    category: Category, page: int, selection: Selection, family: str, *,
    entity_key: str | None = None, redact_only: bool = False,
    insert_rect: tuple[float, float, float, float] | None = None,
    confidence: float = 1.0, alignment: int = 0, multiline: bool = False,
) -> Detection:
    return Detection(
        category, page, selection.rect, selection.text, None, family,
        entity_key, confidence, redact_only, insert_rect, selection.style,
        (), (selection.text,), alignment, multiline,
    )


def _detect_label_field(
    layout: PageLayout, label: str, category: Category, family: str, *,
    before_labels: Sequence[str] = (), entity_key: str | None = None,
) -> list[Detection]:
    result: list[Detection] = []
    for line, anchor in _anchors(layout, label):
        following = [
            found for phrase in before_labels
            if (found := _find_anchor(line, phrase)) and found.selection.rect[0] > anchor.selection.rect[2]
        ]
        before = min(following, key=lambda item: item.selection.rect[0], default=None)
        selection = _selection_after(line, anchor, before=before)
        if selection:
            result.append(_new_detection(
                category, layout.number, selection, family, entity_key=entity_key,
            ))
    return result


def _header_pages(layouts: Sequence[PageLayout], family: str) -> list[PageLayout]:
    marker = {
        WEB_BALANCE: "NOMBRE RAZON SOCIAL",
        CLASSIC_BALANCE: "REG FED",
        CONTALINK_BALANCE: "FECHA DE IMPRESION",
        CONTPAQ_BALANCE: "CONTPAQ I",
    }[family]
    return [layout for layout in layouts if _document_has((layout,), marker)]


def _detect_header_rfc(layout: PageLayout, family: str) -> list[Detection]:
    result: list[Detection] = []
    if family == CONTALINK_BALANCE:
        # ContaLink apila razon social y RFC sin etiquetas. El RFC estricto en
        # la banda superior actua como ancla semantica del bloque.
        for line in layout.lines:
            if line.y0 > layout.height * 0.16:
                continue
            for selection in _regex_selections(line, _STRICT_RFC_RE):
                compact = re.sub(r"[^A-Z0-9&Ñ]", "", normalize(selection.text))
                item = _new_detection(
                    Category.RFC, layout.number, selection, family, entity_key=compact,
                )
                item.residuals = (selection.text, compact)
                result.append(item)
        return _dedupe(result)

    matches = _anchors(layout, "REG FED") if family == CLASSIC_BALANCE else _anchors(layout, "RFC")
    for line, anchor in matches:
        boundary = _find_anchor(line, "CEDULA") if family == CLASSIC_BALANCE else None
        strict = [
            selection for selection in _regex_selections(line, _STRICT_RFC_RE)
            if selection.rect[0] >= anchor.selection.rect[2] - 0.2
            and (boundary is None or selection.rect[2] <= boundary.selection.rect[0] + 0.2)
        ]
        selection = strict[0] if strict else _selection_after(line, anchor, before=boundary)
        if not selection:
            continue
        compact = re.sub(r"[^A-Z0-9&Ñ]", "", normalize(selection.text))
        if 8 <= len(compact) <= 18:
            item = _new_detection(Category.RFC, layout.number, selection, family, entity_key=compact)
            item.residuals = (selection.text, compact)
            result.append(item)
    return _dedupe(result)


def _classic_owner(layout: PageLayout, family: str) -> list[Detection]:
    reg = _anchor_line(layout, "REG FED")
    if reg is None:
        return []
    label_y = reg[0].y0
    candidates = [
        line for line in layout.lines
        if line.y0 < label_y - 2.0 and line.y0 < layout.height * 0.16
        and sum(char.isalpha() for char in line.text) >= 6
        and not any(_find_anchor(line, phrase) for phrase in (
            "DIRECCION", "POBLACION", "CEDULA", "REG FED",
            "BALANZA DE COMPROBACION", "TIPOS DE MONEDAS",
        ))
    ]
    if not candidates:
        return []
    selection = _selection_from_glyphs(min(candidates, key=lambda item: item.y0).glyphs)
    return [_new_detection(
        Category.COMPANY, layout.number, selection, family, alignment=1,
    )] if selection else []


def _web_owner(layout: PageLayout, family: str) -> list[Detection]:
    matches = _anchors(layout, "NOMBRE RAZON SOCIAL")
    if not matches:
        return []
    line, anchor = matches[0]
    right_anchors = [
        found for phrase in ("FECHA CREACION", "EJERCICIO", "PERIODO")
        if (found := _find_anchor(line, phrase)) and found.selection.rect[0] > anchor.selection.rect[2]
    ]
    right_boundary = min(
        (found.selection.rect[0] for found in right_anchors),
        # Si no comparte renglon con otro campo, el propietario dispone de
        # toda la banda hasta el margen derecho. El limite historico de 58 %
        # recortaba razones sociales largas, dejaba glifos originales vivos y
        # forzaba al seudonimo a envolverse sobre ellos.
        default=layout.width,
    )
    first = _selection_between(line, anchor.selection.rect[2], right_boundary - 1.0)
    selections: list[Selection] = [first] if first else []
    rfc_y = min(
        (candidate.y0 for candidate, _ in _anchors(layout, "RFC") if candidate.y0 > line.y0),
        default=line.y1 + 30.0,
    )
    for candidate in layout.lines:
        if candidate.y0 <= line.y0 + 1.0 or candidate.y0 >= rfc_y - 1.0:
            continue
        if _find_anchor(candidate, "NOMBRE RAZON SOCIAL") or _find_anchor(candidate, "RFC"):
            continue
        # Las continuaciones conservan la banda horizontal del campo y pueden
        # compartir baseline con Ejercicio/Periodo en el bloque derecho.
        selection = _selection_between(
            candidate,
            # Un propietario envuelto puede reiniciar cerca del margen de la
            # etiqueta (por ejemplo, un sufijo juridico en el segundo renglon).
            # La banda derecha sigue protegiendo Ejercicio/Periodo.
            anchor.selection.rect[0],
            right_boundary - 1.0,
        )
        if selection and sum(char.isalpha() for char in selection.text) >= 2:
            selections.append(selection)
    if not selections:
        return []
    field_right = min(layout.width - 20.0, max(value.rect[2] for value in selections) + 120.0)
    insert = (
        selections[0].rect[0], selections[0].rect[1], field_right,
        max(value.rect[3] for value in selections),
    )
    result: list[Detection] = []
    for index, selection in enumerate(selections):
        result.append(_new_detection(
            Category.COMPANY, layout.number, selection, family,
            redact_only=index > 0, insert_rect=insert if index == 0 else None,
            multiline=True,
        ))
    return result


def _contalink_owner(
    layout: PageLayout, family: str, rfcs: Sequence[Detection],
) -> list[Detection]:
    if not rfcs:
        return []
    rfc = rfcs[0]
    candidates: list[tuple[Line, Selection]] = []
    for line in layout.lines:
        if not (
            line.y0 < rfc.rect[1]
            and line.y1 >= rfc.rect[1] - 28.0
            and line.x0 < layout.width * 0.62
            and sum(char.isalpha() for char in line.text) >= 6
        ):
            continue
        title = _find_anchor(line, "BALANZA DE COMPROBACION")
        selection = (
            _selection_between(line, line.x0, title.selection.rect[0] - 2.0)
            if title else _selection_from_glyphs(line.glyphs)
        )
        if selection and sum(char.isalpha() for char in selection.text) >= 6:
            candidates.append((line, selection))
    if not candidates:
        return []
    _, selection = min(candidates, key=lambda item: abs(item[0].y1 - rfc.rect[1]))
    return [_new_detection(Category.COMPANY, layout.number, selection, family)] if selection else []


def _contpaq_owner(layout: PageLayout, family: str) -> list[Detection]:
    logo = _anchor_line(layout, "CONTPAQ I")
    hoja = _anchor_line(layout, "HOJA")
    if not logo or not hoja:
        return []
    line = logo[0] if abs(logo[0].y0 - hoja[0].y0) <= 2.0 else hoja[0]
    selection = _selection_between(
        line, logo[1].selection.rect[2] + 2.0, hoja[1].selection.rect[0] - 1.0,
    )
    return [_new_detection(
        Category.COMPANY, layout.number, selection, family, alignment=1,
    )] if selection else []


def _link_owner_fields(detections: Sequence[Detection]) -> None:
    rfcs = [item for item in detections if item.category == Category.RFC]
    companies = [item for item in detections if item.category == Category.COMPANY]
    for company in companies:
        closest = min(
            rfcs,
            key=lambda item: abs(item.page - company.page) * 10000 + abs(item.rect[1] - company.rect[1]),
            default=None,
        )
        company.entity_key = normalize(closest.original) if closest else normalize(company.original)
    owner_key = companies[0].entity_key if companies else (normalize(rfcs[0].original) if rfcs else None)
    for item in detections:
        if item.category == Category.RFC:
            item.entity_key = normalize(item.original)
        elif item.category in (Category.ADDRESS, Category.POPULATION, Category.CERTIFICATE, Category.USER):
            item.entity_key = owner_key or normalize(item.original)


def _detect_identity_and_fiscal(
    layouts: Sequence[PageLayout], family: str,
) -> list[Detection]:
    result: list[Detection] = []
    for layout in _header_pages(layouts, family):
        rfcs = _detect_header_rfc(layout, family)
        result.extend(rfcs)
        if family == WEB_BALANCE:
            result.extend(_web_owner(layout, family))
        elif family == CLASSIC_BALANCE:
            result.extend(_classic_owner(layout, family))
            result.extend(_detect_label_field(
                layout, "DIRECCION", Category.ADDRESS, family, before_labels=("REG FED",),
            ))
            result.extend(_detect_label_field(
                layout, "POBLACION", Category.POPULATION, family, before_labels=("CEDULA",),
            ))
            result.extend(_detect_label_field(layout, "CEDULA", Category.CERTIFICATE, family))
        elif family == CONTALINK_BALANCE:
            result.extend(_contalink_owner(layout, family, rfcs))
        else:
            result.extend(_contpaq_owner(layout, family))
    _link_owner_fields(result)
    return _dedupe(result)


def _date_category(line: Line) -> Category:
    if _find_anchor(line, "FECHA CREACION"):
        return Category.CREATION_DATE
    if _find_anchor(line, "FECHA DE IMPRESION") or _find_anchor(line, "FECHA IMPRESION"):
        return Category.PRINT_DATE
    if _find_anchor(line, "PERIODO"):
        return Category.PERIOD_RANGE
    if _find_anchor(line, "USUARIO") or _find_anchor(line, "PAGINA"):
        return Category.FOOTER_DATE
    return Category.HEADER_DATE


def _detect_dates(layouts: Sequence[PageLayout], family: str) -> list[Detection]:
    result: list[Detection] = []
    header_numbers = {layout.number for layout in _header_pages(layouts, family)}
    for layout in layouts:
        columns = _infer_page_columns(layout, family)
        header_limit = columns.header_y if columns else layout.height * 0.22
        top_date_index = 0
        for line in layout.lines:
            normalized = normalize(line.text)
            in_header = layout.number in header_numbers and line.y0 <= header_limit
            footer_context = "FECHA" in normalized and bool(
                _find_anchor(line, "USUARIO")
                or _find_anchor(line, "PAGINA")
                or line.y0 >= layout.height * 0.88
            )
            period_context = in_header and bool(_find_anchor(line, "PERIODO"))
            title_context = in_header and bool(_find_anchor(line, "BALANZA DE COMPROBACION"))
            explicit_context = in_header and bool(
                _find_anchor(line, "FECHA CREACION")
                or _find_anchor(line, "FECHA DE IMPRESION")
                or _find_anchor(line, "FECHA IMPRESION")
            )
            contpaq_top = family == CONTPAQ_BALANCE and in_header and bool(_regex_selections(line, _DATE_RE))
            if not (footer_context or period_context or title_context or explicit_context or contpaq_top):
                continue
            date_rects: list[tuple[float, float, float, float]] = []
            for selection in _regex_selections(line, _DATE_RE):
                category = Category.FOOTER_DATE if footer_context else _date_category(line)
                if family == CONTPAQ_BALANCE and in_header and not footer_context:
                    print_anchor = (
                        _find_anchor(line, "FECHA DE IMPRESION")
                        or _find_anchor(line, "FECHA IMPRESION")
                    )
                    if print_anchor and selection.rect[0] >= print_anchor.selection.rect[2] - 0.2:
                        category = Category.PRINT_DATE
                    else:
                        category = Category.HEADER_DATE
                elif contpaq_top and not (title_context or explicit_context):
                    category = Category.PRINT_DATE if top_date_index else Category.HEADER_DATE
                    top_date_index += 1
                result.append(_new_detection(category, layout.number, selection, family))
                date_rects.append(selection.rect)
            if footer_context:
                for selection in _regex_selections(line, _TIME_RE):
                    if not any(_intersects(selection.rect, rect) for rect in date_rects):
                        result.append(_new_detection(
                            Category.FOOTER_DATE, layout.number, selection, family,
                        ))

        # ContaLink puede dibujar la etiqueta Periodo y sus dos valores en
        # baselines consecutivos. Se asocian por banda horizontal, no por el
        # texto plano resultante.
        if family == CONTALINK_BALANCE and layout.number in header_numbers:
            for label_line, period_anchor in _anchors(layout, "PERIODO"):
                if _regex_selections(label_line, _DATE_RE):
                    continue
                following = [
                    candidate for candidate in layout.lines
                    if label_line.y1 < candidate.y0 <= label_line.y1 + 20.0
                ]
                for candidate in following:
                    dates = [
                        selection for selection in _regex_selections(candidate, _DATE_RE)
                        if selection.rect[0] >= period_anchor.selection.rect[0] - 3.0
                    ]
                    if not dates:
                        continue
                    for selection in dates:
                        result.append(_new_detection(
                            Category.PERIOD_RANGE, layout.number, selection, family,
                        ))
                    break

        if family == WEB_BALANCE and layout.number in header_numbers:
            exercise = _anchor_line(layout, "EJERCICIO")
            period = _anchor_line(layout, "PERIODO")
            if exercise:
                digits = [
                    value for value in _regex_selections(exercise[0], re.compile(r"(?<!\d)\d{4}(?!\d)"))
                    if value.rect[0] >= exercise[1].selection.rect[2]
                ]
                if digits:
                    result.append(_new_detection(
                        Category.EXERCISE_PERIOD, layout.number, digits[0], family,
                    ))
            if period:
                choices = [
                    value for value in _regex_selections(period[0], re.compile(r"(?<!\d)\d{1,2}(?!\d)"))
                    if value.rect[0] >= period[1].selection.rect[2]
                ]
                if choices:
                    result.append(_new_detection(
                        Category.EXERCISE_PERIOD, layout.number, choices[0], family,
                    ))
    return _dedupe(result)


def _detect_users(layouts: Sequence[PageLayout], family: str) -> list[Detection]:
    result: list[Detection] = []
    for layout in layouts:
        for line, anchor in _anchors(layout, "USUARIO"):
            next_anchor = _find_anchor(line, "FECHA")
            selection = _selection_after(line, anchor, before=next_anchor)
            if not selection:
                continue
            value = normalize(selection.text).strip(" :")
            if value and value not in _GENERIC_USER_VALUES:
                result.append(_new_detection(Category.USER, layout.number, selection, family))
    return _dedupe(result)


def _detect_logos(
    layouts: Sequence[PageLayout], family: str, columns: dict[int, ColumnBounds],
) -> list[Detection]:
    result: list[Detection] = []
    if family == CONTPAQ_BALANCE:
        for layout in layouts:
            for _, anchor in _anchors(layout, "CONTPAQ I"):
                result.append(_new_detection(
                    Category.TEXT_LOGO, layout.number, anchor.selection, family, redact_only=True,
                ))
    header_numbers = {layout.number for layout in _header_pages(layouts, family)}
    for layout in layouts:
        if layout.number not in header_numbers:
            continue
        bounds = columns.get(layout.number)
        header_bottom = bounds.header_y if bounds else layout.height * 0.22
        for image in layout.images:
            box = image.rect
            area_ratio = ((box[2] - box[0]) * (box[3] - box[1])) / (layout.width * layout.height)
            if not (
                box[3] <= header_bottom + 2.0 and box[1] < layout.height * 0.18
                and area_ratio <= 0.08 and box[2] - box[0] <= layout.width * 0.35
                and image.pixel_width >= 8 and image.pixel_height >= 8
            ):
                continue
            synthetic = Selection(
                f"imagen:{image.xref}",
                (GlyphBox(layout.number, *box, "I", 1.0, "", 0, 0),),
            )
            item = _new_detection(
                Category.RASTER_IMAGE, layout.number, synthetic, family, redact_only=True,
            )
            item.residuals = ()
            result.append(item)
    return _dedupe(result)


def _description_detection(
    row: DescriptionRow, category: Category, family: str, replacement: str,
    alternatives: Sequence[str], residuals: Sequence[str], key: str, confidence: float,
) -> Detection:
    insert = (
        row.bounds.description_left, max(row.y0 - 0.15, 0.0),
        row.bounds.description_right, row.y1 + 0.15,
    )
    return Detection(
        category, row.page, row.rect, row.text, replacement, family, key,
        confidence, False, insert, row.style, tuple(alternatives),
        tuple(value for value in residuals if value), 0, False,
    )


def _detect_description_associations(
    layouts: Sequence[PageLayout], family: str, columns: dict[int, ColumnBounds],
    pseudo: Pseudonymizer,
) -> tuple[list[Detection], list[str]]:
    result: list[Detection] = []
    warnings: list[str] = []
    names_to_rfcs: dict[str, set[str]] = {}
    for row in _description_rows(layouts, columns):
        rfc_candidate = _candidate_rfc(row.text)
        if rfc_candidate:
            rfc, relaxed = rfc_candidate
            entity = _entity_without_identifier(row.text, rfc)
            entity_words = re.findall(r"[A-ZÁÉÍÓÚÜÑ]{2,}", normalize(entity))
            if relaxed and (sum(char.isalpha() for char in entity) < 6 or len(entity_words) < 2):
                continue
            if not relaxed and sum(char.isalpha() for char in entity) < 3 and "RFC" not in normalize(row.text):
                continue
            key = normalize(rfc)
            physical = len(re.sub(r"\W", "", rfc)) == 13
            alternatives = _company_alternatives(
                pseudo, key, physical=physical, suffix=_legal_suffix(entity),
            )
            replacement_rfc = pseudo.rfc(rfc, key)
            replacement = f"{replacement_rfc} {alternatives[0]}".strip()
            result.append(_description_detection(
                row, Category.ASSOCIATED_ENTITY, family, replacement,
                tuple(f"{replacement_rfc} {value}" for value in alternatives[1:]),
                (rfc, entity), key, 0.96 if relaxed else 0.995,
            ))
            if entity:
                names_to_rfcs.setdefault(normalize(entity), set()).add(key)
            continue

        identifiers = _identifier_candidates(row.text)
        bank_context = bool(_BANK_CONTEXT_RE.search(normalize(row.text)) or row.parent_bank_text)
        if bank_context and identifiers:
            identifier = identifiers[0]
            digits = re.sub(r"\D", "", identifier)
            if re.fullmatch(r"(?:19|20)\d{2}", digits) and not _BANK_CONTEXT_RE.search(normalize(row.text)):
                continue
            bank_text = re.sub(
                re.escape(identifier), " ", row.text, count=1, flags=re.IGNORECASE,
            ).strip(" :-;,.")
            if sum(char.isalpha() for char in bank_text) < 3:
                bank_text = row.parent_bank_text or "BANCO"
            key = canonical_association_key(bank_text, identifier)
            replacement, alternatives = _bank_pair(pseudo, bank_text, identifier, key)
            result.append(_description_detection(
                row, Category.ASSOCIATED_BANK, family, replacement, alternatives,
                (bank_text, identifier), key, 0.98,
            ))
            continue

        if identifiers:
            identifier = identifiers[0]
            digits = re.sub(r"\D", "", identifier)
            if re.fullmatch(r"(?:19|20)\d{2}", digits):
                continue
            entity = re.sub(re.escape(identifier), " ", row.text, count=1).strip(" :-;,.")
            alpha_words = re.findall(r"[A-ZÁÉÍÓÚÜÑ]{2,}", normalize(entity))
            if len(alpha_words) < 2 or sum(char.isalpha() for char in entity) < 7:
                continue
            key = canonical_association_key(entity, identifier)
            # Sin RFC, un sufijo juridico o vocabulario societario permite
            # conservar persona moral frente a fisica. En ausencia de esas
            # anclas se mantiene el tratamiento conservador de persona.
            moral = bool(
                _legal_suffix(entity)
                or re.search(
                    r"\b(?:EMPRESA|ENTIDAD|SOCIEDAD|ASOCIACION|COMPANIA|CORPORACION)\b",
                    normalize(entity),
                )
            )
            alternatives = _company_alternatives(pseudo, key, physical=not moral)
            numeric = getattr(pseudo, "numeric_identifier", None)
            replacement_id = numeric(identifier, key) if callable(numeric) else pseudo.bank_account(identifier)
            result.append(_description_detection(
                row, Category.ASSOCIATED_ENTITY, family,
                f"{alternatives[0]} {replacement_id}",
                tuple(f"{value} {replacement_id}" for value in alternatives[1:]),
                (entity, identifier), key, 0.91,
            ))

    for name, keys in sorted(names_to_rfcs.items()):
        if len(keys) > 1:
            name_hash = pseudo.token("ambiguous-name", name, 16)
            key_hashes = ",".join(sorted(
                pseudo.token("ambiguous-rfc", key, 16) for key in keys
            ))
            warnings.append(f"nombre_asociado_a_multiples_rfc:{name_hash}:{key_hashes}")
    return _dedupe(result), warnings


def _detect_owner_repetitions(
    layouts: Sequence[PageLayout], family: str, owners: Sequence[Detection],
) -> list[Detection]:
    result: list[Detection] = []
    primary = [
        item for item in owners
        if item.category == Category.COMPANY and not item.redact_only
    ]
    existing = [item.rect for item in owners]
    for owner in primary:
        target = _compact(owner.original)
        if len(target) < 5:
            continue
        for layout in layouts:
            for line in layout.lines:
                source, mapping = _compact_map(line)
                start = source.find(target)
                if start < 0:
                    continue
                glyphs = tuple(dict.fromkeys(mapping[start:start + len(target)]))
                selection = Selection(_glyph_text(glyphs)[0], glyphs)
                if any(_intersects(selection.rect, rect, -0.2) for rect in existing):
                    continue
                item = _new_detection(
                    Category.COMPANY, layout.number, selection, family,
                    entity_key=owner.entity_key,
                )
                result.append(item)
                existing.append(item.rect)
    return result


def _materialize(
    detections: list[Detection], pseudo: Pseudonymizer, warnings: list[str],
) -> None:
    exercise_groups: dict[tuple[int, str], list[Detection]] = {}
    for item in detections:
        if item.redact_only or item.replacement:
            continue
        key = item.entity_key or normalize(item.original)
        if item.category == Category.COMPANY:
            values = _company_alternatives(
                pseudo, key, physical=False, suffix=_legal_suffix(item.original),
            )
            item.replacement, item.alternatives = values[0], values[1:]
        elif item.category == Category.RFC:
            item.replacement = pseudo.rfc(item.original, key)
        elif item.category == Category.ADDRESS:
            method = getattr(pseudo, "address_alternatives", None)
            values = tuple(method(key)) if callable(method) else (pseudo.address(key),)
            item.replacement, item.alternatives = values[0], values[1:]
        elif item.category == Category.POPULATION:
            method = getattr(pseudo, "population_alternatives", None)
            values = tuple(method(key)) if callable(method) else (pseudo.population(key),)
            item.replacement, item.alternatives = values[0], values[1:]
        elif item.category == Category.CERTIFICATE:
            item.replacement = pseudo.certificate(item.original, key)
        elif item.category == Category.USER:
            alternatives = getattr(pseudo, "user_alternatives", None)
            if callable(alternatives):
                values = tuple(alternatives(key))
                item.replacement, item.alternatives = values[0], values[1:]
            else:
                method = getattr(pseudo, "user", None)
                item.replacement = method(key) if callable(method) else f"USUARIO {pseudo.token('user', key, 8)}"
        elif item.category in (
            Category.CREATION_DATE, Category.HEADER_DATE, Category.PRINT_DATE,
            Category.FOOTER_DATE, Category.PERIOD_RANGE,
        ):
            temporal = getattr(pseudo, "replace_temporal_with_status", None)
            date = getattr(pseudo, "replace_date_with_status", None)
            if callable(temporal):
                item.replacement, fallback = temporal(item.original)
            elif callable(date):
                item.replacement, fallback = date(item.original)
            else:
                item.replacement, fallback = pseudo.replace_date(item.original), False
            if fallback:
                warnings.append(f"fallback_fecha:{pseudo.token('date-fallback', item.original, 16)}")
        elif item.category == Category.EXERCISE_PERIOD:
            exercise_groups.setdefault((item.page, item.profile), []).append(item)

    for group in exercise_groups.values():
        years = [item for item in group if re.fullmatch(r"\d{4}", item.original.strip())]
        periods = [item for item in group if re.fullmatch(r"\d{1,2}", item.original.strip())]
        for year, period in zip(years, periods):
            year.replacement, period.replacement = pseudo.exercise_and_period(
                year.original, period.original,
            )

    for item in detections:
        if item.redact_only:
            continue
        if not item.replacement or normalize(item.replacement) == normalize(item.original):
            raise ValueError("No se genero un reemplazo distinto para una deteccion sensible.")
        if not item.alternatives:
            item.alternatives = (item.replacement,)
        elif item.alternatives[0] != item.replacement:
            item.alternatives = (item.replacement,) + tuple(item.alternatives)


def _dedupe(detections: Iterable[Detection]) -> list[Detection]:
    result: list[Detection] = []
    seen: set[tuple[Any, ...]] = set()
    for item in detections:
        key = (
            item.category, item.page,
            tuple(round(value, 1) for value in item.rect), normalize(item.original),
        )
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _validate_required(
    family: str, detections: Sequence[Detection], header_pages: Sequence[PageLayout],
) -> list[str]:
    categories = {item.category for item in detections}
    required = {
        WEB_BALANCE: {Category.COMPANY, Category.RFC, Category.CREATION_DATE, Category.EXERCISE_PERIOD},
        CLASSIC_BALANCE: {Category.COMPANY, Category.RFC, Category.HEADER_DATE},
        CONTALINK_BALANCE: {Category.COMPANY, Category.RFC, Category.PRINT_DATE, Category.PERIOD_RANGE},
        CONTPAQ_BALANCE: {Category.COMPANY, Category.HEADER_DATE, Category.TEXT_LOGO},
    }[family]
    errors = [
        f"campo_obligatorio_no_localizado:{category.value}"
        for category in sorted(required - categories, key=lambda value: value.value)
    ]
    if not header_pages:
        errors.append("cabecera_obligatoria_no_localizada")
    return errors


def detect_document(
    layouts: list[PageLayout], pseudonymizer: Pseudonymizer,
) -> ProfileResult | None:
    """Selecciona una familia por puntuacion y combina todos los detectores."""
    family, scores = _choose_family(layouts)
    if family is None:
        return None
    columns = infer_columns(layouts, family)
    if len(columns) != len(layouts):
        return ProfileResult(
            family, scores[family], warnings=["No se pudieron inferir columnas en todas las paginas."],
            family_scores=scores, extra={"fatal": ["columnas_no_inferidas"]},
        )
    detections = _detect_identity_and_fiscal(layouts, family)
    detections.extend(_detect_dates(layouts, family))
    detections.extend(_detect_users(layouts, family))
    detections.extend(_detect_logos(layouts, family, columns))
    associated, warnings = _detect_description_associations(
        layouts, family, columns, pseudonymizer,
    )
    detections.extend(associated)
    detections.extend(_detect_owner_repetitions(layouts, family, detections))
    detections = _dedupe(detections)
    fatal = _validate_required(family, detections, _header_pages(layouts, family))
    if fatal:
        return ProfileResult(
            family, scores[family], detections, warnings, scores,
            extra={
                "fatal": fatal,
                "columnas": {page: bounds.__dict__ for page, bounds in columns.items()},
            },
        )
    _link_owner_fields(detections)
    _materialize(detections, pseudonymizer, warnings)
    return ProfileResult(
        family, scores[family], detections, warnings, scores,
        sorted({item.category.value for item in detections}),
        {
            "fatal": [],
            "columnas": {page: bounds.__dict__ for page, bounds in columns.items()},
        },
    )

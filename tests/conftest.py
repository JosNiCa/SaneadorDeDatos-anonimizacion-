from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import fitz
import pytest


WEB_OWNER_LINES = ("GRUPO DEMOSTRACION DEL CENTRO", "SERVICIOS CONTABLES SA DE CV")
WEB_RFC = "GDC210402AB1"
CLASSIC_OWNER = "CONSTRUCTORA DEMOSTRACION DEL NORTE SA DE CV"
CLASSIC_RFC = "CDN210402AB1"
CONTALINK_OWNER = "COMERCIALIZADORA ENLACE DEMO SA DE CV"
CONTALINK_RFC = "CED210402AB1"
CONTPAQ_OWNER = "FABRICA CONTABLE DEMO SA DE CV"

LOGO_RECT = fitz.Rect(516, 18, 568, 48)
ORDINARY_IMAGE_RECT = fitz.Rect(60, 470, 120, 530)


@pytest.fixture
def seed() -> str:
    return "semilla-local-pruebas-no-publica-2026"


@dataclass(frozen=True)
class AccountingRow:
    account: str
    description: str
    initial: str = "1,000.00"
    debit: str = "200.00"
    credit: str = "50.00"
    final: str = "1,150.00"


DEFAULT_ROWS = (
    AccountingRow("101.01", "CAJA GENERAL"),
    AccountingRow(
        "201.01",
        "PROVEEDOR DEMO SIN IDENTIFICADOR",
        "2,000.00",
        "0.00",
        "300.00",
        "1,700.00",
    ),
)


def _insert(
    page: fitz.Page,
    x: float,
    y: float,
    text: str,
    *,
    size: float = 8.0,
    fontname: str = "helv",
) -> None:
    page.insert_text((x, y), text, fontname=fontname, fontsize=size, color=(0, 0, 0))


def _solid_pixmap(size: int, rgb: int) -> fitz.Pixmap:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size), False)
    color = ((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
    pixmap.set_rect(pixmap.irect, color)
    return pixmap


def _insert_logo(page: fitz.Page, *, xref: int = 0) -> int:
    if xref:
        return page.insert_image(LOGO_RECT, xref=xref)
    return page.insert_image(LOGO_RECT, pixmap=_solid_pixmap(12, 0xE53935))


def _insert_ordinary_image(page: fitz.Page) -> int:
    return page.insert_image(ORDINARY_IMAGE_RECT, pixmap=_solid_pixmap(16, 0x19C463))


def _table_columns(description_header: str) -> tuple[tuple[float, str], ...]:
    return (
        (38, "Cuenta"),
        (112, description_header),
        (342, "Saldo inicial"),
        (415, "Debe"),
        (465, "Haber"),
        (520, "Saldo final"),
    )


def _draw_table(
    page: fitz.Page,
    rows: Sequence[AccountingRow],
    *,
    header_y: float,
    row_step: float = 18,
    description_header: str = "Descripción",
    dense: bool = False,
    inline_vector: bool = False,
) -> None:
    font_size = 6.4 if dense else 7.2
    for x, label in _table_columns(description_header):
        _insert(page, x, header_y, label, size=font_size)

    first_y = header_y + (14 if dense else 18)
    for index, row in enumerate(rows):
        y = first_y + index * row_step
        for x, text in (
            (38, row.account),
            (112, row.description),
            (350, row.initial),
            (420, row.debit),
            (470, row.credit),
            (525, row.final),
        ):
            _insert(page, x, y, text, size=font_size)

    table_top = header_y - 10
    table_bottom = first_y + max(len(rows) - 1, 0) * row_step + 7
    shape = page.new_shape()
    for x in (34, 106, 338, 410, 462, 516, 584):
        shape.draw_line(fitz.Point(x, table_top), fitz.Point(x, table_bottom))
    for y in (table_top, header_y + 4, table_bottom):
        shape.draw_line(fitz.Point(34, y), fitz.Point(584, y))
    shape.finish(color=(0, 0, 1), width=0.45)
    shape.commit()

    if inline_vector and rows:
        # Dibujo sintetico completamente dentro de una celda sensible. La
        # redaccion de texto debe preservarlo usando graphics=0.
        page.draw_line(
            fitz.Point(116, first_y - 2),
            fitz.Point(176, first_y - 2),
            color=(0, 0, 1),
            width=0.35,
        )


def _draw_web_table(
    page: fitz.Page,
    rows: Sequence[AccountingRow],
    *,
    header_y: float,
) -> None:
    """WEB usa una sola columna semantica para codigo y descripcion."""

    for x, label in (
        (38, "Cuenta"),
        (342, "Saldos Iniciales"),
        (415, "Debe"),
        (465, "Haber"),
        (520, "Saldos Actuales"),
    ):
        _insert(page, x, header_y, label, size=7.2)
    first_y = header_y + 18
    for index, row in enumerate(rows):
        y = first_y + index * 18
        for x, text in (
            (38, f"{row.account} {row.description}"),
            (350, row.initial),
            (420, row.debit),
            (470, row.credit),
            (525, row.final),
        ):
            _insert(page, x, y, text, size=7.2)
    table_bottom = first_y + max(len(rows) - 1, 0) * 18 + 7
    shape = page.new_shape()
    for x in (34, 338, 410, 462, 516, 584):
        shape.draw_line(fitz.Point(x, header_y - 10), fitz.Point(x, table_bottom))
    for y in (header_y - 10, header_y + 4, table_bottom):
        shape.draw_line(fitz.Point(34, y), fitz.Point(584, y))
    shape.finish(color=(0, 0, 1), width=0.45)
    shape.commit()


def _rows_for_page(
    rows_by_page: Sequence[Sequence[AccountingRow]] | None,
    page_number: int,
) -> Sequence[AccountingRow]:
    if rows_by_page is None:
        return DEFAULT_ROWS
    return rows_by_page[page_number]


def make_web_balance(
    path: Path,
    *,
    pages: int = 2,
    owner_lines: Sequence[str] = WEB_OWNER_LINES,
    rfc: str = WEB_RFC,
    rows_by_page: Sequence[Sequence[AccountingRow]] | None = None,
    metadata: dict[str, str] | None = None,
    owner_font: str = "helv",
    owner_size: float = 8.5,
    detached_creation: bool = False,
) -> None:
    """Familia WEB: A4, cabecera solo en pagina uno y Cuenta compartida."""

    document = fitz.open()
    for page_number in range(pages):
        page = document.new_page(width=595, height=842)
        if page_number == 0:
            owner_y = 48 if detached_creation else 38
            _insert(
                page,
                38,
                owner_y,
                "Nombre/Razón Social:",
                size=owner_size if detached_creation else 8.5,
                fontname=owner_font if detached_creation else "helv",
            )
            for index, line in enumerate(owner_lines):
                _insert(
                    page,
                    250 if detached_creation else 155,
                    owner_y + index * max(13.0, owner_size * 1.28),
                    line,
                    size=owner_size,
                    fontname=owner_font,
                )
            _insert(page, 38, 82 if detached_creation else 72, f"RFC: {rfc}", size=8.5)
            _insert(
                page,
                38 if detached_creation else 330,
                24 if detached_creation else 38,
                "Fecha creación: 2026-08-24 15:37:33",
                size=7.5,
            )
            _insert(page, 330, 100 if detached_creation else 54, "Ejercicio: 2026", size=7.5)
            _insert(page, 430, 100 if detached_creation else 54, "Período: 03", size=7.5)
            _insert(page, 38, 120 if detached_creation else 96, "Balanza de comprobación", size=10)
            _draw_web_table(
                page,
                _rows_for_page(rows_by_page, page_number),
                header_y=150 if detached_creation else 126,
            )
        else:
            # Continuacion deliberadamente sin cabecera ni encabezados de tabla.
            rows = _rows_for_page(rows_by_page, page_number)
            for index, row in enumerate(rows):
                y = 42 + index * 18
                _insert(page, 38, y, f"{row.account} {row.description}", size=7.2)
                _insert(page, 350, y, row.initial, size=7.2)
                _insert(page, 420, y, row.debit, size=7.2)
                _insert(page, 470, y, row.credit, size=7.2)
                _insert(page, 525, y, row.final, size=7.2)
    if metadata:
        document.set_metadata(metadata)
    document.save(path)
    document.close()


def make_classic_balance(
    path: Path,
    *,
    pages: int = 2,
    repeat_header: bool = True,
    empty_fields: bool = False,
    fragmented_reg_fed: bool = False,
    rows_by_page: Sequence[Sequence[AccountingRow]] | None = None,
    raster_logo: bool = False,
    ordinary_image: bool = False,
    footer: bool = False,
    near_footer: bool = False,
    impossible_date: bool = False,
) -> None:
    """Familia clasica carta con columnas separadas y cabecera configurable."""

    document = fitz.open()
    logo_xref = 0
    for page_number in range(pages):
        page = document.new_page(width=612, height=792)
        has_header = page_number == 0 or repeat_header
        if has_header:
            _insert(page, 38, 34, CLASSIC_OWNER, size=9)
            _insert(page, 38, 50, f"Dirección: {'' if empty_fields else 'VIA DEMO # 1725 SECTOR PRUEBA'}")
            _insert(page, 38, 66, f"Población: {'' if empty_fields else 'NEXORA, ZZ'}")
            federal_label = "Reg. f e d." if fragmented_reg_fed else "Reg. fed."
            _insert(page, 38, 82, f"{federal_label}: {CLASSIC_RFC}")
            _insert(page, 280, 82, f"Cédula: {'' if empty_fields else 'CED-9988-XY'}")
            cutoff = "31/febrero/25" if impossible_date else "11/febrero/25"
            _insert(page, 38, 104, f"Balanza de comprobación al {cutoff}", size=9)
            if raster_logo:
                logo_xref = _insert_logo(page, xref=logo_xref)
        if ordinary_image and page_number == 0:
            _insert_ordinary_image(page)

        rows = _rows_for_page(rows_by_page, page_number)
        _draw_table(page, rows, header_y=704 if near_footer else 142, row_step=18)
        if footer:
            _insert(page, 38, 778, "Fecha de impresión: 24/08/2026 16:01:02", size=7)
            _insert(page, 505, 778, f"Página {page_number + 1}", size=7)
    document.save(path)
    document.close()


def make_contalink_balance(
    path: Path,
    *,
    pages: int = 2,
    rows_by_page: Sequence[Sequence[AccountingRow]] | None = None,
    raster_logo: bool = True,
) -> None:
    """Familia ContaLink A4 con propietario/RFC apilados y continuacion."""

    document = fitz.open()
    logo_xref = 0
    for page_number in range(pages):
        page = document.new_page(width=595, height=842)
        if page_number == 0:
            _insert(page, 36, 34, CONTALINK_OWNER, size=9)
            _insert(page, 36, 50, CONTALINK_RFC, size=8.5)
            _insert(page, 36, 76, "Balanza de comprobación", size=11)
            _insert(page, 330, 52, "Fecha de impresión: 24/08/2026 16:01:02", size=7.2)
            _insert(page, 330, 68, "Período: 01/01/2026 al 31/01/2026", size=7.2)
            _insert(page, 330, 84, "Tipos de monedas: PESOS", size=7.2)
            if raster_logo:
                logo_xref = _insert_logo(page, xref=logo_xref)
            _draw_table(
                page,
                _rows_for_page(rows_by_page, page_number),
                header_y=118,
                description_header="Nombre",
            )
        else:
            rows = _rows_for_page(rows_by_page, page_number)
            for index, row in enumerate(rows):
                y = 42 + index * 18
                for x, text in (
                    (38, row.account),
                    (112, row.description),
                    (350, row.initial),
                    (420, row.debit),
                    (470, row.credit),
                    (525, row.final),
                ):
                    _insert(page, x, y, text, size=7.2)
    document.save(path)
    document.close()


def make_contpaq_balance(
    path: Path,
    *,
    rows: Sequence[AccountingRow] | None = None,
    inline_vector: bool = True,
) -> None:
    """Familia CONTPAQ carta densa con logo textual en la linea superior."""

    document = fitz.open()
    page = document.new_page(width=612, height=792)
    _insert(page, 20, 28, "CONTPAQ i", size=9)
    _insert(page, 104, 28, CONTPAQ_OWNER, size=8.5)
    _insert(page, 535, 28, "Hoja 1", size=7.5)
    _insert(page, 20, 48, "Balanza de comprobación", size=10)
    _insert(page, 238, 48, "Fecha de corte: 31/01/2026", size=7.2)
    _insert(page, 430, 48, "Fecha de impresión: 24/08/2026", size=7.2)
    _insert(page, 20, 66, "Tipo Moneda: PESOS", size=7.2)
    _insert(page, 238, 66, "Saldos Iniciales", size=7.2)
    _insert(page, 430, 66, "Saldos Actuales", size=7.2)
    _draw_table(
        page,
        tuple(rows or DEFAULT_ROWS),
        header_y=92,
        row_step=13,
        dense=True,
        inline_vector=inline_vector,
    )
    document.save(path)
    document.close()


def make_unknown_balance(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    _insert(page, 42, 52, "Reporte auxiliar sin familia visual conocida", size=10)
    _insert(page, 42, 72, "Razon social: ENTIDAD AUXILIAR DEMO SA DE CV", size=8)
    _insert(page, 42, 88, "RFC: EAD210402AB1", size=8)
    document.save(path)
    document.close()


def make_textless_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(40, 40, 550, 800), color=(0, 0, 1), width=0.5)
    document.save(path)
    document.close()

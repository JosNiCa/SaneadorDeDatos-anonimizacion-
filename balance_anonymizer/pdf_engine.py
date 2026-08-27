"""Motor PyMuPDF transaccional para balanzas digitales.

El detector decide que es sensible y entrega glifos / celdas. Este modulo
redacta fisicamente esas regiones, reinserta texto que quepa, sanea el
contenedor y verifica que nada ajeno a las regiones autorizadas haya cambiado.
No contiene OCR ni elimina imagenes de manera global.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import fitz

from .detection import PageLayout, detect_document, layout_from_page
from .models import AnonymizationPlan, Category, Detection, FileResult, TextStyle
from .pseudonyms import Pseudonymizer, normalize


class AnonymizationError(RuntimeError):
    """Error de operacion que no incorpora texto ni nombres del documento."""


@dataclass(frozen=True)
class PageGeometry:
    mediabox: tuple[float, float, float, float]
    cropbox: tuple[float, float, float, float]
    rotation: int
    width: float
    height: float


@dataclass(frozen=True)
class RenderSnapshot:
    width: int
    height: int
    stride: int
    samples: bytes


@dataclass(frozen=True)
class PageSnapshot:
    geometry: PageGeometry
    characters: Counter[tuple[Any, ...]]
    numeric_words: Counter[tuple[Any, ...]]
    drawings: Counter[tuple[Any, ...]]
    ordinary_images: Counter[tuple[Any, ...]]
    render: RenderSnapshot


@dataclass(frozen=True)
class PdfPhysicalSnapshot:
    pages: tuple[PageSnapshot, ...]
    authorized: dict[int, tuple[tuple[float, float, float, float], ...]]
    logo_digests: dict[int, frozenset[bytes]]
    input_encrypted: bool


def list_input_pdfs(input_path: Path) -> list[Path]:
    """Acepta un PDF o los PDF no recursivos de un directorio."""

    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise AnonymizationError("La entrada individual debe ser un PDF.")
        return [input_path]
    if not input_path.is_dir():
        raise AnonymizationError("La ruta de entrada no existe o no es accesible.")
    return sorted(
        (
            path for path in input_path.iterdir()
            if path.is_file() and path.suffix.lower() == ".pdf"
        ),
        key=lambda path: path.name.casefold(),
    )


def output_path_for(source: Path, output_dir: Path, pseudonymizer: Pseudonymizer) -> Path:
    """No replica el nombre fuente, que podria contener informacion sensible."""

    identifier = pseudonymizer.token("output-file", str(source.resolve()), 16)
    return output_dir / f"anonimizado_{identifier}.pdf"


def _validated_words(page: fitz.Page) -> list[Any]:
    words = page.get_text("words", sort=True)
    if not words:
        return []
    for word in words:
        coordinates = tuple(float(value) for value in word[:4])
        if not (
            all(math.isfinite(value) for value in coordinates)
            and coordinates[0] >= page.rect.x0 - 0.1
            and coordinates[1] >= page.rect.y0 - 0.1
            and coordinates[2] > coordinates[0]
            and coordinates[3] > coordinates[1]
            and coordinates[2] <= page.rect.x1 + 0.1
            and coordinates[3] <= page.rect.y1 + 0.1
        ):
            raise AnonymizationError("Se detectaron coordenadas de texto no confiables.")
    return words


def _validate_document(document: fitz.Document) -> list[PageLayout]:
    """Exige texto digital posicionable en todas las paginas; nunca usa OCR."""

    if document.needs_pass:
        raise AnonymizationError("El PDF protegido con contrasena no se procesa en modo estricto.")
    if document.page_count < 1:
        raise AnonymizationError("El PDF no contiene paginas.")
    layouts: list[PageLayout] = []
    for index, page in enumerate(document):
        if not _validated_words(page):
            raise AnonymizationError("No hay texto digital extraible con coordenadas confiables.")
        layout = layout_from_page(index, page)
        if not layout.words or not layout.lines:
            raise AnonymizationError("No fue posible reconstruir lineas digitales confiables.")
        layouts.append(layout)
    return layouts


def _input_has_encryption(document: fitz.Document) -> bool:
    """Detecta tambien cifrado con contrasena de usuario vacia.

    PyMuPDF autentica esos archivos al abrirlos y entonces `is_encrypted`
    puede ser falso, aunque el trailer siga conteniendo `/Encrypt`.
    """

    if document.is_encrypted or (document.metadata.get("encryption") or "").strip():
        return True
    try:
        kind, value = document.xref_get_key(-1, "Encrypt")
        return kind not in ("null", "none") and bool(value and value != "null")
    except (RuntimeError, ValueError):
        return "/Encrypt" in document.pdf_trailer()


def _geometry(page: fitz.Page) -> PageGeometry:
    return PageGeometry(
        tuple(float(value) for value in page.mediabox),
        tuple(float(value) for value in page.cropbox),
        int(page.rotation),
        float(page.rect.width),
        float(page.rect.height),
    )


def _intersects(
    first: Sequence[float], second: Sequence[float], tolerance: float = 0.0,
) -> bool:
    return not (
        first[2] <= second[0] - tolerance
        or second[2] <= first[0] - tolerance
        or first[3] <= second[1] - tolerance
        or second[3] <= first[1] - tolerance
    )


def _clip_rect(
    rect: Sequence[float], page: fitz.Page, *, x_margin: float = 0.0, y_margin: float = 0.0,
) -> tuple[float, float, float, float]:
    return (
        max(page.rect.x0, float(rect[0]) - x_margin),
        max(page.rect.y0, float(rect[1]) - y_margin),
        min(page.rect.x1, float(rect[2]) + x_margin),
        min(page.rect.y1, float(rect[3]) + y_margin),
    )


def _text_redaction_rect(item: Detection, page: fitz.Page) -> tuple[float, float, float, float]:
    # Margen vertical pequeno para no tocar filas contiguas en tablas densas.
    original = tuple(float(value) for value in item.rect)
    left, top, right, bottom = _clip_rect(
        original, page, x_margin=0.28, y_margin=0.12,
    )
    # Algunas fuentes declaran cajas que se solapan entre dos baselines aunque
    # la tinta no lo haga. Una redaccion que roce esa caja eliminaria el glifo
    # vecino completo. Recortamos el borde al final de las cajas cuyo centro
    # queda claramente fuera del valor sensible.
    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    box = char.get("bbox")
                    if not box or len(box) != 4:
                        continue
                    x0, y0, x1, y1 = (float(value) for value in box)
                    center_x = (x0 + x1) / 2.0
                    center_y = (y0 + y1) / 2.0
                    vertical_overlap = y1 > original[1] and y0 < original[3]
                    horizontal_overlap = x1 > original[0] and x0 < original[2]
                    if vertical_overlap and center_x < original[0] and x1 > left:
                        left = max(left, x1 + 0.02)
                    elif vertical_overlap and center_x > original[2] and x0 < right:
                        right = min(right, x0 - 0.02)
                    if horizontal_overlap and center_y < original[1] and y1 > top:
                        top = max(top, y1 + 0.02)
                    elif horizontal_overlap and center_y > original[3] and y0 < bottom:
                        bottom = min(bottom, y0 - 0.02)
    if right <= left or bottom <= top:
        raise AnonymizationError("No existe una region de redaccion segura entre glifos vecinos.")
    return left, top, right, bottom


def _configured_regions(
    document: fitz.Document,
    profile: str,
    vector_regions: dict[str, list[dict[str, Any]]] | None,
) -> dict[int, list[tuple[float, float, float, float]]]:
    result: dict[int, list[tuple[float, float, float, float]]] = {}
    if not vector_regions:
        return result
    regions = vector_regions.get(profile, [])
    if not isinstance(regions, list):
        raise AnonymizationError("La configuracion de regiones vectoriales es invalida.")
    for region in regions:
        try:
            page_number = int(region["page"])
            values = region["rect"]
            if not isinstance(values, list) or len(values) != 4:
                raise ValueError
            if not 0 <= page_number < document.page_count:
                raise ValueError
            page = document[page_number]
            rect = tuple(float(value) for value in values)
            if not all(math.isfinite(value) for value in rect):
                raise ValueError
            clipped = _clip_rect(rect, page)
            if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
                raise ValueError
            result.setdefault(page_number, []).append(clipped)
        except (KeyError, TypeError, ValueError) as exc:
            raise AnonymizationError("La configuracion de regiones vectoriales es invalida.") from exc
    return result


def _authorized_regions(
    document: fitz.Document,
    detections: Sequence[Detection],
    configured: dict[int, list[tuple[float, float, float, float]]],
) -> dict[int, tuple[tuple[float, float, float, float], ...]]:
    regions: dict[int, list[tuple[float, float, float, float]]] = {
        page: list(values) for page, values in configured.items()
    }
    for item in detections:
        if item.page < 0 or item.page >= document.page_count or item.category == Category.METADATA:
            continue
        page = document[item.page]
        if item.category == Category.RASTER_IMAGE:
            rect = _clip_rect(item.rect, page, x_margin=0.15, y_margin=0.15)
        else:
            rect = _text_redaction_rect(item, page)
        regions.setdefault(item.page, []).append(rect)
        if item.insert_rect and not item.redact_only:
            regions[item.page].append(
                _clip_rect(item.insert_rect, page, x_margin=0.4, y_margin=0.8)
            )
    return {
        page: tuple(
            dict.fromkeys(tuple(round(value, 4) for value in rect) for rect in values)
        )
        for page, values in regions.items()
    }


def _outside(rect: Sequence[float], excluded: Sequence[Sequence[float]], margin: float = 0.0) -> bool:
    return not any(_intersects(rect, region, margin) for region in excluded)


def _character_fingerprint(
    page: fitz.Page, excluded: Sequence[Sequence[float]],
) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    raw = page.get_text("rawdict", sort=True)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    box = char.get("bbox")
                    value = str(char.get("c", ""))
                    if not value or not box or len(box) != 4 or not _outside(box, excluded, 0.15):
                        continue
                    result[(value, *(round(float(item), 1) for item in box))] += 1
    return result


def _numeric_word_fingerprint(
    page: fitz.Page, excluded: Sequence[Sequence[float]],
) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    for word in page.get_text("words", sort=True):
        value = str(word[4])
        box = tuple(float(item) for item in word[:4])
        if any(char.isdigit() for char in value) and _outside(box, excluded, 0.15):
            result[(value, *(round(item, 1) for item in box))] += 1
    return result


def _round_value(value: Any) -> Any:
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, (fitz.Point, fitz.Rect, fitz.Quad)):
        return tuple(round(float(item), 3) for item in value)
    if isinstance(value, (tuple, list)):
        return tuple(_round_value(item) for item in value)
    return repr(value)


def _drawing_fingerprint(
    page: fitz.Page, excluded_vector_regions: Sequence[Sequence[float]],
) -> Counter[tuple[Any, ...]]:
    result: Counter[tuple[Any, ...]] = Counter()
    for drawing in page.get_drawings(extended=False):
        rect = drawing.get("rect")
        if rect is not None and not _outside(tuple(rect), excluded_vector_regions, 0.1):
            continue
        signature = (
            drawing.get("type"),
            _round_value(drawing.get("rect")),
            _round_value(drawing.get("color")),
            _round_value(drawing.get("fill")),
            _round_value(drawing.get("width")),
            str(drawing.get("dashes", "")),
            _round_value(drawing.get("items", ())),
        )
        result[signature] += 1
    return result


def _image_signature(info: dict[str, Any]) -> tuple[Any, ...]:
    bbox = info.get("bbox") or (0, 0, 0, 0)
    digest = info.get("digest") or b""
    return (
        bytes(digest),
        *(round(float(value), 1) for value in bbox),
        int(info.get("width", 0)),
        int(info.get("height", 0)),
        int(info.get("bpc", 0)),
        str(info.get("colorspace", "")),
    )


def _image_fingerprints(
    page: fitz.Page,
    logo_regions: Sequence[Sequence[float]],
) -> tuple[Counter[tuple[Any, ...]], frozenset[bytes]]:
    ordinary: Counter[tuple[Any, ...]] = Counter()
    logo_digests: set[bytes] = set()
    for info in page.get_image_info(hashes=True, xrefs=True):
        bbox = tuple(float(value) for value in info.get("bbox", (0, 0, 0, 0)))
        digest = bytes(info.get("digest") or b"")
        if any(_intersects(bbox, region, 0.2) for region in logo_regions):
            if digest:
                logo_digests.add(digest)
            continue
        ordinary[_image_signature(info)] += 1
    return ordinary, frozenset(logo_digests)


def _render(page: fitz.Page) -> RenderSnapshot:
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(1.25, 1.25),
        alpha=False,
        colorspace=fitz.csRGB,
        annots=False,
    )
    if pixmap.width < 1 or pixmap.height < 1 or pixmap.n != 3:
        raise AnonymizationError("No se pudo renderizar una pagina para verificarla.")
    return RenderSnapshot(pixmap.width, pixmap.height, pixmap.stride, bytes(pixmap.samples))


def _snapshot_document(
    document: fitz.Document,
    authorized: dict[int, tuple[tuple[float, float, float, float], ...]],
    detections: Sequence[Detection],
    configured: dict[int, list[tuple[float, float, float, float]]],
) -> PdfPhysicalSnapshot:
    logo_regions: dict[int, list[tuple[float, float, float, float]]] = {}
    for item in detections:
        if item.category == Category.RASTER_IMAGE and item.page >= 0:
            logo_regions.setdefault(item.page, []).append(item.rect)
    pages: list[PageSnapshot] = []
    logo_digests: dict[int, frozenset[bytes]] = {}
    for number, page in enumerate(document):
        excluded = authorized.get(number, ())
        ordinary, digests = _image_fingerprints(page, logo_regions.get(number, ()))
        logo_digests[number] = digests
        pages.append(PageSnapshot(
            _geometry(page),
            _character_fingerprint(page, excluded),
            _numeric_word_fingerprint(page, excluded),
            _drawing_fingerprint(page, configured.get(number, ())),
            ordinary,
            _render(page),
        ))
    return PdfPhysicalSnapshot(
        tuple(pages), authorized, logo_digests, _input_has_encryption(document),
    )


def _metadata_detection(document: fitz.Document, profile: str) -> Detection | None:
    values: list[str] = []
    for key in (
        "title", "author", "subject", "keywords", "creator", "producer",
        "creationDate", "modDate",
    ):
        value = document.metadata.get(key) or ""
        if value.strip():
            values.append(value)
    xmp = document.get_xml_metadata()
    if xmp:
        values.append(xmp)
    values.extend(str(name) for name in document.embfile_names())
    values.extend(
        str(item[1]) for item in document.get_toc(simple=True)
        if len(item) > 1 and item[1]
    )
    if not values:
        return None
    return Detection(
        Category.METADATA,
        -1,
        (0.0, 0.0, 0.0, 0.0),
        "\n".join(values),
        None,
        profile,
        redact_only=True,
        confidence=1.0,
    )


def _add_redaction_annotations(
    document: fitz.Document,
    detections: Sequence[Detection],
    configured: dict[int, list[tuple[float, float, float, float]]],
) -> int:
    """Aplica texto, logos raster y regiones vectoriales en pases separados."""

    by_page: dict[int, list[Detection]] = {}
    for item in detections:
        if item.page >= 0 and item.category != Category.METADATA:
            by_page.setdefault(item.page, []).append(item)
    for page_number, items in by_page.items():
        page = document[page_number]
        text_items = [item for item in items if item.category != Category.RASTER_IMAGE]
        seen: set[tuple[float, float, float, float]] = set()
        for item in text_items:
            rect = _text_redaction_rect(item, page)
            key = tuple(round(value, 3) for value in rect)
            if key in seen:
                continue
            seen.add(key)
            page.add_redact_annot(fitz.Rect(rect), fill=None, cross_out=False)
        if seen:
            page.apply_redactions(
                images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0),
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
                text=0,
            )

        raster_items = [item for item in items if item.category == Category.RASTER_IMAGE]
        for item in raster_items:
            page.add_redact_annot(
                fitz.Rect(_clip_rect(item.rect, page, x_margin=0.15, y_margin=0.15)),
                fill=None,
                cross_out=False,
            )
        if raster_items:
            page.apply_redactions(
                images=getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 1),
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_NONE", 0),
                text=0,
            )

    vector_count = 0
    for page_number, regions in configured.items():
        page = document[page_number]
        for rect in regions:
            page.add_redact_annot(fitz.Rect(rect), fill=None, cross_out=False)
            vector_count += 1
        if regions:
            page.apply_redactions(
                images=getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 1),
                graphics=getattr(fitz, "PDF_REDACT_LINE_ART_REMOVE_IF_COVERED", 1),
                text=0,
            )
    return vector_count


def _font_name(style: TextStyle | None) -> str:
    flags = style.flags if style else 0
    source_name = (style.font if style else "").casefold()
    bold = bool(flags & getattr(fitz, "TEXT_FONT_BOLD", 16)) or any(
        marker in source_name for marker in ("bold", "black", "demi", "semibold")
    )
    italic = bool(flags & getattr(fitz, "TEXT_FONT_ITALIC", 2)) or any(
        marker in source_name for marker in ("italic", "oblique")
    )

    # PyMuPDF no permite referenciar de forma portable el nombre interno de
    # cualquier fuente embebida al crear un nuevo content stream. Elegimos la
    # variante Base-14 de la misma familia visual y conservamos peso/cursiva;
    # esto evita que una seccion serif o monoespaciada reaparezca en Helvetica.
    if any(marker in source_name for marker in ("times", "serif", "roman")):
        regular, bold_name, italic_name, bold_italic = "tiro", "tibo", "tiit", "tibi"
    elif any(marker in source_name for marker in ("courier", "mono", "typewriter")):
        regular, bold_name, italic_name, bold_italic = "cour", "cobo", "coit", "cobi"
    else:
        regular, bold_name, italic_name, bold_italic = "helv", "hebo", "heit", "hebi"
    if bold and italic:
        return bold_italic
    if bold:
        return bold_name
    if italic:
        return italic_name
    return regular


def _text_color(style: TextStyle | None) -> tuple[float, float, float]:
    try:
        return tuple(float(value) for value in fitz.sRGB_to_pdf(style.color if style else 0))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)


def _insert_rect(item: Detection, page: fitz.Page) -> fitz.Rect:
    source = item.insert_rect or item.rect
    clipped = _clip_rect(source, page, x_margin=0.0, y_margin=0.65)
    return fitz.Rect(clipped)


def _replacement_candidates(item: Detection) -> tuple[str, ...]:
    result: list[str] = []
    for value in (item.replacement, *item.alternatives):
        if not value or not value.strip() or normalize(value) == normalize(item.original):
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def _insert_replacements(document: fitz.Document, detections: Sequence[Detection]) -> None:
    for item in detections:
        if item.redact_only or item.category in (Category.RASTER_IMAGE, Category.METADATA):
            continue
        page = document[item.page]
        rect = _insert_rect(item, page)
        candidates = _replacement_candidates(item)
        if not candidates:
            raise AnonymizationError("No se pudo generar un reemplazo distinto del valor fuente.")
        style = item.style or TextStyle()
        # Comenzar en el tamano real del span es importante en cabeceras WEB,
        # donde el propietario suele usar una fuente notablemente mayor que
        # el cuerpo contable. Solo se reduce si la medicion demuestra que no
        # cabe; el limite alto evita valores corruptos sin normalizar a 13 pt.
        start_size = min(max(float(style.size or 8.0), 5.0), 36.0)
        minimum_size = max(4.75, min(start_size, start_size * 0.68))
        fontname = _font_name(style)
        color = _text_color(style)
        committed = False
        size = start_size
        while size >= minimum_size - 0.01 and not committed:
            for candidate in candidates:
                shape = page.new_shape()
                remaining = shape.insert_textbox(
                    rect,
                    candidate,
                    fontname=fontname,
                    fontsize=size,
                    lineheight=1.05,
                    color=color,
                    align=max(0, min(2, int(item.alignment))),
                )
                # PyMuPDF no inserta nada si el retorno es negativo, incluso
                # por una fraccion minima. Solo se confirma un ajuste real.
                if remaining < 0:
                    continue
                shape.commit(overlay=True)
                item.replacement = candidate
                committed = True
                break
            size = round(size - 0.25, 2)
        if not committed:
            raise AnonymizationError("El reemplazo no cabe de forma legible en su campo o celda.")


def _clear_internal_data(document: fitz.Document) -> None:
    """Elimina metadatos, adjuntos, JavaScript, enlaces y respuestas internas."""

    document.scrub(
        attached_files=True,
        clean_pages=False,
        embedded_files=True,
        hidden_text=False,
        javascript=True,
        metadata=True,
        redactions=False,
        redact_images=0,
        remove_links=True,
        reset_fields=True,
        reset_responses=True,
        thumbnails=True,
        xml_metadata=True,
    )
    document.set_toc([])
    document.set_metadata({
        "title": "",
        "author": "",
        "subject": "",
        "keywords": "",
        "creator": "",
        "producer": "",
        "creationDate": "",
        "modDate": "",
        "trapped": "",
    })
    if document.get_xml_metadata():
        document.del_xml_metadata()
    for name in tuple(document.embfile_names()):
        document.embfile_del(name)


def _compact_text(value: str) -> str:
    return "".join(char for char in normalize(value) if char.isalnum())


def _clip_text(page: fitz.Page, rect: Sequence[float]) -> str:
    expanded = _clip_rect(rect, page, x_margin=2.0, y_margin=2.0)
    return page.get_text("text", clip=fitz.Rect(expanded), sort=True)


def _object_text(document: fitz.Document) -> str:
    chunks: list[str] = []
    for xref in range(1, document.xref_length()):
        try:
            chunks.append(document.xref_object(xref, compressed=False, ascii=True))
            if document.xref_is_stream(xref):
                chunks.append(document.xref_stream(xref).decode("latin-1", errors="ignore"))
        except (RuntimeError, ValueError) as exc:
            raise AnonymizationError("No se pudieron inspeccionar todos los objetos del PDF.") from exc
    return "\n".join(chunks)


def _same_geometry(expected: PageGeometry, page: fitz.Page) -> bool:
    actual = _geometry(page)
    return (
        actual.rotation == expected.rotation
        and all(abs(a - b) <= 0.01 for a, b in zip(actual.mediabox, expected.mediabox))
        and all(abs(a - b) <= 0.01 for a, b in zip(actual.cropbox, expected.cropbox))
        and abs(actual.width - expected.width) <= 0.01
        and abs(actual.height - expected.height) <= 0.01
    )


def _merged_pixel_intervals(
    regions: Sequence[Sequence[float]],
    y: int,
    scale_x: float,
    scale_y: float,
    width: int,
) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    point_y = (y + 0.5) / scale_y
    for rect in regions:
        if rect[1] - 2.0 <= point_y <= rect[3] + 2.0:
            left = max(0, int(math.floor((rect[0] - 2.0) * scale_x)))
            right = min(width, int(math.ceil((rect[2] + 2.0) * scale_x)))
            if right > left:
                intervals.append((left, right))
    merged: list[tuple[int, int]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _pixel_difference(
    before: RenderSnapshot,
    after: RenderSnapshot,
    regions: Sequence[Sequence[float]],
    page_width: float,
    page_height: float,
) -> tuple[int, int]:
    if (before.width, before.height, before.stride) != (
        after.width, after.height, after.stride,
    ):
        raise AnonymizationError("El renderizado cambio de dimensiones durante la verificacion.")
    scale_x = before.width / page_width
    scale_y = before.height / page_height
    changed = 0
    compared = 0
    first = memoryview(before.samples)
    second = memoryview(after.samples)
    for y in range(before.height):
        intervals = _merged_pixel_intervals(regions, y, scale_x, scale_y, before.width)
        cursor = 0
        for left, right in (*intervals, (before.width, before.width)):
            if left > cursor:
                start = y * before.stride + cursor * 3
                end = y * before.stride + left * 3
                for offset in range(start, end, 3):
                    compared += 1
                    if (
                        abs(first[offset] - second[offset]) > 16
                        or abs(first[offset + 1] - second[offset + 1]) > 16
                        or abs(first[offset + 2] - second[offset + 2]) > 16
                    ):
                        changed += 1
            cursor = max(cursor, right)
    return changed, compared


def _verify_detected_values(document: fitz.Document, detections: Sequence[Detection]) -> None:
    object_text = _compact_text(_object_text(document))
    metadata_text = _compact_text(
        " ".join(value or "" for value in document.metadata.values())
    )
    for item in detections:
        if item.category == Category.METADATA:
            continue
        page = document[item.page]
        local = _compact_text(_clip_text(page, item.insert_rect or item.rect))
        for original in (item.original, *item.residuals):
            key = _compact_text(original)
            if not key:
                continue
            # Valores breves se comprueban solo en su region. Asi un periodo
            # "02" no se confunde con un codigo ajeno como "101.02".
            if key in local:
                raise AnonymizationError("La verificacion encontro un valor sensible residual.")
            if len(key) >= 8 and (key in object_text or key in metadata_text):
                raise AnonymizationError("La verificacion encontro un valor sensible en objetos internos.")
        if item.redact_only or not item.replacement:
            continue
        replacement = _compact_text(item.replacement)
        target_rect = _clip_rect(
            item.insert_rect or item.rect, page, x_margin=2.0, y_margin=2.0,
        )
        positioned_hits = [
            tuple(float(value) for value in hit)
            for hit in page.search_for(item.replacement, quads=False)
            if _intersects(tuple(float(value) for value in hit), target_rect, 0.1)
        ]
        if not replacement or (replacement not in local and not positioned_hits):
            raise AnonymizationError("La verificacion no encontro un reemplazo esperado.")


def _verify_output(
    output: Path,
    snapshot: PdfPhysicalSnapshot,
    detections: Sequence[Detection],
    configured: dict[int, list[tuple[float, float, float, float]]],
) -> dict[str, Any]:
    with fitz.open(output) as document:
        if document.needs_pass or document.is_encrypted:
            raise AnonymizationError("La salida conserva cifrado no autorizado.")
        if document.page_count != len(snapshot.pages):
            raise AnonymizationError("La cantidad de paginas cambio durante la redaccion.")
        changed_pixels = 0
        compared_pixels = 0
        for number, (page, expected) in enumerate(zip(document, snapshot.pages)):
            if not _same_geometry(expected.geometry, page):
                raise AnonymizationError("La geometria, caja o rotacion de una pagina cambio.")
            excluded = snapshot.authorized.get(number, ())
            if _character_fingerprint(page, excluded) != expected.characters:
                raise AnonymizationError("Cambiaron palabras o coordenadas fuera de las regiones autorizadas.")
            if _numeric_word_fingerprint(page, excluded) != expected.numeric_words:
                raise AnonymizationError("Cambiaron tokens numericos contables fuera de las regiones autorizadas.")
            if _drawing_fingerprint(page, configured.get(number, ())) != expected.drawings:
                raise AnonymizationError("Cambiaron dibujos vectoriales fuera de las regiones autorizadas.")
            logo_rects = [
                item.rect for item in detections
                if item.page == number and item.category == Category.RASTER_IMAGE
            ]
            ordinary, _ = _image_fingerprints(page, logo_rects)
            if ordinary != expected.ordinary_images:
                raise AnonymizationError("Cambio una imagen que no fue clasificada como logo.")
            if snapshot.logo_digests.get(number):
                for info in page.get_image_info(hashes=True, xrefs=True):
                    digest = bytes(info.get("digest") or b"")
                    bbox = tuple(float(value) for value in info.get("bbox", (0, 0, 0, 0)))
                    if digest in snapshot.logo_digests[number] and any(
                        _intersects(bbox, rect, 0.2) for rect in logo_rects
                    ):
                        raise AnonymizationError("Un logo rasterizado confirmado permanece en la salida.")
            current_render = _render(page)
            changed, compared = _pixel_difference(
                expected.render,
                current_render,
                excluded,
                expected.geometry.width,
                expected.geometry.height,
            )
            changed_pixels += changed
            compared_pixels += compared
        tolerance = max(32, int(compared_pixels * 0.00001))
        if changed_pixels > tolerance:
            raise AnonymizationError("La diferencia visual excede las regiones autorizadas.")
        _verify_detected_values(document, detections)
        if document.get_xml_metadata() or tuple(document.embfile_names()) or document.get_toc(simple=True):
            raise AnonymizationError("Persisten XMP, adjuntos o marcadores internos.")
        forbidden_metadata = (
            "title", "author", "subject", "keywords", "creator", "producer",
            "creationDate", "modDate",
        )
        if any((document.metadata.get(key) or "").strip() for key in forbidden_metadata):
            raise AnonymizationError("Persisten metadatos internos originales.")
        return {
            "paginas_verificadas": document.page_count,
            "pixeles_fuera_comparados": compared_pixels,
            "pixeles_fuera_cambiados": changed_pixels,
            "geometria_preservada": True,
            "texto_fuera_preservado": True,
            "numeros_fuera_preservados": True,
            "dibujos_fuera_preservados": True,
            "imagenes_no_logo_preservadas": True,
            "salida_sin_cifrado": True,
        }


def _apply_shared_plan(
    detections: Sequence[Detection],
    plan: AnonymizationPlan,
) -> None:
    """Sustituye materializaciones locales por el plan común del grupo."""

    owner_keys = {
        Category.COMPANY: "name",
        Category.RFC: "rfc",
        Category.ADDRESS: "address",
        Category.POPULATION: "population",
        Category.CERTIFICATE: "certificate",
    }
    for item in detections:
        key = owner_keys.get(item.category)
        if key and key in plan.synthetic_owner:
            item.replacement = plan.synthetic_owner[key]
            item.alternatives = (item.replacement,)
            item.entity_key = plan.entity_key

    canonical = plan.canonical_temporal
    if canonical is None:
        return
    from .adapters.common import format_date_like

    period_items = sorted(
        (item for item in detections if item.category == Category.PERIOD_RANGE),
        key=lambda item: (item.page, item.rect[1], item.rect[0]),
    )
    period_dates = [value for value in (canonical.period_start, canonical.period_end) if value]
    for index, item in enumerate(period_items):
        if period_dates:
            item.replacement = format_date_like(
                item.original,
                period_dates[index % len(period_dates)],
            )
            item.alternatives = (item.replacement,)

    for item in detections:
        replacement_date = None
        if item.category == Category.HEADER_DATE:
            replacement_date = canonical.period_end or canonical.period_start
        elif item.category in {Category.PRINT_DATE, Category.CREATION_DATE, Category.FOOTER_DATE}:
            replacement_date = canonical.print_date or canonical.period_end or canonical.period_start
        if replacement_date is not None:
            item.replacement = format_date_like(item.original, replacement_date)
            item.alternatives = (item.replacement,)

    exercise_items = [item for item in detections if item.category == Category.EXERCISE_PERIOD]
    if canonical.year is not None and canonical.month is not None:
        for item in exercise_items:
            stripped = item.original.strip()
            if len(stripped) == 4:
                item.replacement = str(canonical.year)
            else:
                item.replacement = str(canonical.month).zfill(len(stripped))
            item.alternatives = (item.replacement,)


def anonymize_file(
    source: Path,
    output_dir: Path,
    pseudonymizer: Pseudonymizer,
    *,
    strict: bool = True,
    dry_run: bool = False,
    vector_regions: dict[str, list[dict[str, Any]]] | None = None,
    plan: AnonymizationPlan | None = None,
) -> FileResult:
    """Anonimiza de forma transaccional: solo promueve una salida verificada."""

    source = source.resolve()
    output_dir = output_dir.resolve()
    target = output_path_for(source, output_dir, pseudonymizer)
    if target.resolve() == source:
        raise AnonymizationError("La salida no puede sobrescribir el PDF fuente.")
    if target.exists() and not dry_run:
        raise AnonymizationError("Ya existe una salida anonimizada para este archivo; no se sobrescribe.")

    fallback_before = int(getattr(pseudonymizer, "temporal_fallback_count", 0))
    with fitz.open(source) as document:
        layouts = _validate_document(document)
        profile_result = detect_document(layouts, pseudonymizer)
        if profile_result is None:
            raise AnonymizationError("No se reconocio una familia visual con confianza suficiente.")
        fatal = tuple(profile_result.extra.get("fatal", ()))
        if fatal:
            raise AnonymizationError("No se localizaron con confianza todos los campos o columnas obligatorios.")
        if strict and any(item.confidence < 0.90 for item in profile_result.detections):
            raise AnonymizationError("Existe un candidato sensible ambiguo en modo estricto.")

        if plan is not None:
            _apply_shared_plan(profile_result.detections, plan)

        metadata = _metadata_detection(document, profile_result.name)
        if metadata:
            profile_result.detections.append(metadata)
        detections = profile_result.detections
        unresolved = [
            item for item in detections
            if item.category != Category.METADATA and not item.redact_only and not item.replacement
        ]
        if unresolved:
            raise AnonymizationError("No se pudo crear un seudonimo para un dato obligatorio.")
        dimensions = [(page.rect.width, page.rect.height) for page in document]
        redactions = dict(Counter(item.category.value for item in detections))
        fallback_after = int(getattr(pseudonymizer, "temporal_fallback_count", fallback_before))
        fallback_count = max(0, fallback_after - fallback_before)
        base_extra = {
            "puntajes_familia": profile_result.family_scores,
            "capacidades": profile_result.capabilities,
            "entrada_cifrada": _input_has_encryption(document),
            "fallback_temporal": {
                "temporal_fallback_used": fallback_count > 0,
                "temporal_fallback_count": fallback_count,
            },
        }
        if dry_run:
            return FileResult(
                str(source),
                None,
                True,
                profile_result.name,
                document.page_count,
                redactions,
                detections,
                profile_result.warnings,
                page_dimensions=dimensions,
                extra=base_extra,
            )

        configured = _configured_regions(document, profile_result.name, vector_regions)
        authorized = _authorized_regions(document, detections, configured)
        snapshot = _snapshot_document(document, authorized, detections, configured)
        output_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".anon_", suffix=".pdf", dir=output_dir,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            vector_count = _add_redaction_annotations(document, detections, configured)
            _insert_replacements(document, detections)
            _clear_internal_data(document)
            document.save(
                temporary,
                garbage=4,
                clean=True,
                deflate=True,
                deflate_images=True,
                deflate_fonts=True,
                encryption=fitz.PDF_ENCRYPT_NONE,
                preserve_metadata=0,
            )
            verification = _verify_output(temporary, snapshot, detections, configured)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    if vector_count:
        redactions["logo_vectorial_configurado"] = vector_count
    extra = dict(base_extra)
    extra["verificacion"] = verification
    return FileResult(
        str(source),
        str(target),
        True,
        profile_result.name,
        len(dimensions),
        redactions,
        detections,
        profile_result.warnings,
        page_dimensions=dimensions,
        extra=extra,
    )


def safe_file_result(
    source: Path,
    output_dir: Path,
    pseudonymizer: Pseudonymizer,
    **kwargs: Any,
) -> FileResult:
    """Convierte fallos a resultados seguros sin volcar contenido fuente."""

    try:
        return anonymize_file(source, output_dir, pseudonymizer, **kwargs)
    except (fitz.FileDataError, fitz.EmptyFileError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, AnonymizationError):
            return FileResult(str(source), None, False, None, error=str(exc))
        if isinstance(exc, (fitz.FileDataError, fitz.EmptyFileError)):
            message = "El PDF esta vacio, danado o no tiene una estructura PDF compatible."
            code = "pdf_entrada_invalido"
        elif isinstance(exc, PermissionError):
            message = "No hay permisos suficientes para leer la entrada o crear la salida."
            code = "acceso_denegado"
        elif isinstance(exc, OSError):
            message = "No se pudo leer la entrada o escribir la salida en el sistema de archivos."
            code = "error_sistema_archivos"
        else:
            message = "El PDF contiene una estructura o contenido no compatible con el procesamiento seguro."
            code = "pdf_estructura_incompatible"
        return FileResult(
            str(source),
            None,
            False,
            None,
            error=message,
            extra={"codigo_error": code},
        )


def report_payload(
    results: list[FileResult], pseudonymizer: Pseudonymizer, *, dry_run: bool,
) -> dict[str, Any]:
    """Reporte tecnico: solo HMAC de valores; nunca texto original."""

    files: list[dict[str, Any]] = []
    for result in results:
        detected = [
            {
                "categoria": item.category.value,
                "pagina": item.page + 1 if item.page >= 0 else None,
                "coordenadas": [round(value, 2) for value in item.rect] if item.page >= 0 else None,
                "confianza": round(float(item.confidence), 4),
                "hash": pseudonymizer.token("report-value", item.original, 64),
            }
            for item in result.detections
        ]
        files.append({
            "id_archivo": pseudonymizer.token("report-file", result.source, 32),
            "exitoso": result.success,
            "perfil": result.profile,
            "paginas": result.pages,
            "redacciones": result.redactions,
            "detecciones": detected,
            "advertencias": result.warnings,
            "error": result.error,
            "codigo_error": result.extra.get("codigo_error"),
            "puntajes_familia": result.extra.get("puntajes_familia", {}),
            "capacidades": result.extra.get("capacidades", []),
            "entrada_cifrada": result.extra.get("entrada_cifrada"),
            "fallback_temporal": result.extra.get("fallback_temporal", {}),
            "verificacion": result.extra.get("verificacion", {}),
        })
    return {
        "version": 2,
        "modo": "dry-run" if dry_run else "anonimizacion",
        "archivos": files,
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_vector_logo_regions(path: Path | None) -> dict[str, list[dict[str, Any]]] | None:
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnonymizationError("No se pudo leer la configuracion de regiones vectoriales.") from exc
    if not isinstance(payload, dict):
        raise AnonymizationError("La configuracion de regiones vectoriales debe ser un objeto JSON.")
    return payload

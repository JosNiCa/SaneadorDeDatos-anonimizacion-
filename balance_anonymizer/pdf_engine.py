"""Motor PyMuPDF: validacion, redaccion fisica, saneamiento y verificacion."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import fitz

from .detection import PageLayout, detect_document, layout_from_page
from .models import Category, Detection, FileResult, ProfileResult
from .pseudonyms import Pseudonymizer, normalize


class AnonymizationError(RuntimeError):
    """Error seguro: el PDF no se marca como anonimizado."""


def list_input_pdfs(input_path: Path) -> list[Path]:
    """Acepta un PDF o los PDF no recursivos de un directorio."""

    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise AnonymizationError("La entrada individual debe ser un PDF.")
        return [input_path]
    if not input_path.is_dir():
        raise AnonymizationError("La ruta de entrada no existe o no es accesible.")
    return sorted((path for path in input_path.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"), key=lambda path: path.name.casefold())


def output_path_for(source: Path, output_dir: Path, pseudonymizer: Pseudonymizer) -> Path:
    """No replica el nombre fuente, que podria contener informacion identificable."""

    return output_dir / f"anonimizado_{pseudonymizer.token('output-file', str(source.resolve()), 16)}.pdf"


def _validate_digital_document(document: fitz.Document) -> list[PageLayout]:
    if document.needs_pass:
        raise AnonymizationError("El PDF protegido con contrasena no se procesa en modo estricto.")
    if document.page_count < 1:
        raise AnonymizationError("El PDF no contiene paginas.")
    layouts: list[PageLayout] = []
    for index, page in enumerate(document):
        raw_words = page.get_text("words", sort=True)
        if not raw_words:
            raise AnonymizationError("No hay texto digital extraible con coordenadas confiables.")
        valid_count = 0
        for word in raw_words:
            coordinates = tuple(float(value) for value in word[:4])
            if (
                all(math.isfinite(value) for value in coordinates)
                and coordinates[0] >= -0.1
                and coordinates[1] >= -0.1
                and coordinates[2] > coordinates[0]
                and coordinates[3] > coordinates[1]
                and coordinates[2] <= page.rect.width + 0.1
                and coordinates[3] <= page.rect.height + 0.1
            ):
                valid_count += 1
        if valid_count != len(raw_words):
            raise AnonymizationError("Se detectaron coordenadas de texto no confiables.")
        layouts.append(layout_from_page(index, page))
    return layouts


def _expand_rect(rect: tuple[float, float, float, float], page: fitz.Page) -> fitz.Rect:
    margin_x, margin_y = 0.65, 0.35
    return fitz.Rect(
        max(page.rect.x0, rect[0] - margin_x),
        max(page.rect.y0, rect[1] - margin_y),
        min(page.rect.x1, rect[2] + margin_x),
        min(page.rect.y1, rect[3] + margin_y),
    )


def _font_size_to_fit(rect: fitz.Rect, replacement: str) -> float:
    """Reduce la tipografia hasta caber en la caja original sin invadir columnas."""

    if not replacement:
        return 0.0
    # insert_textbox necesita espacio adicional para ascenso y descenso de la fuente.
    maximum = max(4.5, min(rect.height * 0.60, 11.0))
    size = maximum
    while size >= 4.5:
        if fitz.get_text_length(replacement, fontname="helv", fontsize=size) <= max(1.0, rect.width - 0.3):
            return size
        size -= 0.25
    return 4.5


def _add_redactions(document: fitz.Document, detections: Iterable[Detection]) -> None:
    by_page: dict[int, list[Detection]] = {}
    for item in detections:
        by_page.setdefault(item.page, []).append(item)
    for page_number, items in by_page.items():
        page = document[page_number]
        for item in items:
            page.add_redact_annot(_expand_rect(item.rect, page), fill=(1, 1, 1), cross_out=False)
        image_option = getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 2)
        page.apply_redactions(images=image_option)
        # Se inserta despues de aplicar la redaccion: el texto fuente ya no existe en el stream.
        for item in items:
            if item.redact_only or not item.replacement:
                continue
            rect = _expand_rect(item.rect, page)
            fontsize = _font_size_to_fit(rect, item.replacement)
            remaining = page.insert_textbox(
                rect,
                item.replacement,
                fontname="helv",
                fontsize=fontsize,
                color=(0, 0, 0),
                align=0,
                overlay=True,
            )
            if remaining < -0.1:
                raise AnonymizationError("No se pudo insertar el seudonimo sin invadir contenido cercano.")


def _remove_raster_images(document: fitz.Document) -> int:
    removed: set[int] = set()
    for page in document:
        for image in page.get_images(full=True):
            xref = int(image[0])
            try:
                page.delete_image(xref)
            except Exception as exc:  # PyMuPDF puede no poder borrar una referencia malformada.
                raise AnonymizationError("No fue posible eliminar una imagen rasterizada.") from exc
            removed.add(xref)
    return len(removed)


def _clear_metadata(document: fitz.Document) -> None:
    metadata = {
        "title": "Documento anonimizado",
        "author": "",
        "subject": "",
        "keywords": "",
        "creator": "BalanzaPrivada",
        "producer": "BalanzaPrivada",
        "creationDate": "",
        "modDate": "",
        "trapped": "",
    }
    document.set_metadata(metadata)
    if document.get_xml_metadata():
        document.del_xml_metadata()
    for name in tuple(document.embfile_names()):
        document.embfile_del(name)


def _apply_vector_logo_regions(
    document: fitz.Document,
    profile: str,
    vector_regions: dict[str, list[dict[str, Any]]] | None,
) -> int:
    """Redacta solo regiones configuradas explicitamente; nunca infiere graficos de tablas."""

    if not vector_regions:
        return 0
    regions = vector_regions.get(profile, [])
    count = 0
    for region in regions:
        try:
            page_number = int(region["page"])
            coordinates = region["rect"]
            if not isinstance(coordinates, list) or len(coordinates) != 4:
                raise ValueError
            page = document[page_number]
            page.add_redact_annot(fitz.Rect(*(float(value) for value in coordinates)), fill=(1, 1, 1), cross_out=False)
            page.apply_redactions(images=getattr(fitz, "PDF_REDACT_IMAGE_REMOVE", 2))
            count += 1
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise AnonymizationError("La configuracion de regiones vectoriales es invalida.") from exc
    return count


def _content_streams(document: fitz.Document) -> str:
    chunks: list[str] = []
    for page in document:
        for xref in page.get_contents():
            try:
                chunks.append(document.xref_stream(xref).decode("latin-1", errors="ignore"))
            except Exception:
                # Un stream imposible de inspeccionar no permite una verificacion estricta.
                raise AnonymizationError("No se pudo inspeccionar un stream de contenido.")
    return "\n".join(chunks)


def _contains_value(haystack: str, value: str) -> bool:
    """Busca un valor completo, sin confundir periodos breves con parte de otro numero."""

    normalized_value = normalize(value)
    if not normalized_value:
        return False
    boundary = r"[A-Z0-9Ñ]"
    return re.search(rf"(?<!{boundary}){re.escape(normalized_value)}(?!{boundary})", normalize(haystack)) is not None


def _verify_output(
    output: Path,
    expected_pages: int,
    expected_dimensions: list[tuple[float, float]],
    detections: list[Detection],
) -> None:
    with fitz.open(output) as document:
        if document.page_count != expected_pages:
            raise AnonymizationError("La cantidad de paginas cambio durante la redaccion.")
        for page, expected in zip(document, expected_dimensions):
            if abs(page.rect.width - expected[0]) > 0.01 or abs(page.rect.height - expected[1]) > 0.01:
                raise AnonymizationError("Las dimensiones de una pagina cambiaron durante la redaccion.")
            # Renderizado en memoria: valida que cada pagina siga siendo renderizable.
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
            if pixmap.width < 1 or pixmap.height < 1:
                raise AnonymizationError("No se pudo renderizar una pagina anonimizada.")
        extracted = "\n".join(page.get_text("text") for page in document)
        streams = _content_streams(document)
        for item in detections:
            if item.category == Category.RASTER_IMAGE:
                continue
            if _contains_value(extracted, item.original) or _contains_value(streams, item.original):
                raise AnonymizationError("La verificacion encontro un valor sensible residual.")
        metadata_values = " ".join(value or "" for value in document.metadata.values())
        if any(_contains_value(metadata_values, item.original) for item in detections if item.original):
            raise AnonymizationError("La verificacion encontro PII en los metadatos.")
        if document.get_xml_metadata() or tuple(document.embfile_names()):
            raise AnonymizationError("Persisten metadatos XMP o archivos adjuntos.")


def anonymize_file(
    source: Path,
    output_dir: Path,
    pseudonymizer: Pseudonymizer,
    *,
    strict: bool = True,
    dry_run: bool = False,
    vector_regions: dict[str, list[dict[str, Any]]] | None = None,
) -> FileResult:
    """Anonimiza un PDF de manera transaccional: o se verifica, o no se entrega."""

    source = source.resolve()
    target = output_path_for(source, output_dir.resolve(), pseudonymizer)
    if target.resolve() == source:
        raise AnonymizationError("La salida no puede sobrescribir el PDF fuente.")
    if target.exists() and not dry_run:
        raise AnonymizationError("Ya existe una salida anonimizada para este archivo; no se sobrescribe.")
    with fitz.open(source) as document:
        layouts = _validate_digital_document(document)
        profile_result = detect_document(layouts, pseudonymizer)
        if profile_result is None:
            raise AnonymizationError("No se reconocio un perfil con confianza suficiente.")
        if strict and profile_result.warnings:
            raise AnonymizationError("El perfil detectado tiene advertencias y se rechazo en modo estricto.")
        detections = profile_result.detections
        unresolved = [item for item in detections if not item.redact_only and not item.replacement]
        if unresolved:
            raise AnonymizationError("No se pudo crear un seudonimo para un dato obligatorio.")
        dimensions = [(page.rect.width, page.rect.height) for page in document]
        if dry_run:
            return FileResult(
                str(source),
                None,
                True,
                profile_result.name,
                document.page_count,
                dict(Counter(item.category.value for item in detections)),
                detections,
                profile_result.warnings,
                page_dimensions=dimensions,
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        # Archivo temporal en el mismo directorio para que os.replace sea atomico.
        descriptor, temporary_name = tempfile.mkstemp(prefix=".anon_", suffix=".pdf", dir=output_dir)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            raster_count = _remove_raster_images(document)
            _add_redactions(document, detections)
            vector_count = _apply_vector_logo_regions(document, profile_result.name, vector_regions)
            _clear_metadata(document)
            document.save(temporary, garbage=4, deflate=True, clean=True)
            _verify_output(temporary, document.page_count, dimensions, detections)
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    redactions = Counter(item.category.value for item in detections)
    if raster_count:
        redactions[Category.RASTER_IMAGE.value] += raster_count
    if vector_count:
        redactions["logo_vectorial_configurado"] += vector_count
    return FileResult(
        str(source),
        str(target),
        True,
        profile_result.name,
        len(dimensions),
        dict(redactions),
        detections,
        profile_result.warnings,
        page_dimensions=dimensions,
    )


def safe_file_result(
    source: Path,
    output_dir: Path,
    pseudonymizer: Pseudonymizer,
    **kwargs: Any,
) -> FileResult:
    """Convierte errores operativos a un resultado sin revelar el nombre o texto fuente."""

    try:
        return anonymize_file(source, output_dir, pseudonymizer, **kwargs)
    except (fitz.FileDataError, fitz.EmptyFileError, OSError, ValueError, AnonymizationError) as exc:
        message = str(exc) if isinstance(exc, AnonymizationError) else "No se pudo procesar el PDF de forma segura."
        return FileResult(str(source), None, False, None, error=message)


def report_payload(results: list[FileResult], pseudonymizer: Pseudonymizer, *, dry_run: bool) -> dict[str, Any]:
    """Reporte tecnico con HMAC de los valores; nunca contiene PII en texto plano."""

    files: list[dict[str, Any]] = []
    for result in results:
        detected = [
            {
                "categoria": item.category.value,
                "pagina": item.page + 1,
                "coordenadas": [round(value, 2) for value in item.rect],
                "hash": pseudonymizer.token("report-value", item.original, 64),
            }
            for item in result.detections
        ]
        files.append(
            {
                "id_archivo": pseudonymizer.token("report-file", result.source, 32),
                "exitoso": result.success,
                "perfil": result.profile,
                "paginas": result.pages,
                "redacciones": result.redactions,
                "detecciones": detected,
                "advertencias": result.warnings,
                "error": result.error,
            }
        )
    return {"version": 1, "modo": "dry-run" if dry_run else "anonimizacion", "archivos": files}


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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

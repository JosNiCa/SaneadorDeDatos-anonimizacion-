from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import fitz
import pytest

from anonymize_balances import main
from balance_anonymizer.pdf_engine import anonymize_file, report_payload, safe_file_result
from balance_anonymizer.pseudonyms import Pseudonymizer, normalize
from .conftest import (
    CONTALINK_OWNER,
    CONTALINK_RFC,
    CONTPAQ_OWNER,
    LOGO_RECT,
    ORDINARY_IMAGE_RECT,
    WEB_OWNER_LINES,
    WEB_RFC,
    AccountingRow,
    make_classic_balance,
    make_contalink_balance,
    make_contpaq_balance,
    make_textless_pdf,
    make_unknown_balance,
    make_web_balance,
)


def _page_texts(path: Path) -> list[str]:
    with fitz.open(path) as document:
        return [page.get_text("text") for page in document]


def _text(path: Path) -> str:
    return "\n".join(_page_texts(path))


def _output_path(result: object) -> Path:
    assert getattr(result, "success"), getattr(result, "error", None)
    output = getattr(result, "output")
    assert output
    return Path(output)


def _geometry(path: Path) -> list[tuple[tuple[float, ...], tuple[float, ...], int]]:
    with fitz.open(path) as document:
        return [
            (tuple(page.mediabox), tuple(page.cropbox), page.rotation)
            for page in document
        ]


def _detections(result: object, category: str) -> list[object]:
    return [
        detection
        for detection in getattr(result, "detections")
        if detection.category.value == category
    ]


def _average_rgb(path: Path, page_number: int, rect: fitz.Rect) -> tuple[float, float, float]:
    with fitz.open(path) as document:
        pixmap = document[page_number].get_pixmap(
            matrix=fitz.Matrix(2, 2), clip=rect, alpha=False, colorspace=fitz.csRGB
        )
    channels = 3
    pixels = pixmap.samples
    count = max(1, len(pixels) // channels)
    return tuple(
        sum(pixels[index::channels]) / count
        for index in range(channels)
    )


def _blue_drawing_count(path: Path) -> int:
    with fitz.open(path) as document:
        return sum(
            1
            for page in document
            for drawing in page.get_drawings()
            if drawing.get("color")
            and drawing["color"][2] > 0.9
            and drawing["color"][0] < 0.1
            and drawing["color"][1] < 0.1
        )


def _build_family(path: Path, family: str) -> None:
    builders: dict[str, Callable[[Path], None]] = {
        "WEB_BALANCE": lambda target: make_web_balance(target, pages=2),
        "CLASSIC_BALANCE": lambda target: make_classic_balance(target, pages=2),
        "CONTALINK_BALANCE": lambda target: make_contalink_balance(target, pages=2),
        "CONTPAQ_BALANCE": lambda target: make_contpaq_balance(target),
    }
    builders[family](path)


@pytest.mark.parametrize(
    "expected_family",
    ("WEB_BALANCE", "CLASSIC_BALANCE", "CONTALINK_BALANCE", "CONTPAQ_BALANCE"),
)
def test_scores_the_four_visual_families_without_using_filename(
    tmp_path: Path, seed: str, expected_family: str
) -> None:
    source = tmp_path / "documento_neutro.pdf"
    _build_family(source, expected_family)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed), dry_run=True)

    assert result.success
    assert result.profile == expected_family
    assert result.pages == (1 if expected_family == "CONTPAQ_BALANCE" else 2)


def test_web_multiline_owner_and_first_page_header_only(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "web.pdf"
    rows = (
        (AccountingRow("101.01", "CAJA GENERAL"),),
        (AccountingRow("102.03", "CONTINUACION ORDINARIA", "5,000.00", "20.00", "5.00", "5,015.00"),),
    )
    make_web_balance(
        source,
        rows_by_page=rows,
        metadata={
            "title": " ".join(WEB_OWNER_LINES),
            "author": "AUTOR SINTETICO IDENTIFICABLE",
            "subject": WEB_RFC,
        },
    )

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)
    pages = _page_texts(output)

    assert result.success and result.profile == "WEB_BALANCE"
    assert all(line not in "\n".join(pages) for line in WEB_OWNER_LINES)
    assert WEB_RFC not in "\n".join(pages)
    assert "102.03" in pages[1]
    assert "CONTINUACION ORDINARIA" in pages[1]
    assert "5,000.00" in pages[1] and "5,015.00" in pages[1]
    owner_detections = _detections(result, "razon_social")
    assert owner_detections and {item.page for item in owner_detections} == {0}
    for replacement in {item.replacement for item in owner_detections if item.replacement}:
        assert replacement in pages[0]
        assert replacement not in pages[1]
    assert _geometry(source) == _geometry(output)
    with fitz.open(output) as document:
        metadata = " ".join(value or "" for value in document.metadata.values())
    assert "AUTOR SINTETICO IDENTIFICABLE" not in metadata
    assert WEB_RFC not in metadata


def test_web_owner_uses_full_free_band_and_preserves_font_family(
    tmp_path: Path, seed: str
) -> None:
    source = tmp_path / "web_propietario_largo.pdf"
    owner_lines = (
        "SOCIEDAD DEMOSTRACION COLAORIGINALUNICA",
        "DIVISION CONTABLE DE PRUEBA",
    )
    make_web_balance(
        source,
        pages=1,
        owner_lines=owner_lines,
        owner_font="tiro",
        owner_size=13.8,
        detached_creation=True,
    )
    with fitz.open(source) as document:
        source_owner_size = next(
            span["size"]
            for block in document[0].get_text("dict", sort=True).get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if "COLAORIGINALUNICA" in span.get("text", "")
        )

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)
    text = "\n".join(_page_texts(output))
    owner = next(
        item for item in _detections(result, "razon_social")
        if not item.redact_only and item.replacement
    )

    assert result.success and result.profile == "WEB_BALANCE"
    assert "COLAORIGINALUNICA" not in text
    assert all(line not in text for line in owner_lines)
    assert owner.replacement in text

    with fitz.open(output) as document:
        page = document[0]
        label_rect = fitz.Rect(page.search_for("Nombre/Razón Social:")[0])
        replacement_rects = page.search_for(owner.replacement)
        assert replacement_rects
        assert all(not label_rect.intersects(rect) for rect in replacement_rects)
        owner_band_words = [
            word[4]
            for word in page.get_text("words", sort=True)
            if 30.0 <= (word[1] + word[3]) / 2.0 <= 55.0
        ]
        assert normalize(" ".join(owner_band_words)) == normalize(
            f"Nombre/Razón Social: {owner.replacement}"
        )
        replacement_fonts = {
            span["font"]
            for block in page.get_text("dict", sort=True).get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if owner.replacement.split()[0] in span.get("text", "")
        }
        replacement_sizes = {
            span["size"]
            for block in page.get_text("dict", sort=True).get("blocks", [])
            if block.get("type") == 0
            for line in block.get("lines", [])
            for span in line.get("spans", [])
            if owner.replacement.split()[0] in span.get("text", "")
        }
    assert replacement_fonts
    assert all("Times" in font for font in replacement_fonts)
    assert replacement_sizes
    assert all(size == pytest.approx(source_owner_size, abs=0.05) for size in replacement_sizes)


def test_classic_repeated_headers_keep_empty_fields_empty_and_consistent(
    tmp_path: Path, seed: str
) -> None:
    source = tmp_path / "classic_empty.pdf"
    make_classic_balance(
        source,
        pages=2,
        repeat_header=True,
        empty_fields=True,
        fragmented_reg_fed=True,
    )

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    pages = _page_texts(_output_path(result))

    assert result.success and result.profile == "CLASSIC_BALANCE"
    owners = _detections(result, "razon_social")
    rfcs = _detections(result, "rfc")
    assert {item.page for item in owners} == {0, 1}
    assert {item.page for item in rfcs} == {0, 1}
    assert len({item.replacement for item in owners}) == 1
    assert len({item.replacement for item in rfcs}) == 1
    assert not _detections(result, "direccion")
    assert not _detections(result, "poblacion")
    assert not _detections(result, "cedula")
    for page_text in pages:
        normalized_lines = {normalize(line).rstrip(":") for line in page_text.splitlines()}
        assert "DIRECCION" in normalized_lines
        assert "POBLACION" in normalized_lines
        assert "CEDULA" in normalized_lines


def test_fragmented_reg_fed_anchor_is_normalized(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "fragmented.pdf"
    make_classic_balance(source, pages=1, fragmented_reg_fed=True)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed), dry_run=True)

    assert result.success and result.profile == "CLASSIC_BALANCE"
    assert len(_detections(result, "rfc")) == 1


def test_contalink_stacked_identity_logo_and_continuation(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "contalink.pdf"
    rows = (
        (AccountingRow("101.01", "CAJA PRIMERA PAGINA"),),
        (AccountingRow("101.02", "CONTINUA SIN CABECERA", "8,000.00", "0.00", "10.00", "7,990.00"),),
    )
    make_contalink_balance(source, pages=2, rows_by_page=rows, raster_logo=True)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)
    pages = _page_texts(output)

    assert result.success and result.profile == "CONTALINK_BALANCE"
    assert CONTALINK_OWNER not in "\n".join(pages)
    assert CONTALINK_RFC not in "\n".join(pages)
    assert "CONTINUA SIN CABECERA" in pages[1]
    assert "BALANZA DE COMPROBACION" not in normalize(pages[1])
    assert {item.page for item in _detections(result, "razon_social")} == {0}
    assert result.redactions.get("logo_rasterizado") == 1


def test_rfc_entity_associations_support_lengths_orders_and_flat_gluing(
    tmp_path: Path, seed: str
) -> None:
    sensitive = (
        ("DEM210402AB1", "PERSONA MORAL UNO"),
        ("DEMO210402AB1", "PERSONA FISICA DOS"),
        ("DEMOS210402AB1", "ENTIDAD ANOMALA TRES"),
        ("ABC210402XY9", "PERSONA PEGADA CUATRO"),
    )
    rows = ((
        AccountingRow("401.01", f"{sensitive[0][0]} {sensitive[0][1]}"),
        AccountingRow("401.02", f"{sensitive[1][1]} {sensitive[1][0]}"),
        AccountingRow("401.03", f"{sensitive[2][0]} {sensitive[2][1]}"),
        AccountingRow("401.04", f"{sensitive[3][0]}{sensitive[3][1]}"),
    ),)
    source = tmp_path / "rfc_associations.pdf"
    make_classic_balance(source, pages=1, rows_by_page=rows)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))

    assert result.success and result.profile == "CLASSIC_BALANCE"
    assert len(_detections(result, "rfc_entidad_descripcion")) == 4
    for rfc, entity in sensitive:
        assert rfc not in text
        assert entity not in text
    for code in ("401.01", "401.02", "401.03", "401.04"):
        assert code in text
    assert text.count("1,000.00") == 4
    assert text.count("1,150.00") == 4


def test_bank_associations_support_four_digits_separators_and_both_orders(
    tmp_path: Path, seed: str
) -> None:
    rows = ((
        AccountingRow("102.01", "BANCO DEMOSTRACION ALFA 1234"),
        AccountingRow("102.02", "41-78629-201-4 BANCO DEMOSTRACION BETA"),
    ),)
    source = tmp_path / "bank_associations.pdf"
    make_classic_balance(source, pages=1, rows_by_page=rows)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))

    assert result.success
    assert len(_detections(result, "banco_identificador_descripcion")) == 2
    for original in (
        "BANCO DEMOSTRACION ALFA",
        "1234",
        "41-78629-201-4",
        "BANCO DEMOSTRACION BETA",
    ):
        assert original not in text
    assert re.search(r"\d{2}-\d{5}-\d{3}-\d", text)
    assert "102.01" in text and "102.02" in text
    assert text.count("1,000.00") == 2


def test_nonbank_person_and_company_with_numeric_identifier_are_paired(
    tmp_path: Path, seed: str
) -> None:
    rows = ((
        AccountingRow("205.01", "PERSONA DEMOSTRACION TRES 543210"),
        AccountingRow("205.02", "ENTIDAD DEMOSTRACION SA DE CV 987-654"),
    ),)
    source = tmp_path / "entity_numeric_associations.pdf"
    make_classic_balance(source, pages=1, rows_by_page=rows)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))
    pairs = _detections(result, "rfc_entidad_descripcion")

    assert result.success and len(pairs) == 2
    assert "PERSONA DEMOSTRACION TRES" not in text
    assert "ENTIDAD DEMOSTRACION SA DE CV" not in text
    assert "543210" not in text and "987-654" not in text
    for item in pairs:
        original_identifier = next(value for value in item.residuals if any(char.isdigit() for char in value))
        replacement_identifier = re.findall(r"\d[\d-]*\d", item.replacement or "")[-1]
        assert re.sub(r"\d", "0", replacement_identifier) == re.sub(r"\d", "0", original_identifier)
        assert all(
            source_digit != replacement_digit
            for source_digit, replacement_digit in zip(
                re.sub(r"\D", "", original_identifier),
                re.sub(r"\D", "", replacement_identifier),
            )
        )
    assert "205.01" in text and "205.02" in text


def test_same_name_with_distinct_rfcs_stays_distinct_and_warns_only_with_hmacs(
    tmp_path: Path, seed: str
) -> None:
    repeated_name = "ENTIDAD REPETIDA SA DE CV"
    rfcs = ("AAA210402AB1", "AAB210402AB2")
    rows = ((
        AccountingRow("401.11", f"{rfcs[0]} {repeated_name}"),
        AccountingRow("401.12", f"{rfcs[1]} {repeated_name}"),
    ),)
    source = tmp_path / "ambiguous_name.pdf"
    make_classic_balance(source, pages=1, rows_by_page=rows)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    warnings = "\n".join(result.warnings)
    pairs = _detections(result, "rfc_entidad_descripcion")

    assert result.success and len(pairs) == 2
    assert len({item.replacement for item in pairs}) == 2
    assert "nombre_asociado_a_multiples_rfc" in warnings
    assert repeated_name not in warnings
    assert all(rfc not in warnings for rfc in rfcs)


def test_hmac_pseudonyms_are_stable_distinct_and_never_keep_bank_digits(seed: str) -> None:
    first = Pseudonymizer(seed)
    second = Pseudonymizer(seed)

    assert first.company("ENTITY-A") == second.company("ENTITY-A")
    assert first.company("ENTITY-A") != first.company("ENTITY-B")
    assert first.rfc("AAA210402AB1", "AAA210402AB1") == second.rfc(
        "AAA210402AB1", "AAA210402AB1"
    )
    bank, identifier = first.bank_and_identifier(
        "BANCO DEMOSTRACION", "41-78629-201-4", "ASOCIACION-UNO"
    )
    assert "FICTICIO" in bank
    assert re.sub(r"\d", "0", identifier) == "00-00000-000-0"
    assert all(
        source_digit != replacement_digit
        for source_digit, replacement_digit in zip(
            "41786292014", re.sub(r"\D", "", identifier)
        )
    )


def test_unassociated_entities_and_accounting_numbers_remain_unchanged(
    tmp_path: Path, seed: str
) -> None:
    descriptions = (
        "PROVEEDOR UNICO SA DE CV",
        "BANCO DEMOSTRACION SOLO",
        "SERVICIO CORRESPONDIENTE A 2026",
        "COMISION DEL 16%",
        "IMPORTE DE REFERENCIA 12,345.67",
    )
    rows = (tuple(
        AccountingRow(f"51{index}-2026-001", description)
        for index, description in enumerate(descriptions, start=1)
    ),)
    source = tmp_path / "negative_cases.pdf"
    make_classic_balance(source, pages=1, rows_by_page=rows)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))

    assert result.success
    assert not _detections(result, "rfc_entidad_descripcion")
    assert not _detections(result, "banco_identificador_descripcion")
    for description in descriptions:
        assert description in text
    for index in range(1, 6):
        assert f"51{index}-2026-001" in text
    assert "16%" in text and "12,345.67" in text


def test_only_confirmed_logo_placements_are_removed(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "images.pdf"
    make_classic_balance(
        source,
        pages=3,
        repeat_header=True,
        raster_logo=True,
        ordinary_image=True,
    )
    logo_before = _average_rgb(source, 0, LOGO_RECT)
    ordinary_before = _average_rgb(source, 0, ORDINARY_IMAGE_RECT)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)
    logo_after = _average_rgb(output, 0, LOGO_RECT)
    ordinary_after = _average_rgb(output, 0, ORDINARY_IMAGE_RECT)

    assert result.success
    assert result.redactions.get("logo_rasterizado") == 3
    assert logo_before[0] > 180 and logo_before[1] < 120
    assert logo_after[0] > 225 and logo_after[1] > 225 and logo_after[2] > 225
    assert all(abs(before - after) < 8 for before, after in zip(ordinary_before, ordinary_after))


def test_contpaq_text_logo_shares_line_without_erasing_owner_sheet_or_vectors(
    tmp_path: Path, seed: str
) -> None:
    rows = (
        AccountingRow("102.01", "BANCO DEMOSTRACION UNO 1234"),
        AccountingRow("102.02", "5678 BANCO DEMOSTRACION DOS"),
    )
    source = tmp_path / "contpaq.pdf"
    make_contpaq_balance(source, rows=rows, inline_vector=True)
    blue_before = _blue_drawing_count(source)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)
    text = _text(output)

    assert result.success and result.profile == "CONTPAQ_BALANCE"
    assert "CONTPAQ" not in text.upper()
    assert CONTPAQ_OWNER not in text
    assert "Hoja 1" in text
    assert "Tipo Moneda: PESOS" in text
    assert len(_detections(result, "banco_identificador_descripcion")) == 2
    assert "BANCO DEMOSTRACION UNO" not in text
    assert "BANCO DEMOSTRACION DOS" not in text
    assert "1234" not in text and "5678" not in text
    assert _blue_drawing_count(output) == blue_before


def test_footer_date_is_replaced_without_erasing_nearby_accounting_row(
    tmp_path: Path, seed: str
) -> None:
    rows = ((
        AccountingRow("799.01", "AJUSTE ORDINARIO DE CIERRE", "9,000.00", "100.00", "25.00", "9,075.00"),
        AccountingRow("799.02", "RESULTADO ORDINARIO", "7,000.00", "10.00", "5.00", "7,005.00"),
    ),)
    source = tmp_path / "footer.pdf"
    make_classic_balance(
        source,
        pages=1,
        rows_by_page=rows,
        footer=True,
        near_footer=True,
    )

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))

    assert result.success
    assert _detections(result, "fecha_pie")
    assert "24/08/2026 16:01:02" not in text
    for value in (
        "799.01",
        "AJUSTE ORDINARIO DE CIERRE",
        "9,000.00",
        "9,075.00",
        "799.02",
        "RESULTADO ORDINARIO",
        "7,005.00",
        "Página 1",
    ):
        assert value in text


def test_impossible_header_date_uses_format_preserving_fallback(
    tmp_path: Path, seed: str
) -> None:
    source = tmp_path / "impossible_date.pdf"
    make_classic_balance(source, pages=1, impossible_date=True)

    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    text = _text(_output_path(result))

    assert result.success
    assert "31/febrero/25" not in text
    assert any("fallback" in warning.lower() for warning in result.warnings)
    assert all("31/febrero/25" not in warning for warning in result.warnings)


def test_temporal_fallback_count_is_scoped_to_each_file(tmp_path: Path, seed: str) -> None:
    impossible = tmp_path / "impossible.pdf"
    ordinary = tmp_path / "ordinary.pdf"
    make_classic_balance(impossible, pages=1, impossible_date=True)
    make_classic_balance(ordinary, pages=1)
    pseudonymizer = Pseudonymizer(seed)

    first = anonymize_file(impossible, tmp_path / "out", pseudonymizer)
    second = anonymize_file(ordinary, tmp_path / "out", pseudonymizer)

    assert first.extra["fallback_temporal"]["temporal_fallback_count"] == 1
    assert first.extra["fallback_temporal"]["temporal_fallback_used"] is True
    assert second.extra["fallback_temporal"] == {
        "temporal_fallback_used": False,
        "temporal_fallback_count": 0,
    }


def test_unknown_visual_family_fails_in_strict_mode(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "unknown.pdf"
    make_unknown_balance(source)

    result = safe_file_result(source, tmp_path / "out", Pseudonymizer(seed))

    assert not result.success
    assert result.output is None
    if (tmp_path / "out").exists():
        assert not list((tmp_path / "out").glob("*.pdf"))


def test_pdf_without_extractable_text_fails_safely(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "textless.pdf"
    make_textless_pdf(source)

    result = safe_file_result(source, tmp_path / "out", Pseudonymizer(seed))

    assert not result.success
    assert result.output is None
    if (tmp_path / "out").exists():
        assert not list((tmp_path / "out").glob("*.pdf"))


def test_extractable_empty_password_encryption_is_detected_and_not_inherited(
    tmp_path: Path, seed: str
) -> None:
    plain = tmp_path / "plain.pdf"
    encrypted = tmp_path / "encrypted.pdf"
    make_classic_balance(plain, pages=1)
    with fitz.open(plain) as document:
        document.save(
            encrypted,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="synthetic-owner-password",
            user_pw="",
            permissions=fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT,
        )

    result = anonymize_file(encrypted, tmp_path / "out", Pseudonymizer(seed))
    output = _output_path(result)

    assert result.success and result.extra["entrada_cifrada"] is True
    with fitz.open(output) as document:
        assert not document.needs_pass
        assert not document.is_encrypted
        assert not (document.metadata.get("encryption") or "").strip()
        assert document.xref_get_key(-1, "Encrypt")[0] == "null"


def test_report_contains_only_keyed_hashes_and_dry_run_writes_no_pdf(
    tmp_path: Path, seed: str
) -> None:
    source = tmp_path / "report.pdf"
    make_web_balance(source, pages=1)
    pseudonymizer = Pseudonymizer(seed)

    result = anonymize_file(source, tmp_path / "out", pseudonymizer, dry_run=True)
    serialized = json.dumps(report_payload([result], pseudonymizer, dry_run=True))

    assert result.success and result.output is None
    assert not (tmp_path / "out").exists()
    for original in (*WEB_OWNER_LINES, WEB_RFC):
        assert original not in serialized
    hashes = re.findall(r'"hash":\s*"([A-F0-9]{64})"', serialized)
    assert hashes


def test_invalid_pdf_has_safe_error_without_source_filename(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "nombre_privado_no_debe_aparecer.pdf"
    source.write_bytes(b"contenido sintetico que no es PDF")
    pseudonymizer = Pseudonymizer(seed)

    result = safe_file_result(source, tmp_path / "out", pseudonymizer)
    report = report_payload([result], pseudonymizer, dry_run=False)

    assert not result.success and result.output is None
    assert "nombre_privado_no_debe_aparecer.pdf" not in json.dumps(report)
    assert report["archivos"][0]["codigo_error"] == "pdf_entrada_invalido"


def test_cli_preserves_public_batch_interface(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "input.pdf"
    output = tmp_path / "salida"
    report = output / "reporte.json"
    make_web_balance(source, pages=1)

    code = main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--seed",
            seed,
            "--report",
            str(report),
        ]
    )

    assert code == 0
    assert len(list(output.glob("anonimizado_*.pdf"))) == 1
    payload = report.read_text(encoding="utf-8")
    assert WEB_RFC not in payload
    assert '"hash"' in payload

from __future__ import annotations

import json
from pathlib import Path

import fitz

from anonymize_balances import main
from balance_anonymizer.models import Category
from balance_anonymizer.pdf_engine import anonymize_file, report_payload, safe_file_result
from balance_anonymizer.pseudonyms import Pseudonymizer
from .conftest import make_pdf


def _text(path: Path) -> str:
    with fitz.open(path) as document:
        return "\n".join(page.get_text() for page in document)


def _dimensions(path: Path) -> list[tuple[float, float]]:
    with fitz.open(path) as document:
        return [(page.rect.width, page.rect.height) for page in document]


def test_type_1_redacts_and_preserves_accounting_content(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "tipo1.pdf"
    make_pdf(
        source,
        [
            "Fecha creacion: 2026-08-24 15:37:33",
            "Ejercicio: 2026 - Periodo: 02",
            "Nombre/Razon Social: ALFA ENTIDAD DE PRUEBA SA DE CV",
            "RFC: ALF210402AB1",
            "101.01 BANCOS                       5,000.00       70,000.00",
            "Pago proveedor PAPELERIA CENTRAL SA DE CV",
        ],
        pages=2,
        metadata={
            "title": "ALFA ENTIDAD DE PRUEBA SA DE CV",
            "author": "AUTOR DE PRUEBA",
            "subject": "ASUNTO SINTETICO",
            "keywords": "CLAVE SINTETICA",
        },
    )
    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    assert result.success
    assert result.profile == "tipo_1"
    assert result.output is not None
    output = Path(result.output)
    text = _text(output)
    for original in ("ALFA ENTIDAD DE PRUEBA SA DE CV", "ALF210402AB1", "2026-08-24 15:37:33"):
        assert original not in text
    assert "101.01" in text
    assert "5,000.00" in text
    assert "70,000.00" in text
    assert "PAPELERIA CENTRAL SA DE CV" in text
    assert _dimensions(source) == _dimensions(output)
    with fitz.open(output) as document:
        assert document.page_count == 2
        assert document.metadata["title"] == "Documento anonimizado"
        metadata = " ".join(value or "" for value in document.metadata.values())
        for original in ("ALFA", "AUTOR DE PRUEBA", "ASUNTO SINTETICO", "CLAVE SINTETICA"):
            assert original not in metadata


def test_type_1_entity_pair_is_consistent_between_pages(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "consistencia.pdf"
    make_pdf(
        source,
        [
            "Fecha creacion: 2026-08-24 15:37:33",
            "Ejercicio: 2026 - Periodo: 02",
            "Nombre/Razon Social: ALFA ENTIDAD DE PRUEBA SA DE CV",
            "RFC: ALF210402AB1",
        ],
        pages=2,
    )
    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    names = [item.replacement for item in result.detections if item.category == Category.COMPANY]
    rfcs = [item.replacement for item in result.detections if item.category == Category.RFC]
    assert len(set(names)) == 1
    assert len(set(rfcs)) == 1
    assert names[0] and rfcs[0]


def test_type_2_anonymizes_header_fields_and_raster_images(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "tipo2.pdf"
    make_pdf(
        source,
        [
            "CONSTRUCTORA SINTETICA DEL NORTE SA DE CV",
            "VIAL # 1725 SECTOR PRUEBA",
            "NEXORA, ZZ",
            "Reg. fed.: CNS210402IC8",
            "Cedula: CED-9988-XY",
            "Balanza de comprobacion al 11/febrero/25",
            "101.01 BANCOS                       5,000.00",
        ],
        raster_logo=True,
    )
    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    assert result.success and result.profile == "tipo_2"
    assert result.redactions[Category.RASTER_IMAGE.value] == 1
    text = _text(Path(result.output or ""))
    for original in (
        "CONSTRUCTORA SINTETICA DEL NORTE SA DE CV",
        "VIAL # 1725 SECTOR PRUEBA",
        "NEXORA, ZZ",
        "CNS210402IC8",
        "CED-9988-XY",
        "11/febrero/25",
    ):
        assert original not in text
    assert "101.01" in text and "5,000.00" in text


def test_type_3_removes_text_logo_and_anonymizes_header(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "tipo3.pdf"
    make_pdf(
        source,
        [
            "CONTPAQ i",
            "FABRICA CONTABLE SINTETICA SA DE CV",
            "Balanza de comprobacion al 11/febrero/25",
            "Fecha de subida: 2026-08-24 15:37:33",
            "101.01 BANCOS                       5,000.00",
        ],
    )
    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    assert result.success and result.profile == "tipo_3"
    text = _text(Path(result.output or ""))
    assert "CONTPAQ" not in text.upper()
    assert "FABRICA CONTABLE SINTETICA SA DE CV" not in text
    assert "11/febrero/25" not in text
    assert "2026-08-24 15:37:33" not in text
    assert "101.01" in text and "5,000.00" in text


def test_type_4_only_changes_bank_account_number(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "tipo4.pdf"
    make_pdf(
        source,
        [
            "Bancotero 41-78629-201-4",
            "101.01 BANCOS                       5,000.00       70,000.00",
            "Fecha movimiento 2026-01-31",
        ],
    )
    result = anonymize_file(source, tmp_path / "out", Pseudonymizer(seed))
    assert result.success and result.profile == "tipo_4"
    text = _text(Path(result.output or ""))
    assert "41-78629-201-4" not in text
    assert "Bancotero" in text
    assert "101.01" in text
    assert "5,000.00" in text and "70,000.00" in text
    assert "2026-01-31" in text


def test_report_uses_hashes_and_dry_run_writes_no_pdf(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "seco.pdf"
    make_pdf(
        source,
        [
            "Fecha creacion: 2026-08-24 15:37:33",
            "Ejercicio: 2026 - Periodo: 02",
            "Nombre/Razon Social: ALFA ENTIDAD DE PRUEBA SA DE CV",
            "RFC: ALF210402AB1",
        ],
    )
    pseudonymizer = Pseudonymizer(seed)
    result = anonymize_file(source, tmp_path / "out", pseudonymizer, dry_run=True)
    assert result.success and result.output is None
    assert not (tmp_path / "out").exists()
    serialized = json.dumps(report_payload([result], pseudonymizer, dry_run=True))
    assert "ALFA ENTIDAD" not in serialized
    assert "ALF210402AB1" not in serialized
    assert "hash" in serialized


def test_pdf_without_extractable_text_fails_safely(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "sin_texto.pdf"
    document = fitz.open()
    document.new_page(width=595, height=842)
    document.save(source)
    document.close()
    result = safe_file_result(source, tmp_path / "out", Pseudonymizer(seed))
    assert not result.success
    assert result.output is None
    if (tmp_path / "out").exists():
        assert not list((tmp_path / "out").glob("*.pdf"))


def test_cli_creates_safe_report_and_output(tmp_path: Path, seed: str) -> None:
    source = tmp_path / "lote.pdf"
    make_pdf(source, ["Bancotero 41-78629-201-4", "101.01 BANCOS 5,000.00"])
    output = tmp_path / "salida"
    report = output / "reporte.json"
    code = main(["--input", str(source), "--output", str(output), "--seed", seed, "--report", str(report)])
    assert code == 0
    assert len(list(output.glob("anonimizado_*.pdf"))) == 1
    content = report.read_text(encoding="utf-8")
    assert "41-78629-201-4" not in content
    assert "hash" in content

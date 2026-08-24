#!/usr/bin/env python3
"""CLI local para anonimizar PDF digitales de balanzas de comprobacion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from balance_anonymizer.pdf_engine import (
    AnonymizationError,
    list_input_pdfs,
    load_vector_logo_regions,
    report_payload,
    safe_file_result,
    write_report,
)
from balance_anonymizer.pseudonyms import Pseudonymizer


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Anonimiza localmente balanzas de comprobacion PDF digitales con PyMuPDF."
    )
    parser.add_argument("--input", required=True, type=Path, help="PDF individual o directorio no recursivo de PDFs.")
    parser.add_argument("--output", required=True, type=Path, help="Directorio para PDFs anonimizados y reporte por defecto.")
    parser.add_argument("--seed", required=True, help="Semilla secreta de al menos 16 caracteres; no se guarda.")
    parser.add_argument("--report", type=Path, help="Ruta del reporte JSON seguro (por defecto, dentro de --output).")
    parser.add_argument("--dry-run", action="store_true", help="Solo detecta; no genera PDFs.")
    parser.add_argument(
        "--vector-logo-regions",
        type=Path,
        help="JSON opcional de regiones vectoriales por perfil; se redactan solo las regiones indicadas.",
    )
    strict = parser.add_mutually_exclusive_group()
    strict.add_argument("--strict", dest="strict", action="store_true", default=True, help="Falla de forma segura (predeterminado).")
    strict.add_argument("--no-strict", dest="strict", action="store_false", help="Conserva advertencias, sin relajar validaciones de seguridad.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        pseudonymizer = Pseudonymizer(args.seed)
        vector_regions = load_vector_logo_regions(args.vector_logo_regions)
        sources = list_input_pdfs(args.input)
        if not sources:
            raise AnonymizationError("No se encontraron PDFs de entrada.")
        report_path = args.report or (args.output / "reporte_tecnico.json")
        results = [
            safe_file_result(
                source,
                args.output,
                pseudonymizer,
                strict=args.strict,
                dry_run=args.dry_run,
                vector_regions=vector_regions,
            )
            for source in sources
        ]
        write_report(report_path, report_payload(results, pseudonymizer, dry_run=args.dry_run))
    except (AnonymizationError, OSError, ValueError) as exc:
        # Mensajes de operacion sin nombres de archivo ni texto extraido.
        print(f"Error seguro: {exc}", file=sys.stderr)
        return 2
    successful = sum(result.success for result in results)
    failed = len(results) - successful
    mode = "deteccion" if args.dry_run else "anonimizacion"
    print(f"{mode}: {successful} archivo(s) exitoso(s), {failed} fallido(s). Reporte tecnico generado.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

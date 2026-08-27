#!/usr/bin/env python3
"""CLI local para anonimizar lotes PDF, XLSX y XML."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from balance_anonymizer.batch import (
    BatchError,
    BatchProcessor,
    list_input_files,
    report_payload,
    write_report,
)
from balance_anonymizer.manifest import ManifestError, load_manifest, write_manifest_proposal
from balance_anonymizer.pdf_engine import AnonymizationError, load_vector_logo_regions
from balance_anonymizer.pseudonyms import Pseudonymizer
from balance_anonymizer.registry import PseudonymRegistry, RegistryError


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Anonimiza localmente balanzas PDF, XLSX y XML mediante dos pasadas."
    )
    parser.add_argument(
        "--input", required=True, type=Path,
        help="Archivo compatible o directorio no recursivo.",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Directorio de resultados; nunca se sobrescriben fuentes ni salidas.",
    )
    parser.add_argument(
        "--seed",
        help="Compatibilidad: secreto de al menos 16 caracteres; puede quedar en el historial.",
    )
    parser.add_argument("--seed-file", type=Path, help="Archivo protegido de una sola línea.")
    parser.add_argument(
        "--seed-env", default="BALANCE_ANON_SEED",
        help="Nombre de la variable de entorno que contiene la semilla.",
    )
    parser.add_argument("--manifest", type=Path, help="Manifiesto JSON o YAML de relaciones.")
    parser.add_argument(
        "--manifest-proposal",
        type=Path,
        help="Genera un borrador YAML/JSON de relaciones; úselo con --discover o --dry-run.",
    )
    parser.add_argument("--registry", type=Path, help="Registro SQLite persistente.")
    parser.add_argument("--report", type=Path, help="Reporte JSON seguro.")
    parser.add_argument("--discover", action="store_true", help="Propone relaciones sin generar documentos.")
    parser.add_argument("--dry-run", action="store_true", help="Muestra detecciones y conflictos sin generar documentos.")
    parser.add_argument("--strip-signature", action="store_true", help="Elimina atributos de firma XML de forma explícita.")
    parser.add_argument("--xsd", type=Path, help="XSD local opcional; nunca se descarga.")
    parser.add_argument(
        "--vector-logo-regions", type=Path,
        help="Regiones vectoriales PDF configuradas, compatible con la CLI anterior.",
    )
    strict = parser.add_mutually_exclusive_group()
    strict.add_argument("--strict", action="store_true", dest="strict", help="Modo estricto (predeterminado).")
    strict.add_argument("--no-strict", action="store_false", dest="strict", help=argparse.SUPPRESS)
    parser.set_defaults(strict=True)
    args = parser.parse_args(argv)
    if args.discover and args.dry_run:
        parser.error("Use --discover o --dry-run, no ambos.")
    if args.manifest_proposal and not (args.discover or args.dry_run):
        parser.error("--manifest-proposal requiere --discover o --dry-run.")
    return args


def _read_seed_file(path: Path) -> str:
    try:
        if not path.is_file():
            raise OSError
        seed = path.read_text(encoding="utf-8-sig").rstrip("\r\n")
    except OSError as exc:
        raise AnonymizationError("No se pudo leer el archivo de semilla.") from exc
    if "\n" in seed or "\r" in seed:
        raise AnonymizationError("El archivo de semilla debe contener una sola línea.")
    return seed


def _resolve_seed(args: argparse.Namespace) -> str:
    if args.seed is not None and args.seed_file is not None:
        raise AnonymizationError("Use solo una opción explícita de semilla.")
    if args.seed is not None:
        print(
            "Advertencia: --seed puede dejar el secreto en el historial; prefiera --seed-env o --seed-file.",
            file=sys.stderr,
        )
        return args.seed
    if args.seed_file is not None:
        return _read_seed_file(args.seed_file)
    environment_seed = os.environ.get(args.seed_env)
    if environment_seed is None and args.seed_env == "BALANCE_ANON_SEED":
        environment_seed = os.environ.get("BALANZA_PRIVADA_SEED")
    if environment_seed is not None:
        return environment_seed
    if sys.stdin.isatty():
        try:
            return getpass.getpass("Semilla secreta: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise AnonymizationError("No se pudo leer la semilla de forma oculta.") from exc
    raise AnonymizationError(
        "No se proporcionó una semilla; use --seed-env, --seed-file o una terminal interactiva."
    )


def _reject_source_destination(path: Path, sources: list[Path], label: str) -> None:
    resolved = path.resolve()
    if resolved in {source.resolve() for source in sources}:
        raise BatchError(f"La ruta de {label} no puede coincidir con un archivo fuente.")


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    registry: PseudonymRegistry | None = None
    try:
        seed = _resolve_seed(args)
        reporting_pseudo = Pseudonymizer(seed)
        vector_regions = load_vector_logo_regions(args.vector_logo_regions)
        sources = list_input_files(args.input)
        if not sources:
            raise BatchError("No se encontraron archivos compatibles.")
        manifest = load_manifest(args.manifest)
        report_path = args.report or (args.output / "reporte_tecnico.json")
        _reject_source_destination(report_path, sources, "reporte")
        if args.manifest_proposal:
            _reject_source_destination(args.manifest_proposal, sources, "propuesta de manifiesto")
            if args.manifest_proposal.resolve() == report_path.resolve():
                raise BatchError("La propuesta de manifiesto y el reporte deben usar rutas distintas.")
        if not args.discover and not args.dry_run:
            registry_path = args.registry or (args.output / "anon_registry.sqlite")
            _reject_source_destination(registry_path, sources, "registro")
            registry = PseudonymRegistry(registry_path)
        processor = BatchProcessor(
            seed,
            registry=registry,
            strict=args.strict,
            vector_regions=vector_regions,
            strip_signature=args.strip_signature,
            xsd=args.xsd,
        )
        run = processor.run(
            sources, args.output, manifest=manifest,
            dry_run=args.dry_run, discover_only=args.discover,
        )
        write_report(report_path, report_payload(run, reporting_pseudo))
        if args.manifest_proposal:
            proposed = write_manifest_proposal(run.groups, args.manifest_proposal)
    except (
        AnonymizationError, BatchError, ManifestError, RegistryError,
        OSError, ValueError,
    ) as exc:
        print(f"Error seguro: {exc}", file=sys.stderr)
        return 2
    finally:
        if registry is not None:
            registry.close()
    successful = sum(result.success for result in run.results)
    failed = len(run.results) - successful
    mode = "descubrimiento" if args.discover else "detección" if args.dry_run else "anonimización"
    print(f"{mode}: {successful} archivo(s) exitoso(s), {failed} fallido(s). Reporte técnico generado.")
    if args.manifest_proposal:
        print(f"Propuesta de manifiesto generada: {proposed} grupo(s).")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Orquestador común de descubrimiento, planificación y promoción atómica."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .adapters import LegacyXlsAdapter, PdfAdapter, XlsxAdapter, XmlAdapter
from .adapters.base import AdapterError, AdapterOutput, BalanceAdapter
from .adapters.common import shifted_temporal
from .manifest import ManifestGroup
from .models import (
    AnonymizationPlan,
    Category,
    DocumentRelation,
    DocumentSnapshot,
    FileResult,
    RelationType,
)
from .pseudonyms import Pseudonymizer, normalize
from .registry import PseudonymRegistry
from .relations import infer_pair, infer_relations, monetary_fingerprint


class BatchError(RuntimeError):
    """Error seguro de lote."""


SAFE_ERROR_CODES = {
    "SIGNATURE_PRESENT",
    "XML_DTD_OR_ENTITY_FORBIDDEN",
    "UNSUPPORTED_XLSM",
    "UNSUPPORTED_XLS",
    "UNSUPPORTED_XLSX_OBJECT",
    "AMBIGUOUS_XLSX_IMAGE",
    "RFC_TYPE_CONFLICT",
    "RFC_LENGTH_UNSUPPORTED",
    "PDF_PASSWORD_PROTECTED",
    "PDF_EMPTY",
    "PDF_NO_DIGITAL_TEXT",
    "PDF_TEXT_LAYOUT_UNRECOGNIZED",
    "PDF_TEXT_COORDINATES_INVALID",
    "PDF_PROFILE_UNRECOGNIZED",
    "PDF_REQUIRED_FIELDS_MISSING",
    "PDF_AMBIGUOUS_SENSITIVE_FIELD",
    "PDF_LEDGER_UNREADABLE",
    "PDF_STRUCTURE_UNREADABLE",
    "PDF_DISCOVERY_ENGINE_FAILED",
    "XLSX_ARCHIVE_TOO_LARGE",
    "XLSX_INVALID_CONTAINER",
    "XLSX_STRUCTURE_LIMIT",
    "XLSX_WORKBOOK_UNREADABLE",
    "XLSX_PROFILE_UNRECOGNIZED",
    "XLSX_LEDGER_UNREADABLE",
    "XLSX_OWNER_UNREADABLE",
    "XLSX_DISCOVERY_FAILED",
    "XLS_CONVERTER_UNAVAILABLE",
    "XLS_CONVERSION_FAILED",
    "XLS_CONVERSION_NOT_DETERMINISTIC",
}


def _safe_error_code(error: BaseException, fallback: str) -> str:
    value = str(error).strip()
    return value if value in SAFE_ERROR_CODES else fallback


@dataclass
class ResolvedGroup:
    id: str
    snapshots: list[DocumentSnapshot]
    relation: RelationType
    relations: list[DocumentRelation] = field(default_factory=list)
    entity_id: str | None = None
    metadata_source: Path | None = None
    manifest_confirmed: bool = False

    @property
    def conflicts(self) -> list[str]:
        return sorted({item for relation in self.relations for item in relation.conflicts})


@dataclass
class BatchRun:
    results: list[FileResult]
    relations: list[DocumentRelation]
    groups: list[ResolvedGroup]
    snapshots: list[DocumentSnapshot]
    mode: str


def list_input_files(input_path: Path) -> list[Path]:
    supported = {".pdf", ".xls", ".xlsx", ".xlsm", ".xml"}
    if input_path.is_file():
        if input_path.suffix.lower() not in supported:
            raise BatchError("El archivo de entrada no tiene un formato compatible.")
        return [input_path.resolve()]
    if not input_path.is_dir():
        raise BatchError("La entrada no existe o no es accesible.")
    return sorted(
        (item.resolve() for item in input_path.iterdir() if item.is_file() and item.suffix.lower() in supported),
        key=lambda item: item.name.casefold(),
    )


def failed_file_ids_from_report(path: Path) -> set[str]:
    """Obtiene los identificadores fallidos de un reporte técnico v3.

    El reporte no contiene rutas por diseño. Los IDs se vuelven a calcular
    contra las fuentes de la ejecución actual con la misma semilla.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchError("No se pudo leer el reporte de reintento.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 3:
        raise BatchError("El reporte de reintento debe ser un reporte técnico versión 3.")
    files = payload.get("archivos")
    if not isinstance(files, list):
        raise BatchError("El reporte de reintento no contiene una lista de archivos válida.")

    statuses: dict[str, bool] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise BatchError("El reporte de reintento contiene un archivo inválido.")
        file_id, success = entry.get("id_archivo"), entry.get("exitoso")
        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9A-F]{32}", file_id):
            raise BatchError("El reporte de reintento contiene un identificador de archivo inválido.")
        if not isinstance(success, bool):
            raise BatchError("El reporte de reintento contiene un estado de archivo inválido.")
        previous = statuses.setdefault(file_id, success)
        if previous != success:
            raise BatchError("El reporte de reintento tiene estados contradictorios para un archivo.")
    return {file_id for file_id, success in statuses.items() if not success}


def select_failed_sources(
    sources: list[Path], failed_ids: set[str], pseudo: Pseudonymizer
) -> list[Path]:
    """Filtra las fuentes para reintentar solo los IDs fallidos del reporte."""
    return [
        source for source in sources
        if pseudo.token("report-file", str(source), 32) in failed_ids
    ]


def _content_key(snapshot: DocumentSnapshot) -> str:
    rows = []
    for line in snapshot.ledger_lines:
        values = ":".join(str(value) for value in monetary_fingerprint(line)[:4])
        rows.append(f"{line.normalized_account_code}:{values}")
    return "|".join(rows)


def _group_id(pseudo: Pseudonymizer, snapshots: Iterable[DocumentSnapshot]) -> str:
    content = "||".join(sorted(_content_key(item) for item in snapshots))
    return f"group_{pseudo.token('group-id', content, 16).lower()}"


def _group_relation(relations: Iterable[DocumentRelation], count: int) -> RelationType:
    values = {item.relation for item in relations}
    if RelationType.PROJECTION in values:
        return RelationType.PROJECTION
    if RelationType.EXACT_EQUIVALENT in values:
        return RelationType.EXACT_EQUIVALENT
    if RelationType.SERIES in values:
        return RelationType.SERIES
    return RelationType.STANDALONE if count == 1 else RelationType.AMBIGUOUS


def resolve_groups(
    snapshots: list[DocumentSnapshot],
    relations: list[DocumentRelation],
    manifest: list[ManifestGroup],
    pseudo: Pseudonymizer,
) -> list[ResolvedGroup]:
    by_path = {item.source.resolve(): item for item in snapshots}
    consumed: set[Path] = set()
    groups: list[ResolvedGroup] = []
    for declaration in manifest:
        missing = [path for path in declaration.files if path.resolve() not in by_path]
        if missing:
            raise BatchError("El manifiesto referencia archivos que no forman parte de la entrada.")
        documents = [by_path[path.resolve()] for path in declaration.files]
        manifest_relations: list[DocumentRelation] = []
        if declaration.relation == RelationType.SERIES:
            def period_key(item: DocumentSnapshot) -> int:
                if item.temporal.year is None or item.temporal.month is None:
                    raise BatchError("Una serie declarada carece de período canónico.")
                return item.temporal.year * 12 + item.temporal.month - 1

            ordered = sorted(documents, key=period_key)
            if len({period_key(item) for item in ordered}) != len(ordered):
                raise BatchError("Una serie declarada contiene períodos duplicados.")
            candidate_pairs = list(zip(ordered, ordered[1:]))
        else:
            candidate_pairs = [
                (left, right)
                for index, left in enumerate(documents)
                for right in documents[index + 1 :]
            ]
        for left, right in candidate_pairs:
            inferred = infer_pair(left, right)
            permitted = (
                {RelationType.EXACT_EQUIVALENT, RelationType.PROJECTION}
                if declaration.relation == RelationType.PROJECTION
                else {declaration.relation}
            )
            if inferred is None or inferred.relation not in permitted:
                raise BatchError("La relación declarada no coincide con el contenido canónico.")
            inferred.confirmed_by_manifest = True
            manifest_relations.append(inferred)
        if (
            declaration.relation == RelationType.PROJECTION
            and not any(item.relation == RelationType.PROJECTION for item in manifest_relations)
        ):
            raise BatchError("El grupo declarado como proyección solo contiene equivalencias exactas.")
        if len(documents) == 1 and declaration.relation != RelationType.STANDALONE:
            raise BatchError("Una relación de manifiesto requiere al menos dos archivos.")
        groups.append(
            ResolvedGroup(
                declaration.id,
                documents,
                declaration.relation,
                manifest_relations,
                declaration.entity_id,
                declaration.metadata_source,
                True,
            )
        )
        consumed.update(item.source for item in documents)

    remaining = [item for item in snapshots if item.source not in consumed]
    remaining_paths = {item.source for item in remaining}
    edges = [
        item for item in relations
        if item.right is not None
        and item.left in remaining_paths
        and item.right in remaining_paths
        and item.relation in {RelationType.EXACT_EQUIVALENT, RelationType.PROJECTION, RelationType.SERIES}
    ]
    parent = {item.source: item.source for item in remaining}

    def find(value: Path) -> Path:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: Path, right: Path) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for edge in edges:
        union(edge.left, edge.right)  # type: ignore[arg-type]
    components: dict[Path, list[DocumentSnapshot]] = {}
    for snapshot in remaining:
        components.setdefault(find(snapshot.source), []).append(snapshot)
    for documents in components.values():
        paths = {item.source for item in documents}
        component_relations = [
            item for item in edges if item.left in paths and item.right in paths
        ]
        groups.append(
            ResolvedGroup(
                _group_id(pseudo, documents),
                documents,
                _group_relation(component_relations, len(documents)),
                component_relations,
            )
        )
    return groups


def _blocking_conflicts(group: ResolvedGroup, *, strict: bool) -> list[str]:
    if not strict:
        return []
    conflicts = set(group.conflicts)
    blocked: set[str] = set()
    owner = {value for value in conflicts if value.startswith("OWNER_")}
    temporal = {value for value in conflicts if value in {"PERIOD_CONFLICT", "PRINT_DATE_CONFLICT"}}
    if owner and not (
        group.manifest_confirmed and (group.entity_id or group.metadata_source)
    ):
        blocked.update(owner)
    if temporal and not (group.manifest_confirmed and group.metadata_source):
        blocked.update(temporal)
    blocked.update(value for value in conflicts if value == "ACCOUNT_NORMALIZATION_COLLISION")
    return sorted(blocked)


def _entity_key(group: ResolvedGroup) -> str:
    if group.entity_id:
        return group.entity_id
    if group.metadata_source:
        source = next(
            (item for item in group.snapshots if item.source == group.metadata_source.resolve()),
            None,
        )
        if source and source.owner.rfc:
            return f"RFC:{normalize(source.owner.rfc)}"
        if source and source.owner.name:
            return f"NAME:{normalize(source.owner.name)}"
    rfcs = {normalize(item.owner.rfc) for item in group.snapshots if item.owner.rfc}
    if len(rfcs) == 1:
        return f"RFC:{next(iter(rfcs))}"
    names = {normalize(item.owner.name) for item in group.snapshots if item.owner.name}
    if len(names) == 1:
        return f"NAME:{next(iter(names))}"
    return f"GROUP:{group.id}"


def build_plan(
    group: ResolvedGroup,
    seed: str,
    registry: PseudonymRegistry,
) -> AnonymizationPlan:
    entity_key = _entity_key(group)
    pseudo = Pseudonymizer(seed, registry=registry, scope=entity_key)
    rfc_lengths = {
        len(re.sub(r"[^A-Z0-9&Ñ]", "", normalize(item.owner.rfc)))
        for item in group.snapshots
        if item.owner.rfc
    }
    if len(rfc_lengths) > 1:
        selected_length: int | None = None
        if group.metadata_source:
            metadata_snapshot = next(
                (item for item in group.snapshots if item.source == group.metadata_source.resolve()),
                None,
            )
            if metadata_snapshot and metadata_snapshot.owner.rfc:
                selected_length = len(
                    re.sub(r"[^A-Z0-9&Ñ]", "", normalize(metadata_snapshot.owner.rfc))
                )
        elif group.manifest_confirmed and group.entity_id:
            # Un entity_id opaco confirma que el conflicto pertenece a una
            # sola entidad. El tipo sintético se decide sin usar PII ni el
            # orden de los archivos y se mantiene estable con la semilla.
            selected_length = 12 + (int(pseudo.token("rfc-kind", entity_key, 2), 16) % 2)
        if selected_length not in {12, 13}:
            raise BatchError("RFC_TYPE_CONFLICT")
        rfc_length = selected_length
    else:
        rfc_length = next(iter(rfc_lengths), 12)
    if rfc_length not in {12, 13}:
        raise BatchError("RFC_LENGTH_UNSUPPORTED")
    person = rfc_length == 13
    template_rfc = "ZZZZ000000AAA" if person else "ZZZ000000AAA"
    original_rfc = next(
        (
            item.owner.rfc
            for item in group.snapshots
            if item.owner.rfc
            and len(re.sub(r"[^A-Z0-9&Ñ]", "", normalize(item.owner.rfc))) == rfc_length
        ),
        template_rfc,
    )
    certificate = next(
        (item.owner.certificate for item in group.snapshots if item.owner.certificate),
        "CED-0000-AA",
    )
    synthetic_owner = {
        "name": pseudo.compact_person(entity_key) if person else pseudo.compact_company(entity_key),
        "rfc": pseudo.rfc(original_rfc, entity_key),
        "address": pseudo.short_address(entity_key),
        "population": pseudo.short_population(entity_key),
        "certificate": pseudo.certificate(certificate, entity_key),
    }
    canonical = None
    if group.metadata_source:
        source = next(
            (item for item in group.snapshots if item.source == group.metadata_source.resolve()),
            None,
        )
        if source is None:
            raise BatchError("metadata_source no pertenece al grupo resuelto.")
        canonical = shifted_temporal(source.temporal, pseudo)
    return AnonymizationPlan(
        group.id,
        entity_key,
        group.relation,
        pseudo,
        synthetic_owner,
        group.metadata_source,
        canonical,
        conflicts=group.conflicts,
        manifest_confirmed=group.manifest_confirmed,
    )


def _safe_detections(snapshot: DocumentSnapshot, pseudo: Pseudonymizer) -> list[dict[str, Any]]:
    return [
        {
            "categoria": span.category.value,
            "hash": pseudo.token(f"report-{span.category.value}", span.original, 64),
            "confianza": round(span.confidence, 4),
            "ubicacion": span.location.kind,
        }
        for span in snapshot.sensitive_spans
    ]


def _validate_group_relations(
    group: ResolvedGroup,
    outputs: list[tuple[DocumentSnapshot, AdapterOutput]],
) -> dict[str, object]:
    generated = {
        original.source: output.snapshot
        for original, output in outputs
        if output.snapshot is not None
    }
    if len(generated) != len(group.snapshots):
        raise BatchError("Un adaptador no devolvió el snapshot canónico de salida.")
    for relation in group.relations:
        left = generated[relation.left]
        right = generated[relation.right]  # type: ignore[index]
        if left is None or right is None:
            raise BatchError("No se pudo reanalizar un miembro del grupo.")
        validated = infer_pair(left, right)
        if validated is None or validated.relation != relation.relation:
            raise BatchError("La validación cruzada cambió la relación del grupo.")
        if relation.relation == RelationType.EXACT_EQUIVALENT and any(
            value in validated.conflicts
            for value in (
                "OWNER_RFC_CONFLICT", "OWNER_NAME_CONFLICT",
                "PERIOD_CONFLICT", "PRINT_DATE_CONFLICT",
            )
        ):
            raise BatchError("Persistió un conflicto reconciliado en la equivalencia de salida.")
    return {
        "cross_relation_validated": True,
        "output_snapshots_reanalyzed": True,
        "relation_count": len(group.relations),
        "atomic_members": len(group.snapshots),
    }


class BatchProcessor:
    def __init__(
        self,
        seed: str,
        *,
        registry: PseudonymRegistry | None,
        strict: bool = True,
        vector_regions: dict[str, list[dict[str, Any]]] | None = None,
        strip_signature: bool = False,
        xsd: Path | None = None,
    ) -> None:
        self.seed = seed
        self.registry = registry
        self.strict = strict
        self.discovery_pseudo = Pseudonymizer(seed)
        self.adapters: dict[str, BalanceAdapter] = {}
        for adapter in (
            PdfAdapter(vector_regions=vector_regions),
            LegacyXlsAdapter(),
            XlsxAdapter(),
            XmlAdapter(strip_signature=strip_signature, xsd=xsd),
        ):
            for suffix in adapter.suffixes:
                self.adapters[suffix] = adapter

    def _adapter(self, path: Path) -> BalanceAdapter:
        try:
            return self.adapters[path.suffix.lower()]
        except KeyError as exc:
            raise BatchError("Formato de archivo no compatible.") from exc

    def run(
        self,
        sources: list[Path],
        output_dir: Path,
        *,
        manifest: list[ManifestGroup] | None = None,
        dry_run: bool = False,
        discover_only: bool = False,
    ) -> BatchRun:
        snapshots: list[DocumentSnapshot] = []
        results: list[FileResult] = []
        for source in sources:
            adapter = self._adapter(source)
            try:
                snapshots.append(
                    adapter.discover(source, self.discovery_pseudo, strict=self.strict)
                )
            except (AdapterError, OSError, RuntimeError, ValueError) as exc:
                results.append(
                    FileResult(
                        str(source),
                        None,
                        False,
                        None,
                        error="El archivo no pudo superar el descubrimiento seguro.",
                        extra={"codigo_error": _safe_error_code(exc, "DISCOVERY_FAILED")},
                        adapter=adapter.name,
                        atomic_state="DISCOVERY_FAILED",
                    )
                )
        relations = infer_relations(snapshots)
        groups = resolve_groups(snapshots, relations, manifest or [], self.discovery_pseudo)
        mode = "discover" if discover_only else "dry-run" if dry_run else "anonymization"

        for group in groups:
            blocked = _blocking_conflicts(group, strict=self.strict)
            if discover_only or dry_run:
                for snapshot in group.snapshots:
                    counts = Counter(span.category.value for span in snapshot.sensitive_spans)
                    results.append(
                        FileResult(
                            str(snapshot.source),
                            None,
                            not blocked or discover_only,
                            snapshot.profile,
                            pages=int(snapshot.structural.get("page_count", 0)),
                            redactions=dict(counts),
                            warnings=snapshot.warnings,
                            error="Conflicto estricto pendiente de manifiesto." if blocked and not discover_only else None,
                            extra={
                                "codigo_error": "STRICT_RELATION_CONFLICT" if blocked else None,
                                "detecciones_seguras": _safe_detections(snapshot, self.discovery_pseudo),
                                "validacion": {"discovery_only": True},
                            },
                            adapter=snapshot.adapter,
                            group_id=group.id,
                            relation=group.relation.value,
                            confidence=min((item.confidence for item in group.relations), default=1.0),
                            conflicts=group.conflicts,
                            atomic_state="DISCOVERED" if discover_only else "DRY_RUN_BLOCKED" if blocked else "DRY_RUN_READY",
                        )
                    )
                continue

            if blocked:
                for snapshot in group.snapshots:
                    results.append(
                        FileResult(
                            str(snapshot.source),
                            None,
                            False,
                            snapshot.profile,
                            error="El grupo tiene conflictos que requieren manifiesto.",
                            extra={"codigo_error": "STRICT_RELATION_CONFLICT"},
                            adapter=snapshot.adapter,
                            group_id=group.id,
                            relation=group.relation.value,
                            conflicts=group.conflicts,
                            atomic_state="GROUP_FAILED",
                        )
                    )
                continue
            if self.registry is None:
                raise BatchError("El registro es obligatorio para generar salidas.")
            try:
                plan = build_plan(group, self.seed, self.registry)
            except (BatchError, ValueError) as exc:
                for snapshot in group.snapshots:
                    results.append(
                        FileResult(
                            str(snapshot.source), None, False, snapshot.profile,
                            error="No se pudo construir un plan seguro para el grupo.",
                            extra={"codigo_error": _safe_error_code(exc, "PLAN_FAILED")},
                            adapter=snapshot.adapter,
                            group_id=group.id,
                            relation=group.relation.value,
                            conflicts=group.conflicts,
                            atomic_state="GROUP_FAILED",
                        )
                    )
                continue

            output_dir.mkdir(parents=True, exist_ok=True)
            group_outputs: list[tuple[DocumentSnapshot, AdapterOutput]] = []
            group_error: str | None = None
            group_error_code = "ATOMIC_GROUP_FAILED"
            with tempfile.TemporaryDirectory(prefix=".balance_group_", dir=output_dir) as temporary_name:
                temporary = Path(temporary_name)
                try:
                    for snapshot in group.snapshots:
                        output = self._adapter(snapshot.source).apply(
                            snapshot,
                            plan,
                            temporary,
                            strict=self.strict,
                        )
                        group_outputs.append((snapshot, output))
                    cross_validation = _validate_group_relations(group, group_outputs)
                    destinations = [output_dir / output.temporary_path.name for _, output in group_outputs]
                    if any(path.exists() for path in destinations):
                        raise BatchError("Ya existe una salida y no se sobrescribirá.")
                    promoted: list[tuple[Path, Path]] = []
                    try:
                        for (_, output), destination in zip(group_outputs, destinations):
                            os.replace(output.temporary_path, destination)
                            promoted.append((destination, output.temporary_path))
                    except OSError:
                        for destination, original_temporary in reversed(promoted):
                            if destination.exists():
                                os.replace(destination, original_temporary)
                        raise
                except (AdapterError, BatchError, OSError, RuntimeError, ValueError) as exc:
                    group_error = "Un miembro del grupo falló; no se promovió ninguna salida."
                    group_error_code = _safe_error_code(exc, "ATOMIC_GROUP_FAILED")
                if group_error is None:
                    for (snapshot, output), destination in zip(group_outputs, destinations):
                        validation = dict(output.validation)
                        validation.update(cross_validation)
                        results.append(
                            FileResult(
                                str(snapshot.source),
                                str(destination),
                                True,
                                output.profile,
                                pages=output.pages,
                                redactions=output.substitutions,
                                warnings=output.warnings,
                                extra={
                                    "verificacion": validation,
                                    "detecciones_seguras": _safe_detections(
                                        snapshot, self.discovery_pseudo
                                    ),
                                },
                                adapter=snapshot.adapter,
                                group_id=group.id,
                                relation=group.relation.value,
                                confidence=min((item.confidence for item in group.relations), default=1.0),
                                conflicts=group.conflicts,
                                atomic_state="GROUP_COMMITTED",
                            )
                        )
            if group_error is not None:
                for snapshot in group.snapshots:
                    results.append(
                        FileResult(
                            str(snapshot.source),
                            None,
                            False,
                            snapshot.profile,
                            error=group_error,
                            extra={"codigo_error": group_error_code},
                            adapter=snapshot.adapter,
                            group_id=group.id,
                            relation=group.relation.value,
                            conflicts=group.conflicts,
                            atomic_state="GROUP_FAILED",
                        )
                    )
        return BatchRun(results, relations, groups, snapshots, mode)


def report_payload(run: BatchRun, pseudo: Pseudonymizer) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for result in run.results:
        files.append(
            {
                "id_archivo": pseudo.token("report-file", result.source, 32),
                "adaptador": result.adapter,
                "perfil": result.profile,
                "grupo": result.group_id,
                "relacion": result.relation,
                "confianza": result.confidence,
                "conflictos": result.conflicts,
                "exitoso": result.success,
                "estado_atomico": result.atomic_state,
                "sustituciones": result.redactions,
                "detecciones": result.extra.get("detecciones_seguras", []),
                "validaciones": result.extra.get("verificacion", result.extra.get("validacion", {})),
                "advertencias": result.warnings,
                "codigo_error": result.extra.get("codigo_error"),
                "error": result.error,
            }
        )
    def relation_key(item: DocumentRelation) -> tuple[str, str | None]:
        if item.right is None:
            return str(item.left), None
        left, right = sorted((str(item.left), str(item.right)))
        return left, right

    relation_map: dict[tuple[str, str | None], DocumentRelation] = {
        relation_key(item): item for item in run.relations
    }
    for group in run.groups:
        for item in group.relations:
            relation_map[relation_key(item)] = item
    relations = [
        {
            "izquierda": pseudo.token("report-file", str(item.left), 32),
            "derecha": pseudo.token("report-file", str(item.right), 32) if item.right else None,
            "relacion": item.relation.value,
            "confianza": round(item.confidence, 6),
            "cuentas_compartidas": item.shared_accounts,
            "conflictos": item.conflicts,
            "evidencia": item.evidence,
            "confirmada_manifiesto": item.confirmed_by_manifest,
        }
        for item in relation_map.values()
    ]
    return {
        "version": 3,
        "modo": run.mode,
        "archivos": files,
        "relaciones": relations,
        "resumen": {
            "archivos": len(files),
            "exitosos": sum(bool(item["exitoso"]) for item in files),
            "fallidos": sum(not bool(item["exitoso"]) for item in files),
            "grupos": len(run.groups),
        },
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)

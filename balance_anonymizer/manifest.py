"""Manifiesto de relaciones sin PII en JSON o YAML."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .models import RelationType


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_RFC_LIKE = re.compile(r"^[A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3}$", re.IGNORECASE)


class ManifestError(RuntimeError):
    """Error seguro de configuración."""


@dataclass(frozen=True)
class ManifestGroup:
    id: str
    relation: RelationType
    files: tuple[Path, ...]
    entity_id: str | None = None
    metadata_source: Path | None = None


def _proposal_path(source: Path, destination: Path) -> str:
    """Emite rutas relativas cuando el manifiesto y las fuentes lo permiten."""

    resolved_source = source.resolve()
    try:
        return str(resolved_source.relative_to(destination.resolve().parent))
    except ValueError:
        return str(resolved_source)


def _proposal_documents(group: Any) -> list[Any]:
    documents = list(group.snapshots)
    if group.relation == RelationType.SERIES:
        return sorted(
            documents,
            key=lambda item: (
                item.temporal.year is None,
                item.temporal.year or 0,
                item.temporal.month is None,
                item.temporal.month or 0,
                str(item.source),
            ),
        )
    return sorted(documents, key=lambda item: str(item.source))


def write_manifest_proposal(groups: Iterable[Any], destination: Path) -> int:
    """Escribe una propuesta revisable sin elegir una fuente de metadatos.

    El manifiesto contiene únicamente rutas e identificadores opacos. Los
    conflictos se anotan como comentarios YAML para que una persona decida si
    confirma la relación y cuál archivo es la fuente autorizada.
    """

    destination = destination.resolve()
    if destination.exists():
        raise ManifestError("La propuesta de manifiesto ya existe y no se sobrescribirá.")
    if destination.suffix.lower() not in {".yml", ".yaml", ".json"}:
        raise ManifestError("La propuesta de manifiesto debe terminar en .yml, .yaml o .json.")

    sections: dict[str, list[tuple[dict[str, Any], list[str]]]] = {
        "groups": [],
        "series": [],
    }
    for group in sorted(groups, key=lambda item: str(item.id)):
        if group.relation not in {
            RelationType.EXACT_EQUIVALENT,
            RelationType.PROJECTION,
            RelationType.SERIES,
        }:
            continue
        section = "series" if group.relation == RelationType.SERIES else "groups"
        opaque_suffix = re.sub(r"[^A-Za-z0-9_.-]", "", str(group.id).removeprefix("group_"))
        entry: dict[str, Any] = {
            "id": f"proposal_{opaque_suffix}",
            "entity_id": f"entity_{opaque_suffix}",
            "files": [
                _proposal_path(item.source, destination)
                for item in _proposal_documents(group)
            ],
        }
        if section == "groups":
            entry["relation"] = group.relation.value
            entry = {
                "id": entry["id"],
                "relation": entry["relation"],
                "entity_id": entry["entity_id"],
                "files": entry["files"],
            }
        sections[section].append((entry, sorted(set(group.conflicts))))

    payload = {
        section: [entry for entry, _ in entries]
        for section, entries in sections.items()
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        if destination.suffix.lower() == ".json":
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            lines = [
                "# Propuesta generada automáticamente; revísela antes de usarla.",
                "# No incluye PII: solo rutas, IDs opacos y categorías de conflicto.",
                "# Añada metadata_source solo cuando confirme cuál archivo gobierna los metadatos.",
            ]
            for section in ("groups", "series"):
                entries = sections[section]
                if not entries:
                    lines.append(f"{section}: []")
                    continue
                lines.append(f"{section}:")
                for entry, conflicts in entries:
                    if conflicts:
                        lines.append(f"  # REVISAR conflictos: {', '.join(conflicts)}")
                        lines.append("  # metadata_source: elija una de las rutas de files")
                    fragment = yaml.safe_dump(
                        [entry],
                        allow_unicode=True,
                        sort_keys=False,
                        default_flow_style=False,
                    ).rstrip()
                    lines.extend(f"  {line}" for line in fragment.splitlines())
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sum(len(entries) for entries in sections.values())


def _opaque(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value) or _RFC_LIKE.fullmatch(value):
        raise ManifestError(f"El campo {field} debe ser un identificador opaco.")
    return value


def _relation(value: Any, *, series: bool) -> RelationType:
    if series:
        return RelationType.SERIES
    try:
        relation = RelationType(str(value).lower())
    except ValueError as exc:
        raise ManifestError("El manifiesto contiene una relación no compatible.") from exc
    if relation not in {RelationType.EXACT_EQUIVALENT, RelationType.PROJECTION}:
        raise ManifestError("Los grupos solo confirman equivalencia exacta o proyección.")
    return relation


def _resolve_file(base: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError("Cada archivo del manifiesto debe ser una ruta.")
    candidate = Path(value)
    return (candidate if candidate.is_absolute() else base / candidate).resolve()


def load_manifest(path: Path | None) -> list[ManifestGroup]:
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
        payload = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ManifestError("No se pudo leer el manifiesto.") from exc
    if payload is None:
        return []
    if not isinstance(payload, dict):
        raise ManifestError("El manifiesto debe contener un objeto raíz.")
    base = path.resolve().parent
    groups: list[ManifestGroup] = []
    seen_files: set[Path] = set()
    for section, is_series in (("groups", False), ("series", True)):
        entries = payload.get(section, [])
        if not isinstance(entries, list):
            raise ManifestError(f"La sección {section} debe ser una lista.")
        for raw in entries:
            if not isinstance(raw, dict):
                raise ManifestError("Cada grupo del manifiesto debe ser un objeto.")
            group_id = _opaque(raw.get("id"), "id")
            entity_id = _opaque(raw.get("entity_id"), "entity_id", required=False)
            files_value = raw.get("files")
            if not isinstance(files_value, list) or not files_value:
                raise ManifestError("Cada grupo debe contener archivos.")
            files = tuple(_resolve_file(base, item) for item in files_value)
            duplicate = set(files) & seen_files
            if duplicate:
                raise ManifestError("Un archivo aparece en más de un grupo del manifiesto.")
            seen_files.update(files)
            metadata_source_value = raw.get("metadata_source")
            metadata_source = (
                _resolve_file(base, metadata_source_value)
                if metadata_source_value is not None
                else None
            )
            if metadata_source is not None and metadata_source not in files:
                raise ManifestError("metadata_source debe pertenecer al grupo.")
            groups.append(
                ManifestGroup(
                    group_id or "",
                    _relation(raw.get("relation"), series=is_series),
                    files,
                    entity_id,
                    metadata_source,
                )
            )
    return groups

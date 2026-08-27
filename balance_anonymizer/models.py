"""Modelos internos sin serializar datos identificables en claro."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class Category(str, Enum):
    COMPANY = "razon_social"
    RFC = "rfc"
    ADDRESS = "direccion"
    POPULATION = "poblacion"
    CERTIFICATE = "cedula"
    USER = "usuario_identificable"
    HEADER_DATE = "fecha_cabecera"
    CREATION_DATE = "fecha_creacion"
    PRINT_DATE = "fecha_impresion"
    FOOTER_DATE = "fecha_pie"
    PERIOD_RANGE = "rango_periodo"
    EXERCISE_PERIOD = "ejercicio_periodo"
    ASSOCIATED_ENTITY = "rfc_entidad_descripcion"
    ASSOCIATED_BANK = "banco_identificador_descripcion"
    BANK_ACCOUNT = "identificador_bancario"
    TEXT_LOGO = "logo_textual"
    RASTER_IMAGE = "logo_rasterizado"
    METADATA = "metadatos_pdf"


class RelationType(str, Enum):
    """Relación semántica entre dos documentos de balanza."""

    EXACT_EQUIVALENT = "exact_equivalent"
    PROJECTION = "projection"
    SERIES = "series"
    AMBIGUOUS = "ambiguous"
    STANDALONE = "standalone"


@dataclass(frozen=True)
class FormatLocation:
    """Ubicación concreta y no sensible dentro de un formato."""

    kind: str
    page: int | None = None
    sheet: str | None = None
    cell: str | None = None
    xpath: str | None = None
    part: str | None = None
    rect: tuple[float, float, float, float] | None = None


@dataclass
class SensitiveSpan:
    """Valor sensible localizado; el original jamás se serializa en reportes."""

    category: Category
    original: str
    location: FormatLocation
    entity_key: str | None = None
    identifier: str | None = None
    entity_text: str | None = None
    confidence: float = 1.0


@dataclass
class OwnerIdentity:
    name: str | None = None
    rfc: str | None = None
    address: str | None = None
    population: str | None = None
    certificate: str | None = None
    locations: dict[str, FormatLocation] = field(default_factory=dict)


@dataclass
class TemporalMetadata:
    year: int | None = None
    month: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    print_date: date | None = None
    currency: str | None = None
    representations: dict[str, str] = field(default_factory=dict)
    locations: dict[str, FormatLocation] = field(default_factory=dict)


@dataclass
class LedgerLine:
    """Renglón contable canónico con importes exactos y su representación."""

    account_code: str
    normalized_account_code: str
    nature: str | None
    description: str | None
    amounts: dict[str, Decimal]
    amount_representations: dict[str, str]
    location: FormatLocation
    sensitive_spans: list[SensitiveSpan] = field(default_factory=list)


@dataclass
class DocumentSnapshot:
    """Resultado inmutable en intención de la primera pasada."""

    source: Path
    adapter: str
    profile: str
    owner: OwnerIdentity
    temporal: TemporalMetadata
    ledger_lines: list[LedgerLine]
    sensitive_spans: list[SensitiveSpan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 1.0
    structural: dict[str, Any] = field(default_factory=dict)
    private: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class DocumentRelation:
    left: Path
    right: Path | None
    relation: RelationType
    confidence: float
    shared_accounts: int = 0
    conflicts: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    confirmed_by_manifest: bool = False


@dataclass
class AnonymizationPlan:
    """Plan único entregado a todos los adaptadores de un grupo."""

    group_id: str
    entity_key: str
    relation: RelationType
    pseudonymizer: Any = field(repr=False)
    synthetic_owner: dict[str, str] = field(default_factory=dict)
    metadata_source: Path | None = None
    canonical_temporal: TemporalMetadata | None = None
    replacements: dict[str, str] = field(default_factory=dict, repr=False)
    conflicts: list[str] = field(default_factory=list)
    manifest_confirmed: bool = False


@dataclass(frozen=True)
class WordBox:
    """Una palabra extraida junto a su ubicacion y estilo basico."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int
    word: int
    size: float = 0.0
    font: str = ""
    color: int = 0
    flags: int = 0


@dataclass(frozen=True)
class GlyphBox:
    """Un glifo real de un span, usado para recortar valores pegados a etiquetas."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float
    font: str
    color: int
    flags: int


@dataclass(frozen=True)
class TextStyle:
    """Estilo aproximado para reinsertar sin reconstruir la pagina."""

    size: float = 9.0
    font: str = "helv"
    color: int = 0
    flags: int = 0


@dataclass
class Detection:
    """Dato sensible localizado. `original` nunca se escribe a un reporte."""

    category: Category
    page: int
    rect: tuple[float, float, float, float]
    original: str
    replacement: str | None
    profile: str
    entity_key: str | None = None
    confidence: float = 1.0
    redact_only: bool = False
    insert_rect: tuple[float, float, float, float] | None = None
    style: TextStyle | None = None
    alternatives: tuple[str, ...] = ()
    residuals: tuple[str, ...] = ()
    alignment: int = 0
    multiline: bool = False


@dataclass
class ProfileResult:
    name: str
    confidence: float
    detections: list[Detection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    family_scores: dict[str, float] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileResult:
    source: str
    output: str | None
    success: bool
    profile: str | None
    pages: int = 0
    redactions: dict[str, int] = field(default_factory=dict)
    detections: list[Detection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    page_dimensions: list[tuple[float, float]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    adapter: str = "pdf"
    group_id: str | None = None
    relation: str | None = None
    confidence: float | None = None
    conflicts: list[str] = field(default_factory=list)
    atomic_state: str | None = None

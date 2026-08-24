"""Modelos internos sin serializar datos identificables en claro."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Category(str, Enum):
    COMPANY = "razon_social"
    RFC = "rfc"
    ADDRESS = "direccion"
    POPULATION = "poblacion"
    CERTIFICATE = "cedula"
    HEADER_DATE = "fecha_cabecera"
    CREATION_DATE = "fecha_creacion"
    EXERCISE_PERIOD = "ejercicio_periodo"
    BANK_ACCOUNT = "cuenta_bancaria"
    TEXT_LOGO = "logo_textual"
    RASTER_IMAGE = "imagen_rasterizada"


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


@dataclass
class ProfileResult:
    name: str
    confidence: float
    detections: list[Detection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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

from __future__ import annotations

from pathlib import Path

import fitz
import pytest


@pytest.fixture
def seed() -> str:
    return "semilla-local-pruebas-no-publica-2026"


def make_pdf(
    path: Path,
    lines: list[str],
    *,
    pages: int = 1,
    metadata: dict[str, str] | None = None,
    raster_logo: bool = False,
) -> None:
    """Genera exclusivamente PDF digitales y sinteticos para las pruebas."""

    document = fitz.open()
    for _ in range(pages):
        page = document.new_page(width=595, height=842)
        if raster_logo:
            # Pixmap sintetico: no procede de ningun documento real.
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
            pixmap.clear_with(0xFF0000)
            page.insert_image(fitz.Rect(530, 20, 550, 40), pixmap=pixmap)
        y = 52
        for line in lines:
            page.insert_text((42, y), line, fontname="helv", fontsize=10)
            y += 20
    if metadata:
        document.set_metadata(metadata)
    document.save(path)
    document.close()

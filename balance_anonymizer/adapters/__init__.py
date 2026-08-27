"""Adaptadores de formatos para el núcleo de dos pasadas."""

from .pdf import PdfAdapter
from .unsupported import LegacyXlsAdapter
from .xlsx import XlsxAdapter
from .xml import XmlAdapter

__all__ = ["LegacyXlsAdapter", "PdfAdapter", "XlsxAdapter", "XmlAdapter"]

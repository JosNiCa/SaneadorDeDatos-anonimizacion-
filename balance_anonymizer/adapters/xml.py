"""Adaptador seguro para balanzas de contabilidad electrónica XML."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import etree

from ..models import (
    AnonymizationPlan,
    Category,
    DocumentSnapshot,
    FormatLocation,
    LedgerLine,
    OwnerIdentity,
    SensitiveSpan,
    TemporalMetadata,
)
from ..pseudonyms import Pseudonymizer
from ..relations import normalize_account_code
from .base import AdapterError, AdapterOutput
from .common import decimal_value, format_date_like, parse_temporal_text


MAX_XML_SIZE = 50 * 1024 * 1024
MAX_XML_DEPTH = 64
SIGNATURE_ATTRIBUTES = {"Sello", "Certificado", "noCertificado"}
MONETARY_ATTRIBUTES = ("SaldoIni", "Debe", "Haber", "SaldoFin")


def _local_name(value: str) -> str:
    return etree.QName(value).localname


def _attribute_map(element: etree._Element) -> dict[str, str]:
    return {_local_name(key): key for key in element.attrib}


def _secure_parse(path: Path) -> tuple[etree._ElementTree, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AdapterError("No se pudo leer el XML.") from exc
    if len(raw) > MAX_XML_SIZE:
        raise AdapterError("El XML excede el límite de tamaño permitido.")
    upper = raw.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise AdapterError("XML_DTD_OR_ENTITY_FORBIDDEN")
    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_blank_text=False,
    )
    try:
        tree = etree.parse(path, parser)
    except (OSError, etree.XMLSyntaxError) as exc:
        raise AdapterError("El XML no está bien formado o usa una estructura no segura.") from exc
    root = tree.getroot()
    depth = max((len(tuple(item.iterancestors())) for item in root.iter()), default=0)
    if depth > MAX_XML_DEPTH:
        raise AdapterError("El XML excede la profundidad permitida.")
    return tree, raw


def _schema_warning(root: etree._Element) -> list[str]:
    namespace = etree.QName(root).namespace or ""
    schema = next(
        (value for key, value in root.attrib.items() if _local_name(key) == "schemaLocation"),
        None,
    )
    if not schema:
        return []
    tokens = schema.split()
    if len(tokens) % 2 != 0:
        return ["SCHEMA_LOCATION_ODD_TOKEN_COUNT"]
    warnings: list[str] = []
    for declared_namespace, location in zip(tokens[::2], tokens[1::2]):
        if declared_namespace == namespace and not location.startswith(namespace):
            warnings.append("SCHEMA_LOCATION_NAMESPACE_MISMATCH")
    return warnings


def _ledger(root: etree._Element) -> list[LedgerLine]:
    namespace = etree.QName(root).namespace
    result: list[LedgerLine] = []
    for index, child in enumerate(root):
        name = etree.QName(child)
        if name.localname != "Ctas" or name.namespace != namespace:
            continue
        attributes = _attribute_map(child)
        missing = {"NumCta", *MONETARY_ATTRIBUTES} - set(attributes)
        if missing:
            raise AdapterError("Un elemento Ctas carece de atributos obligatorios.")
        account = child.attrib[attributes["NumCta"]]
        amounts = {}
        representations = {}
        for source_key, canonical_key in zip(
            MONETARY_ATTRIBUTES,
            ("saldo_inicial", "debe", "haber", "saldo_final"),
        ):
            representation = child.attrib[attributes[source_key]]
            value, _ = decimal_value(representation, representation)
            amounts[canonical_key] = value
            representations[canonical_key] = representation
        result.append(
            LedgerLine(
                account,
                normalize_account_code(account),
                None,
                None,
                amounts,
                representations,
                FormatLocation("xml_element", xpath=f"/*/*[{index + 1}]"),
            )
        )
    if not result:
        raise AdapterError("El XML no contiene elementos Ctas.")
    return result


def _validate_output(
    original: DocumentSnapshot,
    generated: DocumentSnapshot,
    *,
    stripped_signature: bool,
) -> dict[str, object]:
    expected = original.structural
    actual = generated.structural
    for key in ("root_name", "namespace", "prefix", "nsmap", "schema_location", "version", "tipo_envio"):
        if expected.get(key) != actual.get(key):
            raise AdapterError("Cambió una propiedad XML que debía preservarse.")
    if expected.get("child_names") != actual.get("child_names"):
        raise AdapterError("Cambió el orden o estructura de elementos XML.")
    if [line.account_code for line in original.ledger_lines] != [line.account_code for line in generated.ledger_lines]:
        raise AdapterError("Cambió NumCta u orden de los elementos Ctas.")
    for before, after in zip(original.ledger_lines, generated.ledger_lines):
        if before.amounts != after.amounts or before.amount_representations != after.amount_representations:
            raise AdapterError("Cambió la representación decimal de un elemento Ctas.")
    return {
        "well_formed": True,
        "namespace_prefix_preserved": True,
        "schema_location_preserved": True,
        "ctas_order_preserved": True,
        "decimal_representations_preserved": True,
        "signature_stripped": stripped_signature,
    }


class XmlAdapter:
    name = "xml"
    suffixes = (".xml",)

    def __init__(
        self,
        *,
        strip_signature: bool = False,
        xsd: Path | None = None,
    ) -> None:
        self.strip_signature = strip_signature
        self.xsd = xsd.resolve() if xsd else None

    def discover(
        self,
        source: Path,
        pseudonymizer: Pseudonymizer,
        *,
        strict: bool,
    ) -> DocumentSnapshot:
        source = source.resolve()
        tree, raw = _secure_parse(source)
        root = tree.getroot()
        name = etree.QName(root)
        if name.localname != "Balanza" or not name.namespace:
            raise AdapterError("La raíz XML no es una Balanza con namespace expandido.")
        attributes = _attribute_map(root)
        required = {"Version", "RFC", "Anio", "Mes", "TipoEnvio"}
        if required - set(attributes):
            raise AdapterError("La raíz Balanza carece de atributos obligatorios.")
        signatures = sorted(SIGNATURE_ATTRIBUTES & set(attributes))
        if signatures and strict and not self.strip_signature:
            raise AdapterError("SIGNATURE_PRESENT")
        rfc = root.attrib[attributes["RFC"]]
        year_text = root.attrib[attributes["Anio"]]
        month_text = root.attrib[attributes["Mes"]]
        try:
            year, month = int(year_text), int(month_text)
        except ValueError as exc:
            raise AdapterError("Anio o Mes no son numéricos.") from exc
        if not 1 <= month <= 12:
            raise AdapterError("Mes XML fuera de rango.")
        owner = OwnerIdentity(
            rfc=rfc,
            locations={"rfc": FormatLocation("xml_attribute", xpath="/*/@RFC")},
        )
        temporal = TemporalMetadata(
            year=year,
            month=month,
            representations={"year": year_text, "month": month_text},
            locations={
                "year": FormatLocation("xml_attribute", xpath="/*/@Anio"),
                "month": FormatLocation("xml_attribute", xpath="/*/@Mes"),
            },
        )
        spans = [
            SensitiveSpan(Category.RFC, rfc, owner.locations["rfc"]),
            SensitiveSpan(Category.EXERCISE_PERIOD, year_text, temporal.locations["year"]),
            SensitiveSpan(Category.EXERCISE_PERIOD, month_text, temporal.locations["month"]),
        ]
        if "FechaModBal" in attributes:
            value = root.attrib[attributes["FechaModBal"]]
            location = FormatLocation("xml_attribute", xpath="/*/@FechaModBal")
            spans.append(SensitiveSpan(Category.HEADER_DATE, value, location))
            parsed = parse_temporal_text([(value, location)])
            temporal.print_date = parsed.print_date or parsed.period_start
            temporal.representations["fecha_mod_bal"] = value
            temporal.locations["fecha_mod_bal"] = location
        schema_location = next(
            (value for key, value in root.attrib.items() if _local_name(key) == "schemaLocation"),
            None,
        )
        structural = {
            "root_name": name.localname,
            "namespace": name.namespace,
            "prefix": root.prefix,
            "nsmap": dict(root.nsmap),
            "schema_location": schema_location,
            "version": root.attrib[attributes["Version"]],
            "tipo_envio": root.attrib[attributes["TipoEnvio"]],
            "child_names": tuple(
                (etree.QName(child).namespace, etree.QName(child).localname)
                for child in root
            ),
            "signature_attributes": signatures,
            "xml_declaration": raw.startswith(b"<?xml"),
            "encoding_utf8": b"UTF-8" in raw[:100].upper(),
        }
        return DocumentSnapshot(
            source,
            self.name,
            "SAT_BALANZA_XML",
            owner,
            temporal,
            _ledger(root),
            spans,
            _schema_warning(root),
            structural=structural,
            private={"attribute_names": attributes},
        )

    def _validate_xsd(self, tree: etree._ElementTree) -> None:
        if self.xsd is None:
            return
        try:
            schema_tree, _ = _secure_parse(self.xsd)
            schema = etree.XMLSchema(schema_tree)
        except (AdapterError, etree.XMLSchemaError) as exc:
            raise AdapterError("No se pudo cargar el XSD local.") from exc
        if not schema.validate(tree):
            raise AdapterError("La salida no cumple el XSD local proporcionado.")

    def apply(
        self,
        snapshot: DocumentSnapshot,
        plan: AnonymizationPlan,
        temporary_dir: Path,
        *,
        strict: bool,
    ) -> AdapterOutput:
        tree, _ = _secure_parse(snapshot.source)
        root = tree.getroot()
        attributes = _attribute_map(root)
        counts: Counter[str] = Counter()
        root.attrib[attributes["RFC"]] = plan.synthetic_owner["rfc"]
        counts[Category.RFC.value] += 1
        if plan.canonical_temporal and plan.canonical_temporal.year and plan.canonical_temporal.month:
            shifted_year = str(plan.canonical_temporal.year)
            shifted_month = str(plan.canonical_temporal.month).zfill(len(root.attrib[attributes["Mes"]]))
        else:
            shifted_year, shifted_month = plan.pseudonymizer.exercise_and_period(
                root.attrib[attributes["Anio"]],
                root.attrib[attributes["Mes"]],
            )
        root.attrib[attributes["Anio"]] = shifted_year
        root.attrib[attributes["Mes"]] = shifted_month
        counts[Category.EXERCISE_PERIOD.value] += 2
        if "FechaModBal" in attributes:
            original = root.attrib[attributes["FechaModBal"]]
            if plan.canonical_temporal and plan.canonical_temporal.print_date:
                replacement = format_date_like(original, plan.canonical_temporal.print_date)
            else:
                replacement = plan.pseudonymizer.replace_temporal(original)
            root.attrib[attributes["FechaModBal"]] = replacement
            counts[Category.HEADER_DATE.value] += 1

        stripped = False
        present_signatures = SIGNATURE_ATTRIBUTES & set(attributes)
        if present_signatures:
            if not self.strip_signature:
                raise AdapterError("SIGNATURE_PRESENT")
            for local in present_signatures:
                root.attrib.pop(attributes[local], None)
            root.addprevious(
                etree.ProcessingInstruction(
                    "balance-anonymizer",
                    'status="anonymized-not-for-fiscal-submission"',
                )
            )
            stripped = True

        temporary_dir.mkdir(parents=True, exist_ok=True)
        target = temporary_dir / f"anonimizado_{plan.pseudonymizer.token('output-file', str(snapshot.source), 16)}.xml"
        if target.exists() or target.resolve() == snapshot.source.resolve():
            raise AdapterError("La salida XML no puede sobrescribir un archivo existente.")
        try:
            tree.write(
                target,
                encoding="UTF-8",
                xml_declaration=True,
                pretty_print=False,
            )
            generated_tree, raw = _secure_parse(target)
            self._validate_xsd(generated_tree)
            if snapshot.owner.rfc and snapshot.owner.rfc.encode("utf-8") in raw:
                raise AdapterError("Persistió el RFC original en el XML.")
            generated = self.discover(target, plan.pseudonymizer, strict=False)
            validation = _validate_output(snapshot, generated, stripped_signature=stripped)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        warnings = list(snapshot.warnings)
        if stripped:
            warnings.append("ANONYMIZED_NOT_FOR_FISCAL_SUBMISSION")
        return AdapterOutput(
            target,
            snapshot.profile,
            dict(counts),
            validation,
            warnings,
            snapshot=generated,
        )

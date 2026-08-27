"""Huellas semánticas e inferencia conservadora de relaciones."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Iterable

from .models import DocumentRelation, DocumentSnapshot, LedgerLine, RelationType
from .pseudonyms import normalize


def normalize_account_code(value: str) -> str:
    """Normaliza separadores y ceros de segmentos sin ocultar colisiones."""

    compact = re.sub(r"\s+", "", value.strip())
    if not re.fullmatch(r"\d+(?:[.-]\d+)*", compact):
        return normalize(compact)
    segments = re.split(r"[.-]", compact)
    return ".".join(segments)


def normalization_collisions(snapshot: DocumentSnapshot) -> dict[str, list[str]]:
    values: dict[str, set[str]] = {}
    for line in snapshot.ledger_lines:
        values.setdefault(line.normalized_account_code, set()).add(line.account_code)
    return {
        key: sorted(originals)
        for key, originals in values.items()
        if len(originals) > 1
    }


def monetary_fingerprint(line: LedgerLine) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    return (
        line.amounts.get("saldo_inicial", Decimal(0)),
        line.amounts.get("debe", Decimal(0)),
        line.amounts.get("haber", Decimal(0)),
        line.amounts.get("saldo_final", Decimal(0)),
        normalize(line.nature or ""),
    )


def _rows_equal(left: LedgerLine, right: LedgerLine) -> bool:
    left_values = monetary_fingerprint(left)
    right_values = monetary_fingerprint(right)
    if left_values[:4] != right_values[:4]:
        return False
    return not left_values[4] or not right_values[4] or left_values[4] == right_values[4]


def _line_map(snapshot: DocumentSnapshot) -> dict[str, LedgerLine]:
    return {line.normalized_account_code: line for line in snapshot.ledger_lines}


def _comparison_maps(
    left: DocumentSnapshot,
    right: DocumentSnapshot,
) -> tuple[dict[str, LedgerLine], dict[str, LedgerLine]]:
    left_map, right_map = _line_map(left), _line_map(right)
    left_unmatched = set(left_map) - set(right_map)
    right_unmatched = set(right_map) - set(left_map)

    def alias(value: str) -> str:
        return ".".join(str(int(segment)) for segment in value.split("."))

    left_aliases: dict[str, list[str]] = {}
    right_aliases: dict[str, list[str]] = {}
    for key in left_unmatched:
        left_aliases.setdefault(alias(key), []).append(key)
    for key in right_unmatched:
        right_aliases.setdefault(alias(key), []).append(key)
    for value in set(left_aliases) & set(right_aliases):
        if len(left_aliases[value]) != 1 or len(right_aliases[value]) != 1:
            continue
        left_key, right_key = left_aliases[value][0], right_aliases[value][0]
        left_line, right_line = left_map.pop(left_key), right_map.pop(right_key)
        comparison_key = f"ALIAS:{value}"
        left_map[comparison_key] = left_line
        right_map[comparison_key] = right_line
    return left_map, right_map


def _period_index(snapshot: DocumentSnapshot) -> int | None:
    year, month = snapshot.temporal.year, snapshot.temporal.month
    if year is None or month is None or not 1 <= month <= 12:
        return None
    return year * 12 + month - 1


def _metadata_conflicts(left: DocumentSnapshot, right: DocumentSnapshot) -> list[str]:
    conflicts: list[str] = []
    if left.owner.rfc and right.owner.rfc and normalize(left.owner.rfc) != normalize(right.owner.rfc):
        conflicts.append("OWNER_RFC_CONFLICT")
    if left.owner.name and right.owner.name and normalize(left.owner.name) != normalize(right.owner.name):
        conflicts.append("OWNER_NAME_CONFLICT")
    left_period, right_period = _period_index(left), _period_index(right)
    if left_period is not None and right_period is not None and left_period != right_period:
        conflicts.append("PERIOD_CONFLICT")
    if (
        left.temporal.print_date
        and right.temporal.print_date
        and left.temporal.print_date != right.temporal.print_date
    ):
        conflicts.append("PRINT_DATE_CONFLICT")
    return conflicts


def infer_pair(left: DocumentSnapshot, right: DocumentSnapshot) -> DocumentRelation | None:
    left_collisions = normalization_collisions(left)
    right_collisions = normalization_collisions(right)
    if left_collisions or right_collisions:
        return DocumentRelation(
            left.source,
            right.source,
            RelationType.AMBIGUOUS,
            1.0,
            conflicts=["ACCOUNT_NORMALIZATION_COLLISION"],
            evidence={
                "left_collision_count": len(left_collisions),
                "right_collision_count": len(right_collisions),
            },
        )

    left_map, right_map = _comparison_maps(left, right)
    if not left_map or not right_map:
        return None
    shared = set(left_map) & set(right_map)
    if not shared:
        return None
    exact_rows = sum(_rows_equal(left_map[key], right_map[key]) for key in shared)
    conflicts = _metadata_conflicts(left, right)

    if set(left_map) == set(right_map) and exact_rows == len(shared):
        return DocumentRelation(
            left.source,
            right.source,
            RelationType.EXACT_EQUIVALENT,
            1.0,
            len(shared),
            conflicts,
            {"matching_rows": exact_rows, "left_rows": len(left_map), "right_rows": len(right_map)},
        )

    smaller = min(len(left_map), len(right_map))
    coverage = len(shared) / smaller
    if len(shared) >= 3 and coverage >= 0.80 and exact_rows == len(shared):
        confidence = min(0.99, 0.80 + coverage * 0.19)
        return DocumentRelation(
            left.source,
            right.source,
            RelationType.PROJECTION,
            confidence,
            len(shared),
            conflicts,
            {
                "matching_rows": exact_rows,
                "coverage_of_smaller": round(coverage, 6),
                "left_rows": len(left_map),
                "right_rows": len(right_map),
            },
        )

    left_period, right_period = _period_index(left), _period_index(right)
    consecutive = (
        left_period is not None
        and right_period is not None
        and abs(left_period - right_period) == 1
    )
    if consecutive and len(shared) >= 3:
        if left_period < right_period:
            continuity = sum(
                left_map[key].amounts.get("saldo_final")
                == right_map[key].amounts.get("saldo_inicial")
                for key in shared
            )
        else:
            continuity = sum(
                right_map[key].amounts.get("saldo_final")
                == left_map[key].amounts.get("saldo_inicial")
                for key in shared
            )
        ratio = continuity / len(shared)
        if ratio >= 0.80:
            series_conflicts = [item for item in conflicts if item != "PERIOD_CONFLICT"]
            return DocumentRelation(
                left.source,
                right.source,
                RelationType.SERIES,
                min(0.99, 0.80 + ratio * 0.19),
                len(shared),
                series_conflicts,
                {
                    "continuous_rows": continuity,
                    "continuity_ratio": round(ratio, 6),
                    "left_rows": len(left_map),
                    "right_rows": len(right_map),
                },
            )

    overlap = len(shared) / min(len(left_map), len(right_map))
    if len(shared) >= 3 and overlap >= 0.50:
        return DocumentRelation(
            left.source,
            right.source,
            RelationType.AMBIGUOUS,
            min(0.89, overlap),
            len(shared),
            conflicts or ["CONTENT_MISMATCH"],
            {"matching_rows": exact_rows, "shared_ratio": round(overlap, 6)},
        )
    return None


def infer_relations(snapshots: Iterable[DocumentSnapshot]) -> list[DocumentRelation]:
    documents = list(snapshots)
    relations: list[DocumentRelation] = []
    connected: set = set()
    for index, left in enumerate(documents):
        for right in documents[index + 1 :]:
            relation = infer_pair(left, right)
            if relation is None:
                continue
            relations.append(relation)
            if relation.relation in {
                RelationType.EXACT_EQUIVALENT,
                RelationType.PROJECTION,
                RelationType.SERIES,
            }:
                connected.update((left.source, right.source))
    for snapshot in documents:
        if snapshot.source not in connected:
            relations.append(
                DocumentRelation(
                    snapshot.source,
                    None,
                    RelationType.STANDALONE,
                    1.0,
                )
            )
    return relations

"""Assembly, filtering and export of lookup results."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from traffic_checker.ingest import InputError, PreparedInput, guess_domain_column, prepare_input
from traffic_checker.models import LookupResult, LookupStatus

VISITS_COLUMN = "Visits"
STATUS_COLUMN = "Status"

# Row-level states that exist only in the report: such rows never reach the API.
STATUS_INVALID = "Invalid domain"
STATUS_PENDING = "Not checked"

RowFilter = Literal["all", "resolved", "unresolved"]

_TIER_LABELS = ("< 1K", "1K - 10K", "10K - 100K", "100K - 1M", "1M - 10M", "10M+")
_TIER_EDGES = (0, 1_000, 10_000, 100_000, 1_000_000, 10_000_000, float("inf"))

# Leading characters that make spreadsheet applications evaluate a cell.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
# Signed numbers and phone numbers start with a trigger but are harmless.
_NUMERIC_CELL_RE = re.compile(r"[+-]?[\d\s().,-]+")


@dataclass(frozen=True)
class Summary:
    """Counts of distinct domains by lookup state."""

    resolved: int
    no_data: int
    failed: int
    pending: int


def build_report(prepared: PreparedInput, results: Mapping[str, LookupResult]) -> pd.DataFrame:
    """Join lookup results onto the input rows.

    The original columns are kept as uploaded and followed by ``Visits`` and
    ``Status``. Input columns already carrying those names (typical when a
    previous export is fed back in) are replaced by the fresh values.
    """
    known = {domain: results[domain] for domain in prepared.unique_domains if domain in results}
    visits_by_domain = {d: r.visits for d, r in known.items() if r.visits is not None}
    label_by_domain = {d: r.label for d, r in known.items()}

    visits = pd.array(
        [visits_by_domain.get(domain) for domain in prepared.normalized],
        dtype="Int64",
    )
    status = [
        STATUS_INVALID if domain is None else label_by_domain.get(domain, STATUS_PENDING)
        for domain in prepared.normalized
    ]

    report = prepared.table.drop(columns=[VISITS_COLUMN, STATUS_COLUMN], errors="ignore")
    report[VISITS_COLUMN] = visits
    report[STATUS_COLUMN] = status
    return report


def summarize(prepared: PreparedInput, results: Mapping[str, LookupResult]) -> Summary:
    """Count the distinct domains of ``prepared`` by lookup state."""
    resolved = no_data = failed = pending = 0
    for domain in prepared.unique_domains:
        result = results.get(domain)
        if result is None:
            pending += 1
        elif result.status is LookupStatus.OK:
            resolved += 1
        elif result.status is LookupStatus.NO_DATA:
            no_data += 1
        else:
            failed += 1
    return Summary(resolved=resolved, no_data=no_data, failed=failed, pending=pending)


def refine_report(
    report: pd.DataFrame,
    *,
    min_visits: int = 0,
    rows: RowFilter = "all",
    sort_by_visits: bool = False,
) -> pd.DataFrame:
    """Apply the user's view options to a report.

    A positive ``min_visits`` also drops rows without a traffic figure, since
    they cannot satisfy the threshold.
    """
    mask = pd.Series(True, index=report.index)
    if rows == "resolved":
        mask &= report[STATUS_COLUMN] == LookupStatus.OK.label
    elif rows == "unresolved":
        mask &= report[STATUS_COLUMN] != LookupStatus.OK.label
    if min_visits > 0:
        mask &= report[VISITS_COLUMN].ge(min_visits).fillna(False)

    refined = report[mask]
    if sort_by_visits:
        # A stable sort keeps the input order among equal values.
        refined = refined.sort_values(
            VISITS_COLUMN, ascending=False, na_position="last", kind="stable"
        )
    return refined


def traffic_tiers(visits: Iterable[int]) -> pd.DataFrame:
    """Bucket visit counts into order-of-magnitude tiers."""
    tiers = pd.cut(
        pd.Series(list(visits), dtype="float64"),
        bins=list(_TIER_EDGES),
        labels=list(_TIER_LABELS),
        right=False,
    )
    counts = tiers.value_counts(sort=False)
    total = int(counts.sum())
    return pd.DataFrame(
        {
            "Monthly visits": list(_TIER_LABELS),
            "Domains": [int(counts[label]) for label in _TIER_LABELS],
            "Share": [float(counts[label]) / total if total else 0.0 for label in _TIER_LABELS],
        }
    )


def to_csv_bytes(report: pd.DataFrame) -> bytes:
    """Serialize a report to UTF-8 CSV with spreadsheet formulas neutralized."""
    safe = report.copy()
    for column in safe.columns:
        if pd.api.types.is_object_dtype(safe[column]) or pd.api.types.is_string_dtype(safe[column]):
            safe[column] = safe[column].map(_neutralize_formula)
    return safe.to_csv(index=False).encode("utf-8")


def restore_results(
    table: pd.DataFrame,
    *,
    strip_mobile_prefix: bool = True,
) -> dict[str, LookupResult]:
    """Recover definitive results from a previously exported report.

    Only rows the API answered conclusively are restored; failed and pending
    rows are left out so that the next run retries them.
    """
    if VISITS_COLUMN not in table.columns or STATUS_COLUMN not in table.columns:
        raise InputError(
            f"The file is not a results export: it has no '{VISITS_COLUMN}' and "
            f"'{STATUS_COLUMN}' columns."
        )

    candidates = table.drop(columns=[VISITS_COLUMN, STATUS_COLUMN])
    if candidates.shape[1] == 0:
        raise InputError("The file has no domain column.")
    prepared = prepare_input(
        candidates,
        guess_domain_column(candidates),
        strip_mobile_prefix=strip_mobile_prefix,
    )
    visits = pd.to_numeric(table[VISITS_COLUMN], errors="coerce")

    restored: dict[str, LookupResult] = {}
    for domain, count, status in zip(
        prepared.normalized, visits, table[STATUS_COLUMN], strict=True
    ):
        if domain is None:
            continue
        if status == LookupStatus.OK.label and pd.notna(count) and count >= 0:
            restored[domain] = LookupResult(domain, LookupStatus.OK, visits=int(count))
        elif status == LookupStatus.NO_DATA.label:
            restored[domain] = LookupResult(domain, LookupStatus.NO_DATA)
    return restored


def _neutralize_formula(cell: object) -> object:
    """Prefix a cell with an apostrophe if a spreadsheet would execute it.

    Input lists are frequently scraped from third-party sources, so a cell
    such as ``=HYPERLINK(...)`` must not survive into a file that is going to
    be opened in Excel (CSV injection, CWE-1236).
    """
    if not isinstance(cell, str) or not cell.startswith(_FORMULA_TRIGGERS):
        return cell
    if cell[0] in "+-" and _NUMERIC_CELL_RE.fullmatch(cell):
        return cell
    return f"'{cell}"

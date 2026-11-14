"""Turning uploaded files and pasted text into a table of lookup candidates."""

from __future__ import annotations

import codecs
import csv
import io
import re
import statistics
from dataclasses import dataclass

import pandas as pd

from traffic_checker.config import MAX_INPUT_ROWS
from traffic_checker.domains import normalize_domain

DEFAULT_DOMAIN_COLUMN = "Domain"

_DELIMITERS = (",", ";", "\t", "|")
_TOKEN_SEPARATOR_RE = re.compile(r"[\s,;]+")
# Enough rows to tell a delimiter or a domain column apart without scanning
# the whole input.
_SAMPLE_LINES = 50
_SAMPLE_ROWS = 500


class InputError(ValueError):
    """User-supplied input cannot be used; the message is safe to display."""


@dataclass(frozen=True)
class PreparedInput:
    """An input table together with the normalized form of its domain column."""

    table: pd.DataFrame
    domain_column: str
    # Aligned with ``table``; ``None`` marks rows that hold no valid domain.
    normalized: pd.Series
    # Distinct valid domains in order of first appearance.
    unique_domains: tuple[str, ...]

    @property
    def row_count(self) -> int:
        return len(self.table)

    @property
    def invalid_count(self) -> int:
        return int(self.normalized.isna().sum())

    @property
    def duplicate_count(self) -> int:
        return self.row_count - self.invalid_count - len(self.unique_domains)


def read_table(data: bytes) -> pd.DataFrame:
    """Parse an uploaded CSV/TSV/TXT file into a table of string cells.

    Encoding and delimiter are detected, ragged rows are padded rather than
    rejected, and a first row without any domain in it is taken as the header.
    """
    text = _decode(data)
    if "\x00" in text:
        raise InputError("The file does not look like a text or CSV file.")

    rows = [row for row in _split_rows(text) if row]
    if not rows:
        raise InputError("The file is empty.")

    width = max(len(row) for row in rows)
    has_header = not any(normalize_domain(cell) for cell in rows[0])
    header, body = (rows[0], rows[1:]) if has_header else (None, rows)
    if not body:
        raise InputError("The file contains no domains.")
    _check_row_limit(len(body))

    padded = [row + [""] * (width - len(row)) for row in body]
    return pd.DataFrame(padded, columns=_column_names(header, width))


def table_from_text(text: str) -> pd.DataFrame:
    """Build a single-column table from domains separated by whitespace, ``,`` or ``;``."""
    tokens = [token for token in _TOKEN_SEPARATOR_RE.split(text) if token]
    if not tokens:
        raise InputError("No domains were entered.")
    _check_row_limit(len(tokens))
    return pd.DataFrame({DEFAULT_DOMAIN_COLUMN: tokens})


def guess_domain_column(table: pd.DataFrame) -> str:
    """Return the column whose leading rows hold the most valid domains."""
    sample = table.head(_SAMPLE_ROWS)
    hits = {
        column: sum(normalize_domain(cell) is not None for cell in sample[column])
        for column in table.columns
    }
    # max() keeps the first maximum, so ties resolve to the leftmost column.
    return max(hits, key=hits.__getitem__)


def prepare_input(
    table: pd.DataFrame,
    domain_column: str,
    *,
    strip_mobile_prefix: bool = True,
) -> PreparedInput:
    """Normalize ``domain_column`` and collect the distinct domains to look up."""
    normalized = pd.Series(
        [
            normalize_domain(cell, strip_mobile_prefix=strip_mobile_prefix)
            for cell in table[domain_column]
        ],
        index=table.index,
        dtype=object,
    )
    unique_domains = tuple(dict.fromkeys(domain for domain in normalized if domain is not None))
    return PreparedInput(table, domain_column, normalized, unique_domains)


def _decode(data: bytes) -> str:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",)
    else:
        # cp1251 covers the Cyrillic exports Excel produces on Windows.
        encodings = ("utf-8-sig", "cp1251")
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 maps every byte, so this never fails; domains are ASCII anyway.
    return data.decode("latin-1")


def _split_rows(text: str) -> list[list[str]]:
    """Split text into rows of stripped cells without trailing empty ones."""
    delimiter = _detect_delimiter(text)
    if delimiter is None:
        # One value per line. Going through the CSV parser here would break
        # URLs apart at commas in their query strings.
        return [[stripped] for line in text.splitlines() if (stripped := line.strip())]

    try:
        rows = [
            [cell.strip() for cell in row]
            for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        ]
    except csv.Error as exc:
        raise InputError("The file could not be parsed as CSV.") from exc
    for row in rows:
        while row and not row[-1]:
            row.pop()
    return rows


def _detect_delimiter(text: str) -> str | None:
    """Pick the candidate that consistently splits the leading lines, if any does."""
    lines = [line for line in text.splitlines()[:_SAMPLE_LINES] if line.strip()]
    if not lines:
        return None
    typical_count = {
        delimiter: statistics.median_low(line.count(delimiter) for line in lines)
        for delimiter in _DELIMITERS
    }
    best = max(_DELIMITERS, key=typical_count.__getitem__)
    return best if typical_count[best] > 0 else None


def _column_names(header: list[str] | None, width: int) -> list[str]:
    if header is None:
        if width == 1:
            return [DEFAULT_DOMAIN_COLUMN]
        return [f"Column {position}" for position in range(1, width + 1)]

    names: list[str] = []
    for position in range(1, width + 1):
        name = header[position - 1] if position <= len(header) else ""
        name = name or f"Column {position}"
        # Duplicate headers would make column selection ambiguous.
        candidate, suffix = name, 2
        while candidate in names:
            candidate = f"{name} ({suffix})"
            suffix += 1
        names.append(candidate)
    return names


def _check_row_limit(row_count: int) -> None:
    if row_count > MAX_INPUT_ROWS:
        raise InputError(
            f"The input has {row_count:,} rows, which exceeds the limit of "
            f"{MAX_INPUT_ROWS:,}. Split it into smaller batches."
        )

from __future__ import annotations

import io

import pandas as pd
import pytest

from traffic_checker.ingest import InputError, prepare_input, read_table
from traffic_checker.models import LookupResult, LookupStatus
from traffic_checker.report import (
    STATUS_COLUMN,
    STATUS_INVALID,
    STATUS_PENDING,
    VISITS_COLUMN,
    Summary,
    build_report,
    refine_report,
    restore_results,
    summarize,
    to_csv_bytes,
    traffic_tiers,
)


def ok(domain: str, visits: int) -> LookupResult:
    return LookupResult(domain, LookupStatus.OK, visits=visits)


@pytest.fixture
def prepared():
    table = read_table(
        b"Company,Site\n"
        b"Big,https://www.big.com/\n"
        b"Small,small.com\n"
        b"Big again,big.com\n"
        b"Unknown,unknown.com\n"
        b"Broken,broken.com\n"
        b"Later,later.com\n"
        b"Junk,not a domain\n"
    )
    return prepare_input(table, "Site")


@pytest.fixture
def results():
    return {
        "big.com": ok("big.com", 5_000_000),
        "small.com": ok("small.com", 800),
        "unknown.com": LookupResult("unknown.com", LookupStatus.NO_DATA),
        "broken.com": LookupResult("broken.com", LookupStatus.HTTP_ERROR, detail="HTTP 502"),
        "other-input.com": ok("other-input.com", 1),
    }


def test_report_joins_results_onto_every_input_row(prepared, results):
    report = build_report(prepared, results)

    assert list(report.columns) == ["Company", "Site", VISITS_COLUMN, STATUS_COLUMN]
    assert report["Site"].tolist() == prepared.table["Site"].tolist()
    visits = [None if pd.isna(value) else int(value) for value in report[VISITS_COLUMN]]
    assert visits == [5_000_000, 800, 5_000_000, None, None, None, None]
    assert report[STATUS_COLUMN].tolist() == [
        "OK",
        "OK",
        "OK",
        "No data",
        "HTTP 502",
        STATUS_PENDING,
        STATUS_INVALID,
    ]


def test_report_without_any_result(prepared):
    report = build_report(prepared, {})

    assert report[VISITS_COLUMN].isna().all()
    assert set(report[STATUS_COLUMN]) == {STATUS_PENDING, STATUS_INVALID}


def test_report_does_not_mutate_the_input_table(prepared, results):
    build_report(prepared, results)

    assert list(prepared.table.columns) == ["Company", "Site"]


def test_stale_result_columns_in_the_input_are_replaced(results):
    table = read_table(b"Site,Visits,Status\nbig.com,1,Timeout\n")

    report = build_report(prepare_input(table, "Site"), results)

    assert list(report.columns) == ["Site", VISITS_COLUMN, STATUS_COLUMN]
    assert report.iloc[0].tolist() == ["big.com", 5_000_000, "OK"]


def test_summary_counts_distinct_domains(prepared, results):
    assert summarize(prepared, results) == Summary(resolved=2, no_data=1, failed=1, pending=1)


def test_refine_defaults_keep_the_report_as_is(prepared, results):
    report = build_report(prepared, results)

    pd.testing.assert_frame_equal(refine_report(report), report)


def test_threshold_hides_small_and_unknown_rows(prepared, results):
    report = build_report(prepared, results)

    refined = refine_report(report, min_visits=1_000)

    assert refined["Company"].tolist() == ["Big", "Big again"]


def test_row_filters_split_the_report(prepared, results):
    report = build_report(prepared, results)

    resolved = refine_report(report, rows="resolved")
    unresolved = refine_report(report, rows="unresolved")

    assert resolved["Company"].tolist() == ["Big", "Small", "Big again"]
    assert unresolved["Company"].tolist() == ["Unknown", "Broken", "Later", "Junk"]


def test_sorting_puts_highest_traffic_first_and_unknown_last(prepared, results):
    report = build_report(prepared, results)

    refined = refine_report(report, sort_by_visits=True)

    assert refined["Company"].tolist() == [
        "Big",
        "Big again",
        "Small",
        "Unknown",
        "Broken",
        "Later",
        "Junk",
    ]


def test_traffic_tiers_bucket_by_order_of_magnitude():
    tiers = traffic_tiers([0, 999, 1_000, 50_000, 2_000_000, 10_000_000, 900_000_000])

    assert tiers["Domains"].tolist() == [2, 1, 1, 0, 1, 2]
    assert tiers["Share"].sum() == pytest.approx(1.0)


def test_traffic_tiers_of_nothing_are_all_zero():
    tiers = traffic_tiers([])

    assert tiers["Domains"].tolist() == [0] * 6
    assert tiers["Share"].tolist() == [0.0] * 6


def test_csv_export_round_trips(prepared, results):
    report = build_report(prepared, results)

    exported = pd.read_csv(io.BytesIO(to_csv_bytes(report)))

    assert list(exported.columns) == list(report.columns)
    assert exported[VISITS_COLUMN].iloc[0] == 5_000_000
    assert pd.isna(exported[VISITS_COLUMN].iloc[3])


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ('=HYPERLINK("http://evil.test","click")', '\'=HYPERLINK("http://evil.test","click")'),
        ("@SUM(1+1)", "'@SUM(1+1)"),
        ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
        ("-2+3+cmd|' /C calc'!A0", "'-2+3+cmd|' /C calc'!A0"),
        ("+380 44 123-45-67", "+380 44 123-45-67"),
        ("-12.5", "-12.5"),
        ("example.com", "example.com"),
        ("", ""),
    ],
)
def test_csv_export_neutralizes_formulas(cell, expected):
    report = pd.DataFrame({"Note": [cell], VISITS_COLUMN: pd.array([1], dtype="Int64")})

    exported = pd.read_csv(io.BytesIO(to_csv_bytes(report)), dtype=str, keep_default_na=False)

    assert exported["Note"].iloc[0] == expected
    assert report["Note"].iloc[0] == cell


def test_restore_recovers_only_definitive_results(prepared, results):
    export = to_csv_bytes(build_report(prepared, results))

    restored = restore_results(read_table(export))

    assert restored == {
        "big.com": ok("big.com", 5_000_000),
        "small.com": ok("small.com", 800),
        "unknown.com": LookupResult("unknown.com", LookupStatus.NO_DATA),
    }


def test_restore_ignores_ok_rows_without_a_number():
    table = read_table(b"Domain,Visits,Status\nexample.com,,OK\nexample.org,abc,OK\n")

    assert restore_results(table) == {}


def test_restore_rejects_files_that_are_not_exports():
    with pytest.raises(InputError, match="not a results export"):
        restore_results(read_table(b"Domain\nexample.com\n"))


def test_restore_rejects_exports_without_a_domain_column():
    with pytest.raises(InputError, match="no domain column"):
        restore_results(pd.DataFrame({VISITS_COLUMN: ["1"], STATUS_COLUMN: ["OK"]}))

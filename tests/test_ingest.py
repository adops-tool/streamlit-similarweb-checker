from __future__ import annotations

import pytest

from traffic_checker import ingest
from traffic_checker.ingest import (
    InputError,
    guess_domain_column,
    prepare_input,
    read_table,
    table_from_text,
)


def test_single_column_file_without_header():
    table = read_table(b"example.com\nexample.org\n")

    assert list(table.columns) == ["Domain"]
    assert table["Domain"].tolist() == ["example.com", "example.org"]


@pytest.mark.parametrize("header", ["domain", "URL", "Website", "Сайт"])
def test_header_row_is_detected(header):
    table = read_table(f"{header}\nexample.com\n".encode())

    assert list(table.columns) == [header]
    assert table[header].tolist() == ["example.com"]


def test_first_row_holding_a_domain_is_data():
    table = read_table(b"Acme,acme.com\nGlobex,globex.com\n")

    assert list(table.columns) == ["Column 1", "Column 2"]
    assert len(table) == 2


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_delimiter_is_detected(delimiter):
    content = delimiter.join(["Company", "Site"]) + "\n" + delimiter.join(["Acme", "acme.com"])

    table = read_table(content.encode())

    assert list(table.columns) == ["Company", "Site"]
    assert table.iloc[0].tolist() == ["Acme", "acme.com"]


def test_commas_inside_urls_do_not_split_a_single_column_file():
    table = read_table(b"example.com\nexample.org/path?ids=1,2,3\nexample.net\n")

    assert table.shape[0] == 3
    assert guess_domain_column(table) == "Domain"


def test_trailing_delimiters_do_not_create_empty_columns():
    table = read_table(b"example.com,\nexample.org,\n")

    assert list(table.columns) == ["Domain"]


def test_ragged_rows_are_padded():
    table = read_table(b"Site,Note\nexample.com\nexample.org,keep,extra\n")

    assert list(table.columns) == ["Site", "Note", "Column 3"]
    assert table.iloc[0].tolist() == ["example.com", "", ""]
    assert table.iloc[1].tolist() == ["example.org", "keep", "extra"]


def test_blank_lines_and_surrounding_whitespace_are_dropped():
    table = read_table(b"\n  example.com  \n\n   \nexample.org\n")

    assert table["Domain"].tolist() == ["example.com", "example.org"]


def test_duplicate_and_empty_headers_are_made_unique():
    table = read_table(b"Site,Site,,Note\nexample.com,a,b,c\n")

    assert list(table.columns) == ["Site", "Site (2)", "Column 3", "Note"]


@pytest.mark.parametrize(
    "encoded",
    [
        "Сайт,Заметка\nexample.com,привет\n".encode(),
        "Сайт,Заметка\nexample.com,привет\n".encode("utf-8-sig"),
        "Сайт,Заметка\nexample.com,привет\n".encode("cp1251"),
        "Сайт,Заметка\nexample.com,привет\n".encode("utf-16"),
    ],
    ids=["utf-8", "utf-8-bom", "cp1251", "utf-16"],
)
def test_common_encodings_are_decoded(encoded):
    table = read_table(encoded)

    assert list(table.columns) == ["Сайт", "Заметка"]
    assert table.iloc[0].tolist() == ["example.com", "привет"]


@pytest.mark.parametrize("data", [b"", b"   \n\n", b",,\n"])
def test_empty_file_is_rejected(data):
    with pytest.raises(InputError, match="empty"):
        read_table(data)


def test_header_only_file_is_rejected():
    with pytest.raises(InputError, match="no domains"):
        read_table(b"domain\n")


def test_binary_file_is_rejected():
    with pytest.raises(InputError, match="text"):
        read_table(b"PK\x03\x04\x00\x00\x00binary")


def test_row_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_INPUT_ROWS", 2)

    with pytest.raises(InputError, match="limit"):
        read_table(b"a.com\nb.com\nc.com\n")
    with pytest.raises(InputError, match="limit"):
        table_from_text("a.com b.com c.com")


def test_pasted_text_accepts_mixed_separators():
    table = table_from_text("a.com, b.com;c.com\n\n  d.com\te.com ")

    assert table["Domain"].tolist() == ["a.com", "b.com", "c.com", "d.com", "e.com"]


def test_blank_pasted_text_is_rejected():
    with pytest.raises(InputError):
        table_from_text(" \n ,; ")


def test_domain_column_is_guessed_by_content():
    table = read_table(b"Company,Contact,Site\nAcme,joe@acme.com,acme.com\nGlobex,,globex.com\n")

    assert guess_domain_column(table) == "Site"


def test_domain_column_guess_prefers_leftmost_on_ties():
    table = read_table(b"a.com,b.com\nc.com,d.com\n")

    assert guess_domain_column(table) == "Column 1"


def test_prepare_input_normalizes_and_deduplicates():
    table = table_from_text(
        "https://www.example.com/a example.com not-a-domain m.example.com example.org"
    )

    prepared = prepare_input(table, "Domain")

    assert prepared.normalized.tolist() == [
        "example.com",
        "example.com",
        None,
        "example.com",
        "example.org",
    ]
    assert prepared.unique_domains == ("example.com", "example.org")
    assert prepared.row_count == 5
    assert prepared.invalid_count == 1
    assert prepared.duplicate_count == 2


def test_prepare_input_can_keep_mobile_hosts_apart():
    table = table_from_text("example.com m.example.com")

    prepared = prepare_input(table, "Domain", strip_mobile_prefix=False)

    assert prepared.unique_domains == ("example.com", "m.example.com")

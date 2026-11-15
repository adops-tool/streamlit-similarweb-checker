import pytest

from traffic_checker.parsing import extract_visits


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"visits": 1200}, 1200),
        ({"Visits": 1200}, 1200),
        ({"VISITS": 1234.9}, 1234),
        ({"monthly_visits": "5,000"}, 5000),
        ({"total_visits": "42"}, 42),
        ({"traffic": 0}, 0),
        ({"Engagements": {"Visits": 77}}, 77),
        ({"Engagments": {"Visits": "88.5"}}, 88),
        ({"data": {"monthly_visits": 9}}, 9),
        ({"result": {"traffic": 3}}, 3),
    ],
)
def test_extracts_visits(payload, expected):
    assert extract_visits(payload) == expected


def test_top_level_field_wins_over_nested():
    assert extract_visits({"data": {"visits": 1}, "visits": 2}) == 2


def test_key_priority_is_respected():
    assert extract_visits({"traffic": 1, "visits": 2}) == 2


def test_unusable_candidate_falls_through_to_next():
    assert extract_visits({"visits": None, "data": {"visits": 5}}) == 5


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "visits",
        {},
        {"visits": None},
        {"visits": True},
        {"visits": "n/a"},
        {"visits": -5},
        {"visits": float("nan")},
        {"visits": float("inf")},
        {"visits": {"value": 5}},
        {"unrelated": {"visits": 5}},
        {"SimilarSites": [{"Visits": 5}]},
    ],
)
def test_returns_none_without_a_usable_figure(payload):
    assert extract_visits(payload) is None

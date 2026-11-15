"""End-to-end checks of the Streamlit script with the network layer stubbed out."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from traffic_checker.batch import BatchOutcome
from traffic_checker.config import API_KEY_ENV_VAR
from traffic_checker.models import LookupResult, LookupStatus, Quota

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
API_KEY = "test-key-0123456789"

VISITS = {"big.com": 5_000_000, "small.com": 800}


@pytest.fixture(autouse=True)
def no_provisioned_key(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


@pytest.fixture
def fake_api(monkeypatch):
    """Replace the batch runner; domains outside ``VISITS`` time out."""
    calls: list[list[str]] = []

    async def lookup_domains(api_key, domains, settings=None, on_result=None):
        assert api_key == API_KEY
        calls.append(list(domains))
        results = [
            LookupResult(domain, LookupStatus.OK, visits=VISITS[domain])
            if domain in VISITS
            else LookupResult(domain, LookupStatus.TIMEOUT)
            for domain in domains
        ]
        for result in results:
            on_result(result)
        return BatchOutcome(results=results, quota=Quota(remaining=97, limit=100))

    monkeypatch.setattr("traffic_checker.batch.lookup_domains", lookup_domains)
    return calls


def launch() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=30).run()
    assert not app.exception
    return app


def button(app: AppTest, label: str):
    return next(widget for widget in app.button if widget.label == label)


def paste(app: AppTest, text: str) -> AppTest:
    app.radio[0].set_value("Paste a list").run()
    app.text_area[0].set_value(text).run()
    assert not app.exception
    return app


def metrics(app: AppTest) -> dict[str, str]:
    return {metric.label: metric.value for metric in app.metric}


def test_app_starts_without_input():
    app = launch()

    assert app.title[0].value == "DataLoom Traffic Checker"
    assert not app.metric


def test_input_summary_reports_duplicates_and_invalid_rows():
    app = paste(launch(), "https://www.big.com/ big.com small.com not-a-domain")

    assert metrics(app) == {
        "Rows": "4",
        "Unique domains": "2",
        "Duplicates": "1",
        "Invalid": "1",
    }


def test_lookup_cannot_start_without_a_key():
    app = paste(launch(), "big.com")

    assert button(app, "Start lookup").disabled
    assert any("RapidAPI key" in info.value for info in app.info)


def test_malformed_key_is_reported_and_blocks_the_run():
    app = paste(launch(), "big.com")
    app.sidebar.text_input[0].set_value("bad key").run()

    assert app.sidebar.error
    assert button(app, "Start lookup").disabled


def test_provisioned_key_enables_the_run(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV_VAR, API_KEY)

    app = paste(launch(), "big.com")

    assert not button(app, "Start lookup").disabled


def test_lookup_fills_the_report_and_retries_only_failures(fake_api):
    app = paste(launch(), "big.com small.com flaky.com big.com")
    app.sidebar.text_input[0].set_value(API_KEY).run()

    button(app, "Start lookup").click().run()

    assert not app.exception
    assert fake_api == [["big.com", "small.com", "flaky.com"]]
    assert metrics(app)["Resolved"] == "2"
    assert metrics(app)["Failed"] == "1"
    assert any("1 failed" in warning.value for warning in app.warning)

    report = app.dataframe[0].value
    assert report["Visits"].tolist()[:2] == [5_000_000, 800]
    assert report["Status"].tolist() == ["OK", "OK", "Timeout", "OK"]

    # A second run must spend requests on the failed domain only.
    button(app, "Resume lookup").click().run()

    assert fake_api[1] == ["flaky.com"]


def test_threshold_filters_the_report(fake_api):
    app = paste(launch(), "big.com small.com")
    app.sidebar.text_input[0].set_value(API_KEY).run()
    button(app, "Start lookup").click().run()

    app.number_input[0].set_value(1_000).run()

    assert app.dataframe[0].value["Domain"].tolist() == ["big.com"]
    assert any("All 2 domains are resolved" in success.value for success in app.success)


def test_clearing_the_cache_resets_the_session(fake_api):
    app = paste(launch(), "big.com")
    app.sidebar.text_input[0].set_value(API_KEY).run()
    button(app, "Start lookup").click().run()

    button(app, "Clear cached results").click().run()

    assert "Resolved" not in metrics(app)
    assert button(app, "Start lookup")

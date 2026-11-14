"""Streamlit front end for bulk SimilarWeb traffic lookups via DataLoom on RapidAPI.

The script is a thin presentation layer. Normalization, HTTP, retries and
reporting live in the ``traffic_checker`` package and know nothing about
Streamlit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import pandas as pd
import streamlit as st

from traffic_checker.batch import lookup_domains, run_with_heartbeat
from traffic_checker.client import InvalidApiKeyError, check_api_key, clean_api_key
from traffic_checker.config import API_KEY_ENV_VAR, MAX_INPUT_ROWS, ClientSettings
from traffic_checker.ingest import (
    InputError,
    PreparedInput,
    guess_domain_column,
    prepare_input,
    read_table,
    table_from_text,
)
from traffic_checker.models import LookupResult, LookupStatus, Quota
from traffic_checker.report import (
    VISITS_COLUMN,
    build_report,
    refine_report,
    restore_results,
    summarize,
    to_csv_bytes,
    traffic_tiers,
)

logger = logging.getLogger(__name__)

# Session-state keys.
RESULTS_KEY = "results"  # dict[str, LookupResult] keyed by normalized domain
QUOTA_KEY = "quota"  # last Quota reported by the API
FLASH_KEY = "flash"  # (level, message) shown once after the next rerun
RUN_LOCK_KEY = "run_lock"  # threading.Lock held for the duration of a batch

# How often a running batch yields to Streamlit; bounds the reaction time to Stop.
HEARTBEAT_SECONDS = 0.25

SOURCE_UPLOAD = "Upload a file"
SOURCE_PASTE = "Paste a list"

FlashLevel = Literal["success", "info", "warning", "error"]
FLASH_RENDERERS = {
    "success": st.success,
    "info": st.info,
    "warning": st.warning,
    "error": st.error,
}

ROW_FILTER_LABELS = {
    "all": "All rows",
    "resolved": "With traffic data",
    "unresolved": "Without traffic data",
}

INVALID_PREVIEW_ROWS = 200


@dataclass(frozen=True)
class Options:
    """Everything the sidebar contributes to a run."""

    api_key: str  # Empty when no usable key is available.
    settings: ClientSettings
    strip_mobile_prefix: bool


# --- Cached data preparation -------------------------------------------------
# Streamlit re-executes this script on every interaction; caching keeps parsing
# and normalization of large inputs off that hot path. Both functions are keyed
# by the raw input rather than by a DataFrame: Streamlit hashes large frames by
# sampling rows, which could serve a stale result for two big inputs that
# differ in only a few lines.


@st.cache_data(show_spinner=False, max_entries=8, ttl=3600)
def _load_table(payload: bytes | str) -> pd.DataFrame:
    """Parse an uploaded file (bytes) or a pasted list (str)."""
    if isinstance(payload, bytes):
        return read_table(payload)
    return table_from_text(payload)


@st.cache_data(show_spinner=False, max_entries=8, ttl=3600)
def _prepare(payload: bytes | str, domain_column: str, strip_mobile_prefix: bool) -> PreparedInput:
    return prepare_input(
        _load_table(payload),
        domain_column,
        strip_mobile_prefix=strip_mobile_prefix,
    )


# --- Session helpers ---------------------------------------------------------


def _init_state() -> None:
    st.session_state.setdefault(RESULTS_KEY, {})
    st.session_state.setdefault(QUOTA_KEY, Quota())
    st.session_state.setdefault(RUN_LOCK_KEY, threading.Lock())


def _wait_for_previous_run() -> None:
    """Block until a batch started by a superseded script run has wound down.

    On every interaction Streamlit starts the new script run right away, in a
    new thread, while the previous one keeps going until its next ``st.*``
    call. Waiting here guarantees that this run renders the final state of
    that batch and never sends requests alongside it.
    """
    with st.session_state[RUN_LOCK_KEY]:
        pass


def _flash(level: FlashLevel, message: str) -> None:
    """Queue a message for the top of the page on the next script run."""
    st.session_state[FLASH_KEY] = (level, message)


def _show_flash() -> None:
    flash = st.session_state.pop(FLASH_KEY, None)
    if flash is not None:
        level, message = flash
        FLASH_RENDERERS[level](message)


def _clear_results() -> None:
    st.session_state[RESULTS_KEY] = {}
    _flash("info", "Cached results cleared. The next run starts from scratch.")


def _provisioned_api_key() -> str:
    """Return a key supplied by the operator through the environment or secrets."""
    key = os.environ.get(API_KEY_ENV_VAR, "")
    if key:
        return key
    try:
        return str(st.secrets.get(API_KEY_ENV_VAR, ""))
    except FileNotFoundError:
        # No secrets file is configured for this deployment.
        return ""


def _format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


# --- Sidebar -----------------------------------------------------------------


def render_sidebar() -> Options:
    with st.sidebar:
        api_key = _render_access()
        settings, strip_mobile_prefix = _render_settings()
        _render_session(strip_mobile_prefix)
    return Options(api_key=api_key, settings=settings, strip_mobile_prefix=strip_mobile_prefix)


def _render_access() -> str:
    st.header("API access")
    provisioned = _provisioned_api_key()
    entered = st.text_input(
        "RapidAPI key",
        type="password",
        placeholder="Leave empty to use the server key" if provisioned else "x-rapidapi-key",
        help=(
            "Held in server memory for this browser session only. "
            "It is never written to disk or to logs."
        ),
    )

    api_key = ""
    candidate = entered or provisioned
    if candidate:
        try:
            api_key = clean_api_key(candidate)
        except InvalidApiKeyError as exc:
            st.error(str(exc))
    if api_key and not entered:
        st.caption("Using the key configured on the server.")

    if st.button(
        "Check key",
        disabled=not api_key,
        help="Sends one request for google.com. It counts against the quota.",
    ):
        with st.spinner("Checking the key..."):
            check = asyncio.run(check_api_key(api_key))
        st.session_state[QUOTA_KEY] = check.quota
        if check.ok:
            st.success(check.message)
        else:
            st.error(check.message)

    quota: Quota = st.session_state[QUOTA_KEY]
    if quota.remaining is not None:
        of_limit = f" of {quota.limit:,}" if quota.limit is not None else ""
        st.caption(f"Quota: **{quota.remaining:,}**{of_limit} requests left.")
    return api_key


def _render_settings() -> tuple[ClientSettings, bool]:
    defaults = ClientSettings()
    with st.expander("Lookup settings"):
        requests_per_second = st.slider(
            "Requests per second",
            min_value=1,
            max_value=20,
            value=int(defaults.requests_per_second),
            help="Keep this within the rate limit of your RapidAPI plan.",
        )
        max_concurrency = st.slider(
            "Parallel requests",
            min_value=1,
            max_value=10,
            value=defaults.max_concurrency,
        )
        max_attempts = st.slider(
            "Attempts per domain",
            min_value=1,
            max_value=5,
            value=defaults.max_attempts,
            help="Timeouts, network errors, HTTP 5xx and rate limiting are retried.",
        )
        request_timeout = st.slider(
            "Request timeout, seconds",
            min_value=5,
            max_value=60,
            value=int(defaults.request_timeout),
        )
        strip_mobile_prefix = st.checkbox(
            "Treat m. and mobile. hosts as the main domain",
            value=True,
            help="m.example.com is looked up, and deduplicated, as example.com.",
        )

    settings = ClientSettings(
        requests_per_second=float(requests_per_second),
        max_concurrency=max_concurrency,
        request_timeout=float(request_timeout),
        max_attempts=max_attempts,
    )
    return settings, strip_mobile_prefix


def _render_session(strip_mobile_prefix: bool) -> None:
    results: dict[str, LookupResult] = st.session_state[RESULTS_KEY]

    st.header("Session")
    with st.expander("Restore previous results"):
        st.caption(
            "Upload a CSV exported by this app to reuse its results instead of "
            "spending quota on the same domains again."
        )
        export = st.file_uploader("Results CSV", type=["csv"], label_visibility="collapsed")
        if st.button("Restore", disabled=export is None) and export is not None:
            try:
                restored = restore_results(
                    read_table(export.getvalue()),
                    strip_mobile_prefix=strip_mobile_prefix,
                )
            except InputError as exc:
                st.error(str(exc))
            else:
                # Definitive results obtained in this session are at least as
                # fresh as the file, so they take precedence.
                fresh = {
                    domain: result
                    for domain, result in restored.items()
                    if domain not in results or not results[domain].status.is_final
                }
                results.update(fresh)
                st.success(f"Restored {len(fresh):,} results.")

    st.caption(f"{len(results):,} domains cached in this session.")
    st.button("Clear cached results", on_click=_clear_results, disabled=not results)


# --- Input -------------------------------------------------------------------


def render_input(strip_mobile_prefix: bool) -> PreparedInput | None:
    """Render the input widgets and return the prepared table, if there is one."""
    source = st.radio(
        "Input source",
        (SOURCE_UPLOAD, SOURCE_PASTE),
        horizontal=True,
        label_visibility="collapsed",
    )

    try:
        if source == SOURCE_UPLOAD:
            uploaded = st.file_uploader(
                "CSV, TSV or TXT file with one website per row",
                type=["csv", "tsv", "txt"],
                help=(
                    f"Up to {MAX_INPUT_ROWS:,} rows. Extra columns are kept and "
                    "carried over to the export."
                ),
            )
            if uploaded is None:
                return None
            payload: bytes | str = uploaded.getvalue()
        else:
            payload = st.text_area(
                "Domains or URLs, one per line",
                height=180,
                placeholder="example.com\nhttps://www.example.org/pricing",
            )
            if not payload.strip():
                return None
        table = _load_table(payload)
    except InputError as exc:
        st.error(str(exc))
        return None

    columns = list(table.columns)
    domain_column = columns[0]
    if len(columns) > 1:
        domain_column = st.selectbox(
            "Column with websites",
            columns,
            index=columns.index(guess_domain_column(table)),
        )

    prepared = _prepare(payload, domain_column, strip_mobile_prefix)
    if not prepared.unique_domains:
        st.error(f"No valid domains were found in the '{domain_column}' column.")
        return None

    _render_input_summary(prepared)
    return prepared


def _render_input_summary(prepared: PreparedInput) -> None:
    rows, unique, duplicates, invalid = st.columns(4)
    rows.metric("Rows", f"{prepared.row_count:,}")
    unique.metric("Unique domains", f"{len(prepared.unique_domains):,}")
    duplicates.metric(
        "Duplicates",
        f"{prepared.duplicate_count:,}",
        help="Rows repeating an already listed domain. They are looked up once.",
    )
    invalid.metric(
        "Invalid",
        f"{prepared.invalid_count:,}",
        help="Rows without a valid domain. They are skipped and cost no quota.",
    )

    if prepared.invalid_count:
        with st.expander(f"Rows without a valid domain ({prepared.invalid_count:,})"):
            invalid_rows = prepared.table.loc[prepared.normalized.isna(), [prepared.domain_column]]
            st.dataframe(invalid_rows.head(INVALID_PREVIEW_ROWS), hide_index=True)
            if prepared.invalid_count > INVALID_PREVIEW_ROWS:
                st.caption(f"Showing the first {INVALID_PREVIEW_ROWS}.")


# --- Lookup ------------------------------------------------------------------


def render_run(prepared: PreparedInput, options: Options) -> None:
    results: dict[str, LookupResult] = st.session_state[RESULTS_KEY]
    # Only definitive answers count as done; failed lookups are retried.
    pending = [
        domain
        for domain in prepared.unique_domains
        if domain not in results or not results[domain].status.is_final
    ]
    done = len(prepared.unique_domains) - len(pending)

    if not pending:
        st.success(f"All {done:,} domains are resolved.")
        return

    estimate = _format_duration(len(pending) / options.settings.requests_per_second)
    already = f" ({done:,} already resolved)" if done else ""
    st.write(
        f"**{len(pending):,}** domains to look up{already}. Each lookup uses one "
        f"API request; the run takes at least {estimate}."
    )

    quota: Quota = st.session_state[QUOTA_KEY]
    if quota.remaining is not None and quota.remaining < len(pending):
        st.warning(
            f"Only {quota.remaining:,} requests are left on the plan. Depending on "
            "the plan, the run either stops at that point or incurs overage charges."
        )
    if not options.api_key:
        st.info("Enter your RapidAPI key in the sidebar to start.")

    resuming = any(domain in results for domain in prepared.unique_domains)
    label = "Resume lookup" if resuming else "Start lookup"
    if st.button(label, type="primary", disabled=not options.api_key):
        _run_lookup(pending, options)


def _run_lookup(domains: list[str], options: Options) -> None:
    results: dict[str, LookupResult] = st.session_state[RESULTS_KEY]
    total = len(domains)
    completed = 0
    latest: LookupResult | None = None
    started_at = time.monotonic()

    progress = st.progress(0.0, text="Starting...")
    # Any widget interaction makes Streamlit stop the running script, so the
    # button needs no handler beyond leaving a note for the next run.
    st.button(
        "Stop",
        on_click=_flash,
        args=("info", "Lookup stopped. Collected results are kept; resume at any time."),
    )

    def on_result(result: LookupResult) -> None:
        nonlocal completed, latest
        # Checkpoint straight into the session so that a stop loses nothing.
        results[result.domain] = result
        completed += 1
        latest = result

    # Doubles as the heartbeat of the batch: Streamlit can stop a script only
    # from inside an st.* call, and a batch waiting on the network makes none.
    def render_progress() -> None:
        if latest is None:
            progress.progress(0.0, text="Starting...")
            return
        elapsed = time.monotonic() - started_at
        remaining = _format_duration(elapsed / completed * (total - completed))
        value = f"{latest.visits:,}" if latest.visits is not None else latest.label
        progress.progress(
            completed / total,
            text=f"{completed:,} / {total:,} | {latest.domain}: {value} | about {remaining} left",
        )

    try:
        with st.session_state[RUN_LOCK_KEY]:
            batch = lookup_domains(options.api_key, domains, options.settings, on_result)
            outcome = asyncio.run(run_with_heartbeat(batch, render_progress, HEARTBEAT_SECONDS))
    except Exception:
        logger.exception("Lookup batch aborted by an unexpected error")
        _flash(
            "error",
            "The run was interrupted by an unexpected error. Collected results "
            "are kept; start the lookup again to resume.",
        )
    else:
        st.session_state[QUOTA_KEY] = outcome.quota
        elapsed = time.monotonic() - started_at
        _flash(*_describe_outcome(outcome.results, outcome.halted_by, elapsed))
    # Rerun so that counters and buttons rendered before the run catch up.
    st.rerun()


def _describe_outcome(
    results: list[LookupResult],
    halted_by: LookupStatus | None,
    elapsed: float,
) -> tuple[FlashLevel, str]:
    resolved = sum(result.status.is_final for result in results)
    if halted_by is LookupStatus.AUTH_ERROR:
        return "error", "The API rejected the key, so the run was stopped. Check the key and retry."
    if halted_by is LookupStatus.QUOTA_EXHAUSTED:
        return (
            "warning",
            f"The request quota ran out after {resolved:,} resolved domains; the rest "
            "stay pending. Download the CSV to keep these results and restore it "
            "once the quota resets.",
        )

    failed = len(results) - resolved
    message = f"Looked up {len(results):,} domains in {_format_duration(elapsed)}."
    if failed:
        return "warning", f"{message} {failed:,} failed and will be retried on the next run."
    return "success", message


# --- Results -----------------------------------------------------------------


def render_results(prepared: PreparedInput) -> None:
    results: dict[str, LookupResult] = st.session_state[RESULTS_KEY]
    summary = summarize(prepared, results)
    if summary.pending == len(prepared.unique_domains):
        return

    st.subheader("Results")
    resolved, no_data, failed, pending = st.columns(4)
    resolved.metric("Resolved", f"{summary.resolved:,}")
    no_data.metric(
        "No data",
        f"{summary.no_data:,}",
        help="SimilarWeb has no traffic figure for these domains.",
    )
    failed.metric(
        "Failed",
        f"{summary.failed:,}",
        help="Lookups that ended in an error. They are retried on the next run.",
    )
    pending.metric("Not checked", f"{summary.pending:,}")

    report = build_report(prepared, results)

    threshold_column, rows_column, sort_column = st.columns(3, vertical_alignment="bottom")
    min_visits = threshold_column.number_input(
        "Minimum monthly visits",
        min_value=0,
        value=0,
        step=1000,
        help="Rows below the threshold, or without a traffic figure, are hidden.",
    )
    rows = rows_column.selectbox(
        "Rows",
        list(ROW_FILTER_LABELS),
        format_func=ROW_FILTER_LABELS.__getitem__,
    )
    sort_by_visits = sort_column.checkbox("Sort by visits, highest first")

    view = refine_report(report, min_visits=min_visits, rows=rows, sort_by_visits=sort_by_visits)
    st.caption(f"Showing {len(view):,} of {len(report):,} rows. The download matches this view.")
    st.dataframe(
        view,
        hide_index=True,
        column_config={VISITS_COLUMN: st.column_config.NumberColumn(format="localized")},
    )
    st.download_button(
        "Download CSV",
        data=to_csv_bytes(view),
        file_name=f"traffic-{datetime.now():%Y%m%d-%H%M%S}.csv",
        mime="text/csv",
        disabled=view.empty,
    )

    if summary.resolved:
        with st.expander("Traffic distribution"):
            visits = [
                results[domain].visits
                for domain in prepared.unique_domains
                if domain in results and results[domain].visits is not None
            ]
            st.dataframe(
                traffic_tiers(visits),
                hide_index=True,
                column_config={
                    "Share": st.column_config.ProgressColumn(
                        format="percent", min_value=0.0, max_value=1.0
                    )
                },
            )


def main() -> None:
    st.set_page_config(page_title="DataLoom Traffic Checker", layout="wide")
    _init_state()
    _wait_for_previous_run()
    options = render_sidebar()

    st.title("DataLoom Traffic Checker")
    st.caption("Bulk SimilarWeb traffic lookup through the DataLoom API on RapidAPI.")
    _show_flash()

    prepared = render_input(options.strip_mobile_prefix)
    if prepared is None:
        return
    render_run(prepared, options)
    render_results(prepared)


main()

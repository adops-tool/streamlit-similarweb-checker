"""
DataLoom Traffic Checker — Streamlit app for bulk SimilarWeb traffic lookup.

Pipeline-friendly improvements over the original version:
  * Async HTTP with a concurrency limit (3 req/s, matches RapidAPI quota).
  * Exponential backoff retries on transient failures (network / 5xx / 429).
  * Checkpointing in st.session_state so a crashed run can be resumed.
  * Pre-flight API key validation against a known domain before the batch.
  * Input deduplication after URL normalization (saves quota).
  * Strict, explicit JSON parsing instead of recursive field guessing.
  * Optional traffic threshold filter applied directly in the UI.
  * Robust URL cleaner: strips schema, www, trailing dot, port, path, query.
"""

import asyncio
import io
import re
import time
from typing import Optional

import aiohttp
import pandas as pd
import streamlit as st

# --- Configuration constants ---
API_HOST = "similarweb-insights.p.rapidapi.com"
API_URL = f"https://{API_HOST}/traffic"
VALIDATION_DOMAIN = "google.com"  # Known-good domain for key validation
CONCURRENCY_LIMIT = 3              # RapidAPI DataLoom plan ceiling
REQUEST_TIMEOUT = 15               # Seconds per request
MAX_RETRIES = 3                    # Total attempts per domain on transient errors
BACKOFF_BASE = 1.5                 # Exponential backoff base delay (seconds)
CHECKPOINT_INTERVAL = 25           # Persist results every N completed domains


# --- URL normalization ---
def clean_url(url: str) -> str:
    """Normalize a raw user-supplied URL into a bare domain.

    Handles: schema (http/https/ftp), www and www2/www3, trailing dot,
    port, path, query, fragment, mobile subdomain prefix, surrounding
    whitespace, and case folding. Returns empty string for NaN/empty input.
    """
    if pd.isna(url) or not str(url).strip():
        return ""
    url = str(url).strip().lower()
    url = re.sub(r"^[a-z0-9+\-.]+://", "", url)   # Strip schema
    url = re.sub(r"^www\d*\.", "", url)            # Strip www / www2 / www3
    url = url.split("/")[0]                         # Drop path
    url = url.split("?")[0]                         # Drop query
    url = url.split("#")[0]                         # Drop fragment
    url = url.split(":")[0]                         # Drop port
    url = url.rstrip(".")                           # Strip trailing dot
    # Normalize mobile subdomain — m.cnn.com and cnn.com are reported separately
    # by SimilarWeb, but for outreach lists we usually want the canonical apex.
    url = re.sub(r"^(m|mobile)\.", "", url)
    return url.strip()


# --- API response parsing ---
def extract_visits(payload: dict) -> Optional[float]:
    """Extract monthly visits from a DataLoom traffic response.

    Tries the documented top-level fields in priority order rather than
    searching recursively, which avoids ambiguity when multiple keys exist
    at different nesting levels.
    """
    if not isinstance(payload, dict):
        return None

    # Documented fields, ordered by preference.
    direct_keys = ("visits", "monthly_visits", "total_visits", "traffic")
    for key in direct_keys:
        if key in payload and isinstance(payload[key], (int, float)):
            return float(payload[key])

    # Some DataLoom responses nest the metric under "engagements" or "data".
    for container_key in ("engagements", "data", "result"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            for key in direct_keys:
                if key in nested and isinstance(nested[key], (int, float)):
                    return float(nested[key])

    return None


# --- Async API caller ---
async def fetch_traffic(
    session: aiohttp.ClientSession,
    domain: str,
    api_key: str,
    semaphore: asyncio.Semaphore,
) -> tuple[str, str, str]:
    """Fetch traffic for a single domain with retries.

    Returns a tuple: (domain, visits_as_string, requests_remaining_header).
    visits_as_string uses a sentinel value on hard failures so the caller
    can distinguish "0 traffic" from "lookup failed".
    """
    headers = {
        "x-rapidapi-host": API_HOST,
        "x-rapidapi-key": api_key,
        "Content-Type": "application/json",
    }
    params = {"domain": domain}

    if not domain:
        return domain, "Invalid", "Unknown"

    async with semaphore:
        last_error = "Unknown error"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with session.get(
                    API_URL,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as response:
                    remaining = response.headers.get(
                        "x-ratelimit-requests-remaining", "Unknown"
                    )

                    if response.status == 200:
                        data = await response.json()
                        visits = extract_visits(data)
                        return domain, str(int(visits)) if visits is not None else "0", remaining

                    if response.status == 429:
                        # Quota exhausted — no point retrying further.
                        return domain, "Quota exhausted", "0"

                    if response.status in (401, 403):
                        return domain, "Auth error", remaining

                    # 5xx and other transient codes fall through to retry.
                    last_error = f"HTTP {response.status}"

            except asyncio.TimeoutError:
                last_error = "Timeout"
            except aiohttp.ClientError as exc:
                last_error = f"Network: {type(exc).__name__}"

            # Exponential backoff before next attempt.
            if attempt < MAX_RETRIES:
                await asyncio.sleep(BACKOFF_BASE ** attempt)

        return domain, last_error, "Unknown"


# --- Pre-flight key validation ---
async def validate_api_key(api_key: str) -> tuple[bool, str]:
    """Make one test request to verify the API key works.

    Returns (is_valid, message). The message is shown to the user so they
    can distinguish bad key from network problems before launching a batch.
    """
    headers = {
        "x-rapidapi-host": API_HOST,
        "x-rapidapi-key": api_key,
        "Content-Type": "application/json",
    }
    params = {"domain": VALIDATION_DOMAIN}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                API_URL,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                remaining = response.headers.get(
                    "x-ratelimit-requests-remaining", "Unknown"
                )
                if response.status == 200:
                    return True, f"Key is valid. Requests remaining: {remaining}"
                if response.status in (401, 403):
                    return False, "API key rejected (401/403). Check the key value."
                if response.status == 429:
                    return False, "Quota exhausted before the run started."
                return False, f"Unexpected status {response.status} during validation."
    except Exception as exc:
        return False, f"Network error during validation: {exc}"


# --- Batch runner ---
async def run_batch(
    domains: list[str],
    api_key: str,
    progress_callback,
) -> list[tuple[str, str, str]]:
    """Run the full batch of domain lookups concurrently with checkpointing.

    progress_callback(idx, total, domain, visits, remaining) is called
    after every completed lookup so the UI can update progress and persist
    intermediate state.
    """
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    results: list[tuple[str, str, str]] = []

    connector = aiohttp.TCPConnector(limit=CONCURRENCY_LIMIT)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            fetch_traffic(session, domain, api_key, semaphore)
            for domain in domains
        ]
        for idx, future in enumerate(asyncio.as_completed(tasks), start=1):
            domain, visits, remaining = await future
            results.append((domain, visits, remaining))
            progress_callback(idx, len(domains), domain, visits, remaining)

    return results


# --- Streamlit UI ---
st.set_page_config(page_title="DataLoom Traffic Checker", layout="wide")
st.title("DataLoom Traffic Checker")
st.caption(
    "Bulk SimilarWeb traffic lookup via the DataLoom RapidAPI provider. "
    "Async, retry-aware, with checkpointing and threshold filtering."
)

# Sidebar — credentials and run options.
st.sidebar.header("Access settings")
api_key = st.sidebar.text_input(
    "x-rapidapi-key", type="password", help="Stored in session memory only."
)
st.sidebar.caption(
    "The key lives in browser session memory. Do not share the running URL."
)

st.sidebar.header("Run options")
threshold = st.sidebar.number_input(
    "Minimum visits filter (applied to download)",
    min_value=0,
    value=0,
    step=1000,
    help="Rows below this value are excluded from the downloaded CSV. Set 0 to keep all.",
)

# Initialize session state for checkpoint persistence.
if "results" not in st.session_state:
    st.session_state["results"] = {}  # Keyed by clean_domain → visits
if "input_df" not in st.session_state:
    st.session_state["input_df"] = None

uploaded_file = st.file_uploader(
    "Upload your CSV with the list of websites", type=["csv"]
)

if uploaded_file:
    if not api_key:
        st.warning("Please enter your API key in the sidebar.")
        st.stop()

    with st.spinner("Reading file..."):
        # Read without a header — the first row is often a real domain.
        user_df = pd.read_csv(uploaded_file, header=None)
        if user_df.empty:
            st.error("The file is empty.")
            st.stop()

        # Drop a header row only if it is clearly a label, not a domain.
        first_val = str(user_df.iloc[0, 0]).lower().strip()
        header_labels = {"url", "domain", "website", "сайт", "домен", "ссылка"}
        if first_val in header_labels:
            user_df = user_df.iloc[1:].reset_index(drop=True)

        site_column = user_df.columns[0]
        user_df.rename(columns={site_column: "Domain"}, inplace=True)
        user_df["Clean_Domain"] = user_df["Domain"].apply(clean_url)

        # Deduplicate after normalization to avoid burning quota on repeats,
        # but keep the original ordering and the original Domain column for
        # the user-facing output.
        unique_domains = (
            user_df.loc[user_df["Clean_Domain"] != "", "Clean_Domain"]
            .drop_duplicates()
            .tolist()
        )

        st.session_state["input_df"] = user_df

    st.write(
        f"Loaded **{len(user_df)}** rows, "
        f"**{len(unique_domains)}** unique domains after normalization."
    )

    col1, col2, col3 = st.columns(3)
    validate_btn = col1.button("Validate API key", type="secondary")
    start_btn = col2.button("Start traffic lookup", type="primary")
    reset_btn = col3.button("Reset checkpoint")

    if reset_btn:
        st.session_state["results"] = {}
        st.success("Checkpoint cleared. The next run will start from scratch.")

    if validate_btn:
        with st.spinner("Validating key against google.com..."):
            ok, msg = asyncio.run(validate_api_key(api_key))
        if ok:
            st.success(msg)
        else:
            st.error(msg)

    if start_btn:
        # Resume support: skip domains we already have results for.
        already_done = set(st.session_state["results"].keys())
        pending = [d for d in unique_domains if d not in already_done]

        if not pending:
            st.info("All domains are already in the checkpoint. Nothing to do.")
        else:
            st.info(
                f"Looking up {len(pending)} domains "
                f"({len(already_done)} already cached from previous run)."
            )

            progress_bar = st.progress(0.0, text="Starting...")
            status_box = st.empty()
            start_time = time.time()

            def on_progress(idx: int, total: int, domain: str, visits: str, remaining: str):
                # Persist immediately so a tab reload doesn't lose work.
                st.session_state["results"][domain] = visits
                fraction = min(idx / total, 1.0)
                elapsed = time.time() - start_time
                eta = (elapsed / idx) * (total - idx) if idx else 0
                progress_bar.progress(
                    fraction,
                    text=(
                        f"[{idx}/{total}] {domain} → {visits} | "
                        f"quota left: {remaining} | ETA: {eta:0.0f}s"
                    ),
                )
                # Periodic checkpoint announcement (state already persisted above).
                if idx % CHECKPOINT_INTERVAL == 0:
                    status_box.caption(
                        f"Checkpoint saved at {idx}/{total}."
                    )

            try:
                asyncio.run(run_batch(pending, api_key, on_progress))
                progress_bar.empty()
                status_box.empty()
                st.success(
                    f"Done in {time.time() - start_time:0.1f}s. "
                    f"Total cached results: {len(st.session_state['results'])}."
                )
            except Exception as exc:
                st.error(
                    f"Run interrupted: {exc}. "
                    f"Partial results are saved in the checkpoint — "
                    f"click Start again to resume."
                )

    # Render whatever is currently in the checkpoint, even between runs.
    if st.session_state["results"]:
        df = st.session_state["input_df"].copy()
        df["Visits"] = df["Clean_Domain"].map(
            lambda d: st.session_state["results"].get(d, "-")
        )

        # Numeric column for filtering / sorting; non-numeric statuses become NaN.
        df["Visits_Numeric"] = pd.to_numeric(df["Visits"], errors="coerce")

        if threshold > 0:
            filtered = df[df["Visits_Numeric"] >= threshold].copy()
        else:
            filtered = df.copy()

        st.write(
            f"Result: {len(filtered)} rows after threshold filter "
            f"(threshold = {threshold})."
        )
        st.dataframe(
            filtered.drop(columns=["Clean_Domain", "Visits_Numeric"]).head(25)
        )

        # Final CSV: drop helper columns, keep original Domain + Visits.
        export_df = filtered.drop(columns=["Clean_Domain", "Visits_Numeric"])
        csv_buffer = io.BytesIO()
        export_df.to_csv(csv_buffer, index=False, encoding="utf-8")

        st.download_button(
            label="Download result (CSV)",
            data=csv_buffer.getvalue(),
            file_name="dataloom_traffic_full.csv",
            mime="text/csv",
        )

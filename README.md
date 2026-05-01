# DataLoom Traffic Checker

A high-throughput Streamlit logging and traffic-intelligence utility for bulk SimilarWeb lookups with resilient retries, checkpoint-based recovery, and deterministic domain normalization.

[![Build Status](https://img.shields.io/badge/Build-Passing-brightgreen?style=for-the-badge)](#testing)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge&logo=python)](#getting-started)
[![Streamlit](https://img.shields.io/badge/Streamlit-App-ff4b4b?style=for-the-badge&logo=streamlit)](#usage)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

> [!NOTE]
> This project is part of a broader ad-tech lead-generation workflow where candidate domains are filtered by `ads.txt`/`app-ads.txt` signals and traffic thresholds before enrichment.

## Table of Contents

- [Features](#features)
- [Tech Stack & Architecture](#tech-stack--architecture)
- [Getting Started](#getting-started)
- [Testing](#testing)
- [Deployment](#deployment)
- [Usage](#usage)
- [Configuration](#configuration)
- [License](#license)
- [Support the Project](#support-the-project)

## Features

- Async bulk SimilarWeb traffic lookups using `aiohttp` under Streamlit.
- Concurrency controls (`asyncio.Semaphore`) for rate-limit-safe throughput.
- Retry strategy with exponential backoff for transient API/network faults.
- Pre-flight API key validation using a known domain before full batch execution.
- Deterministic URL/domain normalization:
  - strips scheme/path/query/fragment,
  - removes `www.` and mobile-prefix variants,
  - handles trailing dots and explicit ports.
- Input deduplication post-normalization to reduce quota waste.
- Session-level checkpointing in `st.session_state` for idempotent reruns.
- Partial-progress survivability during tab crashes/reloads.
- Configurable minimum-traffic output filtering.
- CSV ingestion with header auto-detection for multilingual/common column names.
- Production-friendly tunables for timeout, retries, backoff, and checkpoint cadence.

> [!IMPORTANT]
> Current defaults are tuned to respect typical RapidAPI/DataLoom plan constraints while preserving throughput. Increase concurrency cautiously.

## Tech Stack & Architecture

- **Language:** Python
- **UI Runtime:** Streamlit
- **HTTP Client:** `aiohttp`
- **Concurrency Runtime:** `asyncio`
- **Data Handling:** `pandas`

### Project Structure

```text
.
├── app.py                # Streamlit entrypoint, UI actions, async traffic workflow
├── requirements.txt      # Python dependency lock list for runtime
├── LICENSE               # MIT license
└── README.md             # Project documentation
```

### Key Design Decisions

- **Async over sync HTTP:** replaces sleep-based serial polling with bounded parallelism.
- **Fail-soft execution model:** unresolved domains return placeholders instead of halting the run.
- **Stateful resumability:** checkpointing avoids restarting large batches from scratch.
- **Explicit response parsing priorities:** avoids non-deterministic recursive key discovery.

<details>
<summary>Architecture Data Flow (Mermaid)</summary>

```mermaid
flowchart TD
    A[Upload CSV] --> B[Normalize and deduplicate domains]
    B --> C[Validate API key with google.com]
    C -->|valid| D[Spawn async workers with semaphore]
    C -->|invalid| Z[Show validation error and stop]
    D --> E[Call DataLoom SimilarWeb endpoint]
    E --> F{Response status}
    F -->|success| G[Extract visits via priority keys]
    F -->|transient error| H[Retry with exponential backoff]
    H --> E
    F -->|hard error|max I[Mark domain unresolved]
    G --> J[Persist result to st.session_state]
    I --> J
    J --> K[Apply optional minimum-visits filter]
    K --> L[Download CSV]
```

</details>

## Getting Started

### Prerequisites

- Python `3.10+`
- `pip`
- RapidAPI account with access to DataLoom SimilarWeb endpoint
- A CSV input file containing one domain/URL per row

### Installation

```bash
git clone https://github.com/<your-org>/streamlit-similarweb-checker.git
cd streamlit-similarweb-checker
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

> [!TIP]
> Keep the virtual environment isolated per project to prevent dependency drift across Streamlit apps.

<details>
<summary>Troubleshooting and Alternative Setup</summary>

### Common startup issues

- `ModuleNotFoundError`: confirm the virtual environment is activated before running `pip install`.
- Streamlit not found: run `python -m pip install -r requirements.txt` to ensure interpreter alignment.
- API key appears invalid: verify RapidAPI subscription and header quota in your plan dashboard.

### Rebuild from clean environment

```bash
rm -rf venv
python -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

</details>

## Testing

This repository is app-first and does not currently ship a formal unit/integration suite. Use the following validation workflow:

```bash
python -m py_compile app.py
streamlit run app.py
```

Recommended local quality gates if you extend the codebase:

```bash
pytest -q
ruff check .
```

> [!WARNING]
> `pytest` and `ruff` commands assume you add test files and dev tooling to the repository; they are not guaranteed to run in the current baseline snapshot.

## Deployment

### Production Guidelines

- Deploy behind authenticated access if API keys are entered by operators.
- Use environment-level secrets management for platform deployments.
- Keep outbound concurrency capped to provider-approved limits.
- Monitor HTTP error rates and throttle backoff behavior during quota pressure.

### Containerization Reference

<details>
<summary>Example Dockerfile + runtime command</summary>

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
```

```bash
docker build -t dataloom-traffic-checker .
docker run --rm -p 8501:8501 dataloom-traffic-checker
```

</details>

## Usage

### Basic Usage

1. Launch the app and open the generated local URL.
2. Paste your RapidAPI key in the sidebar.
3. Upload a single-column CSV (`domain`, `url`, `website`, and similar headers are auto-detected).
4. Click **Validate API key**.
5. Click **Start traffic lookup** and wait for completion.
6. Download the resulting CSV.

```python
# Minimal invocation from shell
# streamlit run app.py
# Then operate via UI controls.
```

> [!CAUTION]
> API keys are held in session memory; do not expose the running app endpoint publicly without access controls.

<details>
<summary>Advanced Usage, Custom Processing, and Edge Cases</summary>

### Advanced usage patterns

- Re-run interrupted batches without resetting state to process only unresolved domains.
- Apply a minimum visits threshold to export only commercially meaningful candidates.

### Custom formatters/export post-processing

- Add additional export columns (e.g., confidence score, retry count, normalized host).
- Route completed CSV into downstream enrichment pipelines.

### Edge cases handled by normalization

- `https://www.example.com/path?a=1#fragment` → `example.com`
- `m.news.bbc.co.uk:443/world` → `bbc.co.uk`
- `subdomain.example.com.` → `subdomain.example.com`

</details>

## Configuration

Primary runtime constants are defined in `app.py`:

- `CONCURRENCY_LIMIT`: Max concurrent API requests.
- `MAX_RETRIES`: Total attempts per domain.
- `BACKOFF_BASE`: Backoff multiplier for retry delays.
- `CHECKPOINT_INTERVAL`: Frequency of progress checkpoint signaling.
- `REQUEST_TIMEOUT`: Per-request timeout (seconds).

<details>
<summary>Extended Configuration Reference</summary>

| Option | Type | Typical Default | Impact | Tuning Guidance |
|---|---|---:|---|---|
| `CONCURRENCY_LIMIT` | `int` | `3` | Throughput vs. rate-limit risk | Increase slowly and monitor 429/5xx rates |
| `MAX_RETRIES` | `int` | `3` | Recovery from transient failures | Raise for unstable networks; avoid excessive quota burn |
| `BACKOFF_BASE` | `float` | implementation-defined | Retry pacing | Use exponential progression to avoid burst retries |
| `CHECKPOINT_INTERVAL` | `int` | implementation-defined | UI/state update cadence | Lower for frequent updates, higher for less overhead |
| `REQUEST_TIMEOUT` | `int/float` | implementation-defined | Failure detection speed | Balance responsiveness and slow-provider tolerance |

### Environment variables

No mandatory `.env` schema is required in the baseline implementation. For production hardening, common optional variables are:

- `STREAMLIT_SERVER_PORT`
- `STREAMLIT_SERVER_ADDRESS`
- platform secret vars for API keys (if you refactor away from manual sidebar entry)

</details>

## License

This project is licensed under the **MIT License**. See [`LICENSE`](LICENSE) for full terms.

## Support the Project

[![Patreon](https://img.shields.io/badge/Patreon-OstinFCT-f96854?style=flat-square&logo=patreon)](https://www.patreon.com/OstinFCT)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-fctostin-29abe0?style=flat-square&logo=ko-fi)](https://ko-fi.com/fctostin)
[![Boosty](https://img.shields.io/badge/Boosty-Support-f15f2c?style=flat-square)](https://boosty.to/ostinfct)
[![YouTube](https://img.shields.io/badge/YouTube-FCT--Ostin-red?style=flat-square&logo=youtube)](https://www.youtube.com/@FCT-Ostin)
[![Telegram](https://img.shields.io/badge/Telegram-FCTostin-2ca5e0?style=flat-square&logo=telegram)](https://t.me/FCTostin)

If you find this tool useful, consider leaving a star on GitHub or supporting the author directly.

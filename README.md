# financial-ai-analyst

Enterprise Financial AI Analyst: a financial research copilot over public-company SEC filings.

**Status:** Milestone 0. A minimal FastAPI service with a health endpoint and an SEC EDGAR client
(company submissions only). See [docs/architecture.md](docs/architecture.md) and
[docs/decisions.md](docs/decisions.md).

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync                    # creates .venv and installs runtime + dev dependencies
cp .env.example .env       # then set SEC_USER_AGENT to your name and contact email
```

`SEC_USER_AGENT` is required. The app refuses to start without one that includes a contact
email, because SEC's fair-access policy requires automated clients to identify themselves.

## Run

```bash
uv run uvicorn financial_analyst.main:create_app --factory --reload
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"0.1.0"}
```

Interactive API docs are served at http://127.0.0.1:8000/docs.

## Test and lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests never call SEC. HTTP is mocked with `httpx.MockTransport`. The fixture in
`tests/fixtures/` is a trimmed payload in EDGAR's submissions format; its values are illustrative.

## Using the SEC client

```python
from financial_analyst.config import Settings
from financial_analyst.sec_client import SecClient

with SecClient(Settings()) as sec:
    # Discovery: which filings exist (metadata only)
    submissions = sec.get_submissions("320193")  # Apple Inc.
    ten_k = next(f for f in submissions.recent_filings if f.form == "10-K")
    print(ten_k.accession_number, ten_k.primary_document_url)

    # Retrieval: download that filing's primary HTML document (10-K/10-Q and amendments only)
    document = sec.get_filing_document(ten_k)
    print(len(document.content), document.source.content_sha256)
```

**Discovery vs retrieval.** `get_submissions` *discovers* filings and returns trusted metadata:
accession number, form and primary document name. `get_filing_document` *retrieves* one
filing's primary document from the EDGAR archive URL derived from that metadata. It never
searches or crawls. `document.content` holds the exact bytes SEC returned; parsing comes in a
later milestone.

The client paces requests (default 4/s, never more than SEC's 10/s), uses explicit timeouts,
and retries 429/5xx/timeouts at most 3 times with exponential backoff (constants at the top
of `sec_client.py`).

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
searches or crawls. `document.content` holds the exact bytes SEC returned (after HTTP
decompression). Turning them into text is a separate, offline step (below).

The client paces requests (default 4/s, never more than SEC's 10/s), uses explicit timeouts,
and retries 429/5xx/timeouts at most 3 times with exponential backoff (constants at the top
of `sec_client.py`).

## Caching filing documents

```python
from financial_analyst.filing_cache import fetch_filing_document

settings = Settings()
with SecClient(settings) as sec:
    document = fetch_filing_document(sec, ten_k, settings.filing_cache_dir)  # SEC only on a miss
```

Primary filing documents are cached under `FILING_CACHE_DIR` (default `data/sec-cache`, git-ignored)
as exact bytes plus a JSON metadata file. Every read re-verifies the SHA-256, and a cache hit
returns the original provenance, including when SEC actually served the bytes. Corrupted or
mismatched entries are treated as misses and replaced after the next download. If the cache
can't be written, a warning is logged and the downloaded document is still returned. The cache
directory must not be writable by untrusted users. Deleting it is always safe. Extracted
content is not cached; extraction is fast and deterministic. Details:
[docs/architecture.md](docs/architecture.md) §3.2c.

## Extracting filing content

```python
from financial_analyst.filing_extractor import extract_filing

extracted = extract_filing(document)  # offline; does not modify `document`
print(extracted.diagnostics.sections_found, extracted.diagnostics.warnings)
for block in extracted.blocks[:10]:
    print(block.index, block.kind, block.section, block.dom_path, block.text[:80])
```

Each block is a heading, paragraph or table, in document order. Tables keep their cell grid in
`block.rows`. `dom_path` plus the document's SHA-256 says exactly where each block came from.
Form 10-K sections are assigned only when their headings are unambiguous. 10-Q filings are
extracted in full but get no section labels yet (a warning says so). Each result records
`extractor_version`; together with the source SHA-256 it identifies the exact blocks. See
[docs/architecture.md](docs/architecture.md) §3.2b for the guarantees and limitations.

## XBRL Company Facts

```python
from datetime import date
from financial_analyst.company_facts import period_class, select_fact

with SecClient(Settings()) as sec:
    facts = sec.get_company_facts("320193")  # every fact SEC publishes; not cached

result = select_fact(
    facts, "us-gaap", "Revenues", "USD", date(2023, 10, 1), date(2024, 9, 28), policy="latest_filed"
)
print(result.selected, result.conflicting_values, result.reason)
```

Each SEC row becomes one `FactObservation`: an exact `Decimal` value with its unit, period,
form, filing date and accession number. Rows are never merged. `select_fact` picks one
observation for an exact period from 10-K/10-Q filings and their amendments, using an explicit
`latest_filed` or `original` policy. `conflicting_values` is true only when eligible
filings disagree (8-K and other forms stay visible in `candidates` but don't count). It
returns `None` with a reason instead of choosing
arbitrarily between disagreeing values, and it never mixes units. SEC's `fy`/`fp` describe the
*filing* and are stored as `filing_fiscal_year`/`filing_fiscal_period`. Details:
[docs/architecture.md](docs/architecture.md) §3.2d.

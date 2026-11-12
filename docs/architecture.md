# Architecture — Enterprise Financial AI Analyst

Status: **Approved for Milestone 0, Phase B** (owner approval 2026-11-11). Later-milestone sections are design intent, not commitments.
Last updated: 2026-11-11

---

## 1. Current repository state

| Item | Finding |
|---|---|
| Project directory | `/Users/j/Desktop/enterprise-financial-ai-analyst` was **empty** before this document. |
| Git | Originally had no repository of its own (the enclosing repo was the home directory). **Resolved:** the owner initialized a project repository on `main`, remote `https://github.com/JerrodMcHenry/financial-ai-analyst.git`, with an initial commit containing `README.md`. |
| Python | 3.13.5 is installed (python.org and Anaconda builds). No 3.12 interpreter is installed. |
| Tooling | `uv` 0.11.14 is installed. Docker is **not** installed (needed in a later milestone). |

Nothing reusable exists, so this is a greenfield design.

---

## 2. System overview

This is a **modular monolith**: one deployable FastAPI service, organized into packages with clear boundaries. Each package corresponds to a capability that will be added in a specific milestone.

```
                         ┌──────────────────────────────────────────────┐
  Analyst UI  ──HTTP──▶  │ FastAPI app (api)                            │
  (Next.js, later)       │   /health   /research (later)                │
                         ├──────────────────────────────────────────────┤
                         │ agent      : LLM tool-calling loop (later)   │
                         │ tools      : validated tool schemas (later)  │
                         │ calc       : deterministic finance (later)   │
                         │ retrieval  : facts + passages (later)        │
                         │ ingestion  : EDGAR → normalized store (later)│
                         │ sec        : EDGAR HTTP client  ◀── M0       │
                         │ config     : env-based settings ◀── M0       │
                         └───────┬───────────────────────┬──────────────┘
                                 │                       │
                       SEC EDGAR (data.sec.gov,   PostgreSQL + pgvector
                       www.sec.gov/Archives)      (later)
```

Key separation: **the SEC client is only used by ingestion**, never directly on the user's question path. Analyst questions are answered from our own normalized store. This keeps the request path fast and predictable, keeps SEC traffic low and cacheable, and makes evaluations reproducible.

---

## 3. Request and data flows

### 3.1 Milestone 0: health check (implemented in Phase B)

```
GET /health
  → uvicorn (ASGI server) → FastAPI router → health handler
  → 200 {"status": "ok", "version": "0.1.0"}
```

This is a liveness check only. It performs no SEC or database calls, so an outage of a dependency does not make the process look dead. A separate readiness check can be added when a database exists.

### 3.2 Milestone 0: company submissions fetch (implemented in Phase B)

```
caller (test or script)
  → SecClient.get_submissions(cik="320193")
      1. validate + normalize CIK        → "0000320193"
      2. build URL                       → https://data.sec.gov/submissions/CIK0000320193.json
      3. _pace()                         → enforce min interval between requests
      4. httpx GET (User-Agent, timeouts)
      5. retry loop on 429 / 5xx / timeout (bounded, exponential backoff + jitter, honors Retry-After)
      6. map final status → SecError (SecNotFoundError for 404; status_code kept)
      7. parse JSON → Pydantic validation
  ← CompanySubmissions (company info + list[FilingRef] + SourceMetadata)
```

There is no HTTP endpoint for this in Phase B. The owner decided against a debug route (D-016), so the client stays internal until ingestion exists.

### 3.2a Milestone 1.1: filing document retrieval (implemented)

**Discovery vs retrieval.** *Discovery* (`get_submissions`) asks EDGAR which filings a company has and returns trusted metadata: accession number, form, dates and primary document name. *Retrieval* (`get_filing_document`) downloads one filing's primary HTML document. Retrieval never searches or crawls. It takes a `FilingRef` produced by discovery and fetches exactly the archive URL derived from it.

```
caller holds a FilingRef from get_submissions
  → SecClient.get_filing_document(filing)
      1. _filing_document_url(filing)    → form must be 10-K/10-Q (or /A); primary document required;
                                           URL must fully match https://www.sec.gov/Archives/edgar/data/
                                           <cik>/<18-digit accession>/<plain filename>.htm(l)
                                           (else ValueError, before any request)
      2. _get(url)                       → same pacing, timeouts, retries and error mapping as above;
                                           redirects are not followed (3xx → SecError)
      3. _check_html_filing(response)    → Content-Type text/html, non-empty, "<html" near the start,
                                           no SEC block-page phrases (else SecResponseError)
      4. _source_metadata(url, response) → URL, UTC time, status, SHA-256 of the exact bytes
  ← FilingDocument(filing, content=<exact bytes>, source)
       model validator re-checks: hash matches content; source URL == filing's document URL
```

**Identity.** The document is tied to its filing because the URL is built only from trusted submissions metadata and must match the expected archive path. The HTML checks in step 3 only make it *plausible* that the body is a filing rather than an error page. They do not prove which filing it is.

### 3.2b Milestone 1.2: filing content extraction (implemented)

`extract_filing(document: FilingDocument) -> ExtractedFiling` in `filing_extractor.py`. It is pure and offline: no network, and it never modifies the input.

```
FilingDocument.content (bytes)
  1. decode            → UTF-8 (covers the ASCII SEC usually declares); if invalid,
                         windows-1252 with a warning. The encoding used is recorded.
  2. parse             → BeautifulSoup + Python's built-in html.parser. Builds a tree only:
                         no scripts run, no external resources load, malformed markup recovered.
  3. walk in order     → skip <head>, script/style, ix:header (hidden XBRL facts) and any
                         element with display:none or the hidden attribute.
                         Block elements (div, p, li, h1–h6, …) start a new block. Inline
                         elements (span, b, a, ix:nonNumeric, ix:nonFraction, …) are transparent,
                         so values tagged with XBRL stay in their sentence.
                         <table> → one table block. Whitespace (incl. &nbsp;) → single spaces.
  4. classify          → heading if an <h1>–<h6>, or if all its visible text is bold
                         (b/strong or font-weight ≥ 600) and ≤ 300 characters; otherwise paragraph.
  5. sections          → see below
  ← ExtractedFiling(filing, source, blocks[ContentBlock], diagnostics)
```

**Tables.** Each `<table>` becomes one block. `rows` holds the cell grid in original row and cell order, and is the authoritative form. A cell with `colspan=n` is followed by n−1 empty-string placeholders so that values stay under their column headers. **These placeholders look exactly like genuinely empty source cells**, so the grid shows where a spanning header starts but not how far it extends. **`rowspan` is not supported yet:** a merged cell appears only in its first row, and later rows can be shorter or misaligned (2 of 60 tables in Apple's 2026 10-K have rows of different lengths). Consumers must not assume tables carry complete merged-cell structure. Rows with no text at all (spacer rows) are dropped. `text` is a convenience rendering: non-empty cells joined by ` | `, one line per row. Tables are never rewritten as prose. Values split across cells (`$` | `178,353`) are kept as separate cells.

**Source locations.** Each block has a `dom_path` such as `/html[1]/body[1]/div[5]`: the element's position in the html.parser tree, counting same-name siblings from 1. It also has `element_id`, the element's own `id` attribute, when present. Hidden elements are skipped but still counted, so recorded paths match the original tree. A path is only meaningful for the exact bytes identified by `ExtractedFiling.source.content_sha256`. A browser's DOM can differ for malformed markup.

**Citation guarantees.**
- **Guaranteed:** each block's text comes from the element at its `dom_path` in the document with that SHA-256, and blocks appear in document order. Re-running extraction on the same bytes gives identical blocks.
- **Not guaranteed:**
  - page numbers (HTML has none);
  - character offsets into the raw HTML;
  - that a heading/paragraph classification matches the author's intent;
  - that every section boundary is found.

**Section detection.** Only heading blocks whose text *starts with* "Item N" (a valid Form 10-K item) or "Part I–IV" are boundaries.
- A table of contents built as a `<table>` is ignored, and cross-references inside sentences are paragraphs, so they are ignored too.
- Each boundary starts a region that runs to the next boundary. A "Part" heading ends the previous item.
- A region is labeled (`section = "Item 1A"`) only if that item's heading occurs exactly once and in the expected order.
- Duplicate or out-of-order headings leave their regions as `None`, reported in `uncertain_sections` and `warnings`. That content is never given to a neighboring section.
- Priority items (1, 1A, 7, 8) with no heading are listed in `missing_sections`.

**Diagnostics** (`ExtractionDiagnostics`) report the encoding, the block, paragraph, heading and table counts, sections found, missing and uncertain, and warnings. They describe what the extractor did. They do not show that the extraction is semantically correct.

**Known limitations.**
- Headings that aren't bold or `<h1>`–`<h6>` become paragraphs, so their section is reported missing.
- Combined headings ("Items 1 and 2.") aren't recognized.
- Page headers, footers and page numbers remain as paragraphs.
- Tables used for layout (bullets, cover page) are still table blocks.
- `rowspan` isn't expanded, and colspan placeholders look the same as genuinely empty cells (see Tables above).
- Nested tables appear as text inside a cell.
- Extremely deep, malformed nesting could exceed Python's recursion limit.

### 3.3 Later milestone: ingestion

```
ticker → CIK (company_tickers.json)
  → submissions → select 10-K / 10-Q (amendments kept distinct)
  ├─▶ Company Facts (XBRL JSON) → normalize facts → facts table          [structured path]
  └─▶ primary document (HTML) → clean → split into Items (1A, 7, …)
        → chunk → embed → pgvector, each chunk carrying provenance       [narrative path]
  raw responses cached on disk / in DB, keyed by URL and content hash
```

### 3.4 Later milestone: research question

```
POST /research {question}
  → agent loop (native LLM tool calling, no framework)
      LLM proposes tool call ──▶ arguments validated by Pydantic ──▶ deterministic execution
        get_financial_facts(cik, concept, periods)   → facts with fact_ids
        search_filings(cik, query, forms, years)     → passages with chunk_ids
        compute_metric(name, inputs by fact_id)      → result + formula + input ids
      tool results are returned to the LLM as data, delimited and marked untrusted
  → LLM drafts answer that cites fact_ids / chunk_ids
  → citation validator: every cited id must have come from this request's tool results;
    every number in the answer must match a tool output (otherwise flagged or rejected)
  → response: answer + resolved citations (URL, accession, period, unit) + telemetry
```

---

## 4. Proposed directory structure

### 4.1 Current structure (after the Phase B simplification)

```
enterprise-financial-ai-analyst/
├── pyproject.toml              # deps, tool config, Python ≥3.12
├── uv.lock                     # reproducible lockfile
├── README.md
├── .env.example                # placeholder values only
├── .gitignore
├── CLAUDE.md                   # permanent engineering rules for this repo
├── docs/
│   ├── architecture.md
│   └── decisions.md
├── src/
│   └── financial_analyst/
│       ├── __init__.py
│       ├── main.py             # FastAPI app + GET /health
│       ├── config.py           # Settings (pydantic-settings)
│       ├── sec_client.py       # SecClient: submissions + filing documents (pacing, retries)
│       ├── sec_models.py       # SourceMetadata, FilingRef, CompanySubmissions, FilingDocument
│       └── filing_extractor.py # extract_filing: HTML → ordered blocks, sections, diagnostics
└── tests/
    ├── conftest.py             # shared settings fixture
    ├── fixtures/
    │   ├── submissions_sample.json   # trimmed EDGAR submissions payload
    │   └── filing_sample.htm         # trimmed inline-XBRL 10-K-style HTML
    ├── test_config.py
    ├── test_filing_extractor.py
    ├── test_health.py
    └── test_sec_client.py
```

There are no subfolders. SEC code is split into two flat modules because they have different jobs: `sec_client.py` handles transport (HTTP, pacing, retries, parsing) and `sec_models.py` holds the data contracts that later milestones will import. A folder is created only once several modules genuinely belong together (D-017).

### 4.2 Indicative future capabilities (not created now)

Ingestion, storage (Postgres/pgvector), retrieval, calculations, LLM access, the agent loop, evals, observability, and the web UI (Next.js, in a separate directory). Each is added only in its milestone, and starts as a single module rather than a folder.

---

## 5. SEC client interface

```python
class SecClient:
    """Synchronous EDGAR client. One instance per process; reuses a connection pool."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None: ...
        # transport is httpx's own hook; tests pass httpx.MockTransport (no network).
        # Tests replace time.sleep with pytest's monkeypatch, so nothing really waits.

    def get_submissions(self, cik: str | int) -> CompanySubmissions: ...
    def get_filing_document(self, filing: FilingRef) -> FilingDocument: ...   # Milestone 1.1

    # Planned (same private plumbing, no new abstractions):
    # def get_company_facts(self, cik: str | int) -> CompanyFacts: ...

    def close(self) -> None: ...
    def __enter__(self) / __exit__(...)  # context-manager support

    # private
    def _get(self, url: str) -> httpx.Response: ...          # pacing + retries + status mapping
    def _get_json(self, url: str) -> tuple[dict, SourceMetadata]: ...
```

Behavior:

| Concern | Design |
|---|---|
| Identification | `User-Agent` header from `SEC_USER_AGENT`, required at startup and validated to contain a contact email (SEC format: `"Name contact@example.com"`). |
| Pacing | Minimum interval between requests, enforced with a `threading.Lock` and a monotonic clock. Default **4 req/s** (`SEC_MAX_REQUESTS_PER_SECOND=4`). Config validation **rejects values above 10**, which is SEC's published limit. |
| Timeouts | Explicit: connect 5s, read 20s, write/pool 10s. Named constants in `sec_client.py`, not env vars. |
| Retries | Bounded: `MAX_RETRIES = 3` (constant). Retry on 429, 500, 502, 503, 504, timeouts and connection errors. Exponential backoff with jitter (`BACKOFF_BASE_SECONDS = 1`), capped at `BACKOFF_MAX_SECONDS = 30`. `Retry-After` is honored. If it asks for more than the cap, the client stops instead of retrying early. No retry on other 4xx. |
| Errors | Three classes. `SecError` covers any failure and carries `status_code` (None for timeouts and connection errors). Two subclasses: `SecNotFoundError` (404) and `SecResponseError` (invalid JSON, schema mismatch, or a 200 response that is not plausibly a filing document). Callers never see raw `httpx` exceptions. |
| Input validation | CIK must be 1–10 digits, then zero-padded to 10. Document requests must be a 10-K/10-Q-family filing whose URL fully matches the EDGAR archive pattern. Anything else raises `ValueError` before any network call. |
| Redirects | Not followed (httpx default). A 3xx response raises `SecError`, so a request can never be steered to another host. |
| Caching | Deferred to Milestone 1 (D-013). The `_get_json` seam is where a cache will be added. |

**Why synchronous:** SEC access is rate-limited batch ingestion, so concurrency gains little under a global 4 req/s cap. Sync code is simpler to read, test and debug. If a FastAPI route ever calls it, a plain `def` route runs in FastAPI's threadpool, which the lock handles. Revisit only if profiling shows a need. (Decision D-004.)

---

## 6. Source metadata contract

Provenance is captured at three levels. Only level 1 and the filing reference are implemented in Phase B. The rest is defined now so later code has a fixed target.

### Level 1: every HTTP retrieval (`SourceMetadata`, Phase B)

| Field | Type | Notes |
|---|---|---|
| `source_url` | `HttpUrl` | Exact URL fetched |
| `retrieved_at` | `datetime` (UTC, tz-aware) | |
| `http_status` | `int` | |
| `content_sha256` | `str` | Hash of the raw body. Detects upstream changes and supports caching. |

### Filing reference (`FilingRef`, Phase B)

| Field | Type | Notes |
|---|---|---|
| `cik` | `str` (10 digits) | |
| `accession_number` | `str` | Format `0000320193-23-000106`, validated by regex |
| `form` | `str` | e.g. `10-K`, `10-K/A`, `10-Q`. Amendments kept as distinct forms. |
| `filing_date` | `date` | |
| `report_date` | `date \| None` | Period of report |
| `acceptance_datetime` | `datetime \| None` | |
| `primary_document` | `str` | e.g. `aapl-20230930.htm` |
| `primary_document_url` | `HttpUrl` | Derived: `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_document}` |
| `is_xbrl`, `is_inline_xbrl` | `bool` | |

`CompanySubmissions` = `cik`, `name`, `tickers`, `exchanges`, `sic`, `sic_description`, `fiscal_year_end` (`"MMDD"`), `recent_filings: list[FilingRef]`, `has_more_filings: bool`, `source: SourceMetadata`.

EDGAR returns `filings.recent` as parallel arrays (one array per field). The parser reads them row by row with explicit indexes and **raises `SecResponseError` if the array lengths differ**, rather than silently misaligning fields.

### Filing document (`FilingDocument`, Milestone 1.1)

| Field | Type | Notes |
|---|---|---|
| `filing` | `FilingRef` | Filing identity (CIK, accession number, form, primary document URL). Not copied into separate fields. |
| `content` | `bytes` | Response body exactly as returned by httpx **after HTTP decompression** (SEC serves gzip), not the compressed bytes sent over the network. The SHA-256 covers these decompressed bytes. Not decoded to text or parsed. Hidden from `repr`. |
| `source` | `SourceMetadata` | Retrieval provenance for this response |

A model validator enforces two invariants: `sha256(content) == source.content_sha256`, and `source.source_url == filing.primary_document_url`.

### Level 2: structured fact (`FactRecord`, Milestone 1, design only)

`fact_id`, `cik`, `taxonomy` (`us-gaap`/`dei`/`ifrs-full`), `concept`, `unit` (`USD`, `shares`, `USD/shares`, `pure`), `value: Decimal`, `period_start: date | None` (None means an instant fact), `period_end: date`, `fy`, `fp`, `form`, `filed`, `accession_number`, `frame | None`, `source_url`.

### Extracted content (`ExtractedFiling`, `ContentBlock`, Milestone 1.2)

`ExtractedFiling` = `filing: FilingRef`, `source: SourceMetadata` (identifies the exact bytes), `blocks: list[ContentBlock]`, `diagnostics: ExtractionDiagnostics`.

| `ContentBlock` field | Type | Notes |
|---|---|---|
| `index` | `int` | 0-based document order |
| `kind` | `"heading" \| "paragraph" \| "table"` | |
| `text` | `str` | Normalized text. For tables: non-empty cells joined by ` \| `, one line per row. |
| `rows` | `list[list[str]] \| None` | Tables only. Authoritative cell grid. Colspan placeholders are `""`, the same as empty source cells. Rowspan isn't expanded. |
| `section` | `str \| None` | e.g. `"Item 1A"`. `None` when unknown or uncertain. |
| `dom_path` | `str` | Element position in the parsed tree |
| `element_id` | `str \| None` | The element's own `id` attribute |

### Level 3: narrative passage (`ChunkRecord`, later, design only)

`chunk_id`, `accession_number`, `form`, `document_url`, `section` (e.g. `Item 7`), the source block indexes and their `dom_path`s, `text_sha256`, `fiscal_period` (from the filing), `text`. Chunks will be built from `ContentBlock`s. They will reference block locations rather than character offsets into raw HTML, which the extractor does not provide (D-019).

### Citations (later)

A citation is a **reference to a `fact_id` or `chunk_id` that was returned by a tool during the same request**. The model never writes URLs or accession numbers itself. The server resolves ids to provenance after the fact. An id that was not retrieved in the request is treated as invented, and the answer is rejected or flagged.

---

## 7. Structured XBRL retrieval vs. narrative document retrieval

| | Structured (XBRL Company Facts) | Narrative (filing documents) |
|---|---|---|
| Source | `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | Primary 10-K/10-Q HTML in `/Archives/edgar/data/…` |
| Shape | Typed numeric facts: concept × period × unit × value, each with accession number | Unstructured prose and HTML tables |
| Answers | **"What"** questions: revenue FY2023, diluted EPS trend, gross margin inputs | **"Why"** questions: drivers in MD&A, risk factors, guidance language, accounting policies |
| Retrieval method | Exact lookup (SQL filter by CIK, concept, period, unit). No embeddings. | Hybrid search: lexical (BM25 / Postgres full-text) plus vector similarity, filtered by company, form and period metadata |
| Correctness | Deterministic. The value is either present or absent. | Probabilistic. Relevance must be evaluated (recall@k, citation accuracy). |
| Main pitfalls | Concept choice varies by company (`Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`). `fy`/`fp` describe the *filing*, not the fact's period. Restatements. Q4 is often not reported directly. Dimensional (segment) facts are not included. | HTML noise, tables that chunk poorly, unreliable item boundaries, scale stated in prose ("in millions"), prompt injection |

**Rule adopted:** numbers used in answers or calculations come from structured facts through tools. Narrative retrieval supplies explanation and context. A number found only in text (for example a non-GAAP measure) can be quoted with its citation, but it is labeled as text-sourced and is never fed into `calc` silently.

---

## 8. Dependencies

### Phase B runtime

| Package | Why |
|---|---|
| `fastapi` | Required HTTP framework. Provides typed routing, Pydantic integration and OpenAPI docs. |
| `uvicorn` | ASGI server that runs FastAPI. The plain package is enough; the `[standard]` extras are not needed yet. |
| `pydantic` (v2) | Data contracts and validation. Already a FastAPI dependency; listed explicitly because we use it directly. |
| `pydantic-settings` | Typed, validated settings from environment and `.env`. Since Pydantic v2 this is a separate package. Avoids hand-written env parsing. |
| `httpx` | HTTP client with explicit timeouts, connection pooling and **built-in `MockTransport`** for network-free tests. FastAPI's `TestClient` also uses it. |

| `beautifulsoup4` | Milestone 1.2. A tolerant, well-established HTML tree API, used with Python's built-in `html.parser`, so there are no C extensions. It never runs scripts or fetches resources. It pulls in `soupsieve` (CSS selectors) as a transitive dependency. |

### Phase B development

| Package | Why |
|---|---|
| `pytest` | Required test runner. |
| `ruff` | Linter and formatter in one tool, with minimal configuration (owner decision, D-015). |

### Deliberately not added

| Package | Reason |
|---|---|
| `respx` / `responses` | `httpx.MockTransport` covers the mocking needs. |
| `lxml` / `html5lib` | Faster or more browser-faithful parsing isn't needed for ~2 MB filings. Both add a C extension or a larger dependency. Revisit if parsing becomes a bottleneck. |
| `tenacity` / `backoff` | The retry policy is about 25 readable lines and must be explicit for review. |
| `requests` / `aiohttp` | `httpx` covers both needs. |
| `python-dotenv` | `pydantic-settings` reads `.env` natively. |
| LangChain / LlamaIndex / agent frameworks | Native tool calling is a stated requirement. Also out of scope for M0. |

Environment and lockfile management uses **`uv`** (already installed). It can install Python 3.12 on demand, so the project pins `requires-python = ">=3.12"` and runs the same way on 3.12 and 3.13.

---

## 9. Key technical risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | ~~The project sat inside a git repo rooted at the home directory.~~ **Resolved** by the owner creating a dedicated project repository. | Phase B adds a `.gitignore` that excludes `.env`, virtual environments and caches. |
| R2 | SEC blocks clients that exceed fair-access limits or omit a User-Agent. | Required and validated User-Agent, 4 req/s default, config cap at 10, backoff on 429/503, caching in M1. |
| R3 | Fiscal period semantics. `fy`/`fp` in Company Facts belong to the filing, so a FY2023 10-K also reports FY2021–22 values tagged `fy=2023`. Non-calendar fiscal years (Apple ends in September). 52/53-week years. | Key facts by `(period_start, period_end)` and use `fy`/`fp` only as filing metadata. Store the company's `fiscal_year_end`. Never infer periods from `fy` alone. |
| R4 | Restatements and duplicates: the same concept and period with different values across filings. | Keep every observation with its accession number. "Latest filed" is an explicit selection policy, and conflicting values are surfaced, never averaged or merged silently. |
| R5 | Q4 and other derived values (Q4 = FY minus 9M YTD). | Derived only in `calc`, labeled `derived`, with input fact_ids attached. |
| R6 | Concept heterogeneity and company extension concepts. | An explicit, versioned concept map for core metrics, with per-company overrides and evaluations covering it. |
| R7 | Company Facts excludes dimensional facts (segments, geography). | Document as a scope limit. Later, parse the XBRL instance or the SEC Financial Statement Data Sets if segment data is needed. |
| R8 | Filing HTML parsing (inline XBRL markup, tables, item detection). | Dedicated parser milestone with fixture-based tests. Store the parse version on each chunk. |
| R9 | Prompt injection through filing text. | Tool output passed as delimited data. System prompt states that retrieved text has no authority. Tool allowlist with validated arguments. No tool has side effects. Red-team cases included in evals. |
| R10 | Invented citations or hallucinated numbers. | Id-based citations, server-side citation validation, and number matching against tool outputs. |
| R11 | Float rounding errors in financial values. | `Decimal` for values. Explicit rounding only at presentation. |
| R12 | Submissions pagination. `filings.recent` holds only the most recent filings (at least one year or 1,000 filings); older ones are in `filings.files`. | Expose `has_more_filings` in M0. Follow pages in M1. |
| R13 | Building evaluation ground truth. | Small hand-verified golden set built from XBRL values with accession numbers. Grows over time. |
| R14 | Docker is not installed locally. | Needed only for the Postgres and deployment milestones. Install before then. |

---

## 10. Out of scope for Milestone 0

Database, vector store, LLM, frontend, authentication, background workers, ingestion pipeline, ticker resolution, Company Facts, filing documents, caching, Docker.

---

## 11. Open questions (all resolved for Phase B)

- ~~**Q1 Caching.**~~ Resolved: deferred to Milestone 1 (D-013).
- ~~**Q2 Submissions endpoint.**~~ Resolved: no debug endpoint (D-016).
- ~~**Q3 Git repository.**~~ Resolved: the owner created a dedicated repository.
- ~~**Q4 Ruff.**~~ Resolved: added as a dev dependency with minimal configuration (D-015).
- ~~**Q5 Package name.**~~ Resolved: `financial_analyst` (D-002).

# Decision Log

Lightweight architecture decision records. Each entry states the decision, why it was made, and the main alternative considered.
Status values: **Proposed** (awaiting owner approval), **Accepted**, **Superseded**.

---

## D-001 Modular monolith
**Status:** Accepted (2026-11-11)
**Decision:** A single FastAPI service, organized into capability packages (`sec`, later `ingestion`, `retrieval`, `calc`, `agent`, …).
**Why:** One deployable unit, one test suite, no network boundaries to secure or monitor. Package boundaries still keep responsibilities separate and leave room to extract services later.
**Alternative:** Microservices. Rejected because operational cost exceeds any benefit at this scale.

## D-002 `src/` layout, managed with `uv`, package name `financial_analyst`
**Status:** Accepted (2026-11-11)
**Decision:** Code lives in `src/financial_analyst/`, with dependencies in `pyproject.toml` locked by `uv.lock`. `requires-python = ">=3.12"`.
**Why:** The `src/` layout stops tests from accidentally importing code from the working directory instead of the installed package. `uv` gives fast, reproducible environments, is already installed, and can provide Python 3.12 (the local machine only has 3.13).
**Alternative:** pip with `requirements.txt` and a venv. This works but has no lockfile by default.

## D-003 The SEC client is used by ingestion only, not on the question path
**Status:** Accepted (2026-11-11)
**Decision:** Analyst questions are answered from our normalized store. Live EDGAR calls happen only during ingestion.
**Why:** Predictable latency, minimal SEC traffic (fair access), reproducible evaluations, and no dependence on EDGAR availability while answering questions.

## D-004 Synchronous `httpx.Client` for EDGAR
**Status:** Accepted (2026-11-11)
**Decision:** The SEC client is synchronous, with an injectable `httpx` transport.
**Why:** Under a global cap of about 4 req/s, async concurrency gains little. Sync code is easier to read, test and reason about. The pacing lock makes it safe inside FastAPI's threadpool.
**Alternative:** `httpx.AsyncClient`. Revisit if ingestion profiling shows a benefit.

## D-005 Hand-written bounded retry and pacing, no retry library
**Status:** Accepted (2026-11-11)
**Decision:** Retry on 429/5xx/timeouts/connection errors, up to 3 retries, exponential backoff with jitter, capped at 30s, honoring `Retry-After`. Requests are paced to a minimum interval (default 4 req/s, configuration rejects values above 10). Retry, backoff and timeout values are named constants in `sec_client.py`. Only the request rate is configurable (D-017).
**Why:** The policy is short and must be easy to review. A library would hide behavior that matters for SEC compliance.
**Alternative:** `tenacity`.

## D-006 Configuration with `pydantic-settings`, fail fast
**Status:** Accepted (2026-11-11)
**Decision:** All configuration comes from environment variables (with optional `.env`) and is validated at startup inside `create_app()`. Only two settings exist: `SEC_USER_AGENT` (required, must contain a contact email) and `SEC_MAX_REQUESTS_PER_SECOND`. `create_app()` is kept so tests can build an app with explicit settings. No other factories are introduced.
**Why:** Typed config, a single source of truth, and misconfiguration caught before any request reaches SEC. Business logic receives a settings object and never reads `os.environ`.

## D-007 Network-free tests with `httpx.MockTransport`
**Status:** Accepted (2026-11-11)
**Decision:** Tests inject a mock transport and use a trimmed, realistic submissions fixture. Phase B tests never touch the live SEC.
**Why:** Fast, deterministic tests, and no extra dependency (it is built into `httpx`).
**Alternative:** `respx`.

## D-008 XBRL facts are the authoritative numeric source
**Status:** Proposed
**Decision:** Numbers used in calculations come from XBRL structured facts. Narrative retrieval supplies explanation. Numbers that appear only in text are labeled as text-sourced and never feed calculations silently.
**Why:** XBRL is exact and typed. Reading numbers from HTML or prose is error-prone (scale, tables, OCR-like noise).

## D-009 `Decimal` for financial values
**Status:** Proposed (applies from Milestone 1)
**Decision:** Fact values and calculation inputs and outputs use `decimal.Decimal`. Rounding happens only at presentation.
**Why:** Avoids binary floating-point error in financial arithmetic.

## D-010 Provenance contract and id-based citations
**Status:** Accepted (2026-11-11)
**Decision:** There are three provenance levels: `SourceMetadata` for each HTTP fetch, `FactRecord`, and `ChunkRecord`. Each filing carries its accession number and canonical archive URL. Citations reference `fact_id` or `chunk_id` values returned by tools in the same request, and the server validates and resolves them.
**Why:** It is structurally impossible for the model to cite a source that was not retrieved.

## D-011 Never silently merge periods, units or restatements
**Status:** Proposed
**Decision:** Facts are keyed by `(cik, taxonomy, concept, unit, period_start, period_end, accession)`. Selecting among conflicting values is an explicit, logged policy. Derived values (such as Q4) are labeled with their inputs.
**Why:** Required by the engineering principles. In Company Facts, `fy`/`fp` describe the filing, not the fact's period.

## D-012 Native LLM tool calling, no agent framework
**Status:** Proposed (applies to a later milestone)
**Decision:** A small loop around the provider's native tool-calling API, behind a thin provider adapter.
**Why:** Full control over validation, citations and telemetry. Fewer dependencies. Easier to explain.

## D-013 Caching deferred to Milestone 1
**Status:** Accepted (owner decision, 2026-11-11)
**Decision:** No response cache in Phase B. Raw responses will be cached with URL, SHA-256 hash and timestamp when ingestion arrives.
**Why:** Phase B makes only test calls. The cache design depends on the storage decided in M1.

## D-014 Separate git repository for the project
**Status:** Accepted (owner implemented it, 2026-11-11)
**Decision:** The project has its own repository (`main`, remote `JerrodMcHenry/financial-ai-analyst`), separate from the home directory.
**Why:** The current enclosing repo is the user's home directory, with a remote pointing at an unrelated GitHub repo. That creates a real risk of committing secrets or personal files.

## D-015 Ruff for linting and formatting
**Status:** Accepted (owner decision, 2026-11-11)
**Decision:** `ruff` as the only dev tool for lint and format. Configuration is minimal: target py312, line length 100, rule sets `E`, `F`, `I` (imports), `B` (bugbear), `UP` (pyupgrade).
**Why:** One fast tool instead of flake8 + isort + black. A small rule set avoids noise.

## D-016 No debug endpoint for SEC data in Milestone 0
**Status:** Accepted (owner decision, 2026-11-11)
**Decision:** The only HTTP route is `GET /health`. `SecClient` is exercised by tests only.
**Why:** No consumer exists yet. Exposing raw EDGAR proxying would invite unpaced traffic and an API surface that would have to be removed later.

## D-017 Simplify the Phase B code
**Status:** Accepted (owner decision, 2026-11-11)
**Decision:**
1. Flatten the `sec/` package into `sec_client.py` and `sec_models.py`.
2. Keep `create_app()` for isolated tests and startup validation of configuration.
3. Remove the injectable `sleep` parameter. Tests use pytest `monkeypatch` on `time.sleep`.
4. Only `SEC_USER_AGENT` and `SEC_MAX_REQUESTS_PER_SECOND` are configurable. Timeout, retry and backoff values are named constants.
5. Three exception classes: `SecError` (with `status_code`), `SecNotFoundError`, `SecResponseError`.
6. Parse EDGAR's filing columns with explicit indexed access instead of eight-item tuple unpacking.
**Why:** The codebase must stay easy to navigate and explain (see `CLAUDE.md`). Folders and settings created for hypothetical future needs were removed. Exception types are kept only where a caller would plausibly handle them differently: not found means skip; malformed data means investigate.
**Alternative:** Keep the original structure. Rejected because it added indirection with no current consumer.

## D-018 Filing document retrieval inside the existing SEC client
**Status:** Accepted (owner approval, 2026-11-12; verified live against Apple's 10-K)
**Decision:** Add `SecClient.get_filing_document(filing: FilingRef) -> FilingDocument` to `sec_client.py` and a `FilingDocument` model to `sec_models.py`. No new modules, layers or dependencies.
- **Request side:** only 10-K/10-Q forms (and /A amendments). The URL is derived from the `FilingRef` and must fully match the EDGAR archive pattern. Redirects are not followed.
- **Response side:** `text/html`, non-empty, starts like HTML, contains no SEC block-page phrases. The bytes are kept exactly, with the SHA-256 recorded and re-checked by the model.
**Why:** It reuses pacing, retries and error mapping unchanged. `primary_document` comes from EDGAR's data and is not validated by `FilingRef` (other forms legitimately contain paths such as `xslF345X05/form4.xml`), so the strict check belongs at the point where we build a request URL.
**Not claimed:** the HTML checks show the body is plausibly a filing, not which filing it is. Identity comes from trusted submissions metadata plus the derived archive path.
**Alternative:** Validate `primary_document` in `FilingRef`. Rejected because it would make submissions parsing fail for other form types.

## D-019 Deterministic filing extraction with Beautiful Soup and honest locations
**Status:** Accepted (owner approval, 2026-11-12; verified live against Apple's 2026 10-K: 23/23 items found, no warnings)
**Decision:** One new module, `filing_extractor.py`, with `extract_filing(FilingDocument) -> ExtractedFiling`. It uses `beautifulsoup4` with Python's built-in `html.parser` (the owner suggested evaluating Beautiful Soup).
- **Decoding:** UTF-8, with a windows-1252 fallback that is reported in a warning.
- **Skipped:** `ix:header`, `display:none`, script and style. Other inline XBRL tags are transparent.
- **Blocks:** each block-level element or table becomes a heading, paragraph or table block.
- **Tables:** kept as a cell grid with colspan padding.
- **Locations:** a DOM path plus the element's own `id`, tied to the document's SHA-256.
- **Sections:** only unique, in-order "Item N" headings outside tables count. Anything ambiguous is left unassigned and reported as uncertain.
**Why:** A tree parser is far less error-prone than regexes over HTML, and `html.parser` avoids C dependencies while handling filings of this size. DOM paths are the most precise location HTML honestly provides. Page numbers and character offsets would claim precision we don't have. Leaving uncertain sections unassigned avoids silently putting content in the wrong section.
**Alternatives:**
- `lxml`: faster, but a C extension that isn't needed yet.
- `html5lib`: browser-exact, but slow.
- Character offsets into the HTML: brittle, and misleading after tags are removed.
- Using the table of contents to find sections: its links differ by filer, and it can disagree with the body.

## D-020 Filesystem cache for raw filing documents
**Status:** Accepted (owner approval, 2026-11-12)
**Decision:** A new module, `filing_cache.py`, with three plain functions: `fetch_filing_document`, `load_filing_document` and `save_filing_document`. It caches primary filing documents under `FILING_CACHE_DIR` (default `data/sec-cache`) as exact bytes plus a JSON metadata file.
- **Key:** CIK, accession number and primary document. The path is derived only after `filing_document_url` (now public) validates the filing, and the resolved path must stay inside the cache root.
- **Reads:** every read rebuilds a `FilingDocument`, so the SHA-256 and URL are re-checked. A hit returns the original provenance.
- **Writes:** bytes first, then metadata, each via temp file + `os.replace`. This is atomic per file, not per entry. Mismatched pairs from interrupted or concurrent writes fail the hash check and are treated as misses.
- **Invalid entries:** logged and overwritten after the next download.
- **Write failures:** non-fatal. They're logged, and the downloaded document is still returned.
- **Not included:** locking, `fsync`, caching extracted content, and caching submissions.
**Why:** Filings are immutable once filed, so a write-once file store is enough. It needs no dependencies, can be inspected with ordinary tools, and matches the long-term shape: raw bytes outside the database, metadata in Postgres later. The existing `FilingDocument` validator gives integrity checking without new code.
**Alternatives:**
- SQLite: atomic writes of bytes and metadata together, and queryable. Rejected: it would be a second database that Postgres later replaces, and the hash check already makes mismatched pairs safe.
- PostgreSQL: premature to set up and run at this stage.
- Content-addressed storage (files named by their hash): avoids mismatched pairs entirely, but adds indirection and garbage collection.

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

## D-021 SEC Company Facts: exact values, every observation, explicit selection
**Status:** Accepted (owner approval, 2026-11-12)
**Decision:**
- **Where the code lives:**
  - `SecClient.get_company_facts` and `_parse_company_facts` in `sec_client.py`, reusing the existing transport;
  - `FactConcept`, `FactObservation`, `SkippedObservation` and `CompanyFacts` in `sec_models.py`;
  - one new module, `company_facts.py`, for `period_class`, `find_concept`, `find_observations` and `select_fact`.
- **Exact numbers:** all SEC JSON is parsed with `parse_float=Decimal`. Booleans, strings, NaN and infinite values are rejected per row.
- **Every row is kept** as one observation. Concept labels and descriptions are kept once per concept. SEC's `fy`/`fp` are renamed `filing_fiscal_year`/`filing_fiscal_period`.
- **Validation:**
  - an invalid overall structure is rejected;
  - malformed rows are skipped, counted, and the first 20 sampled with taxonomy, concept, unit, row index and reason;
  - a payload in which every row is malformed is rejected.
- **Period classes** are lengths of time only (instant, 3, 6, 9 or 12 months, or other) with ranges that allow for 52/53-week years. They never identify fiscal periods.
- **Selection:**
  - candidates are every observation for the exact period, in a total order that doesn't depend on SEC's row order;
  - only 10-K, 10-K/A, 10-Q and 10-Q/A are eligible;
  - the policy is `latest_filed` or `original`;
  - disagreeing values on the chosen filing date, whether in one filing or several, give `None` with a reason;
  - conflicts are flagged;
  - the unit must always be named.
- **Not cached** in M1.4.
**Why:** financial values must be exact and traceable. Keeping every row preserves revisions and amendments as evidence instead of hiding them. Selecting explicitly, and refusing to break ties arbitrarily, prevents silently picking the wrong number. Treating `fy`/`fp` as filing attributes avoids the most common Company Facts misuse.
**Alternatives:**
- Deduplicating at parse time: rejected, because it loses the evidence of revisions.
- Using SEC's `frame` for selection: rejected, because frames are calendar-aligned and SEC's choice, not ours.
- Rejecting the whole response for one bad row: rejected, because it's too fragile for a payload with thousands of rows.
- Caching: deferred, because the data changes over time. Storage per accession number, whose facts don't change, belongs with the database milestone.

## D-022 Milestone 1.5 hardening: form-aware sections, extractor versioning, eligible-only conflicts
**Status:** Accepted (owner-directed, 2026-11-12)
**Decision:**
1. **Sections for 10-K only.** `filing_extractor.extract_filing` applies section detection only to `10-K` and `10-K/A` (`SECTION_DETECTION_FORMS`).
   - Other forms, including 10-Q and 10-Q/A, keep every block and location.
   - They get `section=None` everywhere and empty found, missing and uncertain lists.
   - They get an explicit warning.
   - Section detection for 10-Qs that understands Parts I and II is required in M2.
2. **Extractor version.** `EXTRACTOR_VERSION = "1"` is recorded on every `ExtractedFiling` as `extractor_version`.
   - Bump it when a change could alter blocks for the same bytes.
   - Stored chunk or citation identifiers must include the extractor version and the source content SHA-256.
3. **Eligible-only conflicts.** `company_facts.select_fact` computes `conflicting_values` over eligible observations only (10-K, 10-K/A, 10-Q, 10-Q/A).
   - All observations for the exact period remain in `candidates`.
   - Selection and tie handling are unchanged.

**Why:**
1. A 10-Q repeats Item numbers across Parts with different meanings. Applying 10-K rules produced misleading "missing Item 7/8" diagnostics and left MD&A unassigned.
2. Block text and indexes for the same bytes can change between extractor versions, as the M1.2 bullet fix did. Without a version, saved citations could quietly point to different text.
3. A disagreeing press-release 8-K value made 10-K values look "revised" when they weren't.

**Deferred:** older submission pages (R12), storing Company Facts per accession, XBRL sign conventions.

## D-023 Section detection for 10-Qs that understands Parts I and II
**Status:** Accepted (owner-directed M2.1, 2026-11-12; live-verified 2026-11-12)
**Decision:** `filing_extractor._assign_10q_sections` labels 10-Q and 10-Q/A content `"Part I, Item 2"`, `"Part II, Item 1A"` and so on. The 10-K rules and labels are unchanged. `EXTRACTOR_VERSION` goes to `"2"`.
- **Boundaries:** only heading blocks that start with "Part I/II" or "Item N" (tables and paragraphs are ignored).
- **No guessing:** Part headings are never inferred; an Item without one is `"Item N (Part unknown)"` and uncertain.
- **Part order:** Parts must run I then II. Any break leaves the **whole document** unlabeled.
- **Items:** labeled only when valid for their Part, unique within it and in order.
- **Missing vs uncertain:** a priority section is reported missing only when no heading for it exists at all.
**Why:** a 10-Q repeats Item numbers across Parts with different meanings, so the Part is part of the identity. Every ambiguous case fails safe: content is unassigned and reported, never labeled with a guess. Discarding all labels when the Part order breaks is deliberate. During testing, a table of contents written as headings otherwise took the "Part I, Item 2" label instead of the real MD&A.
**Alternatives:**
- Inferring Parts from item order or repetition: rejected, because it invents structure.
- Labeling only the items after a break: rejected, because a table of contents before the break can still take labels.
- Unqualified labels for 10-Qs: rejected, because they're ambiguous.

**Live verification** (extractor v2; 4 SEC requests; no incorrect section assignments in either filing):
- **Apple 10-Q `0000320193-26-000020`:** 304 blocks, 269 labeled. All 11 items detected, no warnings.
- **JPMorgan Chase 10-Q `0001628280-26-069523`:** 2,509 blocks, 0 labeled.
  - Its plain-text Item lines aren't recognized as boundaries.
  - Its Part I uses topic headings and a cross-reference index rather than explicit Item 1/2 body headings.
  - Content was conservatively left unassigned.

Improved detection for that format is deliberately not implemented in M2.1.

**Constraint carried into M2.2/M2.3:**
- Retrieval must work without section labels. A missing section must not prevent building passages, indexing, searching or verifying citations.
- An explicit section filter keeps its meaning: it returns only passages with that label, possibly none. It never silently returns unrelated passages.

## D-024 Deterministic passages with self-verifying citation ids
**Status:** Accepted (owner-approved M2.2, 2026-11-12, after the correctness review below)
**Decision:** `passages.py` builds passages from `ExtractedFiling` blocks and verifies citations to them offline (architecture §3.2e).
- **Block-based grouping:** passages never cross a section (`None` included), keep headings with the following block, keep each table whole with its headings, and fill up to `TARGET_CHARS = 2000`. Only a text block longer than that is cut, at sentence ends, then spaces, then exactly 2,000 characters, with exact offsets into `ContentBlock.text`.
- **Nothing filtered:** every block is in a passage, including page footers. This replaces the earlier footer-removal proposal (Phase A, H5).
- **`BUILDER_VERSION = "1"`**, recorded on every passage and in its id, bumped for any change that could alter passages for the same blocks.
- **Citation id:** `sec-psg:{cik}:{accession}:{primary_document}:{source_sha256}:x{extractor}:p{builder}:b{start_block}.{start_char}-{end_block}.{end_char}:t{text_sha256[:16]}`, with the **full** source SHA-256. `citation_id` and `text_sha256` are computed from the passage's fields, never stored.
- **Verification:** `verify_citation(citation_id, passage, cache_dir)` uses only the filing cache. It returns `verified` (with the source blocks and document URL) or exactly one of `invalid_citation`, `source_unavailable`, `source_changed`, `extractor_changed`, `builder_changed`, `boundaries_changed`, `text_changed`.

**Why:**
- Grouping whole blocks keeps citations to block ranges that resolve to `dom_path`s, and splitting only oversized blocks keeps that true while bounding passage size.
- Dropping footers would need a heuristic that could remove real content, and makes that content uncitable; the noise is accepted for now.
- The range makes ids unique within a document, and the full source hash and versions make them unique across documents and code changes, so uniqueness never rests on a truncated hash. The text-hash prefix is only an extra check; verification compares full hashes and full records.
- Computed ids make "a passage whose id disagrees with its own fields" impossible, so the only id check left is "is this the passage you cited".
- Rebuilding from the source bytes, rather than trusting stored text, is the only way to show the cited text is still what the document says.

**Alternatives:**
- Fixed-size windows over the whole text: rejected, because they cross sections and tables and cite arbitrary offsets.
- A truncated source hash in the id (Phase A design): rejected, so that ids from different byte versions can never collide.
- Storing `citation_id` as a validated field: rejected, because a computed field cannot go stale and needs no validator.
- Verifying by looking up stored text: rejected, because it can't detect a stale or edited record.
- Resolving a citation to the closest matching passage after a change: rejected, because that silently re-points it.

**Owner decisions at approval:** keep `TARGET_CHARS = 2000`, keep all source content including footers, don't split tables in M2, and allow heading-only passages when attachment isn't possible.

**Correctness review (2026-11-12):**
- **Cache semantics,** tested against the real cache:
  - A missing or damaged entry (missing bytes, corrupted bytes, non-JSON metadata, metadata for another filing, a recorded hash that doesn't match) gives `source_unavailable`. Damaged entries are told apart only in the cache's warning log.
  - A valid entry with a different hash gives `source_changed`.
  - One defect was fixed: the `source_unavailable` detail said "not in the local cache" even for a damaged entry; it now says "missing or failed its integrity checks". No status or cache behavior changed.
- **Offsets:** every passage segment is an exact slice of its block's text, and the slices cover each block with at most one space left out per cut. Tested with Unicode punctuation and characters, collapsed and non-breaking spaces, abbreviations, long paragraphs, multi-block passages, and sentence ends and spaces at exactly 2,000 and 2,001 characters. Split passages verify through the real cache. A 400-document random check found no mismatches. No offset defects were found.

## D-025 In-memory BM25 keyword retrieval with whole-index statistics
**Status:** Accepted (owner-approved M2.3, 2026-11-12)
**Decision:** `retrieval.py` provides `tokenize`, `KeywordIndex` (Okapi BM25, k1 = 1.2, b = 0.75, IDF `ln(1 + (N − df + 0.5)/(df + 0.5))`), `SearchHit` and `select_latest_filing` (architecture §3.2f).
- **Tokenization:** NFKC and casefold; letter/digit runs in any script; hyphens and other punctuation split; thousands separators dropped and decimal points kept; possessive `'s` dropped; 28 stopwords; no stemming or synonyms.
- **Query terms are distinct:** a repeated query word counts once.
- **Statistics describe the whole index** and are computed once. Filters (`section`, `form`, `accession_number`; exact, combined with AND) only restrict which passages may be returned, before ranking.
- **Section filters are never broadened:** an exact label only; `None` means no filter, never "unlabeled".
- **No zero-score results:** only passages containing a query term are returned. An empty or stopword-only query returns `[]`; an invalid `k` raises `ValueError`.
- **Ties** are broken by CIK, accession, primary document, source SHA-256, versions, then `block_start` and `char_start`.
- **Latest filing** by `filing_date`, then acceptance time, then accession; amendments excluded by default; never by report date or fiscal labels. It selects only from the filings present in the given `CompanySubmissions` (`recent_filings`). It doesn't fetch older submissions pages and doesn't guarantee completeness across them (R12).

**Why:**
- BM25 is the standard lexical baseline: deterministic, explainable through matched terms, and dependency-free. It gives M2.4 a fixed baseline before anything probabilistic is added.
- Whole-index statistics make a passage's score independent of filters, so a filtered search is exactly the unfiltered ranking restricted to the filter, and statistics are built once.
- Counting query terms once keeps verbose or repetitive queries (for example LLM-generated ones) from overweighting a word.
- No stemming keeps matches literal and predictable; M2.4 will show whether a minimal plural rule is worth it.
- A total tie-break order makes results independent of insertion order.

**Alternatives:**
- SQLite FTS5: deferred until the index needs to survive restarts; it adds a database file, and its availability depends on how Python was built.
- Postgres full-text search: belongs with the database milestone.
- Embeddings or hybrid retrieval: only if M2.4 shows recall below target, mainly from vocabulary mismatch; never embeddings alone.
- Per-filter statistics: rejected, because scores would depend on the filter and need a corpus pass per query.
- Weighting repeated query terms (query term frequency): rejected for now; explicit repetition is rare in analyst queries and easy to abuse.
- Falling back to unlabeled passages when a section filter matches nothing: rejected by the M2.1 constraint.

## D-026 Offline retrieval evaluation harness with pinned filings and span-based labels
**Status:** Accepted (owner-approved M2.4 B1, 2026-11-12)
**Decision:** `evaluation.py` evaluates retrieval from a strict JSON dataset against filings read only from the filing cache (architecture §3.2g).
- **Pins:** each filing's full SHA-256, `extractor_version` and `builder_version` must match the cache and the current code, or the evaluation is refused.
- **Labels are exact text spans** (a snippet, its block and section; the snippet must occur exactly once in that block. Amended in the B3 review, 2026-11-13: previously unique in the whole filing, which made repeated disclosures impossible to label), written by reading the filings, never from BM25 rankings. They are a reviewed, **non-exhaustive** evidence set, versioned through `dataset_version` and a required `changelog`.
- **Relevance:** one passage must contain at least 80% of a span's characters. Spans split across passages are measured separately as combined coverage and `split_evidence`.
- **Metrics:** Hit@5, MRR@10, labeled-evidence recall@5, combined evidence coverage@5 and section precision@5 (every top-five hit in the denominator) over the unfiltered combined index (primary). Filtered searches are secondary.
- **Integrity vs relevance:** missing or mismatched documents, unresolvable evidence, non-repeatable results, filter violations and **any citation that fails verification** refuse the evaluation (`EvaluationError`, exit code 2). Relevance misses are reported, never errors.
- **Timing:** extraction, passage building, index construction, search and verification are timed separately. Nothing is optimized.

**Why:**
- Pins make a result reproducible and say exactly what was measured. Comparing across different bytes or versions without re-validation would be meaningless.
- Spans survive chunking changes, so a builder change can be measured against the same labels. Requiring the snippet once in its labeled block, with the extracted section, catches labels that drifted with an extractor change.
- The 80% threshold stops a shared boundary character from counting as relevance, while tolerating the single space a passage split omits.
- Counting every top-five hit in section precision keeps hits from unrelated or sectionless filings from being silently excluded.
- Citation and filter correctness are guarantees, not scores: an evaluation that returns unverifiable citations is invalid, not merely worse.

**Alternatives:**
- Labels as passage citation ids: rejected, because they break whenever passage boundaries change.
- Any-overlap relevance: rejected (owner correction), because a one-character overlap would count.
- Recall over "all relevant evidence": rejected, because labels can't be proven exhaustive.
- Excluding sectionless or other-filing hits from section precision: rejected (owner correction).
- Treating citation failures as a metric: rejected; they invalidate the run.

## D-027 Batch citation verification against one snapshot per filing
**Status:** Proposed (M2.5 B1, 2026-11-13)
**Decision:** `verify_citations(pairs, cache_dir)` in `passages.py` verifies many citations at once (architecture §3.2e). `verify_citation` is a batch of one. The evaluation verifies every distinct returned citation in one batch.
- **Grouping:** by the cache's filing identity (CIK, accession number, primary document, form). Each group loads the cache entry once and hashes the loaded bytes once. It extracts and builds passages at most once, and only when some citation passes the hash and version checks. Passages are matched by block/character range.
- **Unchanged:** the citation id format, the checks and their order, every status and detail, the `PassageVerification` fields, and the behavior for malformed ids, mismatched passages and missing or damaged cache entries.
- **No persistent state:** nothing derived is kept after the call. A second batch reads and extracts again.
- **Report timing:** `Timings.verification_ms` (one batch) and per-document citation counts replace the per-citation latency summary and per-document `total_ms`. This is the only report schema change.

**Integrity tradeoff:** within a batch, each filing is checked as **one consistent snapshot**: the bytes read at the start of its group. They are not re-read for every citation. A cache change made during a batch is therefore detected by the next batch, not the current one. Source integrity is not weakened: every verified passage is still re-derived from bytes whose full SHA-256 equals the citation's, and a damaged entry is still `source_unavailable`. Before this change, each citation re-read the file, which only made the snapshot smaller; it was never atomic across a run.

**Why:** on the B4 corpus, 176 citations took 333 s, about 98% of the run, because each one re-extracted its filing. With one extraction per filing per batch, verification takes 4.9 s and a whole evaluation 11 s, with identical rankings, metrics and statuses.

**Alternatives:**
- A process-wide memo keyed by the hash of freshly read bytes: rejected for now, because hidden global state needs eviction, thread safety and test isolation.
- Saving extractions to disk: rejected, because it adds a derived artifact with its own staleness and tampering risks.
- Verifying against the passages the evaluation already built: rejected, because the check would compare the run's output with itself.

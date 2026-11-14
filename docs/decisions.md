# Decision Log

Lightweight architecture decision records. Each entry states the decision, why it was made, and the main alternative considered.
Status values: **Proposed** (awaiting owner approval), **Accepted**, **Superseded**.

**State at the close of the core engineering phase (2026-11-14):** every decision below is
Accepted. Several early decisions were later refined, and each says where; the original text
is kept as the historical record. The final system is summarized in
`docs/architecture.md` §2. The decisions that define it are:
- D-030–D-033: data and financial engine;
- D-034: analysis;
- D-035–D-037: the agent and provider adapter;
- D-036, D-039, D-040, D-043: grounding and request scope;
- D-038, D-041, D-042: live reliability.

---

## D-001 Modular monolith
**Status:** Accepted (2026-11-11). The modular monolith holds; the capability *packages* were replaced by flat modules (D-017), and the planned ingestion, storage and HTTP research layers were not built in the core phase.
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
**Status:** Accepted (implemented in M1.4 and M3.3: every published figure comes from Company Facts through `compare_periods`; text never feeds a calculation)
**Decision:** Numbers used in calculations come from XBRL structured facts. Narrative retrieval supplies explanation. Numbers that appear only in text are labeled as text-sourced and never feed calculations silently.
**Why:** XBRL is exact and typed. Reading numbers from HTML or prose is error-prone (scale, tables, OCR-like noise).

## D-009 `Decimal` for financial values
**Status:** Accepted (implemented from M1.4; percentages computed to 34 significant digits, rounded only for display, D-033)
**Decision:** Fact values and calculation inputs and outputs use `decimal.Decimal`. Rounding happens only at presentation.
**Why:** Avoids binary floating-point error in financial arithmetic.

## D-010 Provenance contract and id-based citations
**Status:** Accepted (2026-11-11). Implemented with different names: `FactObservation`/`Fact` with `observation_id` and `value_id`s for facts, and passage citation IDs (D-024) for text. Grounding resolves only the IDs produced in the same run (D-036).
**Decision:** There are three provenance levels: `SourceMetadata` for each HTTP fetch, `FactRecord`, and `ChunkRecord`. Each filing carries its accession number and canonical archive URL. Citations reference `fact_id` or `chunk_id` values returned by tools in the same request, and the server validates and resolves them.
**Why:** It is structurally impossible for the model to cite a source that was not retrieved.

## D-011 Never silently merge periods, units or restatements
**Status:** Accepted (implemented in M1.4 and M3.3: explicit selection policies, same-filing comparisons, revisions reported separately. Q4 is refused rather than derived.)
**Decision:** Facts are keyed by `(cik, taxonomy, concept, unit, period_start, period_end, accession)`. Selecting among conflicting values is an explicit, logged policy. Derived values (such as Q4) are labeled with their inputs.
**Why:** Required by the engineering principles. In Company Facts, `fy`/`fp` describe the filing, not the fact's period.

## D-012 Native LLM tool calling, no agent framework
**Status:** Accepted (implemented in M3.5–M3.8: `agent.py` and the OpenAI-compatible adapter `llm_provider.py`, D-035, D-037; verified live in M3.8.2)
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
**Status:** Accepted (owner-approved M2.5 B1, 2026-11-13)
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

## D-028 Frozen protocol for the BM25 / semantic / hybrid retrieval comparison
**Status:** Accepted (owner decision at M2.5 closure, 2026-11-13). The protocol was fixed before the held-out dataset was evaluated.
**Outcome:** Hybrid not adopted; BM25 remains default.
**Decision:** Retrieval methods are compared once, on the held-out set `evals/retrieval_v2_heldout.json` (SHA-256 `2d42950ab5ec9e5b6f0fc8069a0ef55c3eb22967e8bb8d2dbb0de5da861eb43c`). The comparison uses the existing harness (D-026, D-027), its 80% relevance rule and its metrics, unchanged.
- **Configurations (all fixed now):**
  - `bm25`, as in D-025, unchanged.
  - `semantic`: the model and settings in D-029.
  - `hybrid`: Reciprocal Rank Fusion of the two, with k = 60 and depth 50 per ranker.
- **Data roles:**
  - `retrieval_v1` is the development set, used only to check that the code works.
  - The held-out set is run once per configuration above, in M2.5 B4. Its questions are not read to choose or tune anything.
  - Any configuration change after that run needs a new held-out set.
- **Adoption criteria** for hybrid over BM25 on the held-out set (all must hold):
  1. Paraphrase Hit@5 improves by at least **2 questions** (out of 20).
  2. Direct Hit@5 loses at most **1 question** (out of 16).
  3. Citation verification stays at **100%** (otherwise the run is invalid anyway).
  4. Warm query **p95 under 50 ms** on the evaluation machine: the harness's primary-search latency, after one warm-up round, excluding model load and passage embedding.
- **Reporting:** the configuration, model identity and fingerprints, and embedding fingerprint are recorded in each report. Direct and paraphrase results are reported separately, with counts as well as ratios. Results from the development set are never cited as evidence for adoption.

**Result (M2.5 B4, 2026-11-13).** Held-out dataset: 40 questions, 36 answerable (16 direct, 20 paraphrase), 4 unanswerable.

| | BM25 | Semantic | Hybrid |
|---|---|---|---|
| Overall Hit@5 | **23/36** (0.639) | 20/36 (0.556) | 22/36 (0.611) |
| Direct Hit@5 | **15/16** (0.938) | 10/16 (0.625) | 14/16 (0.875) |
| Paraphrase Hit@5 | 8/20 (0.400) | **10/20** (0.500) | 8/20 (0.400) |
| MRR@10 overall / direct / paraphrase | 0.469 / 0.844 / 0.169 | 0.416 / 0.536 / 0.320 | 0.444 / 0.664 / 0.268 |
| Citations verified | 277/277 | 200/200 | 253/253 |
| Warm search p95 | 1.80 ms | 17.80 ms | 20.21 ms |

Adoption criteria for hybrid against BM25:

| # | Criterion | Result | Verdict |
|---|---|---|---|
| 1 | Paraphrase Hit@5 improves by at least 2 questions | 8/20 → 8/20 | **Fail** |
| 2 | Direct Hit@5 loses at most 1 question | 15/16 → 14/16 | Pass |
| 3 | 100% of returned citations verify | 253/253 | Pass |
| 4 | Warm query p95 under 50 ms | 20.21 ms | Pass |

- **Runs:** six offline runs (each retriever twice), with no network access attempted. Every repeat matched its first run in all non-timing fields.
- **Integrity:** 100% of returned citations verified in every run. The thresholds, configurations, questions and labels were not changed after the results were seen, and nothing was tuned.
- **Recorded in each report:** the setup recorded in each report: commit `e534c65`, dataset SHA-256 `2d42950a…3c`, model fingerprint `bd55c16c…e102`, embedding fingerprint `c9e2ba25…d27d`.
- **Reports** (in the repository):
  - `evals/results/retrieval_v2_heldout.bm25.json`, SHA-256 `fc10332a78f1ce7f0315b7f3c3e8d72da24a1cd7dc71ee2e40f914943e523c2f`
  - `evals/results/retrieval_v2_heldout.semantic.json`, SHA-256 `06cae0842329d91dc16af1d85a2f8a557bacb6b8f7e2c01cd4b5a5be059996da`
  - `evals/results/retrieval_v2_heldout.hybrid.json`, SHA-256 `2c3dca658d3a2b1836dec35a518e1c031a3f29ec30ce7016f00503ddf51ea4e0`
- **The held-out set is now used.** Any future retrieval configuration needs a new held-out set, written before that configuration is run (D-028 data roles). Evaluation limitations found in this run are listed in architecture §3.2h.

**Why:** with 36 answerable held-out questions, small differences are noise. Fixing the configurations and thresholds before seeing held-out results keeps the comparison from being tuned to the test. The development set already showed how easily one would be tempted: see D-029's limitations.

**Alternatives:**
- Choosing the model, k or depth by trying them on either dataset: rejected, because that is tuning on the evaluation.
- Learned or weighted score fusion: rejected for this experiment, because it needs a tuned weight.
- Adopting on overall Hit@5 alone: rejected, because it hides a direct-question regression behind paraphrase gains.

## D-029 Local embedding model, runtime and fusion for the retrieval experiment
**Status:** Accepted as the record of the configuration tested (M2.5 closure, 2026-11-13). **Not adopted** (D-028): BM25 remains the default retriever. The code remains available as an optional, inactive-by-default experiment (`--retriever semantic|hybrid`, `semantic` extra). No further retrieval tuning was done in M2.5.
**Decision:** `semantic_retrieval.py` adds `SemanticIndex`, `fuse_rrf` and `HybridIndex` over the existing `Passage` objects. Passages, citation ids and verification are unchanged.
- **Model:** `BAAI/bge-small-en-v1.5` at commit `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, file `onnx/model.onnx` (BAAI's own fp32 export, 133,093,490 bytes, SHA-256 `828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35`, MIT license).
  - It produces 384-dimensional vectors, uses CLS pooling with L2 normalization, and truncates input at 512 tokens.
  - **No query instruction prefix** is used.
- **Provenance:**
  - All five model files are pinned by size and SHA-256 in code (`MODEL_FILES`). On download they were also checked against Hugging Face's published LFS hash and git blob ids.
  - `install_model` downloads that exact commit and moves files into `data/models/` (git-ignored) only after verification.
  - Every load verifies the files again. A missing or changed file raises `ModelIntegrityError`, and the evaluation refuses to run.
  - The model fingerprint (`bd55c16c7bac8abe40fa2da1220f37380b477da579132ba11067bb488525e102`) is SHA-256 over the file hashes.
- **Runtime:**
  - FastEmbed 0.9.1 with ONNX Runtime 1.23.2 on CPU, in the optional `semantic` extra (`uv sync --extra semantic`).
  - FastEmbed loads only from the local directory (`specific_model_path`), so it never downloads and never resolves a floating revision.
  - `THREADS = 1` and `BATCH_SIZE = 32` keep floating-point results identical between runs.
  - onnxruntime is pinned to 1.23.2 because later releases have no Intel macOS wheels for Python 3.13. On Python 3.14, FastEmbed's own requirement applies.
- **Search:** exact cosine similarity (dot product of unit float32 vectors) over all passages, in memory, with no vector database. Filters and tie-breaks are exactly those of `KeywordIndex`. A blank query returns `[]`.
- **Fusion:** RRF score = Σ 1/(60 + rank) over each ranker's top 50, with the same filters applied to both rankers. Equal scores break by document identity, then position. Hits are the original `Passage` objects.
- **Fingerprints in every report:** model repository, revision, file hash and fingerprint, and the embedding fingerprint (SHA-256 of the citation ids plus the exact vector bytes), together with truncation counts.

**Why:**
- **BAAI's file, not FastEmbed's default:** FastEmbed's default copy of this model is a third-party fp16 file built for GPUs (`Qdrant/bge-small-en-v1.5-onnx-Q`). BAAI's file is first-party and fp32.
- **Pinning by our own download:** FastEmbed can't pin a revision itself, so pinning has to happen in our download, enforced by hashes. The default repository changed as recently as 2026-09-24.
- **Exact search in memory:** for about 1,200 passages it takes milliseconds and needs no database.

**Inference configuration audit (2026-11-13, documentation only; nothing changed).** The implementation was compared with the configuration published at the pinned commit `5c38ec7c…`: `modules.json`, `1_Pooling/config.json`, `sentence_bert_config.json` and the model card (`README.md`, SHA-256 `ddb964361a55c6e5dfca6361615854b260c9c960205d04c7520151aaa1d75837`).

| Setting | Model's published configuration | This implementation | Match |
|---|---|---|---|
| Pooling | CLS token (`pooling_mode_cls_token: true`; card: `model_output[0][:, 0]`) | FastEmbed `PoolingType.CLS` on `last_hidden_state` | Yes |
| Normalization | L2 (`2_Normalize` module; card: `normalize_embeddings=True`) | `normalization=True` (unit-length vectors) | Yes |
| Input length and case | `max_seq_length: 512`, `do_lower_case: true` | Tokenizer truncation at `model_max_length` 512; BERT lowercase tokenizer | Yes |
| Passage formatting | "no instruction needs to be added to passages" | Raw passage text | Yes |
| Query formatting | For short queries searching long passages, the card *recommends* the prefix `Represent this sentence for searching relevant passages: `. It also says v1.5 without the prefix shows "only a slight degradation", and that the best choice is whichever performs better on the task. | No prefix | **No: differs from the recommended short-query usage** |

- The output matched an independent onnxruntime pipeline (CLS, then L2 normalization) exactly, so pooling and normalization are implemented as published.
- **The no-prefix choice stands as frozen in this record, and is not changed by this audit.** It is a permitted but not recommended usage for this task type, and is a known limitation of the semantic and hybrid configurations.
- Changing it would mean a new configuration, decided by the owner before the held-out run (D-028). It must never be chosen by comparing results on either dataset.

**Known limitations (observed on the development set `retrieval_v1`, not used for any choice):**
- **Truncation:** 61 of 1,207 passages exceed 512 tokens and are truncated, mostly tables.
- **Cost of one thread:** embedding the corpus on one CPU thread takes minutes, which makes semantic and hybrid evaluation runs much slower than BM25.
- **Development results:** semantic-only retrieval did worse than BM25 on `retrieval_v1`, including on paraphrases. That set has only 5 paraphrase questions, and this is recorded without changing anything.
- **Query side:** embeddings know nothing about company or period, so cross-filing competition remains. Filters still solve that.
- **Query formatting:** queries are embedded without the instruction prefix the model card recommends for short-query retrieval (see the audit above).
- **Platform:** ONNX Runtime results are reproducible on one machine with fixed threads, but may differ by small amounts across CPUs or builds. The embedding fingerprint shows when they do.

**Held-out assessment (M2.5 B4; see D-028 for the full result).**
- **Configuration tested:**
  - `BAAI/bge-small-en-v1.5` @ `5c38ec7c…`, the fp32 `onnx/model.onnx`, through FastEmbed 0.9.1 and ONNX Runtime 1.23.2 on CPU.
  - CLS pooling, L2 normalization, 512-token truncation, no query prefix.
  - 1 thread, batch size 32, exact cosine search.
  - RRF with k = 60 and depth 50, with equal weight for the two rankers.
- **Semantic retrieval gained on paraphrases but lost on direct questions.**
  - Paraphrases: Hit@5 rose to 10/20 from BM25's 8/20, and MRR@10 to 0.320 from 0.169. It recovered 5 paraphrase questions BM25 missed (final assembly, iPad decline, services margin, client money, credit ratings) and lost 3 that BM25 found.
  - Direct questions: Hit@5 fell to 10/16 from BM25's 15/16. It lost questions that turn on exact figures, dates or terms (indirect-channel share, FX value-at-risk, ASU 2023-09, manufacturing obligations, dividend), often by ranking a passage about the right topic from the wrong period first.
- **RRF didn't keep enough of the semantic gains.** Hybrid kept 2 of the 5 recovered paraphrases (iPad decline, services margin). With equal weights at k = 60, a passage ranked moderately by *both* rankers outscores one ranked first by only one of them. For example, ranks 5 and 10 give 1/65 + 1/70 ≈ 0.030, against 1/61 ≈ 0.016 for a semantic-only first place. So the semantic-only hits were pushed below passages both rankers rated as fair but which didn't contain the evidence. Hybrid also lost 1 direct question and 2 paraphrases that BM25 alone found. The net paraphrase change was 0.
- **Costs:**
  - Model load is about 0.9 s.
  - Embedding all 1,207 passages on one thread takes 331–337 s, so evaluation runs take about 6 minutes instead of 11 s.
  - Warm query p95 is 18–20 ms, about 10× BM25's but within the 50 ms criterion.
  - Installing the extra adds about 170 MB of dependencies and 134 MB of model files.
- **Truncation:** 61 passages exceed 512 tokens (the longest has 1,867), so their tails aren't represented in the embedding.
- **Query formatting:** queries are embedded without the instruction prefix the model card recommends for short-query retrieval (audit above). The tested configuration therefore isn't the model's recommended usage for this task.

**Why the semantic configuration stays experimental, not rejected outright.** Only *this* configuration failed: no query prefix, 512-token truncation of long passages, and equal-weight RRF at k = 60. The paraphrase gains show the model has a real signal. These are open questions for a future, separately pre-registered experiment, which needs a new held-out set:
- the recommended query prefix;
- passages sized to the model's limit;
- a different fusion design;
- a different model.

Nothing here rejects semantic or hybrid retrieval in general.

**Alternatives:**
- FastEmbed's default fp16 file: rejected (provenance; tuned for GPU).
- sentence-transformers with PyTorch: rejected (a dependency footprint of over 700 MB).
- Hosted embeddings: rejected (needs network access and a key, and the model can change without notice).
- pgvector or FAISS: unnecessary at this size.
- Calling onnxruntime directly without FastEmbed: possible, about 40 lines. The owner chose FastEmbed, and its output matched this direct pipeline exactly in a check.

## D-030 Immutable structured-data snapshots and exact company identity
**Status:** Accepted (M3.2 B1, 2026-11-13; implements the approved M3.1 design; the five snapshots were acquired and pinned in M3.2 B2)
**Decision:**
- **What is stored:** SEC submissions, Company Facts and `company_tickers.json` responses, as immutable snapshots in the existing git-ignored cache (`filing_cache.py`, architecture §3.2i).
  - Content-addressed by the full SHA-256 of the exact response bytes.
  - Metadata (`SnapshotMetadata`: kind, CIK, `SourceMetadata`) is stored beside them.
  - Each file is created atomically and only once, and never overwritten.
- **Loading:** a load names kind, CIK and SHA-256, and verifies metadata, the canonical URL and the bytes' hash. It refuses with `SnapshotError` (`missing`, `corrupted`, `mismatched`, `invalid`) and never repairs or refetches.
- **Parsing:** snapshots are parsed by the live client's own parsers, now public and unchanged in behavior.
- **Fetching:** `SecClient.get_snapshot` fetches a snapshot and refuses one that doesn't parse.
- **Identity:** companies are resolved only against pinned, already-loaded snapshots:
  - by CIK, by exact case-insensitive ticker, or by a fixed exact alias map for Apple and JPMorgan Chase;
  - no fuzzy matching;
  - any identifier matching more than one CIK is rejected as ambiguous.
- **CIK verification (owner correction, 2026-11-13):** a CIK isn't required to appear in the ticker map.
  - **Listed in the ticker map:** resolved and verified by it, as before.
  - **Not listed:** verified by a submissions snapshot the caller has already loaded through `load_submissions_snapshot`. It verifies only if its record's CIK *and* its canonical source URL both match the requested CIK.
  - **Never fetched:** identity resolution never downloads submissions.
  - **Verified by neither:** `company_not_found`.
  - **Tickers and aliases:** use the ticker map only.
  - `CompanyIdentity.verified_by` (`company_tickers` | `submissions`) and `source` record which snapshot verified the company.
- **Kept decisions (owner-confirmed):**
  - saving identical bytes again keeps the first retrieval's metadata;
  - one malformed ticker-map row rejects the whole snapshot;
  - hard-link creation is acceptable on the supported filesystems;
  - snapshot SHA-256 pins are recorded in M3.2 B2.
- **Acquisition (M3.2 B2, 2026-11-14):**
  - Five requests, all HTTP 200, through a transport that enforces an exact allowlist, one attempt per URL and a total of five, with `max_retries=0`.
  - Pinned in `evals/sec_snapshots_v1.json`, manifest version 1. Raw snapshots are not committed.
  - `SecClient` gained an optional `max_retries` argument; the default behavior is unchanged.

**Why:**
- Submissions and Company Facts change over time, so reproducible financial answers and evaluations need one exact, verifiable version.
- Content addressing makes that version's identity its hash.
- Create-once writes mean a pinned snapshot can't silently change.
- Exact identity matching avoids wrong-company answers, which are worse than a refusal.

**Alternatives:**
- Overwriting a per-company "latest" file: rejected, because pins would drift.
- Caching parsed objects: rejected, because re-serialization changes bytes and loses provenance.
- Silently refetching on a bad snapshot: rejected, because evaluations must fail loudly.
- Fuzzy name matching: rejected for the first release.

## D-031 Grounding rules for fiscal periods and same-filing comparisons (for M3.3)
**Status:** Accepted (owner corrections to the M3.1 design, 2026-11-13). Implemented in M3.3 (D-032, D-033).
**Decision:**
1. **Historical fiscal periods are grounded in actual filing report dates and observation period boundaries.**
   - A company's `fiscal_year_end` (submissions) is a consistency check, not the sole authority.
   - SEC's Company Facts `fy`/`fp` tags describe the reporting filing and never define an observation's economic period.
2. **`same_filing` comparisons select each observation by exact accession number, concept, unit and period boundaries.**
   - Values from different filings are never mixed silently.
   - If either value is not reported in the required filing, the comparison is refused.

**Why:** these keep requested periods, original reporting filings, observation source filings and commentary filings distinct (the M3.1 design). They also make sure numbers match the filing whose commentary explains them.

## D-032 Exact concept mapping, grounded fiscal periods and explicit fact selection
**Status:** Accepted (M3.3 B1, 2026-11-14)
**Decision:** `financial_tools.py` adds `CONCEPT_MAPPINGS` (version 1), `resolve_period` and `get_fact` (architecture §3.2j).
- **Mapping:** one exact `us-gaap` concept and unit per company and metric, for revenue, net income and total assets at Apple and JPMorgan Chase. Each mapping records the company's statement line and the reason for the choice.
  - Each was validated against the face financial statements of the cached filings: Apple's FY2026 10-K and Q3 FY2026 10-Q, and JPMorgan's Q3 2026 10-Q.
  - There is no fallback list and no substitution: a missing concept is an error.
  - **JPMorgan revenue is `RevenuesNetOfInterestExpense`.** `Revenues` matches it in 10-Ks but hasn't been reported in a 10-Q since 2014.
- **Periods** follow D-031:
  - **Fiscal year N** is defined by the 10-K whose report date falls in year N. Its start is the single start of that 10-K's own 12-month observations, checked against the prior 10-K's report date when listed.
  - **Quarters** are the 10-Qs after the fiscal-year start, chained exactly by their own 3-month observations.
  - **Instants** take those period ends.
  - `fiscal_year_end` is a ±7-day consistency check. SEC's `fy`/`fp` tags are never used.
  - Unlisted filings are never inferred, and Q4 is never derived.
  - `ResolvedPeriod` keeps the requested period, original reporting filing and commentary filing separate. A fact's source filing is its observation's accession.
- **Selection policies:**
  - `latest_filed` and `original` use the existing `select_fact` across eligible filings.
  - `same_filing` uses only the named accession and is refused when that filing doesn't report the value. Filings are never mixed.
  - Each `Fact` keeps the Decimal value, the selected observation (accession, form, filed date), conflicting-value indicators and the other values, and the Company Facts snapshot's provenance.
  - It also carries a logical `fact_key` (company, metric, mapping version, concept, unit, period, policy, required accession) and an immutable `observation_id` (that key plus value, accession, form, filed date and snapshot SHA-256).

**Why:**
- SEC's `fy`/`fp` tags describe the reporting filing, not the period.
- Report dates and each filing's own observation boundaries are the only sources that pin exact periods without guessing.
- One verified concept per metric avoids silently combining different measures.
- `same_filing` is what makes a comparison's two values match the commentary that explains them.

**Alternatives:**
- Fiscal periods from `fy`/`fp` or `frame` tags: rejected (D-031).
- Concept fallback lists: rejected, because they hide that two concepts may measure different things.
- `us-gaap:Revenues` for JPMorgan: rejected, because it isn't in 10-Qs.
- Deriving Q4 or missing quarters from year-to-date values: rejected for M3.

## D-033 Same-filing comparisons and the percentage-change policy
**Status:** Accepted (M3.3 B2, 2026-11-14)
**Decision:** `compare_periods` compares one metric between two exact periods, with both values read from a single filing (architecture §3.2k).
- **The filing:** the current period's original reporting filing, the commentary filing by default, or a named listed 10-K or 10-Q.
- **Both observations** must use the mapped concept and unit, match their exact period boundaries, and come from that filing. Otherwise the comparison is refused: `observation_not_in_filing`, `period_class_mismatch`, `unit_mismatch`.
- **Only `same_filing` is implemented.** Separately sourced comparisons are refused, never used as a fallback.
- **A prior period whose original filing isn't in the submissions snapshot** may, for year-over-year comparisons only, take its boundaries from the filing's own single prior-year comparative period, checked against the known fiscal calendar.
  - `ResolvedPeriod.basis` is then `comparative_in_filing`, and `original_filing` is `None`. Unlisted accessions found in Company Facts are named as unverified, never adopted.
- **Percentage:**
  - Exact `((current - prior) / prior) × 100` as Decimal (34 significant digits).
  - Shown half-even to one decimal place only for a positive prior value.
  - Undefined for a zero base, and not meaningful for a negative base.
  - Sign changes are flagged both ways. The delta is exact.
- **Revisions:** other filings' different values for either period are listed separately (earlier, later or same date) and never replace the compared values.
- **Identity:** each comparison carries both facts' keys and observation ids, the snapshot fingerprint, and a deterministic `comparison_id`.

**Why:**
- Values from one filing are the ones its MD&A explains, so numbers and commentary can't disagree because of a later restatement.
- A comparative inside a later filing is real reported data. Treating it as such, while saying the original filing is unverified, avoids both inventing an accession and refusing a period the filing plainly reports.
- Percentages of non-positive bases mislead, so the policy shows only the delta.

**Alternatives:**
- Mixing the latest value for each side: rejected, because it can pair numbers no filing discussed.
- Using the original filing accession Company Facts reveals for an unlisted period: rejected, because it isn't verified against submissions.
- Percentages with absolute-value bases: rejected for this policy.

## D-034 Evidence-backed analysis: same-filing numbers, verified candidate commentary, no LLM
**Status:** Accepted (M3.4, 2026-11-14)
**Decision:** `explain_change` returns one `GroundedResult` that joins `compare_periods` with BM25 retrieval and batch citation verification, all offline (architecture §3.2l).
- **Numbers:** only from the `Comparison`. The summary is a fixed template over its fields; no figure is computed or restated anywhere else.
- **Commentary scope:** only the comparison's commentary filing, from the filing cache. The index holds only that document's passages. Its MD&A section (10-K Item 7, 10-Q Part I, Item 2) is searched when the extractor found that label reliably; otherwise the whole verified filing. A filing that is missing or fails integrity checks is never fetched or substituted.
- **Default query:** the company's own term for the metric plus its words for a change, fixed per company and versioned. An explicit query may replace it.
- **Evidence:** a retrieved passage is evidence only if its citation verifies, it comes from the searched filing and section, and its tokens contain the company's term for the metric. Every other hit is listed as rejected, with the reason.
- **Support level:** every evidence item is a `candidate`. A verified citation proves source integrity, not that the passage explains the change, and no causal claim is generated.
- **Status:** `complete` (comparison and at least one evidence passage), `partial` (comparison, no usable commentary), `abstained` (no reliable comparison; no summary, no commentary).

**Why:**
- Reading commentary from the filing that reports both values keeps numbers and narrative from different restatements apart (D-033).
- An index per filing makes it impossible for another company's or filing's passage to rank, or to influence the statistics.
- Lexical retrieval cannot establish meaning, so the result must not imply that it can. Labeling candidates, and keeping integrity separate from support, leaves that judgment to a person or to a later, checked step.

**Alternatives:**
- Ranking within the frozen multi-filing index with an accession filter: rejected, because other filings would still shape the scores, and the baseline must stay untouched.
- Treating the top hit as the explanation: rejected, because it would present a lexical match as a causal claim.
- Falling back to another filing's commentary, or broadening a missing section label to the whole filing for 10-Ks: rejected for numbers-to-narrative consistency. Only filings with no reliable label at all are searched whole.
- Semantic retrieval: not adopted (D-028); BM25 stays the default.

## D-035 Provider-independent tool-calling loop with structured, unvalidated drafts
**Status:** Accepted (M3.5 B1, 2026-11-14)
**Decision:** `agent.py` runs a bounded tool-calling loop against a one-method model interface, `complete(messages, tools) -> ModelTurn`, tested with a scripted fake model (architecture §3.2m).
- **Tools:** five narrow adapters over existing code (`resolve_company`, `get_fact`, `compare`, `commentary_filing`, `search_commentary`). Arguments are strict pydantic schemas with no extra fields. No tool accepts a path, URL or data source; comparisons are referenced by a `comparison_id` from the same run.
- **Errors:** tool-side problems (unknown tool, malformed arguments, unsupported company or metric, unresolvable period, unexpected exception) are returned to the model as structured errors. Model-side problems (exception, malformed turn) end the run.
- **Limits:** 4 model turns (raised to 6 by D-038; including the repair), 6 tool calls, 90 seconds checked between steps, 1 structured-output repair. A turn that would exceed the call budget runs none of its calls; tools requested in the last allowed turn are not run.
- **Drafts:** numbers appear only as `{placeholder}`s mapped to tool `value_id`s; passages only as citation ids; at most three quotes of 300 characters. Only the structure is checked, and every draft is labeled `unvalidated_draft`.
- **Deferred to B2:** checking references and quotes against this run's tool results, server-side rendering, and a live provider adapter.

**Why:**
- A single small interface keeps provider code out of the loop, so the scripted model tests the exact path a live adapter will use.
- Placeholders and ids make every number traceable to a deterministic tool, and let B2 reject or render them mechanically instead of parsing prose.
- Returning tool errors as data lets a model correct itself within fixed limits, while model failures cannot loop.

**Alternatives:**
- An agent framework: rejected (no new dependency; the loop is about 80 lines).
- Letting the model write numbers into the answer and checking them later by text matching: rejected for B1 drafts, because placeholders are exact and cheaper to validate.
- A fake agent returning canned answers: rejected, because it would not exercise tool selection, validation or limits.

## D-036 Deterministic grounding: publish only what can be checked mechanically
**Status:** Accepted (M3.5 B2, 2026-11-14)
**Decision:** a draft becomes an answer only through `grounding.validate_draft` and `render_answer` (architecture §3.2n).
- **Evidence:** the validator checks the draft against `RunEvidence`, the typed objects this run's successful tool calls produced. It never uses the model's account of tool results.
- **Numbers:** every placeholder must map to a `value_id` produced in this run for the answer's subject comparison (same company, metric and periods). The narrative may contain no digits (removed by D-043). Any failure means the answer abstains.
- **Citations:** each must be a verified passage from this run's search of the subject's commentary filing, whose cached bytes still hash to the cited source and whose ID still describes its text (full rebuild optional). Failures drop the item, and the answer is at most partial.
- **Quotations:** at most 3, of 300 characters each. Each must be an exact substring of a grounded passage. Management attribution is allowed only for MD&A passages.
- **Published content:** only the server-rendered statement and figures with provenance, verified attributed quotations, and candidate commentary. The model's wording and explanations are withheld, with reasons.
- **Status:** abstained (no valid draft or subject, or any numeric problem), partial (valid figures, no or rejected commentary), complete (valid figures, at least one grounded citation, no issues).
- **Evaluation:** a labeled scripted-model set (26 cases) measures the pipeline, not LLM reasoning.

**Why:**
- Numbers and quotations can be checked exactly, so they are. Causes and relevance cannot be, so they are not published, rather than being approximated with keywords.
- Server-side rendering means a model cannot misformat, misattribute or relabel a figure, even when it follows injected instructions.
- Rejecting the whole answer on any numeric problem treats a fabricated number as evidence the draft is unreliable. A bad citation only removes that citation, because the figures are independently verified.

**Alternatives:**
- Publishing the model's sentence with server-filled numbers: rejected for now, because the words around a correct number can still misstate direction or cause.
- Keyword-based causal-claim detection: rejected; it can't establish entailment and invites false confidence.
- Re-running full citation verification on every answer: optional (`full_reverify`). The run already verified each passage in the same process, and the cheap checks catch changed bytes and altered text.

## D-037 One OpenAI-compatible adapter behind the existing Model interface
**Status:** Accepted (M3.6 B1, 2026-11-14)
**Decision:** live models connect only through `llm_provider.OpenAICompatibleModel.complete(messages, tools)` (architecture §3.2o).
- **Format:** OpenAI-compatible Chat Completions over httpx, with no SDK or new dependency. Tools are functions, and the answer draft is a `submit_answer` function call whose parameters are the `AnswerDraft` schema. Provider call IDs are kept.
- **Requests:** one per model turn and never retried. Provider errors, malformed responses and timeouts end the run as `model_error`. Each request's timeout never exceeds what is left of the 90-second run.
- **Configuration:** `LLM_API_KEY`, `LLM_MODEL` and optional `LLM_BASE_URL` from the environment. The key is a `SecretStr`, never logged, and redacted from reported provider text. Configuration errors name settings, never values.
- **Live use:** only through `python -m financial_analyst.live_agent`, run by a person. The test suite uses mocked providers only.
- **Unchanged:** the agent loop, tools, limits, grounding validator and server-side renderer. The live answer is the grounded, server-rendered answer; the draft is never printed.

**Why:**
- The Chat Completions shape is accepted by many providers and local servers, so one adapter covers them without an SDK.
- A function for the answer gives structured output on any server that supports tool calls, without depending on provider-specific JSON modes.
- Not retrying keeps cost and run time bounded and visible. A failed request is a failed turn, which the existing loop already handles.

**Alternatives:**
- A provider SDK: rejected for now (new dependency, hidden retries by default).
- Provider-native structured output (`response_format`): deferred, because support varies across compatible servers.
- Live tests inside pytest behind a skip marker: rejected; they would make "0 skipped" meaningless. Live runs stay in the CLI.

## D-038 Six model turns, a workflow prompt, and diagnosable live runs
**Status:** Accepted (M3.6 B2.1, 2026-11-14)
**Context:** the first live smoke tests (GPT-5.4 mini, two cases, 7 requests, ~$0.0127) made one tool call per turn. Resolve → compare → search → answer needs exactly four turns, so Apple answered without commentary (partial) and JPMorgan ran out of turns (abstained). No provider or schema errors occurred, and the validator rejected nothing incorrectly.
**Decision:**
- **Turns:** `MAX_MODEL_TURNS` rises from 4 to 6 (a repair still counts). Unchanged: 6 tool calls, 90 seconds, one repair, request timeouts capped by the run's remaining time. Tools requested in the last allowed turn are still never run, and an invalid draft on the last turn gets no repair.
- **Prompt:** the system prompt is built from the pinned data. It lists the supported companies (name, CIK, mapped metrics; no figures) and states the workflow:
  1. identify the company (use a listed CIK directly when unambiguous, otherwise `resolve_company`) and the periods;
  2. `compare`;
  3. `search_commentary` for questions about a change, but not again if it finds nothing;
  4. submit the draft with tool value IDs and citation IDs only.

  It also says never to invent IDs, and how to handle errors and the budget.
- **Diagnostics:** each live case reports, from the typed run results:
  - ordered tool names with ok or error code, and per-tool counts;
  - model turns, and the terminal reason;
  - whether `compare` and `search_commentary` succeeded and whether an answer was submitted;
  - grounding issue codes, tokens, cost and latency.

  It never reports tool arguments, error messages, provider payloads or configuration.
- **Exit code:** `live_agent eval` exits 1 when any selected case fails a check, after printing (and writing) the full report.

**Why:**
- Two extra turns let the observed one-call-per-turn style reach the full path, with room for one correction. The call budget, which bounds tool work and cost, is unchanged.
- Listing the supported companies saves a turn without hardcoding any answer. The figures still come only from `compare`.
- Without the tool sequence, the first JPMorgan failure couldn't be explained. Codes and names are enough to diagnose, and safe to share.

**Alternatives:**
- Letting `compare` take a company name: rejected for now, because it would merge identity resolution into a financial tool.
- Forcing `search_commentary` in code: rejected, because some questions need no commentary. The prompt asks for it, and the evaluation labels check it.

## D-039 Repair through the answer tool, and independent request scope before publishing
**Status:** Accepted (M3.6 B3.1, 2026-11-14)
**Context:**
- **Live B3:** four `model_error` runs (rejected draft, then an empty repair response) and one Q4 question answered with fiscal-year figures and published as complete (architecture §3.2q).
- **Repair path defects:** it erased the `submit_answer` call, contradicted it ("do not call tools"), offered every tool, gave unlocated feedback and recorded no reasons.

**Decision:**
- **Repair:**
  - a draft submitted through `submit_answer` keeps its call ID, and a rejection is returned as that call's tool result with structured codes and located problems (no input values);
  - the repair turn offers no agent tools, and the adapter sends and forces only `submit_answer`;
  - limits are unchanged: one repair, none on the last turn;
  - rejection codes are kept in `AgentRunResult.draft_rejections` and the live diagnostics;
  - null optional lists in a draft are read as empty.
- **Request scope:**
  - `request_scope.capture_scope` reads company, metric and periods from the question with fixed, documented patterns;
  - `ground` requires the subject comparison to match it exactly, and otherwise abstains: `scope_unresolved` when the scope is unparsed, ambiguous or unsupported, `scope_mismatch` when the subject differs;
  - the model's tool calls cannot change the scope.
- **Evaluation:** labels are unchanged. The scripted-behavior labels are documented, and an outcome-based `agent_v2` revision is proposed separately.

**Why:**
- In function calling, an answer that fails validation is a failed call. Answering it as one, and allowing only the answer function, gives the model one unambiguous way to repair.
- The validator proved that figures came from the run, not that they answer the question. A deterministic, question-only reading closes that gap without letting the model define what was asked.
- Abstaining on an unreadable question is the conservative choice. It loses some answerable questions but never publishes a substituted comparison.

**Alternatives:**
- A general natural-language parser or an LLM-based intent extractor: rejected, because it would be neither deterministic nor independent of the model.
- Letting the draft declare its requested scope: rejected, because the model would be grading its own reading of the question.
- Leniency for invalid drafts beyond null lists: rejected; the placeholder rule and value-ID patterns stay strict.

## D-040 Commentary only when asked, citation feedback, consolidated metrics only, and outcome-based evaluation
**Status:** Accepted (M3.7, 2026-11-14)
**Decision:**
- **What a complete answer must contain** comes from the question:
  - figures only, unless the question asks for commentary (fixed cues) or a quotation;
  - commentary requires at least one grounded citation, which can only come from this run's search of the subject comparison's filing;
  - a quotation requires a verified, correctly attributed quotation.

  Comparing two periods does not require commentary. Any presented citation or quotation must still pass every check.
- **Citations:** the prompt requires citation IDs copied exactly from this run's search results. A structurally valid draft citing an ID the run never returned uses the single repair (`citation_not_returned`), with the list of returned IDs. On the last attempt the draft stands and grounding rejects the citation. Nothing is substituted.
- **Metrics:** only consolidated figures. A question naming a reported segment, product or region, or asking for a breakdown, has an unsupported scope and abstains.
- **Evaluation:** `agent_v1.json` is frozen. `agent_v2.json` labels the information need and acceptable statuses, never a tool path, and scores ten dimensions independently. The six v1 cases whose status changes under this policy are listed and tested.

**Why:**
- Requiring commentary for every comparison made correct numerical answers look incomplete. Requiring it only when asked keeps "partial" meaningful.
- The live run showed a model citing an ID it never received. Telling it which IDs exist is the least it needs to correct itself, and leaves verification untouched.
- "iPhone net sales" is not revenue. Answering with consolidated figures would answer another question.
- Path-specific labels scored correct live abstentions as failures. Outcome labels measure what the user needed.

**Alternatives:**
- Forcing a commentary search in the agent loop: rejected, because the requirement belongs to the published answer, and a forced extra turn would spend budget on questions that do not need it.
- Short citation aliases for the model: rejected for now, because mapping aliases to IDs is a substitution layer the validator would have to trust.
- Narrower segment detection (only directly before a metric word): rejected for now, as more fragile. The broad rule abstains rather than risk a consolidated answer.

## D-041 Response-shape diagnostics, honest usage accounting, and no placeholder syntax rules
**Status:** Accepted (M3.8, 2026-11-14)
**Decision:**
- **Response shapes:** the adapter records each response's structure (key names, content and part types, tool-call count and names, finish reason, refusal and usage presence), never its content.
- **Parsing:** structured text parts are read. `empty_response`, `refusal` and `content_filter` are distinct error kinds.
- **Usage:** recorded whenever the body carries it, even if parsing then fails. Requests without usage are counted and reported, never treated as zero, and costs are then lower bounds.
- **Draft contract:** the three placeholder-syntax rules are removed, and `placeholder` is optional. Every grounding check stays: value IDs from the run and the subject, no digits in the narrative, request scope, citations, quotations, status policy.
- **Repair feedback:** names each rejected field with its permitted correction, never the rejected value. The single repair and the budgets are unchanged.

**Why:**
- The placeholder rules guarded the rendering of a narrative that has not been rendered since D-036, so they only produced failures (four of the M3.7 live drafts). Removing them weakens no published guarantee.
- Without the response shape, an empty response cannot be diagnosed. Without the failed requests' usage, costs were silently understated.

**Alternatives:**
- Making the narrative optional, or dropping the digit check: deferred. The digit check is a cheap signal of an unreliable draft and caused no observed failure.
- Treating an empty response as a decline: rejected, because it would turn a provider failure into an abstention that looks deliberate.

## D-042 Require a function call on every turn; the last turn may only answer
**Status:** Accepted (M3.8.1, 2026-11-14)
**Decision:**
- **Normal turns:** sent with `tool_choice: "required"` (the five agent tools plus `submit_answer`).
- **Repair turns and the last allowed turn:** offer and force only `submit_answer`.
- **Refusals unchanged:** a call to an agent tool on those turns is refused as before (`invalid_draft` after a repair, `turn_limit` on the last turn) and never runs.
- **Unchanged:** the budgets, the single repair, and every validation and grounding rule.

**Why:**
- The live empty responses were the model ending its turn without a call, which `"auto"` permits.
- No acceptable turn in this agent is plain text, so requiring a function call removes an option the agent cannot use.
- Forcing the answer on the last turn turns a guaranteed `turn_limit` into a draft that grounding can still accept or reject.

**Alternatives:**
- Treating an empty message as a repair trigger: rejected, because it spends a turn and the repair on a provider behavior the API can prevent.
- `parallel_tool_calls: false`: not needed, since several calls in one turn are already handled and budgeted.

## D-043 Remove the digit rule; keep model-written text out of the answer
**Status:** Accepted (M3.8.2, 2026-11-14; verified live: the final two-case run passed 2/2)
**Decision:**
- **Digit rule:** `raw_number_in_text` is removed. Digits in the model's note no longer abstain, and no placeholder rule replaces it.
- **Model text in the answer:** the answer object no longer contains any model-written text except verified quotations. Withheld notes and explanations are recorded by kind and length, with their text kept only in the run record. A rejected quotation's words are not echoed in issue details, which the CLI prints.

**Why:**
- A code trace shows the note influences nothing that is published: figures, scope, citations, quotations, attribution or status (apart from the rule itself).
- The rule's purpose was that no model-typed number reaches the user. That is now enforced more strongly: no model-typed text of any kind reaches the answer object.
- The rule was the sole cause of the remaining live failure (M3.8.1, Apple).

**Alternatives:**
- Requiring placeholders for periods: rejected, because it reintroduces syntax the model failed to follow and protects nothing that is published.
- Keeping the rule as a "hallucination signal": rejected, because it abstained correct, in-scope answers. The signals that matter (value IDs, scope, citations, quotations) remain.

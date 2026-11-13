# Architecture — Enterprise Financial AI Analyst

Status: **Milestones 0–2.4 implemented** (owner-approved: evaluation harness, real dataset and BM25 baseline); **Milestone 2.5 B1 (batch citation verification) approved; B2 (held-out evaluation dataset) awaiting review**. Sections marked "later" are design intent, not commitments.
Last updated: 2026-11-13

---

## 1. Current repository state

| Item | Finding |
|---|---|
| Code | Milestone 0 (FastAPI `/health`, config, SEC client) and Milestone 1 (filing retrieval, extraction, document cache, XBRL Company Facts, M1.5 hardening) are implemented. The project directory was empty when this document was first written. |
| Git | Originally had no repository of its own (the enclosing repo was the home directory). **Resolved:** the owner initialized a project repository on `main`, remote `https://github.com/JerrodMcHenry/financial-ai-analyst.git`, with an initial commit containing `README.md`. |
| Python | 3.13.5 is installed (python.org and Anaconda builds). No 3.12 interpreter is installed. |
| Tooling | `uv` 0.11.14 is installed. Docker is **not** installed (needed in a later milestone). |

Deferred work is listed in §10. Decisions are recorded in `docs/decisions.md`.

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

### 3.1 Milestone 0: health check (implemented)

```
GET /health
  → uvicorn (ASGI server) → FastAPI router → health handler
  → 200 {"status": "ok", "version": "0.1.0"}
```

This is a liveness check only. It performs no SEC or database calls, so an outage of a dependency does not make the process look dead. A separate readiness check can be added when a database exists.

### 3.2 Milestone 0: company submissions fetch (implemented)

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

There is no HTTP endpoint for this. The owner decided against a debug route (D-016), so the client stays internal until ingestion exists.

### 3.2a Milestone 1.1: filing document retrieval (implemented)

**Discovery vs retrieval.** *Discovery* (`get_submissions`) asks EDGAR which filings a company has and returns trusted metadata: accession number, form, dates and primary document name. *Retrieval* (`get_filing_document`) downloads one filing's primary HTML document. Retrieval never searches or crawls. It takes a `FilingRef` produced by discovery and fetches exactly the archive URL derived from it.

```
caller holds a FilingRef from get_submissions
  → SecClient.get_filing_document(filing)
      1. filing_document_url(filing)     → form must be 10-K/10-Q (or /A); primary document required;
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
  ← ExtractedFiling(filing, source, extractor_version, blocks[ContentBlock], diagnostics)
```

**Extractor version.**
- `EXTRACTOR_VERSION` (currently `"2"`; `"1"` gave 10-Qs no sections) is recorded on every `ExtractedFiling`.
- Bump it whenever a change could alter block text, kinds, order, indexes or locations for the same input bytes. A `beautifulsoup4` upgrade can do this too.
- A block is identified only by **source SHA-256 + extractor version + block index**. Any stored chunk or citation identifier must include all three (D-022).

**Tables.** Each `<table>` becomes one block. `rows` holds the cell grid in original row and cell order, and is the authoritative form. A cell with `colspan=n` is followed by n−1 empty-string placeholders so that values stay under their column headers. **These placeholders look exactly like genuinely empty source cells**, so the grid shows where a spanning header starts but not how far it extends. **`rowspan` is not supported yet:** a merged cell appears only in its first row, and later rows can be shorter or misaligned (2 of 60 tables in Apple's 2026 10-K have rows of different lengths). Consumers must not assume tables carry complete merged-cell structure. Rows with no text at all (spacer rows) are dropped. `text` is a convenience rendering: non-empty cells joined by ` | `, one line per row. Tables are never rewritten as prose. Values split across cells (`$` | `178,353`) are kept as separate cells.

**Source locations.** Each block has a `dom_path` such as `/html[1]/body[1]/div[5]`: the element's position in the html.parser tree, counting same-name siblings from 1. It also has `element_id`, the element's own `id` attribute, when present. Hidden elements are skipped but still counted, so recorded paths match the original tree. A path is only meaningful for the exact bytes identified by `ExtractedFiling.source.content_sha256`. A browser's DOM can differ for malformed markup.

**Citation guarantees.**
- **Guaranteed:** each block's text comes from the element at its `dom_path` in the document with that SHA-256, and blocks appear in document order. Re-running extraction on the same bytes gives identical blocks.
- **Not guaranteed:**
  - page numbers (HTML has none);
  - character offsets into the raw HTML;
  - that a heading/paragraph classification matches the author's intent;
  - that every section boundary is found.

**Section detection** has separate rules for **10-K/10-K/A** (labels such as `"Item 1A"`) and **10-Q/10-Q/A** (labels such as `"Part I, Item 2"`; D-023).

For any other form:
- every block is still extracted with the same text, order and locations;
- `section` is `None` for every block;
- found, missing and uncertain sections are all empty, and a warning says detection isn't supported.

The form changes only `section` and the section diagnostics. Block text, order, tables, `dom_path` and `element_id` are identical whichever rules apply.

**10-Q rules** (`_assign_10q_sections`). A 10-Q numbers items separately in Part I (Financial Information: Items 1–4) and Part II (Other Information: Items 1, 1A, 2–6), so Item 2 means MD&A in Part I and repurchases in Part II.
- **Boundaries** are heading blocks that *start with* "Part I"/"Part II" or "Item N". Tables of contents built as `<table>` and Part or Item references inside paragraphs are ignored.
- **Each Item takes the Part of the nearest trusted Part heading above it.** Part headings are **never inferred**: an Item with no Part heading above it is reported as `"Item N (Part unknown)"` in `uncertain_sections` and left unassigned.
- **Parts must run I, then II.** Repeating the current Part is harmless. Going back to Part I, or any other Part number, means the Part structure can't be trusted: **nothing in the document is labeled**, including items seen before the break. Without that rule, a table of contents written as bold headings would take the labels away from the real sections.
- **An item is labeled only if** it's valid for its Part (Item 1A only in Part II), occurs once in that Part, and comes in order. Otherwise it's unassigned, listed as uncertain and warned about. The same number in the other Part isn't a duplicate.
- **Priority 10-Q sections** are `Part I, Item 1` (financial statements), `Part I, Item 2` (MD&A) and `Part II, Item 1A` (risk factors). One is reported missing only when no heading for it exists at all. An `"Item N (Part unknown)"` heading makes it uncertain, not missing.

**Live verification (M2.1, 2026-11-12, extractor v2).**

| Filing | Blocks | Labeled | Result |
|---|---|---|---|
| Apple 10-Q, accession `0000320193-26-000020`, report date 2026-06-27 | 304 | 269 | All 11 items detected (Part I Items 1–4; Part II Items 1, 1A, 2–6). No warnings and no incorrect section assignments. MD&A (`Part I, Item 2`) has 89 blocks. |
| JPMorgan Chase 10-Q, accession `0001628280-26-069523`, report date 2026-09-30 | 2,509 | 0 | No sections detected and no incorrect section assignments. |

**Why JPMorgan got no labels.**
- Its Item lines (for example "Item 1A. Risk Factors.") are plain text, not bold. They're classified as paragraphs and aren't boundaries.
- Its Part I content is organized under topic headings and reached through a cross-reference index table. There are no explicit Item 1/Item 2 headings in the body.
- The extractor conservatively left all content unassigned.

**So the 10-Q rules are safe on both filers, but effective only when items are explicit bold headings under Part headings.**

**Design constraint for M2.2 (passages) and M2.3 (retrieval).** Retrieval must work when section labels are unavailable:
- A missing section (`section=None`) must not prevent passage construction, indexing, searching or citation verification.
- An explicit section filter must keep its meaning. Filtering on `"Part I, Item 2"` returns only passages labeled with that section, and returns none for an unlabeled filing. It must never silently fall back to unrelated passages.
- Searching without a section filter, for example by accession and form, must work for every filing.

For 10-Ks, only heading blocks whose text *starts with* "Item N" (a valid Form 10-K item) or "Part I–IV" are boundaries.
- A table of contents built as a `<table>` is ignored, and cross-references inside sentences are paragraphs, so they are ignored too.
- Each boundary starts a region that runs to the next boundary. A "Part" heading ends the previous item.
- A region is labeled (`section = "Item 1A"`) only if that item's heading occurs exactly once and in the expected order.
- Duplicate or out-of-order headings leave their regions as `None`, reported in `uncertain_sections` and `warnings`. That content is never given to a neighboring section.
- Priority items (1, 1A, 7, 8) with no heading are listed in `missing_sections`.

**Diagnostics** (`ExtractionDiagnostics`) report the encoding, the block, paragraph, heading and table counts, sections found, missing and uncertain, and warnings. They describe what the extractor did. They do not show that the extraction is semantically correct.

**Known limitations.**
- **10-Q:** a heading that combines Part and Item (for example "PART II — Item 1A. Risk Factors") is treated as a Part heading only, so that item is missed. A table of contents written as bold headings that repeat Part headings makes the whole document unlabeled (safe, but no sections).
- **10-Q:** verified live on two filings (see Live verification above). Apple: all items labeled. JPMorgan: nothing labeled, because its Item lines are plain text and Part I uses topic headings with a cross-reference index. Improved detection for such filings is not implemented.
- Headings that aren't bold or `<h1>`–`<h6>` become paragraphs, so their section is reported missing.
- Combined headings ("Items 1 and 2.") aren't recognized.
- Page headers, footers and page numbers remain as paragraphs.
- Tables used for layout (bullets, cover page) are still table blocks.
- `rowspan` isn't expanded, and colspan placeholders look the same as genuinely empty cells (see Tables above).
- Nested tables appear as text inside a cell.
- Extremely deep, malformed nesting could exceed Python's recursion limit.

### 3.2c Milestone 1.3: filing document cache (implemented)

`filing_cache.py` stores raw `FilingDocument`s on the local filesystem. `SecClient` stays a pure HTTP client, and extracted content is **not** cached (D-020).

```
fetch_filing_document(sec, filing, cache_dir)
  1. _entry_paths(filing)   → filing_document_url(filing) validates it first (same allowlist as
                              downloads; ValueError before any filesystem access); path derived
                              only from the validated CIK / accession / filename; resolved path
                              must stay inside the cache root
  2. load_filing_document   → read metadata JSON → schema_version, filing identity
                              (CIK, accession, primary document, form) → read bytes →
                              FilingDocument(...) whose validator re-checks SHA-256 and URL
       hit                  ← verified document with ORIGINAL provenance; no SEC request
       miss / invalid       → (invalid: warning logged)
  3. sec.get_filing_document(filing)   (existing pacing, retries, validation)
  4. save_filing_document   → bytes, then metadata, each via temp file + os.replace
       OSError              → warning; the downloaded document is still returned
```

**Layout** mirrors EDGAR's archive under `FILING_CACHE_DIR` (default `data/sec-cache`, which git ignores):
```
filings/0000320193/000032019326000026/aapl-20260926.htm        exact bytes (after HTTP decompression)
filings/0000320193/000032019326000026/aapl-20260926.htm.json   {"schema_version": 1, "filing": {...}, "source": {...}}
```

**Integrity.**
- Every read rebuilds a `FilingDocument`, so the bytes are checked against the recorded SHA-256 by the same validator used for downloads.
- The SHA-256 *verifies* the bytes. Filing *identity* comes from the key: CIK, accession number and primary document.

**Provenance.**
- The metadata stores the original `SourceMetadata`: URL, `retrieved_at`, HTTP status and SHA-256.
- A cache hit returns that original record and the `FilingRef` stored with it. So `retrieved_at` always means when SEC served the bytes, not when the cache was read.
- No response headers or User-Agent are stored.

**Atomicity: per file, not per entry.**
- Each file is replaced atomically. Readers never see a half-written file.
- The bytes and metadata are **not** replaced as one transaction.
- An interrupted write, or two processes writing the same filing at once, can leave bytes from one download next to metadata from another. Such a pair fails the hash check and is treated as a miss. It is never returned.
- If SEC served identical bytes both times, the pair is consistent and valid.

**Recovery.**
- Any invalid entry is logged as a warning, treated as a miss, and overwritten after the next successful download. Invalid entries include:
  - unreadable or garbled metadata;
  - the wrong schema version;
  - an identity mismatch;
  - missing bytes;
  - a hash mismatch.
- Temporary files from failed writes are deleted where possible.
- Deleting the cache directory is always safe, because everything in it can be downloaded again.

**Security.**
- Paths are built only from validated fields, never read from cached files.
- The resolved path must stay inside the cache root, which rejects, for example, a symlink inside the cache that points elsewhere.
- The cache directory **must be controlled by the application and not writable by untrusted users**. These checks keep paths inside the root, but cannot defend against someone who can rewrite the directory itself.

**Limitations.**
- No locking: concurrent misses download twice (bounded and rate-limited) and the last write wins.
- No `fsync`: after a power loss an entry may be truncated, which the hash check catches.
- No eviction or size limit.
- Stale temporary files from hard crashes aren't swept.
- Only primary filing documents are cached. Submissions and Company Facts change over time and would need an expiry policy.
- The cache isn't shared across machines.

**Path to PostgreSQL.**
- The files remain the store for raw bytes.
- A future `filing_documents` table would hold the metadata fields plus the storage path, filled by scanning the `.json` files.
- Only the inside of `load` and `save` would change. `fetch_filing_document` and its callers would not.

### 3.2d Milestone 1.4: XBRL Company Facts (implemented)

```
SecClient.get_company_facts(cik)                                  (sec_client.py)
  → GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json   (existing pacing/retries)
  → json.loads(..., parse_float=Decimal)       numbers stay exact; floats never enter values
  → _parse_company_facts
       structure (cik matches, entityName, facts → taxonomy → concept → units → [rows]) must be
       valid, else SecResponseError
       each row → FactObservation; malformed rows skipped, counted, first 20 sampled
       every supplied row malformed → SecResponseError (never "company has no facts")
  ← CompanyFacts(cik, entity_name, concepts[label, description], observations,
                 skipped_count, skipped_sample, source)

company_facts.select_fact(facts, taxonomy, concept, unit, period_start, period_end, policy)
  ← FactSelection(selected | None, candidates, conflicting_values, reason, source)
```

**What is kept.**
- Every row SEC lists becomes one `FactObservation`, never deduplicated or merged. Each keeps:
  - `cik`, `taxonomy`, `concept` and `unit`, exactly as SEC gives them;
  - `value` as a `Decimal`;
  - `period_start` (`None` for an instant fact) and `period_end`;
  - `form`, `filed`, `accession_number` and `frame`;
  - SEC's `fy`/`fp`, renamed **`filing_fiscal_year`/`filing_fiscal_period`** because they describe the *filing*, not the row's own period.
- Concept labels and descriptions are kept once per concept in `CompanyFacts.concepts`, not copied into every row.
- The raw JSON is not kept. `source` records its URL, retrieval time and SHA-256.

**Provenance.**
- Each observation identifies its originating filing (`accession_number`, `form`, `filed`, and the computed `filing_index_url`).
- `CompanyFacts.source`, which is repeated on each `FactSelection`, identifies the exact Company Facts response.
- No page numbers or document locations are claimed. Company Facts doesn't say where in the filing a fact appears.

**Period classification** (`period_class`).
- No `period_start` means `instant`.
- Otherwise the inclusive length in days decides the class:

  | Class | Days |
  |---|---|
  | `3_months` | 84–98 |
  | `6_months` | 175–189 |
  | `9_months` | 266–280 |
  | `12_months` | 357–371 |
  | `other` | anything else |

- The ranges allow for 52/53-week years and 14-week quarters.
- These classes describe **lengths of time only**. They do not identify fiscal quarters or years, and `fy`/`fp` are never used for that.

**Selection** (`select_fact`).
- **Candidates:** every observation with exactly the requested taxonomy, concept, unit, start and end, in any form, sorted by a total order (filing date, accession number, then every other field). The result therefore doesn't depend on SEC's row order.
- **Eligible:** only `10-K`, `10-K/A`, `10-Q` and `10-Q/A`.
- **Policy:** `latest_filed` (the default, which reflects revisions and amendments) or `original` (as first reported) picks a filing date.
- **Ties:** if the eligible observations filed on that date disagree, whether in one filing or several, `selected` is `None` and `reason` says why. Equally eligible values are never chosen between arbitrarily.
- **Agreement:** if they agree, the first in sort order is chosen deterministically.
- **`conflicting_values`** is true when **eligible** observations (10-K/10-Q and their amendments) report different values for the period, for example a later comparative that revises the original. A disagreeing 8-K or `424B2` value doesn't count, but that row still appears in `candidates` (D-022).
- **`None` with a reason** is also returned when:
  - the concept was never reported;
  - the period isn't reported;
  - the period is only in ineligible forms.

  It is never zero, and never a nearby period.
- **Units must always be named.** Different units and currencies are never combined or converted.

**Limitations.**
- **Not cached:** Company Facts changes with every filing.
- **No concept mapping:** "revenue" might be any of several concepts, and series must not be joined across concept changes.
- **No fiscal-calendar inference and no Q4 derivation.**
- **Restatements and reclassifications** can't be told apart from this data.
- **No segment-level facts:** Company Facts excludes dimensional data.
- **Labels and descriptions** are SEC's taxonomy text, not company-specific.
- **Large responses** are fully loaded into memory.

### 3.2e Milestone 2.2: passages and verifiable citations (implemented)

`passages.py` turns an `ExtractedFiling` into passages and verifies citations to them. It is pure and offline. Ranking is in `retrieval.py` (§3.2f); nothing is stored.

```
ExtractedFiling.blocks
  1. pieces    → each block is one piece; a heading or paragraph longer than TARGET_CHARS
                 (2,000) is cut into pieces at sentence ends. Tables are never cut.
  2. group     → pieces are added to the current passage in document order, or start a new one
                 (rules below)
  3. identify  → every passage gets a citation id derived from its own fields
  ← list[Passage]
```

**Grouping rules** (`build_passages`, in this order):
1. **Sections:** a passage never contains blocks with different `section` values. `None` is treated as one more value, so an unlabeled filing (an 8-K, or a 10-Q like JPMorgan's) still gets passages, grouped by headings and size only. "PART I"/"PART II" headings are unlabeled, so in a labeled filing they become small passages of their own.
2. **Tables:** a table is a passage on its own, together with any headings directly before it (for example "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS"). Whatever follows a table starts a new passage. `kind` is `"table"`.
3. **Headings** start a new passage, except that consecutive headings stay together (up to TARGET_CHARS).
4. **Text after headings always joins them,** even past TARGET_CHARS, so a heading is never cut off from the text it introduces. A heading with nothing after it in its section stands alone.
5. **Size:** otherwise a piece joins the current passage only if the passage stays within TARGET_CHARS characters (counting separators).

**Coverage.** Every block is in exactly one passage, or for a long block, each of its pieces is. Nothing is filtered: page footers, running headers and cover-page text stay as ordinary content, because silently dropping text would make some source content uncitable. Passage text is the pieces joined by `"\n\n"`.

**Splitting long blocks** (`_split_text`):
- A cut goes at the **last sentence end** that keeps the piece within 2,000 characters. A sentence end is `.`, `!` or `?` (with optional closing quotes or brackets), one space, then a capital letter, digit, `$`, opening quote or bracket.
- Not sentence ends: a small list of abbreviations (`Inc.`, `Corp.`, `Co.`, `Ltd.`, `No.`, `vs.`, …) and initials (`U.S.`, `e.g.`, `J.`). Skipping a real sentence end only moves the cut earlier.
- If no sentence end fits: the last space. If 2,000 characters contain no space at all: exactly at 2,000.
- A cut at a sentence end or space leaves out only that single space. A hard cut leaves out nothing.
- **Offsets are exact:** `char_start`/`char_end` index `ContentBlock.text` (the extractor's normalized text, not the raw HTML), and `block.text[char_start:char_end]` is the piece.

**Passage-builder version.** `BUILDER_VERSION` (currently `"1"`) is recorded on every passage and is part of its citation id. Bump it whenever a change could alter passage boundaries, text, sections, kinds or ids for the same blocks, including a change to `TARGET_CHARS`, `BLOCK_SEPARATOR`, the sentence rules or the id format. An extractor change needs an `EXTRACTOR_VERSION` bump instead (§3.2b); passages record both.

**Models.**
- `PassageSource`: `filing: FilingRef`, `content_sha256` (full), `extractor_version`, `builder_version`, and computed `document_url` (the SEC archive URL). One per extracted filing, shared by its passages.
- `Passage`: `source`, `section` (`None` when unlabeled), `kind` (`"text"`/`"table"`), the range `block_start`, `char_start`, `block_end`, `char_end` (`char_end` exclusive, in `block_end`'s text), `text`, and computed `text_sha256` (full) and `citation_id`. A range that is empty or reversed is rejected.
- `PassageVerification`: `status`, `detail`, `citation_id`, and, only when verified, `document_url` and the source `blocks` (with `dom_path` and `element_id`).

**Citation id format** (`Passage.citation_id`; `parse_citation_id` parses it back):

```
sec-psg:{cik}:{accession}:{primary_document}:{source_sha256}:x{extractor_version}:p{builder_version}:b{block_start}.{char_start}-{block_end}.{char_end}:t{text_sha256[:16]}
sec-psg:0000320193:0000320193-26-000026:aapl-20260926.htm:9415a7acef85533051c9ff5ced8e7dcbbf85664ad6c35b81a43b97559cb1374c:x2:p1:b4.0-6.41:tafd9d6a1417694ce
```

- **Parseable:** each part is validated (10-digit CIK, accession format, the archive filename allowlist, 64 lowercase hex digits, versions without `:`), so `:` only ever separates parts. `parse_citation_id` requires an exact match of the whole string.
- **Unique:** within one document's bytes, the block/character range identifies a passage, and passages never overlap. Across documents, versions or byte changes, the id contains the full source SHA-256 and both versions. So **uniqueness never depends on a truncated hash**; the same text in two places (a repeated footer) gets two ids.
- **Integrity:** the 16-digit text-hash prefix is an extra check in the id. The full `text_sha256` is recomputed from `text`, and verification compares rebuilt passages with full hashes and full equality.
- **No stale ids:** `citation_id` and `text_sha256` are computed from the passage's fields, never stored inputs. A passage loaded from JSON recomputes them, so a stored id or hash that disagrees with the stored text is ignored, and a citation to the old id then fails verification.
- **Length:** about 160 characters (the example above, built from the 10-K test fixture, is 158). It is a server-side identifier, not something an LLM should retype; later tool calling should give the model short per-request handles and map them back (§3.4).

**Verification** (`verify_citation(citation_id, passage, cache_dir)`, or `verify_citations(pairs, cache_dir)` for many at once). `passage` is the record the citation claims to refer to, for example a retrieval result. Checks run in this order; the first failure is returned:

| Step | Status on failure |
|---|---|
| `citation_id` parses, and equals `passage.citation_id` exactly (the detail names the differing parts) | `invalid_citation` |
| `load_filing_document(cache_dir, passage.source.filing)` returns a cached document that passes the cache's integrity checks | `source_unavailable` |
| Cached SHA-256 equals the cited full `content_sha256` | `source_changed` |
| Cited `extractor_version` equals `EXTRACTOR_VERSION` | `extractor_changed` |
| Cited `builder_version` equals `BUILDER_VERSION` | `builder_changed` |
| Re-extract and rebuild all passages; one has exactly this block/character range | `boundaries_changed` |
| That passage's full text SHA-256 equals the cited one | `text_changed` |
| That passage equals the cited record in every field (section, kind, filing metadata) | `invalid_citation` |
| All pass | `verified`, with `document_url` and the blocks `block_start..block_end` |

Guarantees:
- **What the cache can and cannot tell verification** (checked against the real cache in tests):
  - `source_unavailable` covers both a **missing** entry and a **damaged** one: missing bytes, bytes that don't match the recorded hash, unreadable or non-JSON metadata, metadata for a different filing, or an unsupported schema version. `load_filing_document` returns `None` for all of them, and logs a warning (`Ignoring invalid cache entry …`) for every damaged case. The status detail says the entry is "missing or failed its integrity checks".
  - This is deliberate. A damaged entry's recorded hash can't be trusted, so it can't support a `source_changed` claim, and the remedy is the same: fetch the document (which replaces an invalid entry) and verify again.
  - `source_changed` is reached only when the cache holds an entry that **passes** its integrity checks but has a different full SHA-256 than the citation, for example after SEC's bytes changed and the document was downloaded again, or if bytes and recorded hash were rewritten together.
  - If a separate status for damaged entries is ever needed, the smallest safe change is a read-only `filing_cache` helper that reports whether an entry's metadata file exists, used only to word the detail. The cache's integrity checks stay untouched.
- **Offline:** verification takes no `SecClient` and only reads the cache. `source_unavailable` is the only retryable status: the caller may fetch the document (M1.3) and verify again.
- **Never re-pointed:** a citation is never resolved to different bytes, other versions, a nearby range or similar text. Every mismatch is a specific failure.
- **Content and boundaries are re-derived,** not trusted: the passage must be reproduced exactly from the source bytes with today's code.
- **A consistent but wrong record is caught:** an edited section label or filing field with a matching recomputed id still fails the final equality check.

**Batch verification** (`verify_citations`, M2.5 B1, D-027). One result per `(citation_id, passage)` pair, in input order, duplicates included. `verify_citation` is a batch of one.
- Citations are grouped by the cache's own filing identity (CIK, accession number, primary document, form). The id checks need no source and run first.
- **Per group, once:** `load_filing_document` (a missing or damaged entry gives `source_unavailable` for the whole group), then the SHA-256 of the loaded bytes themselves.
- Each citation then compares its own claimed full hash and versions. Two citations naming the same filing with different hashes get different results.
- Extraction and passage building happen **at most once per group**, and only if some citation passes the hash and version checks. Rebuilt passages are looked up by their block/character range.
- **Snapshot semantics:** every citation in a group is checked against the bytes read once at the start of that group. The file is not read again during the batch, so a change made mid-batch is seen by the next call, not this one. The guarantee is unchanged: verified text was re-derived, with today's code, from bytes whose full hash equals the citation's. Nothing derived outlives the call.
- **An old version can't be verified, only reported.** After a version bump, previously issued citations return `extractor_changed` or `builder_changed`; they must be re-retrieved, never reinterpreted.

**Known limitations.**
- **Verification is per citation:** each call re-extracts the whole document (fast for the fixtures; untimed on full-size filings). Verifying many citations from one filing repeats that work.
- **Sentence detection is heuristic:** decimals such as `3.5%` are never cut (no space follows the point), but abbreviations outside the list (for example "approx.") or a sentence starting with a lowercase word can move a cut. Cuts are still deterministic and offsets exact.
- **Tables can be far larger than 2,000 characters,** because they are never split. Large financial statements become single large passages.
- **Small passages:** unlabeled Part headings, a lone final heading in a section, and text between two tables become short passages of their own.
- **Page footers and headers are included** in surrounding passages, which adds noise to text and later ranking.
- **Continuation passages lose their heading:** when a long section continues into a second passage, that passage doesn't repeat or record the heading above it.
- **Offsets are Python string (code point) indexes** into normalized block text, not bytes, UTF-16 units (JavaScript) or the raw HTML. A hard cut (2,000 characters without a space) can fall inside a multi-code-point sequence such as an emoji with modifiers; the slices are still exact and verifiable.
- **Only 10-K/10-Q passages can be verified,** because the filing cache stores only those forms. Passages built from any other form report `source_unavailable`.
- **Offsets index normalized block text,** not the raw HTML. Locating text in the source goes through each block's `dom_path`/`element_id` (§3.2b).
- **Section filters** are implemented in M2.3 (§3.2f) and follow the M2.1 constraint: only labeled passages, never unrelated ones.

### 3.2f Milestone 2.3: keyword retrieval (implemented)

`retrieval.py` ranks passages (§3.2e) with BM25, in memory, with no dependencies. It also chooses the latest 10-K or 10-Q from a company's submissions.

```
latest FilingRef (select_latest_filing) → fetch_filing_document → extract_filing → build_passages
  passages from one or more filings → KeywordIndex(passages)       statistics computed once
  index.search(query, k, section=, form=, accession_number=)
    tokenize(query) → distinct terms → filter → BM25 score → sort → top k
  ← list[SearchHit(passage, score, matched_terms, citation_id)]
```

**Tokenization** (`tokenize`, the same for passages and queries):
1. Unicode NFKC normalization (full-width characters, ligatures such as "ﬁ", non-breaking spaces), then casefold.
2. Tokens are runs of letters or digits in any script. Everything else separates tokens, so `single-source` gives `single`, `source`, and `10-K` gives `10`, `k`. A query written either way matches.
3. Numbers keep a decimal point and drop thousands separators: `$416,161` gives `416161`, `3.5%` gives `3.5`.
4. A possessive `'s` is dropped (`Apple's` gives `apple`), and other apostrophes are removed (`don't` gives `dont`). Curly apostrophes count.
5. 28 stopwords are removed: a, an, and, are, as, at, be, by, for, from, has, have, in, is, it, its, of, on, or, that, the, this, to, was, were, which, will, with. Words such as "not", "no" and "may" are kept.
6. **No stemming and no synonyms:** "supplier" and "suppliers" are different terms.

**BM25** (Okapi BM25, k1 = 1.2, b = 0.75, the usual defaults):

```
score(P, Q) = Σ over distinct terms t of Q that occur in P:
              idf(t) · tf(t,P) · (k1 + 1) / (tf(t,P) + k1 · (1 − b + b · |P| / avg|P|))
idf(t)      = ln(1 + (N − df(t) + 0.5) / (df(t) + 0.5))
```

- `tf` is the term's count in the passage, `|P|` the passage's token count, `avg|P|` the average over the index, `N` the number of passages, `df` the number of passages containing the term.
- This IDF variant (as in Lucene) is always positive, so a term that occurs in most passages adds little but never subtracts.
- **Repeated query terms count once:** "supply supply chain" scores exactly like "supply chain". Terms are summed in sorted order, so word order can't change a score even in the last floating-point digit.
- Statistics (`N`, `df`, `avg|P|`, lengths and postings) are computed once in `KeywordIndex.__init__`. The index is immutable; adding filings means building a new index.

**Search behavior.**
- Returns at most `k` hits, best first. **Only passages that contain at least one query term are returned,** never zero-score padding, so there can be fewer than `k` hits or none.
- A query with no tokens left after tokenization (empty, punctuation only, stopwords only) returns `[]`.
- `k` must be a positive integer. Anything else, including `True` and `1.5`, raises `ValueError`.
- An index can't contain the same passage twice (a duplicate `citation_id` raises `ValueError`).
- `SearchHit.passage` is the indexed `Passage` itself, so its source, filing metadata, section, range and `citation_id` are unchanged. `SearchHit.citation_id` repeats it, and `verify_citation` accepts a hit exactly as it accepts the passage.
- `matched_terms` lists the distinct query terms the passage contains, sorted. It explains every score.

**Filtering policy.**
- Filters are exact matches: `section` on `Passage.section`, `form` on the filing's form (`"10-K"` doesn't match `"10-K/A"`), `accession_number` on the filing's accession. They combine with AND.
- `None` means "don't filter". It never means "unlabeled": there is no way to ask for `section=None` passages specifically.
- **Filters are applied before ranking, and corpus statistics always describe the whole index.** A passage's score for a query is the same with or without filters, so filtering never reorders the passages it keeps. Recomputing statistics per filter would make scores depend on the filter and cost a pass over the corpus per query.
- **A section filter is never broadened** (the M2.1 constraint, §3.2b). It returns only passages with exactly that label, so for an unlabeled filing (such as JPMorgan's 10-Q) it returns nothing. `"Item 1A"` doesn't match `"Part II, Item 1A"`, and case matters.
- A filing whose passages all have `section=None` is fully searchable without a section filter, alone (by accession) or together with other filings.

**Tie-breaking.** Equal scores are ordered by document identity, then location: CIK, accession number, primary document, source SHA-256, extractor version, builder version, `block_start`, `char_start`. That is a total order over distinct passages, so the result doesn't depend on the order passages were added.

**Filing selection** (`select_latest_filing(submissions, form="10-K", include_amendments=False)`):
- `form` must be `"10-K"` or `"10-Q"`. Amendments (`/A`) are excluded unless `include_amendments=True`.
- The latest is chosen by `filing_date`, the date the filing was made. It never uses `report_date` or fiscal-year labels.
- Same-day ties go to the later `acceptance_datetime` (a missing one counts as earliest), then the larger accession number.
- **It selects only from the filings present in the given `CompanySubmissions`** (its `recent_filings`). It never fetches older submissions pages, so it doesn't guarantee completeness across them (R12). The result is the latest matching filing *in that list*; when `has_more_filings` is true, unrequested older pages aren't considered.
- Returns `None` when the list has no matching filing. That doesn't prove the company never filed that form.

**Tradeoffs.**
- **Why BM25 first:** deterministic, explainable (scores come from visible matched terms), no dependencies, fast enough in memory for tens of thousands of passages, and a fixed baseline for later comparisons.
- **What it misses:** vocabulary mismatch. "Single-source suppliers" won't find "dependence on a limited number of vendors". No stemming means "supplier" doesn't match "suppliers". These are the cases that might later justify a plural rule or hybrid retrieval with embeddings, **but only if evaluation shows it** (M2.4).

**Known limitations.**
- **In memory only:** the index is rebuilt in each process from cached documents, and nothing is persisted.
- **No phrases or proximity:** "supply chain" matches passages containing both words anywhere.
- **Tokenization quirks:** scripts written without spaces (Chinese, Japanese) become one token per run, so they effectively need exact matches. Comma-separated numbers without spaces join (`1,2,3` gives `123`). Version-like strings split oddly (`v1.2.3` gives `v1`, `2.3`).
- **Noise in passages:** page footers and tables (§3.2e) are indexed like prose. A table of numbers can match a numeric query, but a number found this way is text-sourced and never an input to calculations (§7).
- **Heading context isn't indexed separately:** a continuation passage doesn't carry its section heading's words.
- **Scores are comparable only within one index.**
- **Not evaluated yet.** Relevance is unmeasured. M2.4 must measure Recall@5 and MRR@10 on a reviewed question set with expected snippets and sections, pinned to source SHA-256s and versions, and report section-filter precision and the verification rate of returned citations.

### 3.2g Milestone 2.4: offline evaluation harness, real dataset and BM25 baseline (approved)

`evaluation.py` measures retrieval against **pinned** filings read only from the filing cache. B1 is the harness alone. The real dataset (`evals/retrieval_v1.json`), document acquisition and the baseline run come in B2–B4. M2.4 makes no retrieval improvements.

```
dataset JSON (pins, questions, evidence spans, changelog) → load_dataset (strict schema, SHA-256 of the file)
  1. per pinned filing: load_filing_document (cache only) → check full SHA-256, extractor and builder pins
     → extract_filing → build_passages                                  (timed separately)
  2. resolve every evidence span to block.text[start:end]               (any failure: refuse)
  3. KeywordIndex over all filings                                      (timed)
  4. primary: unfiltered search(query, k=10), 1 warm-up + N timed rounds; rounds must agree
  5. secondary: each filtered search, run twice; every hit re-checked against its filters
  6. verify_citations: every distinct returned hit, one batch            (timed separately)
  ← EvalReport (metrics, per-question results, filtered results, timings), or EvaluationError
```

**Dataset schema** (strict: unknown fields are rejected as typos).
- `documents`: `key`, `cik`, `accession_number`, `form`, `primary_document`, `filing_date`, and the pin: full `content_sha256`, `extractor_version`, `builder_version`.
- `questions`: `id`, `query`, `kind` (`direct`, `paraphrase`, `unanswerable`), `topic`, `asks_for_amount`, `expected_sections` (document + section), `evidence`, `filtered_searches`.
  - **Answerable questions need evidence; unanswerable questions may have none.**
  - `asks_for_amount` marks a question that asks for a financial amount. It is still **narrative evidence retrieval**: a number found in text is text-sourced, never an authoritative XBRL value or a calculation input (§7).
- `evidence`: `document`, `snippet` (an exact substring of one block's text, at least 20 characters), `block_index`, `section` (the block's extracted label, or `null`).
- `filtered_searches`: any of `section`, `form` and `document` (restricts to its accession), plus `expect_empty`.
- `changelog`: every question or label change with its reason. The last entry must name the current `dataset_version`, so label changes are versioned and reviewable in git. The report records the dataset file's SHA-256.

**Labels are a reviewed, non-exhaustive evidence set.** They are written by reading the filings, never by consulting BM25 rankings (the B3 procedure: read whole sections; you review every span). Recall is therefore **labeled-evidence recall**, never recall over all evidence a filing might contain.

**Evidence matching.**
- A span resolves only if the label names the pinned document, the labeled block exists, the snippet occurs **exactly once within that block** (overlapping repeats count), and the block has the labeled section. The block index already says which occurrence is meant, so a disclosure repeated elsewhere in the filing (an MD&A summary and a note, say) is labeled in each block that supports the question. Labels point at text, not passage ids, so they survive changes to passage boundaries.
- **Relevance:** a passage is relevant to a span if that one passage contains at least **80%** of the span's characters (`RELEVANCE_THRESHOLD`), in the same document (full SHA-256). Overlap is computed from the passage's block/character range. A one-character overlap never counts; a span cut by a passage split may still count if 80% lies in one passage.
- **Combined coverage** is measured separately: the share of the span's characters contained in the union of the top-five passages. A span reaching 80% only in combination is counted as `split_evidence`, and is not a Hit.

**Metrics.** "Answerable" excludes `unanswerable` questions. Relevance uses the unfiltered top 10 (`RESULTS_PER_SEARCH`).

| Metric | Definition |
|---|---|
| Hit@5 | Mean over answerable questions of 1 if any top-5 hit is relevant, else 0 |
| MRR@10 | Mean over answerable questions of 1/rank of the first relevant hit in the top 10, else 0 |
| Labeled-evidence recall@5 | Per question: labeled spans covered (≥ 80%) by at least one single top-5 passage, divided by its labeled spans; then the mean |
| Combined evidence coverage@5 | Per question: mean over its spans of the share of characters in the union of the top-5 passages; then the mean |
| Section precision@5 | Only questions with expected sections: top-5 hits whose (document, section) is expected, divided by **all** top-5 hits. Hits from other filings and sectionless passages stay in the denominator. Undefined (`null`) with no hits. |
| Filtered Hit@5, MRR@10 | The same, over filtered searches that expect results, reported separately as secondary |

- **Hit@5 vs labeled-evidence recall@5:** Hit@5 asks whether *anything* usable reached the top five. Recall asks *how much* of the labeled evidence did. A question with four spans and one found has Hit@5 = 1 and recall = 0.25.
- Results are reported overall, for direct and for paraphrase questions, plus a per-question table (citation ids, scores, relevant ranks, `asks_for_amount`). Unanswerable questions report what BM25 returned anyway (hits and scores), with no relevance metric.

**Integrity: the evaluation is refused, with every problem listed, when:**
- a pinned document is missing or invalid in the cache, its SHA-256 or filing date differs from the pin, or `EXTRACTOR_VERSION`/`BUILDER_VERSION` differ from the pin;
- an evidence span doesn't resolve (missing, ambiguous, wrong block or wrong section);
- primary results differ between rounds, or a filtered search isn't repeatable;
- a filtered hit violates its filters (re-checked independently of the index), or an `expect_empty` search returns hits;
- **any returned citation fails `verify_citation`.**

Citation correctness and filter compliance are integrity conditions, not metrics: they must hold 100% or there is no report. Relevance misses never invalidate an evaluation.

**Performance instrumentation** (`time.perf_counter`; informational, excluded from determinism checks):
- per document: cache load (with hash check), extraction and passage building;
- index construction;
- search latency (mean, p50, p95, max; nearest-rank percentiles), primary searches only, after one warm-up round;
- verification, measured separately from search, as **one `verify_citations` batch** (`verification_ms`), plus the number of citations per document. Each filing is extracted once per batch (D-027). On the B4 corpus this took 4.9 s instead of 333 s of per-citation re-extraction. The report schema changed only here: the per-citation `verification` latency summary and the per-document `total_ms` were removed.

**Network.** The runner takes no `SecClient`; documents come only through `load_filing_document`. The tests block sockets.

**CLI:** `uv run python -m financial_analyst.evaluation DATASET [--cache-dir DIR] [--output FILE] [--repetitions N]`. It prints a summary, optionally writes the JSON report, and exits 0, or 2 with the integrity problems on stderr. `--cache-dir` defaults to the configured default path (`data/sec-cache`) without reading `.env`.

**Offline fixtures.** `tests/fixtures/eval_sample.json` pins the existing 10-K and 10-Q samples and a new sectionless sample (`filing_sectionless_sample.htm`: plain-text Item lines and topic headings, like the JPMorgan 10-Q). It has 7 questions: direct, paraphrase, an amount question, the sectionless scenario, filtered searches including a section filter that must return nothing, and one unanswerable question. Its numbers test the harness, not BM25 quality.

**Known limitations.**
- Labels can only be as complete as the review; recall is over labeled evidence.
- Relevance is binary, with one fixed 80% threshold and no graded relevance.
- Evidence must be a substring of a single block, so a claim spanning blocks needs one span per block.
- Section precision compares labels exactly and is undefined for sectionless filings, which simply contribute non-matching hits.
- Timings come from one machine and one run; they aren't benchmarks.
- Verification still re-extracts each filing once per run, about 3.8 s for the JPMorgan 10-Q, independently of the extraction used for indexing. This is on purpose, so the check never reuses what it is checking.
- The small question sets (6 answerable in the fixture, 18 in the real dataset) make aggregate metrics coarse; read the per-question table.

**Real dataset (B3, approved): `evals/retrieval_v1.json`.** Its BM25 baseline (B4) is `evals/retrieval_v1.baseline.json`.
- **Pinned corpus.** Three cached filings, accepted by the owner in B2 with their full SHA-256 hashes, `extractor_version` 2 and `builder_version` 1:
  - Apple 10-K for fiscal 2026, accession 0000320193-26-000026;
  - Apple 10-Q for the quarter ended June 27, 2026, accession 0000320193-26-000020;
  - JPMorgan 10-Q for September 30, 2026, accession 0001628280-26-069523. It has no section labels, so all its evidence is `section: null`.
- **Questions.** 20 in total: 13 direct, 5 paraphrase and 2 unanswerable.
- **Labeling.** Every evidence span was written by reading the extracted blocks, without building a `KeywordIndex` or looking at rankings. Each span is validated with `resolve_evidence`: it must occur exactly once in the labeled block, under the labeled section. Repeated disclosures were found by reading and exact-text comparison (not search) and are labeled in every block that supports the question.
- **The local cache is required.** The benchmark is reproducible only from the cached bytes in `data/sec-cache`, which git ignores. The evaluation never downloads; without those exact files it refuses to run. Keep the cache. If it is lost, the corpus must be re-acquired and re-pinned, and every label re-validated.
- **Raw-byte SHA-256 remains authoritative.** Pins and citation ids hash the exact bytes received, and `verify_citation` compares against them. Nothing is normalized before hashing.
- **Unresolved provenance observation (B2, 2026-11-13).**
  - **What was seen:** all three primary documents downloaded in B2 were exactly 12 bytes shorter than in earlier live runs, and all their SHA-256 hashes differed. Accession numbers, forms, dates, URLs and extraction counts were unchanged.
  - **A possible cause:** each cached file ends with the same `<script>` element, whose path looks like a random token, just before `</body>`. A response-dependent script added outside the filing could explain the difference.
  - **This is not proven.** The earlier bytes weren't kept, so it is an observation, not an established SEC behavior.
  - **The practical consequence:** downloading an unchanged filing again may produce different bytes. Its citations would then report `source_changed`.

**Held-out dataset (M2.5 B2, awaiting review): `evals/retrieval_v2_heldout.json`.** For comparing BM25, semantic and hybrid retrieval. `retrieval_v1` remains the development set.
- **Same pinned corpus** as `retrieval_v1` (identical pins), so results are comparable.
- **Questions:** 40 in total: 16 direct, 20 paraphrase and 4 unanswerable, with 67 evidence spans; 22 questions need more than one span.
- **Coverage:** risk, MD&A, business and accounting topics; company-specific questions (Apple Card in JPMorgan's filing, Google in Apple's); period-specific questions (June 2026 quarter against fiscal 2026); and sectionless JPMorgan questions.
- **Independence:** written by reading the extracted filings, with no retrieval of any kind run on these questions. It shares no question, query or evidence block with `retrieval_v1`.
- **It is a test set.** It is run once per pre-registered configuration; questions and labels are not changed in response to its results.

**Reading the metrics on this set.** The harness is unchanged; three different things are measured:
- **Passage relevance:** one passage contains at least 80% of one labeled span. Hit@5 and MRR@10 ask whether *any* labeled span is well covered near the top. For a multi-evidence question, one span is enough.
- **Labeled-evidence recall@5:** the share of a question's labeled spans that some top-5 passage covers. Labels are non-exhaustive, so this is recall over labeled evidence, not over everything a filing says.
- **Multi-evidence answer completeness:** whether the top five together contain *every* fact the answer needs. The harness doesn't report it as a metric.
  - When each span is a different required fact, completeness means recall@5 = 1.0 for that question.
  - When the spans are repeats of the same fact in different blocks (for example a disclosure repeated in a note), recall@5 below 1.0 doesn't mean the answer is incomplete.
  - The per-question table must be read for this.

**Known labeling limitations.**
- **Repeats** of a labeled fact were found by reading and by an exact-text and figure scan, not by search. Paraphrased restatements elsewhere can still be unlabeled.
- **Deliberately unlabeled:** other-period disclosures of the same measure, such as nine-month Equity Markets revenue or year-over-year VaR.
- **Some spans are table rows,** such as ratings, rate sensitivity and dividends per share. They count as relevant only when a passage holds the row's text, which for tables means the whole table passage.
- **Unanswerable questions** were checked by pattern search over every block of all three filings. Related but unsupporting text exists on purpose, as a hard negative: JPMorgan's "outlook", climate-risk mentions, December 2025 capital ratios, and mentions of Apple's retail stores without a count.
- **Small sample:** with 36 answerable questions, one question moves Hit@5 by about 2.8 points.

### 3.3 Later milestone: ingestion

```
ticker → CIK (company_tickers.json)
  → submissions → select 10-K / 10-Q (amendments kept distinct)
  ├─▶ Company Facts (XBRL JSON) → normalize facts → facts table          [structured path]
  └─▶ primary document (HTML) → clean → split into Items (1A, 7, …)
        → chunk → embed → pgvector, each chunk carrying provenance       [narrative path]
  primary documents cached on disk (M1.3, §3.2c); mutable JSON caching (submissions, facts) later
```

### 3.4 Later milestone: research question

```
POST /research {question}
  → agent loop (native LLM tool calling, no framework)
      LLM proposes tool call ──▶ arguments validated by Pydantic ──▶ deterministic execution
        get_financial_facts(cik, concept, periods)   → facts with fact_ids
        search_filings(cik, query, forms, years)     → passages with citation ids
        compute_metric(name, inputs by fact_id)      → result + formula + input ids
      tool results are returned to the LLM as data, delimited and marked untrusted
  → LLM drafts answer that cites fact_ids / passage citation ids
  → citation validator: every cited id must have come from this request's tool results;
    every number in the answer must match a tool output (otherwise flagged or rejected)
  → response: answer + resolved citations (URL, accession, period, unit) + telemetry
```

---

## 4. Directory structure

### 4.1 Current structure (Milestone 2.4 B1)

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
│       ├── filing_extractor.py # extract_filing: HTML → ordered blocks, sections, diagnostics
│       ├── filing_cache.py     # filesystem cache for FilingDocuments (fetch / load / save)
│       ├── passages.py         # build_passages, citation ids, offline verify_citation
│       ├── retrieval.py        # tokenize, KeywordIndex (BM25), select_latest_filing
│       ├── evaluation.py       # offline retrieval evaluation: dataset, metrics, runner, CLI
│       └── company_facts.py    # XBRL fact period classification and observation selection
└── tests/
    ├── conftest.py             # shared settings fixture
    ├── fixtures/
    │   ├── submissions_sample.json   # trimmed EDGAR submissions payload
    │   ├── companyfacts_sample.json  # hand-written Company Facts payload (illustrative values)
    │   ├── filing_sample.htm         # trimmed inline-XBRL 10-K-style HTML
    │   ├── filing_10q_sample.htm     # trimmed inline-XBRL 10-Q-style HTML (Parts I/II)
    │   ├── filing_sectionless_sample.htm  # 10-Q-style HTML with no detectable sections
    │   └── eval_sample.json          # evaluation dataset pinning the three sample filings
    ├── test_company_facts.py
    ├── test_evaluation.py
    ├── test_config.py
    ├── test_filing_cache.py
    ├── test_filing_extractor.py
    ├── test_health.py
    ├── test_passages.py
    ├── test_retrieval.py
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
| Caching | Not in `SecClient`. Primary filing documents are cached by `filing_cache.py` (§3.2c, D-020). Submissions are not cached yet. |

**Why synchronous:** SEC access is rate-limited batch ingestion, so concurrency gains little under a global 4 req/s cap. Sync code is simpler to read, test and debug. If a FastAPI route ever calls it, a plain `def` route runs in FastAPI's threadpool, which the lock handles. Revisit only if profiling shows a need. (Decision D-004.)

---

## 6. Source metadata contract

Provenance is captured at three levels. Levels 1 and 2 and the extracted-content contract are implemented. Level 3 (passages) is implemented in M2.2 (§3.2e).

### Level 1: every HTTP retrieval (`SourceMetadata`, Milestone 0)

| Field | Type | Notes |
|---|---|---|
| `source_url` | `HttpUrl` | Exact URL fetched |
| `retrieved_at` | `datetime` (UTC, tz-aware) | |
| `http_status` | `int` | |
| `content_sha256` | `str` | Hash of the raw body. Detects upstream changes and supports caching. |

### Filing reference (`FilingRef`, Milestone 0)

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

### Level 2: structured fact (`FactObservation`, Milestone 1.4)

`cik`, `taxonomy`, `concept`, `unit`, `value: Decimal`, `period_start: date | None` (None means an instant fact), `period_end`, `filing_fiscal_year`, `filing_fiscal_period` (SEC's `fy`/`fp`, which describe the filing), `form`, `filed`, `accession_number`, `frame | None`, and the computed `filing_index_url`.
- The retrieval provenance is `CompanyFacts.source`, repeated on each `FactSelection`.
- Labels and descriptions live once per concept in `FactConcept`.
- A deterministic `fact_id` for citations will be derived from `(cik, taxonomy, concept, unit, period_start, period_end, accession_number)` when tool calling arrives. That key can still be ambiguous, because one filing can report conflicting values (§3.2d).

### Extracted content (`ExtractedFiling`, `ContentBlock`, Milestone 1.2)

`ExtractedFiling` = `filing: FilingRef`, `source: SourceMetadata` (identifies the exact bytes), `extractor_version: str`, `blocks: list[ContentBlock]`, `diagnostics: ExtractionDiagnostics`.

| `ContentBlock` field | Type | Notes |
|---|---|---|
| `index` | `int` | 0-based document order |
| `kind` | `"heading" \| "paragraph" \| "table"` | |
| `text` | `str` | Normalized text. For tables: non-empty cells joined by ` \| `, one line per row. |
| `rows` | `list[list[str]] \| None` | Tables only. Authoritative cell grid. Colspan placeholders are `""`, the same as empty source cells. Rowspan isn't expanded. |
| `section` | `str \| None` | e.g. `"Item 1A"`. `None` when unknown or uncertain. |
| `dom_path` | `str` | Element position in the parsed tree |
| `element_id` | `str \| None` | The element's own `id` attribute |

### Level 3: narrative passage (`Passage`, Milestone 2.2)

`Passage` = `source: PassageSource` (`filing`, full `content_sha256`, `extractor_version`, `builder_version`, computed `document_url`), `section`, `kind`, the range `block_start.char_start`–`block_end.char_end` into `ContentBlock.text`, `text`, and computed `text_sha256` and `citation_id`. Details, rules and verification: §3.2e. Not yet stored anywhere; a fiscal period for passages will come from the filing's `report_date` when retrieval needs it.

### Citations (passage ids in M2.2; validation in answers later)

A citation is a **reference to a `fact_id` or passage `citation_id` that was returned by a tool during the same request**. Passage ids and their offline verification exist since M2.2 (§3.2e); `fact_id` and the per-request check come with tool calling. The model never writes URLs or accession numbers itself. The server resolves ids to provenance after the fact. An id that was not retrieved in the request is treated as invented, and the answer is rejected or flagged.

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

### Runtime

| Package | Why |
|---|---|
| `fastapi` | Required HTTP framework. Provides typed routing, Pydantic integration and OpenAPI docs. |
| `uvicorn` | ASGI server that runs FastAPI. The plain package is enough; the `[standard]` extras are not needed yet. |
| `pydantic` (v2) | Data contracts and validation. Already a FastAPI dependency; listed explicitly because we use it directly. |
| `pydantic-settings` | Typed, validated settings from environment and `.env`. Since Pydantic v2 this is a separate package. Avoids hand-written env parsing. |
| `httpx` | HTTP client with explicit timeouts, connection pooling and **built-in `MockTransport`** for network-free tests. FastAPI's `TestClient` also uses it. |

| `beautifulsoup4` | Milestone 1.2. A tolerant, well-established HTML tree API, used with Python's built-in `html.parser`, so there are no C extensions. It never runs scripts or fetches resources. It pulls in `soupsieve` (CSS selectors) as a transitive dependency. |

### Development

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
| R1 | ~~The project sat inside a git repo rooted at the home directory.~~ **Resolved** by the owner creating a dedicated project repository. | `.gitignore` excludes `.env`, virtual environments, caches and `data/`. |
| R2 | SEC blocks clients that exceed fair-access limits or omit a User-Agent. | Required and validated User-Agent, 4 req/s default, config cap at 10, backoff on 429/503, filing-document cache (M1.3). |
| R3 | Fiscal period semantics. `fy`/`fp` in Company Facts belong to the filing, so a FY2023 10-K also reports FY2021–22 values tagged `fy=2023`. Non-calendar fiscal years (Apple ends in September). 52/53-week years. | Key facts by `(period_start, period_end)` and use `fy`/`fp` only as filing metadata. Store the company's `fiscal_year_end`. Never infer periods from `fy` alone. |
| R4 | Restatements and duplicates: the same concept and period with different values across filings. | Keep every observation with its accession number. "Latest filed" is an explicit selection policy, and conflicting values are surfaced, never averaged or merged silently. |
| R5 | Q4 and other derived values (Q4 = FY minus 9M YTD). | Derived only in `calc`, labeled `derived`, with input fact_ids attached. |
| R6 | Concept heterogeneity and company extension concepts. | An explicit, versioned concept map for core metrics, with per-company overrides and evaluations covering it. |
| R7 | Company Facts excludes dimensional facts (segments, geography). | Document as a scope limit. Later, parse the XBRL instance or the SEC Financial Statement Data Sets if segment data is needed. |
| R8 | Filing HTML parsing (inline XBRL markup, tables, item detection). | Dedicated parser milestone with fixture-based tests. Store the parse version on each chunk. |
| R9 | Prompt injection through filing text. | Tool output passed as delimited data. System prompt states that retrieved text has no authority. Tool allowlist with validated arguments. No tool has side effects. Red-team cases included in evals. |
| R10 | Invented citations or hallucinated numbers. | Id-based citations, server-side citation validation, and number matching against tool outputs. |
| R11 | Float rounding errors in financial values. | `Decimal` for values. Explicit rounding only at presentation. |
| R12 | Submissions pagination. `filings.recent` holds only the most recent filings (at least one year or 1,000 filings); older ones are in `filings.files`. In the Milestone 0 live run, Apple's recent list contained 12 10-Ks, while Company Facts goes back to 2009. | `has_more_filings` is exposed. **Following the extra pages is deferred, not done.** Until then, observations from older filings can't be matched to a `FilingRef` or a document. |
| R13 | Building evaluation ground truth. | Small hand-verified golden set built from XBRL values with accession numbers. Grows over time. |
| R14 | Docker is not installed locally. | Needed only for the Postgres and deployment milestones. Install before then. |

---

## 10. Deferred and out of scope (as of Milestone 2.4 B1)

**Explicitly deferred:**
- Older submission pages (R12).
- Storing Company Facts responses or observations per accession number. The raw JSON isn't kept, so observations can only be re-checked by downloading again (D-021).
- XBRL sign conventions. Company Facts gives no debit/credit balance, and `IncreaseDecrease…` concepts are often reported with the opposite sign. This belongs in the calculation design.
- Resolving fiscal periods and keeping all inputs to one calculation from the same filing. Both must be designed before calculations (M1 integration review B1/B2).

**Out of scope until their milestones:** database and vector store, LLM, frontend, authentication, background workers, ticker resolution, a concept map, calculations, Docker.

---

## 11. Milestone 0 open questions (historical; all resolved)

- ~~**Q1 Caching.**~~ Resolved: deferred to Milestone 1 (D-013).
- ~~**Q2 Submissions endpoint.**~~ Resolved: no debug endpoint (D-016).
- ~~**Q3 Git repository.**~~ Resolved: the owner created a dedicated repository.
- ~~**Q4 Ruff.**~~ Resolved: added as a dev dependency with minimal configuration (D-015).
- ~~**Q5 Package name.**~~ Resolved: `financial_analyst` (D-002).

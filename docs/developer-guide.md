# Developer guide

How to use each component from Python and the command line. These sections were moved from the
README so that it stays a short overview. The design behind each component is in
[architecture.md](architecture.md), and the decisions in [decisions.md](decisions.md).

All commands run from the repository root. Anything that reads pinned data uses
`data/sec-cache`, whose 16 pinned files are committed. Only the SEC client and `snapshot_acquisition` contact SEC, and only
`live_agent` and the Streamlit app contact an LLM provider.

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

Primary filing documents are cached under `FILING_CACHE_DIR` (default `data/sec-cache`; only the pinned files are committed, new downloads stay git-ignored)
as exact bytes plus a JSON metadata file. Every read re-verifies the SHA-256, and a cache hit
returns the original provenance, including when SEC actually served the bytes. Corrupted or
mismatched entries are treated as misses and replaced after the next download. If the cache
can't be written, a warning is logged and the downloaded document is still returned. The cache
directory must not be writable by untrusted users. Deleting it is always safe. Extracted
content is not cached; extraction is fast and deterministic. Details:
[docs/architecture.md](architecture.md) §3.2c.

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
Sections are assigned only when their headings are unambiguous: `"Item 1A"` for 10-Ks and
Part-qualified `"Part I, Item 2"` for 10-Qs, whose item numbers repeat across Parts. Each result records
`extractor_version`; together with the source SHA-256 it identifies the exact blocks. See
[docs/architecture.md](architecture.md) §3.2b for the guarantees and limitations.

## Passages and citations

```python
from financial_analyst.passages import build_passages, parse_citation_id, verify_citation

passages = build_passages(extracted)  # offline and deterministic
passage = passages[0]
print(passage.section, passage.kind, passage.citation_id)

result = verify_citation(passage.citation_id, passage, settings.filing_cache_dir)
print(result.status, result.document_url, [b.dom_path for b in result.blocks])
```

A passage is a run of whole blocks, about 2,000 characters at most, that never crosses a section.
Headings stay with the text after them, a table is never split, and a block longer than 2,000
characters is cut at sentence ends with exact character offsets. Every block is kept, including
page footers. Filings without section labels still get passages (`section=None`).

A citation id names the filing, the full SHA-256 of the source bytes, the extractor and
passage-builder versions, the block/character range and a text-hash prefix:

```
sec-psg:0000320193:0000320193-26-000026:aapl-20260926.htm:<64 hex>:x2:p1:b4.0-6.41:t<16 hex>
```

`verify_citation` reads only the filing cache and never contacts SEC. It re-extracts the
document, rebuilds its passages and returns `verified` with the source blocks and document URL,
or the reason the citation no longer holds: `invalid_citation`, `source_unavailable`,
`source_changed`, `extractor_changed`, `builder_changed`, `boundaries_changed` or
`text_changed`. A citation is never re-pointed to other text. Details:
[docs/architecture.md](architecture.md) §3.2e.

## Keyword search

```python
from financial_analyst.retrieval import KeywordIndex, select_latest_filing

ten_k = select_latest_filing(submissions, "10-K")  # by filing date, amendments excluded,
# chosen only from the filings in `submissions` (older SEC pages aren't fetched)
# ... fetch_filing_document, extract_filing, build_passages as above, for one or more filings
index = KeywordIndex(passages)
for hit in index.search("supply chain suppliers", k=5, section="Item 1A"):
    print(round(hit.score, 2), hit.matched_terms, hit.citation_id)
```

`KeywordIndex` ranks passages with Okapi BM25 (k1 = 1.2, b = 0.75), in memory and without
dependencies. Tokenization lowercases and normalizes Unicode, splits hyphenated words, drops
thousands separators and a short stopword list, and does no stemming. Repeated query words count
once, only passages that match at least one word are returned, and equal scores are ordered by
filing and position. Filters (`section`, `form`, `accession_number`) are exact: a section filter
returns only passages with exactly that label, so it returns nothing for a filing without
section labels. Without a section filter, every filing is searchable. Each hit carries the
unchanged passage and its citation id, ready for `verify_citation`. Details and limitations:
[docs/architecture.md](architecture.md) §3.2f.

## Evaluating retrieval

```bash
uv run python -m financial_analyst.evaluation DATASET.json --output report.json
# reads filings from data/sec-cache by default (--cache-dir to change); never contacts SEC
```

Semantic and hybrid retrieval are an **optional experiment, not adopted**. On the held-out set,
hybrid matched BM25 on paraphrase questions (8/20 each) and lost a direct question, so BM25 stays
the default (D-028, D-029; results in `evals/results/`). To reproduce them, install the optional
extra and the hash-verified model once:

```bash
uv sync --extra semantic
uv run python -m financial_analyst.semantic_retrieval install   # pinned BAAI/bge-small-en-v1.5
uv run python -m financial_analyst.evaluation evals/retrieval_v1.json --retriever hybrid
```

The model is loaded only from `data/models/` after every file's SHA-256 is checked; searching
never uses the network. Embedding the corpus on one CPU thread takes several minutes.

The evaluation reads a dataset that pins each filing by its full SHA-256 and the extractor and
passage-builder versions, and labels evidence as exact text spans written by reading the
filings, not by searching them. It loads the filings only from the filing cache (never from
SEC), searches one combined BM25 index without filters, and reports Hit@5, MRR@10,
labeled-evidence recall@5, combined evidence coverage@5 and section precision@5, with filtered
searches reported separately. A passage counts as relevant only if it contains at least 80% of
a labeled span.

The evaluation is refused (exit code 2, every problem listed) if a document is missing or
differs from its pin, evidence can't be located exactly once, results aren't repeatable, a
filtered hit breaks its filter, or any returned citation fails `verify_citation`. Relevance
misses are results, not errors. Search latency and citation-verification time are reported
separately. All returned citations are verified in one batch, which extracts each filing once. The fixture dataset tests the harness.

The real dataset, `evals/retrieval_v1.json`, has 20 questions over three pinned filings: Apple's
fiscal 2026 10-K, Apple's June 2026 10-Q and JPMorgan's September 2026 10-Q. Its BM25
baseline is `evals/retrieval_v1.baseline.json`. It can only be reproduced from the committed
filing cache. The pins are raw-byte
SHA-256 hashes of the cached copies, and a fresh download of an unchanged filing has been
observed to differ by a few bytes, so do not replace the files in `data/sec-cache`. A separate held-out set,
`evals/retrieval_v2_heldout.json` (40 questions over the same three filings), is reserved for
comparing retrieval methods and is never used for tuning. Details:
[docs/architecture.md](architecture.md) §3.2g.

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
[docs/architecture.md](architecture.md) §3.2d.

## Evidence-backed analysis (offline)

```bash
uv run python -m financial_analyst.analysis            # Apple FY2026 revenue, JPMorgan Q3 2026 net income
uv run python -m financial_analyst.analysis AAPL revenue FY2026Q3 FY2025Q3 --query "iPhone net sales"
```

```python
from pathlib import Path

from financial_analyst.analysis import explain_change, load_pinned_data, parse_period

cache = Path("data/sec-cache")
data = load_pinned_data(Path("evals/sec_snapshots_v1.json"), cache)
fy2026, fy2025 = parse_period("FY2026"), parse_period("FY2025")
result = explain_change("AAPL", "revenue", fy2026, fy2025, data=data, cache_dir=cache)
print(result.status, result.summary)
for passage in result.evidence:  # verified candidates, not proof of an explanation
    print(passage.citation_id, passage.text[:200])
```

`explain_change` needs the pinned snapshots and the cached filings, and never contacts SEC.
Every figure comes from `compare_periods`. Commentary is searched only in the filing that
reports both values (its MD&A when the section label is reliable), and a passage is returned
only if its citation verifies and it mentions the metric. A verified citation proves the text
is exactly what the filing says, not that it explains the change, so passages are labeled
candidates. The status is `complete`, `partial` (no usable commentary) or `abstained` (no
reliable comparison), with reasons. Details: [docs/architecture.md](architecture.md) §3.2l.

## Tool-calling agent (scripted model, unvalidated drafts)

```python
from financial_analyst.agent import ModelTurn, ScriptedModel, ToolCall, run_agent

# data and cache as in the analysis example above
lookup = ToolCall(id="1", name="resolve_company", arguments={"identifier": "AAPL"})
model = ScriptedModel(  # a stand-in for a live model adapter
    [ModelTurn(tool_calls=[lookup]), ModelTurn(answer={"answer": "Apple resolved."})]
)
outcome = run_agent("Who is AAPL?", model, data=data, cache_dir=cache)
print(outcome.status, outcome.draft_label, outcome.tool_results[0].output["cik"])
```

`run_agent` gives a model five tools over the pinned data (`resolve_company`, `get_fact`,
`compare`, `commentary_filing`, `search_commentary`), validates every call, and returns tool
errors to the model as data. A run is limited to 6 model turns, 6 tool calls, 90 seconds and
one repair of a malformed draft. A draft refers to numbers only through tool `value_id`s and
to passages through citation ids, and is always labeled `unvalidated_draft`: checking it and
rendering a verified answer is M3.5 B2. No live model is connected. Details:
[docs/architecture.md](architecture.md) §3.2m.

## Grounded answers and the agent evaluation

```python
from financial_analyst.grounding import ground

answer = ground(outcome, data=data, cache_dir=cache)  # outcome from run_agent
print(answer.status, answer.statement)
for quote in answer.quotations:
    print(quote.attribution, quote.text)
```

`ground` checks a draft only against what the run's own tools produced. Every value must
come from this run's comparison for the same company, metric and periods, and the narrative
is never published. Every citation must be a verified passage from that comparison's
filing, and every quotation must be an exact substring of one. The published answer is
rendered by the server: figures and provenance, verified quotations, and candidate
commentary. The model's own wording and explanations are withheld, because causes can't be
verified mechanically. Details: [docs/architecture.md](architecture.md) §3.2n.

```bash
uv run python -m financial_analyst.agent_eval    # v1: 26 cases, frozen path-specific labels
uv run python -m financial_analyst.agent_eval --dataset evals/agent_v2.json   # v2: 28 outcome-labeled cases
```

The evaluation tests the pipeline under fixed model behaviors (good, bad and adversarial).
It does not measure how a real LLM reasons.

## Live model (OpenAI-compatible provider)

Set the provider in your environment (or in the git-ignored `.env`; see `.env.example`).
`OPENAI_API_KEY` works in place of `LLM_API_KEY`; `LLM_API_KEY` wins if both are set:

```bash
export LLM_API_KEY="..."          # never commit it
export LLM_MODEL="your-model-id"
# optional: LLM_BASE_URL (default https://api.openai.com/v1), LLM_REQUEST_TIMEOUT_SECONDS,
# LLM_TEMPERATURE, LLM_INPUT_COST_PER_MTOK / LLM_OUTPUT_COST_PER_MTOK for cost estimates
```

```bash
uv run python -m financial_analyst.live_agent ask "How much did Apple's revenue grow in fiscal 2026?"
uv run python -m financial_analyst.live_agent eval                  # two smoke cases
uv run python -m financial_analyst.live_agent eval --all --output live.json   # 19 cases
# eval prints per-case diagnostics (tools, error codes, how the run ended) and exits 1 if any
# case fails its checks
```

These commands send requests to the provider and may cost money. A question makes at most
6 requests, and each one resends the conversation so far, including tool outputs. Only the
server-rendered, grounded answer is printed, never the model's draft. An answer is
published only if its comparison matches the company, metric and periods read from the
question by fixed patterns (`request_scope.py`); otherwise it abstains. Segment and product
metrics ("iPhone net sales") are not supported and abstain. A question that asks only for
figures is complete without commentary; one that asks why, what drove a change, or what
management said needs a verified citation (and a quotation, if one is asked for). The live
report shows the structure (never the content) of any failed provider response, and counts
requests without usage instead of treating their tokens as zero. Every provider request
requires a function call; on a repair or the last allowed turn, only `submit_answer` is
offered, and it is forced. Without a key or model
the command names the missing setting and sends nothing. `uv run pytest` never contacts a
provider: the adapter is tested against mocked responses. Details:
[docs/architecture.md](architecture.md) §3.2o.

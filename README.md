# Financial AI Analyst

A research assistant for public-company SEC filings that **cannot publish a number it did not
compute**. Ask *"How much did Apple's revenue grow in fiscal 2026 compared with fiscal 2025?"*
and you get exact figures computed from SEC XBRL data, the filing they came from, verified
filing citations when you ask for commentary, and an explicit **complete / partial /
abstained** status. A language model decides which tools to call; the server computes and
writes every figure, period and citation.

## Screenshot

![Financial AI Analyst showing a verified Apple revenue comparison](docs/images/streamlit-demo.png)

## The problem

Language models answering financial questions produce numbers, fiscal periods and citations
that look plausible and can be wrong:
- a figure from the wrong filing or a restated year;
- a calendar quarter mistaken for a fiscal one;
- a "quote" that is not in the document.

In financial research, a confident wrong number is worse than no answer. This project keeps
the model away from the facts. It may only:
- choose deterministic tools;
- point to their results by ID.

Every published value is computed from official SEC data, checked against the question that
was asked, and rendered by the server. When that cannot be done, the answer says partial or
abstains.

## Core technologies

| Area | Used |
|---|---|
| Language and tooling | Python 3.12+, [uv](https://docs.astral.sh/uv/), pytest, ruff |
| Data contracts | Pydantic v2 (frozen models, strict tool-argument validation) |
| SEC data | SEC EDGAR submissions, XBRL Company Facts and filing HTML, via `httpx` (paced, bounded retries); immutable snapshots pinned by SHA-256 |
| Filing text | BeautifulSoup extraction, deterministic passages, in-house BM25 retrieval, byte-level citation verification |
| Financial engine | Exact XBRL concept mapping, fiscal periods from filing report dates, `Decimal` arithmetic |
| LLM | Provider-independent tool-calling loop; OpenAI-compatible Chat Completions adapter over `httpx` (no SDK, no agent framework); verified with GPT-5.4 mini |
| Interface | Streamlit (local demo), plus command-line tools; FastAPI serves a health check |

## Run the demo

**Prerequisites:** [uv](https://docs.astral.sh/uv/) and Python 3.12+. The pinned SEC data the
demo reads (five snapshots and three filings, about 30 MB) is committed under `data/sec-cache`,
so a fresh clone needs no download and no SEC access.

### Offline, no API key

```bash
git clone https://github.com/JerrodMcHenry/financial-ai-analyst.git
cd financial-ai-analyst
uv sync
uv run python -m financial_analyst.analysis                              # both example questions
uv run python -m financial_analyst.agent_eval --dataset evals/agent_v2.json  # 28/28
```

The first command answers both example questions with the deterministic engine: exact figures,
fiscal periods, the source filing, and BM25 commentary with verified citations. Expected:
Apple FY2026 revenue +62,673,000,000 (+15.1%) and JPMorgan Q3 2026 net income +912,000,000
(+6.3%), both `COMPLETE`. The second runs the full agent pipeline (tools, limits, request scope,
grounding, rendering) with scripted models; it does not measure a real model's reasoning.
[docs/demo.md](docs/demo.md) walks through both, with real output. Neither command sends a
request to SEC or an LLM provider, and no `.env` is needed.

### Optional: the Streamlit app, with a provider key

```bash
uv sync --extra ui          # adds the optional Streamlit dependency
cp .env.example .env        # then set OPENAI_API_KEY (or LLM_API_KEY) and LLM_MODEL
uv run --extra ui streamlit run src/financial_analyst/app.py    # from the repository root
```

Open http://localhost:8501, type a question and press **Analyze**. Each question sends a few
requests to your provider (at most 6) and costs money. The page shows:
- the grounded status;
- the verified figures and percentage change, with a chart;
- the source filing link;
- verified quotations and candidate commentary with citation IDs.

It never shows the model's own wording. The app has no offline mode: without a key, **Analyze**
reports the missing setting and sends nothing.

## What it can answer

| Area | Supported |
|---|---|
| Companies | Apple Inc. (CIK 0000320193), JPMorgan Chase & Co. (CIK 0000019617) |
| Metrics (consolidated only) | revenue, net income, total assets |
| Periods | fiscal years; fiscal quarters 1–3 (Q4 is not reported separately); year-end and quarter-end balances |
| Questions | one metric compared between two periods (or one period against the year before), optionally asking why, what drove it, what management said, or for a quotation |
| Data | five SEC snapshots pinned in [`evals/sec_snapshots_v1.json`](evals/sec_snapshots_v1.json), plus three cached filings (Apple FY2026 10-K and June 2026 10-Q, JPMorgan September 2026 10-Q) |

Anything else abstains rather than guessing. That includes:
- other companies;
- segment or product metrics ("iPhone net sales");
- EBITDA and other unmapped metrics;
- fourth quarters and forecasts;
- questions naming two companies or two metrics.

## How it works

```
question ─▶ request scope: company, metric, periods, commentary asked?  (read from the question alone)
    │
    ▼
agent loop ◀──▶ model (live provider, or a scripted model in tests)
    │   tools: resolve_company · get_fact · compare · commentary_filing · search_commentary
    │   every turn must call a function; repair and last turns may only call submit_answer
    │   limits: 6 model turns, 6 tool calls, 90 seconds, 1 repair
    ▼
run evidence: comparisons, facts and verified passages produced by this run's tools
    │
    ▼
grounding: check the model's draft against the run evidence and the request scope
    │
    ▼
server-rendered answer: complete | partial | abstained
```

- **`financial_tools.compare_periods`:** computes each comparison deterministically.
  - It uses the company's exact XBRL concept and fiscal periods taken from filing report dates.
  - Both values come from one filing; restatements in other filings are reported separately.
  - The arithmetic is `Decimal`. The percentage is shown half-even to one decimal, and only for
    a positive base.
- **`search_commentary`:** returns BM25-ranked passages from that same filing (its MD&A when
  the section label is reliable). Each passage has a citation ID that pins the exact source
  bytes.
- **The draft:** the model submits which comparison it is answering, the value IDs and citation
  IDs it relies on, and optional quotations. It never submits a figure.

The full design is in [docs/architecture.md](docs/architecture.md) §2; decisions are in
[docs/decisions.md](docs/decisions.md).

## How figures and citations are verified

The validator checks the draft only against what this run's own tools produced, never against
what the model claims they returned.

| Check | Failure means |
|---|---|
| The answer's comparison was produced in this run, and every value ID belongs to it | **abstained**: no figures shown |
| The comparison matches the company, metric and periods read independently from the question (a Q4 question answered with fiscal years, or "iPhone net sales" answered with total revenue, is rejected) | **abstained** |
| Each citation is a passage this run retrieved from the comparison's filing, and the cached bytes still match it | the citation is dropped; **partial** |
| Each quotation is an exact substring of its passage, and "management" only for MD&A text | the quotation is dropped; **partial** |
| If the question asks for commentary or a quotation, at least one verified one exists | **partial** |

What gets published:
- **Written by the server:** the statement, figures, provenance (comparison ID, XBRL concept,
  filing and Company Facts SHA-256) and attribution.
- **Withheld:** the model's own note and any explanations, kept only in the run record.
- **The only text taken from the filing:** verified quotations, and passages labeled as
  candidate commentary, since a verified citation proves the text is exact, not that it
  explains the change.

## Evaluation

| What | Result | Notes |
|---|---|---|
| Offline test suite | **836 passed, 0 skipped** with the semantic extra and model; **814 passed, 1 skipped** with plain `uv sync` | the skipped module is the optional semantic retrieval (see [Development](#development)). Never calls SEC or a provider |
| Scripted agent evaluation v2 ([`evals/agent_v2.json`](evals/agent_v2.json)) | **28/28 cases** | outcome-based labels; scripted models; measures the pipeline, not model reasoning |
| Scripted agent evaluation v1 ([`evals/agent_v1.json`](evals/agent_v1.json), frozen) | 19/26 (exits 1) | the 7 failures are expected: documented, intended policy changes since v1, each pinned by a test |
| Final live verification (GPT-5.4 mini) | **2/2** | Apple FY2026 revenue and JPMorgan Q3 2026 net income, figures-only; 4 requests, about $0.01 |
| Earlier live runs, in order | 0/2, 1/2, 3/8, 0/4, 2/8, 0/2, 1/2 | provider and draft-reliability failures, each diagnosed and fixed offline; the 3/8 and 0/4 runs used the path-specific v1 labels |

**How reliability was reached:** the earlier live runs found real problems.
- **Drafts:** a contradictory repair protocol, and validation rules that rejected correct drafts.
- **Provider:** the model ending turns with empty messages.
- **Accounting:** understated costs.
- **Off-question answers:** one off-question answer was published: a Q4 question answered with
  full-year figures. Request-scope validation was added in response.

No run published an incorrect figure. The story is told run by run in
[docs/case-study.md](docs/case-study.md).

Not re-verified live after the final changes: commentary-required questions, unsupported
questions and prompt injection. They pass the scripted evaluation only.

## Limitations

- **Narrow scope:** two companies, three consolidated metrics, three cached filings. There is
  no ingestion pipeline, database, hosted service or authentication; the Streamlit app runs
  locally.
- **Pinned data cannot be re-downloaded:** the committed files in `data/sec-cache` are the only
  copy that matches the pins. SEC's submissions and Company Facts endpoints change as companies
  file, and a fresh download of an unchanged filing has been observed to differ by a few bytes,
  so re-acquired data would not match `evals/sec_snapshots_v1.json` and would be refused. The
  data reflects SEC as of 2026-11-13/14 and is not updated.
- **Fixed patterns:** request scope abstains on phrasings it does not recognize, and abstains
  broadly on any segment or product name.
- **Lexical relevance:** commentary is chosen by keyword (BM25) retrieval. Passages are
  labeled candidates, and the model's interpretations are withheld, not verified.
- **Limited live verification:** live results vary from run to run, and the final live check
  was two cases in one run.

## Development

```bash
uv sync                          # runtime + dev dependencies (add --extra ui for the app)
uv run pytest                    # 814 passed, 1 skipped (the optional semantic module)
uv run ruff check . && uv run ruff format --check .
```

With the pinned data present, the suite takes several minutes, mostly re-verifying citations
against the filings. To run all 836 tests, add the optional semantic extra and install its
hash-verified embedding model once (about 128 MB into the git-ignored `data/models/`, a
download from Hugging Face):

```bash
uv sync --extra semantic
uv run python -m financial_analyst.semantic_retrieval install
uv run pytest                    # 836 passed, 0 skipped
```

| Command | What it does |
|---|---|
| `uv run --extra ui streamlit run src/financial_analyst/app.py` | local web app (live provider) |
| `uv run python -m financial_analyst.analysis` | deterministic comparison plus verified commentary, no model |
| `uv run python -m financial_analyst.agent_eval --dataset evals/agent_v2.json` | scripted agent evaluation v2 (offline) |
| `uv run python -m financial_analyst.agent_eval` | scripted agent evaluation v1, frozen (offline); 19/26 and exit 1 are expected |
| `uv run python -m financial_analyst.live_agent ask "QUESTION"` | one live answer in the terminal |
| `uv run python -m financial_analyst.live_agent eval --dataset evals/agent_v2.json --cases v2-apple-fy-revenue,v2-jpm-q3-net-income` | the final live verification |
| `uv run python -m financial_analyst.evaluation evals/retrieval_v1.json` | retrieval evaluation (BM25, offline) |
| `uv run uvicorn financial_analyst.main:create_app --factory` | health-check service (`/health`; needs `SEC_USER_AGENT`) |

**Configuration** (environment or `.env`; see [`.env.example`](.env.example)). The offline
demo, the tests and the scripted evaluations need none of it.
- **For SEC access and the health service only:** `SEC_USER_AGENT`, your name and a contact
  email, which SEC's fair-access policy requires. The demo, the app and the evaluations read
  the committed pinned data and do not need it.
- **For live answers:** `OPENAI_API_KEY` (or `LLM_API_KEY`) and `LLM_MODEL`.
- **Optional:** `LLM_BASE_URL`, `LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_TEMPERATURE`, and
  `LLM_INPUT_COST_PER_MTOK` / `LLM_OUTPUT_COST_PER_MTOK` for cost estimates.

Keys stay server-side and are never printed. A missing setting is reported by name only.

## Data provenance

The 16 files under `data/sec-cache` are unmodified copies of public SEC EDGAR data, retrieved
2026-11-13 and 2026-11-14:
- **Snapshots** (pinned in [`evals/sec_snapshots_v1.json`](evals/sec_snapshots_v1.json)):
  SEC's `company_tickers.json`, plus submissions and XBRL Company Facts for Apple Inc.
  (CIK 0000320193) and JPMorgan Chase & Co. (CIK 0000019617), from `data.sec.gov`.
- **Filings:** the primary documents of Apple's 10-K (0000320193-26-000026) and 10-Q
  (0000320193-26-000020), and JPMorgan's 10-Q (0001628280-26-069523), from SEC's EDGAR
  archive.

Each data file has a small metadata file recording its source URL, retrieval time, HTTP status
and SHA-256, and is verified against that hash on every load, so a modified file is refused
rather than used. Source: U.S. Securities and Exchange Commission, EDGAR
(https://www.sec.gov/edgar). This project is not affiliated with or endorsed by the SEC, Apple
or JPMorgan Chase.

## Documentation

| Document | Contents |
|---|---|
| [docs/case-study.md](docs/case-study.md) | portfolio summary: accomplishments, live reliability story, limitations |
| [docs/demo.md](docs/demo.md) | reproducible demonstration with real output |
| [docs/architecture.md](docs/architecture.md) | final system (§2) and the record of every milestone |
| [docs/decisions.md](docs/decisions.md) | architecture decision log (D-001 – D-043) |
| [docs/developer-guide.md](docs/developer-guide.md) | using each component from Python and the command line |

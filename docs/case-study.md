# Case study: a financial AI analyst that cannot invent a number

## Problem

Language models answering financial questions can produce numbers, periods and citations that
look right and are not. The goal was a research assistant over SEC filings in which every
published figure is computed deterministically from official data, every citation can be
re-verified byte for byte, and the system says *partial* or *abstained* instead of guessing.

## What was built

- **Data:** five SEC snapshots (submissions and XBRL Company Facts for Apple and JPMorgan
  Chase, plus SEC's ticker map), each pinned by SHA-256 in a committed manifest and verified on
  every load, and three cached 10-K/10-Q filings. Tests and evaluations never contact SEC.
- **Financial engine** (`financial_tools.py`):
  - exact per-company XBRL concept mapping;
  - fiscal periods grounded in filing report dates rather than SEC's `fy`/`fp` tags;
  - same-filing comparisons with Decimal arithmetic, so a restatement is reported separately
    and never mixed in;
  - an explicit percentage policy: undefined for a zero base, not shown for a negative one.
- **Retrieval and citations:** a deterministic extractor and passage builder. BM25 is the
  default retriever (semantic and hybrid retrieval were evaluated on a held-out set and not
  adopted). Citation IDs pin the source bytes, extractor and builder versions, and the exact
  character range, and are re-verified against the cache.
- **Agent:**
  - a provider-independent tool-calling loop with five validated tools;
  - required function calls on every turn, with a forced, answer-only last turn;
  - a single structured repair;
  - limits of 6 turns, 6 tool calls and 90 seconds;
  - an OpenAI-compatible adapter over httpx, with no SDK and no agent framework.
- **Grounding and rendering:**
  - an independent reading of the question's scope;
  - validation of the draft against the run's own tool results;
  - server-side rendering of every published figure and label.

  The model never writes a published number, period or citation.
- **Evaluation:** 836 offline tests, plus an outcome-based scripted evaluation (28 cases) that
  scores answering the question, figures, context, citation integrity, abstention, status,
  reliability, budget and safe content independently.

## Strongest technical accomplishments

1. **No model-written fact can be published.** Figures come only from a `Comparison` produced
   in the same run and are formatted by the server. A model that invents a value ID,
   substitutes a period or obeys injected text gets an abstention or a partial answer, not a
   wrong number.
2. **Independent request scope.** The company, metric and periods are read from the question
   by fixed patterns, and the answer must match them. This caught a live model answering a Q4
   question with full-year figures.
3. **Verifiable citations.** A citation ID determines exactly which bytes it refers to.
   Grounding re-checks the cached source, rejects any ID the run did not retrieve, and
   requires quotations to be exact substrings with honest attribution: management only for
   MD&A.
4. **Diagnosis by evidence, not guesswork.** Each live failure was instrumented before it was
   fixed:
   - draft rejection codes;
   - the structure of provider responses, never their content;
   - usage counted even for unusable responses.

   That separated real causes (a contradictory repair protocol, obsolete placeholder and digit
   rules, the provider allowing empty turns) from hypotheses.
5. **Honest evaluation.** Path-specific labels that scored correct live abstentions as
   failures were replaced by outcome-based labels in a new dataset. The original stays frozen,
   with each intended difference pinned by a test. Scripted results are never presented as
   live model results.

## The live reliability story

| Run (GPT-5.4 mini) | Result | Finding → fix |
|---|---|---|
| First smoke test | 0/2 | one tool call per turn exhausted 4 turns → 6 turns, workflow prompt, diagnostics |
| Smoke rerun | 1/2 | unsupported "management" attribution → attribution rule in the prompt |
| Eight cases | 3/8 | empty repair responses and a Q4 → full-year substitution → repair via the answer tool, request-scope validation |
| Four cases | 0/4 (v1 labels) | correct abstentions scored as failures; skipped commentary; an unreturned citation ID → outcome-based evaluation v2, commentary only when asked, citation feedback |
| Eight cases (v2) | 2/8 | placeholder rules failing drafts; empty turns; understated cost → simpler draft contract, response shapes, honest usage |
| Two cases | 0/2 | well-formed empty messages after `compare` → required function calls |
| Two cases | 1/2 | digit rule on an unpublished note → rule removed; model text kept out of the answer |
| **Final two cases** | **2/2** | figures-only questions answered correctly, in two requests each |

No run published an incorrect figure. One run (the first eight-case run) published an
off-question answer: a Q4 question answered with full-year figures and labeled complete.
Request-scope validation was added in response. When the model attempted the same
substitution again (the four-case run), the check blocked it; in the next eight-case run, the
substituted draft failed draft validation before reaching grounding.

## Limitations

- **Narrow scope:** two companies, three consolidated metrics, three filings. There is no
  ingestion pipeline, database, HTTP research endpoint or hosted UI (only a minimal local
  Streamlit page).
- **Data not in the repository:** the pinned data lives in a git-ignored cache that cannot be
  recreated byte for byte from a fresh clone.
- **Limited live verification:** two figures-only cases in one run after the final changes.
  Commentary, unsupported and injection questions are verified offline only. Live behavior
  varies between runs.
- **Relevance is lexical:** commentary is chosen by BM25, and a verified citation proves the
  text is exact, not that it explains the change. Model interpretations are withheld rather
  than verified.
- **Fixed patterns:** request scope abstains on phrasings it does not recognize, and abstains
  broadly on any segment or product name.

## Possible next steps

- Ingestion for more companies and metrics, with snapshot manifests per release.
- A repeated-sampling live evaluation on the full v2 set, to measure variance.
- A research HTTP endpoint behind the grounded answer, and a hosted version of the local
  Streamlit page.

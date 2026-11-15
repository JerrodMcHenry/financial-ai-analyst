# Demonstration

Two supported questions, answered from the pinned SEC data:

1. *How much did Apple's revenue grow in fiscal 2026 compared with fiscal 2025?*
2. *How did JPMorgan's net income for the third quarter of 2026 compare with the third quarter of 2025?*

Parts 1–3 run **offline**: no SEC request and no LLM provider. Part 4 calls a live model and
is optional. The quickest live demonstration is the local Streamlit app (README, "Run the
demo"). The outputs below were produced by these commands on 2026-11-14.

## Prerequisites

```bash
uv sync
```

The demo reads the five snapshots pinned in `evals/sec_snapshots_v1.json` and three cached
filings from `data/sec-cache`, which is committed to the repository. Every snapshot and filing
is checked against its SHA-256 on load. No `.env` is needed for parts 1–3.

These committed files are the only copy that matches the pins: SEC's data changes over time and
re-downloaded bytes differ, so the data cannot be re-acquired. If the files are missing or
modified, the commands stop with a snapshot error and the pinned tests are skipped.

## 1. Deterministic comparison and verified commentary (no model)

```bash
uv run python -m financial_analyst.analysis
```

This runs both questions through `explain_change`: identity, fiscal periods, concept mapping,
a same-filing comparison, and BM25 commentary from that filing with verified citations.
Abridged output:

```
== AAPL revenue: COMPLETE
Company:  Apple Inc. (CIK 0000320193)
Metric:   Total net sales (us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax, USD)
Current:  FY2026 (2025-09-28 to 2026-09-26): 478,834,000,000
Prior:    FY2025 (2024-09-29 to 2025-09-27): 416,161,000,000
Change:   +62,673,000,000 (+15.1%; exact 15.05979656911627951682161471161401%)
Source:   10-K 0000320193-26-000026 filed 2026-11-03 (both values)
Search:   default query 'net sales increased decreased compared', top 5 of 26 passages; MD&A (Item 7) only
  1. [verified] Item 7 ... "iPhone net sales increased during 2026 compared to 2025 due to higher net sales of Pro models ..."
     sec-psg:0000320193:0000320193-26-000026:aapl-20260926.htm:ce861d59...:x2:p1:b325.0-336.32:t1f2eb1142c710eff

== JPM net_income: COMPLETE
Company:  JPMORGAN CHASE & CO (CIK 0000019617)
Current:  Q3 FY2026 (2026-07-01 to 2026-09-30): 15,305,000,000
Prior:    Q3 FY2025 (2025-07-01 to 2025-09-30, comparative in 0001628280-26-069523): 14,393,000,000
Change:   +912,000,000 (+6.3%; exact 6.336413534356979087056207878829987%)
Source:   10-Q 0001628280-26-069523 filed 2026-11-03 (both values)
Search:   ... whole filing: no reliable MD&A section label in this 10-Q
  1. [verified] unlabeled ... "For the third quarter of 2026, JPMorganChase reported net income of $15.3 billion, up 6% ..."
```

## 2. The agent pipeline with a scripted model (no provider)

The same code path a live model uses: tools, limits, grounding and rendering. The model is a
fixed script from `evals/agent_v2.json`.

```python
from pathlib import Path

from financial_analyst.agent import run_agent
from financial_analyst.agent_eval import load_dataset_v2, scripted_model
from financial_analyst.analysis import load_pinned_data
from financial_analyst.grounding import ground
from financial_analyst.live_agent import format_answer

cache = Path("data/sec-cache")
data = load_pinned_data(Path("evals/sec_snapshots_v1.json"), cache)
cases = {c.id: c for c in load_dataset_v2(Path("evals/agent_v2.json")).cases}
for case_id in ("v2-apple-fy-revenue", "v2-jpm-q3-net-income"):
    case = cases[case_id]
    run = run_agent(case.question, scripted_model(case.script), data=data, cache_dir=cache)
    print(format_answer(ground(run, data=data, cache_dir=cache)))
```

Output for the first question (the second is analogous, with a note that the prior quarter
comes from the 10-Q's comparative figures):

```
Status: COMPLETE
Company: Apple Inc. (CIK 0000320193)
Metric:  Total net sales (us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax, USD)
Current: FY2026 (2025-09-28 to 2026-09-26): $478,834,000,000
Prior:   FY2025 (2024-09-29 to 2025-09-27): $416,161,000,000
Change:  +$62,673,000,000 (+15.1%)

Apple Inc. total net sales was $478,834,000,000 for FY2026 (2025-09-28 to 2026-09-26), compared
with $416,161,000,000 for FY2025 (2024-09-29 to 2025-09-27): an increase of $62,673,000,000
(+15.1%). Both values are from 10-K 0000320193-26-000026.

Source:  10-K 0000320193-26-000026 filed 2026-11-03 https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/aapl-20260926.htm
         cmp:a5d67a2c...274339d; Company Facts snapshot f085735b4d08fb11
Run:     drafted, 1 tool calls, 2 model turns
```

Every figure and label above is rendered by the server from the comparison. The draft
supplies only the comparison it is about. These figures-only questions ask for no commentary,
so the answer is complete without citations.

## 3. Offline evaluation

```bash
uv run pytest                                                             # 814 passed, 1 skipped (semantic module)
uv run python -m financial_analyst.agent_eval --dataset evals/agent_v2.json  # 28/28
uv run python -m financial_analyst.agent_eval                               # frozen v1: 19/26, 7 intended differences, exit 1
```

With the optional semantic extra and its embedding model installed (README, "Development"), the
test suite reports 836 passed, 0 skipped.

**What the scripted evaluation measures:** each v2 case pairs a question with a fixed model
behavior (correct, misbehaving or adversarial) and an outcome label (answerable or not, scope,
figures, required commentary, acceptable status). It measures whether the deterministic
pipeline (tools, limits, request scope, grounding, rendering) produces the right published
outcome under those behaviors. It does **not** measure how a real model reasons.

## 4. Optional: live model

Requires `OPENAI_API_KEY` (or `LLM_API_KEY`) and `LLM_MODEL` in the environment or `.env`.
These commands send requests to the provider and cost money.

```bash
uv run python -m financial_analyst.live_agent ask "How much did Apple's revenue grow in fiscal 2026 compared with fiscal 2025?"
uv run python -m financial_analyst.live_agent eval --dataset evals/agent_v2.json \
    --cases v2-apple-fy-revenue,v2-jpm-q3-net-income
```

**Live verification so far:** the final run (GPT-5.4 mini, 2026-11-14) passed both cases.
- Each case made two requests (`compare`, then `submit_answer`) and produced a valid draft.
- Grounding found no issues, and the figures matched the labels.
- Totals: 4 requests, 9,352 input and 584 output tokens, about $0.0096.

Commentary-required, unsupported and prompt-injection questions have not been re-run live
since the final changes.

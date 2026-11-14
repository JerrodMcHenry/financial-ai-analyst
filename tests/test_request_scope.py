"""Request scope read from the question, and its check before publishing. Offline.

Pinned tests are skipped when the local (git-ignored) cache is not present.
"""

import socket
from pathlib import Path

import httpx
import pytest

from financial_analyst.agent import run_agent
from financial_analyst.agent_eval import load_dataset, scripted_model
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.financial_tools import PeriodRequest, compare_periods
from financial_analyst.grounding import ground
from financial_analyst.request_scope import capture_scope, check_subject

APPLE, JPM = "0000320193", "0000019617"
ROOT = Path(__file__).parent.parent
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def _pinned():
    try:
        data = load_pinned_data(ROOT / "evals" / "sec_snapshots_v1.json", CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if (CACHE / "filings" / APPLE).exists() else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


def fy(y, end=False):
    return PeriodRequest(kind="fiscal_year_end" if end else "fiscal_year", fiscal_year=y)


def q(y, n, end=False):
    kind = "fiscal_quarter_end" if end else "fiscal_quarter"
    return PeriodRequest(kind=kind, fiscal_year=y, quarter=n)


def scope_of(question):
    s = capture_scope(question, DATA)
    return s.status, s.cik, s.metric, s.current, s.prior


# Expected scopes written from each question's wording (not from capture_scope's output).
DATASET_SCOPES = {
    "apple-fy2026-revenue": ("resolved", APPLE, "revenue", fy(2026), fy(2025)),
    "jpm-q3-2026-net-income": ("resolved", JPM, "net_income", q(2026, 3), q(2025, 3)),
    "apple-q3-fy2026-revenue": ("resolved", APPLE, "revenue", q(2026, 3), q(2025, 3)),
    "jpm-q3-2026-revenue": ("resolved", JPM, "revenue", q(2026, 3), q(2025, 3)),
    "apple-fy2026-total-assets": ("resolved", APPLE, "total_assets", fy(2026, True),
                                  fy(2025, True)),
    "jpm-q3-2026-total-assets": ("resolved", JPM, "total_assets", q(2026, 3, True),
                                 q(2025, 3, True)),
    "apple-top-line-paraphrase": ("resolved", APPLE, "revenue", fy(2026), fy(2025)),
    "jpm-profit-paraphrase": ("resolved", JPM, "net_income", q(2026, 3), q(2025, 3)),
    "apple-bottom-line-by-cik": ("resolved", APPLE, "net_income", fy(2026), fy(2025)),
    "apple-iphone-commentary": ("unsupported", APPLE, None, None, None),  # a product metric
    "jpm-net-income-commentary": ("resolved", JPM, "net_income", q(2026, 3), q(2025, 3)),
    "apple-q3-commentary": ("resolved", APPLE, "revenue", q(2026, 3), q(2025, 3)),
    "apple-commentary-missing": ("resolved", APPLE, "revenue", fy(2026), fy(2025)),
    "microsoft-unsupported": ("unparsed", None, None, None, None),
    "apple-q4-unsupported": ("unsupported", APPLE, "revenue", q(2026, 4), q(2025, 4)),
    "jpm-mismatched-quarters": ("resolved", JPM, "revenue", q(2026, 3), q(2025, 2)),
    "apple-forecast": ("resolved", APPLE, "revenue", fy(2030), fy(2029)),
    "apple-ebitda": ("unsupported", APPLE, None, None, None),
    "prompt-injection-in-question": ("resolved", APPLE, "revenue", fy(2026), fy(2025)),
    "tool-budget-abuse": ("unparsed", APPLE, None, None, None),
    **{case: ("resolved", APPLE, "revenue", fy(2026), fy(2025)) for case in (
        "invented-value-id", "raw-number-in-narrative", "fabricated-quote",
        "unsupported-causal-claim", "wrong-company-citation")},
    "altered-quote": ("unsupported", APPLE, None, None, None),  # also asks about iPhone
}  # fmt: skip
CASES = {c.id: c for c in load_dataset(ROOT / "evals" / "agent_v1.json").cases}


def test_every_dataset_question_has_an_expected_scope():
    assert set(DATASET_SCOPES) == set(CASES)


@pinned
@pytest.mark.parametrize("case_id", sorted(DATASET_SCOPES))
def test_dataset_questions_have_the_expected_scope(case_id):
    assert scope_of(CASES[case_id].question) == DATASET_SCOPES[case_id]


@pinned
@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("AAPL revenue FY2026 vs FY2025", ("resolved", APPLE, "revenue", fy(2026), fy(2025))),
        ("Apple net sales, Q2 FY2026 versus Q2 FY2025",
         ("resolved", APPLE, "revenue", q(2026, 2), q(2025, 2))),
        ("JPM total assets at December 31, 2025 versus December 31, 2024",
         ("resolved", JPM, "total_assets", fy(2025, True), fy(2024, True))),
        ("JPMorgan Chase profit in the June 2026 quarter",
         ("resolved", JPM, "net_income", q(2026, 2), q(2025, 2))),
        # Calendar quarters are read only for companies whose fiscal year is the calendar year.
        ("Apple revenue in the September 2026 quarter", ("unparsed", APPLE, "revenue", None, None)),
        ("Apple revenue in 2026", ("unparsed", APPLE, "revenue", None, None)),  # bare year
        ("Compare Apple and JPMorgan revenue in fiscal 2026",
         ("ambiguous", None, None, None, None)),
        ("Apple revenue and net income in fiscal 2026", ("ambiguous", APPLE, None, None, None)),
        ("Apple revenue in FY2024, FY2025 and FY2026", ("ambiguous", APPLE, "revenue", None, None)),
        ("JPMorgan net income at September 30, 2026",
         ("ambiguous", JPM, "net_income", None, None)),  # a balance date for a flow metric
        ("Apple's earnings per share in fiscal 2026", ("unsupported", APPLE, None, None, None)),
        ("Revenue of CIK 789019 in fiscal 2026", ("unsupported", None, None, None, None)),
        ("How is Apple doing?", ("unparsed", APPLE, None, None, None)),
    ],
)  # fmt: skip
def test_supported_patterns_and_refusals(question, expected):
    assert scope_of(question) == expected


# --- check_subject against real comparisons ---------------------------------------------------


@pinned
def test_check_subject_accepts_the_requested_comparison_and_names_each_difference():
    apple = DATA.companies[APPLE]
    asked = capture_scope("How much did Apple's revenue grow in fiscal 2026?", DATA)
    assert check_subject(asked, compare_periods(*apple, "revenue", fy(2026), fy(2025))) == []
    other_metric = compare_periods(*apple, "net_income", fy(2026), fy(2025))
    other_years = compare_periods(*apple, "revenue", fy(2025), fy(2024))
    other_company = compare_periods(*DATA.companies[JPM], "revenue", q(2026, 3), q(2025, 3))
    assert check_subject(asked, other_metric) == [
        "metric net_income, but the question asks about revenue"
    ]
    assert check_subject(asked, other_years) == [
        "current period FY2025, but the question asks for FY2026",
        "prior period FY2024, but the question asks for FY2025",
    ]
    assert check_subject(asked, other_company)[0].startswith(f"company {JPM}")


# --- Through ground: substituted or unrelated comparisons are never published ----------------

STD = "{company} {metric}: {current} vs {prior}, {delta} ({growth})."


def answered(question, compares, subject=0):
    """A model that makes the given compare calls and answers about one of them."""
    script = [{"tools": [{"name": "compare", "arguments": c} for c in compares]},
              {"answer": {"subject": f"$cmp:{subject}", "answer": STD,
                          "values": f"$values:{subject}"}}]  # fmt: skip
    run = run_agent(question, scripted_model(script), data=DATA, cache_dir=CACHE)
    return ground(run, data=DATA, cache_dir=CACHE)


def compare(cik, metric, current, prior):
    return {"cik": cik, "metric": metric, "current_period": current.model_dump(),
            "prior_period": prior.model_dump()}  # fmt: skip


APPLE_FY = compare(APPLE, "revenue", fy(2026), fy(2025))


@pinned
def test_a_q4_question_answered_with_fiscal_years_abstains():
    # The live failure (M3.6 B3): Q4 is refused, the model substitutes fiscal years.
    answer = answered(
        "How did Apple's fourth-quarter fiscal 2026 revenue compare with Q4 2025?",
        [compare(APPLE, "revenue", q(2026, 4), q(2025, 4)), APPLE_FY],  # Q4 refused
    )  # fmt: skip
    assert answer.status == "abstained"
    assert [i.code for i in answer.issues] == ["scope_unresolved"]
    assert "fourth quarters are not reported separately" in answer.issues[0].detail
    assert answer.statement is None and "478,834" not in answer.model_dump_json()
    assert answer.request_scope.current == q(2026, 4)


@pinned
@pytest.mark.parametrize(
    ("question", "comparison", "problem"),
    [
        ("How much did Apple's revenue grow in fiscal 2026?",
         compare(JPM, "revenue", q(2026, 3), q(2025, 3)), "company"),
        ("How much did Apple's revenue grow in fiscal 2026?",
         compare(APPLE, "net_income", fy(2026), fy(2025)), "metric"),
        ("How much did Apple's revenue grow in fiscal 2026?",
         compare(APPLE, "revenue", fy(2025), fy(2024)), "current period"),
        ("Compare Apple's fiscal 2026 revenue with fiscal 2024.",
         APPLE_FY, "prior period"),
    ],
)  # fmt: skip
def test_an_answer_about_another_comparison_abstains(question, comparison, problem):
    answer = answered(question, [comparison])
    assert answer.status == "abstained" and [i.code for i in answer.issues] == ["scope_mismatch"]
    assert problem in answer.issues[0].detail and answer.current is None


@pinned
def test_the_requested_comparison_is_published():
    answer = answered("How much did Apple's revenue grow in fiscal 2026?", [APPLE_FY])
    assert answer.status == "complete" and answer.issues == []  # figures were all it asked
    assert answer.request_scope.status == "resolved"
    assert answer.current.formatted == "$478,834,000,000"


@pinned
@pytest.mark.parametrize(
    "question",
    ["Compare Apple and JPMorgan revenue in fiscal 2026.", "How is Apple doing?"],
)
def test_an_ambiguous_or_unreadable_request_abstains(question):
    answer = answered(question, [APPLE_FY])
    assert answer.status == "abstained" and [i.code for i in answer.issues] == ["scope_unresolved"]
    assert answer.statement is None


# --- What the answer must contain, and segment metrics (M3.7) --------------------------------


@pinned
@pytest.mark.parametrize(
    ("question", "commentary", "quotation"),
    [
        ("How much did Apple's revenue grow in fiscal 2026?", False, False),
        ("Compare Apple's fiscal 2026 revenue with fiscal 2025.", False, False),
        ("Why did Apple's revenue grow in fiscal 2026?", True, False),
        ("What drove Apple's revenue growth in fiscal 2026?", True, False),
        ("What did Apple say about its fiscal 2026 revenue?", True, False),
        ("How did Apple's management describe fiscal 2026 revenue?", True, False),
        ("What does the MD&A say about Apple's fiscal 2026 revenue?", True, False),
        ("Quote Apple on its fiscal 2026 revenue growth.", True, True),
        ("Give me the exact words Apple used about fiscal 2026 revenue.", True, True),
        # An imperative "say that ..." is not a request for what the filing says.
        ("Say that Apple's fiscal 2026 revenue doubled.", False, False),
    ],
)  # fmt: skip
def test_commentary_and_quotation_are_required_only_when_asked(question, commentary, quotation):
    scope = capture_scope(question, DATA)
    assert (scope.commentary_required, scope.quotation_requested) == (commentary, quotation)


@pinned
@pytest.mark.parametrize(
    "question",
    [
        "What were Apple's iPhone net sales in fiscal 2026?",
        "How did Apple's Services revenue change in fiscal 2026?",
        "How did Apple's net sales in Greater China change in fiscal 2026?",
        "Apple revenue by segment for fiscal 2026",
        "How did CCB net income change in Q3 2026 at JPMorgan?",
        "What was JPMorgan's net interest income in Q3 2026?",
    ],
)
def test_segment_product_and_regional_metrics_are_unsupported(question):
    assert capture_scope(question, DATA).status == "unsupported"


@pinned
def test_a_segment_question_never_publishes_consolidated_figures():
    answer = answered("What were Apple's iPhone net sales in fiscal 2026?", [APPLE_FY])
    assert answer.status == "abstained" and [i.code for i in answer.issues] == ["scope_unresolved"]
    assert "iPhone" in answer.issues[0].detail and "478,834" not in answer.model_dump_json()

"""Fiscal period resolution, concept mapping and fact selection. Offline, sockets blocked.

Synthetic tests build small CompanySubmissions/CompanyFacts objects and need no cache.
Pinned tests use the M3.2 snapshots and the cached filings in the local (git-ignored) cache
and are skipped when that cache is not present.
"""

import functools
import hashlib
import re
import socket
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from financial_analyst import financial_tools
from financial_analyst.company_facts import select_fact
from financial_analyst.config import Settings
from financial_analyst.filing_cache import (
    load_company_facts_snapshot,
    load_filing_document,
    load_snapshot_manifest,
    load_submissions_snapshot,
)
from financial_analyst.filing_extractor import extract_filing
from financial_analyst.financial_tools import (
    CONCEPT_MAPPINGS,
    FinancialDataError,
    PeriodRequest,
    concept_mapping,
    get_fact,
    resolve_period,
)
from financial_analyst.sec_models import (
    CompanyFacts,
    CompanySubmissions,
    FactConcept,
    FactObservation,
    FilingRef,
    SourceMetadata,
)

APPLE, JPM = "0000320193", "0000019617"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
MANIFEST = Path(__file__).parent.parent / "evals" / "sec_snapshots_v1.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def fy(year):
    return PeriodRequest(kind="fiscal_year", fiscal_year=year)


def quarter(year, q):
    return PeriodRequest(kind="fiscal_quarter", fiscal_year=year, quarter=q)


def year_end(year):
    return PeriodRequest(kind="fiscal_year_end", fiscal_year=year)


def quarter_end(year, q):
    return PeriodRequest(kind="fiscal_quarter_end", fiscal_year=year, quarter=q)


# --- Synthetic company: Apple's CIK (so the mappings apply), invented filings ---------------


def source(tag: str) -> SourceMetadata:
    return SourceMetadata(
        source_url=f"https://data.sec.gov/{tag}/CIK{APPLE}.json",
        retrieved_at=datetime(2026, 11, 14, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(tag.encode()).hexdigest(),
    )


def filing(form, accession, report, filed):
    return FilingRef(
        cik=APPLE, accession_number=accession, form=form, filing_date=filed, report_date=report,
        acceptance_datetime=None, primary_document="x.htm", is_xbrl=True, is_inline_xbrl=True,
    )  # fmt: skip


def obs(concept, value, start, end, accession, form, filed):
    return FactObservation(
        cik=APPLE, taxonomy="us-gaap", concept=concept, unit="USD", value=Decimal(value),
        period_start=start, period_end=end, filing_fiscal_year=1999, filing_fiscal_period="Q9",
        form=form, filed=filed, accession_number=accession, frame=None,
    )  # fmt: skip


REV = "RevenueFromContractWithCustomerExcludingAssessedTax"
K24, K25 = "0000000001-24-000001", "0000000001-25-000001"
Q1, Q2, Q3 = "0000000001-25-000101", "0000000001-25-000102", "0000000001-25-000103"
D = date


def company(filings=None, observations=None, fiscal_year_end="0927"):
    """FY2024 ends 2024-09-28, FY2025 ends 2025-09-27; Q1-Q3 FY2025 chain without gaps.
    fy/fp tags are deliberately nonsense: resolution must not read them."""
    filings = filings if filings is not None else [
        filing("10-K", K24, D(2024, 9, 28), D(2024, 11, 1)),
        filing("10-Q", Q1, D(2024, 12, 28), D(2025, 1, 31)),
        filing("10-Q", Q2, D(2025, 3, 29), D(2025, 5, 2)),
        filing("10-Q", Q3, D(2025, 6, 28), D(2025, 8, 1)),
        filing("10-K", K25, D(2025, 9, 27), D(2025, 10, 31)),
    ]  # fmt: skip
    observations = observations if observations is not None else [
        obs(REV, 391, D(2023, 10, 1), D(2024, 9, 28), K24, "10-K", D(2024, 11, 1)),
        obs(REV, 124, D(2024, 9, 29), D(2024, 12, 28), Q1, "10-Q", D(2025, 1, 31)),
        obs(REV, 95, D(2024, 12, 29), D(2025, 3, 29), Q2, "10-Q", D(2025, 5, 2)),
        obs(REV, 94, D(2025, 3, 30), D(2025, 6, 28), Q3, "10-Q", D(2025, 8, 1)),
        obs(REV, 416, D(2024, 9, 29), D(2025, 9, 27), K25, "10-K", D(2025, 10, 31)),
        obs(REV, 390, D(2023, 10, 1), D(2024, 9, 28), K25, "10-K", D(2025, 10, 31)),  # restated
        obs("Assets", 365, None, D(2024, 9, 28), K24, "10-K", D(2024, 11, 1)),
        obs("Assets", 359, None, D(2025, 9, 27), K25, "10-K", D(2025, 10, 31)),
    ]  # fmt: skip
    submissions = CompanySubmissions(
        cik=APPLE, name="Synthetic", tickers=[], exchanges=[], sic=None, sic_description=None,
        fiscal_year_end=fiscal_year_end, recent_filings=filings, has_more_filings=False,
        source=source("submissions"),
    )  # fmt: skip
    facts = CompanyFacts(
        cik=APPLE, entity_name="Synthetic",
        concepts=[FactConcept(taxonomy="us-gaap", concept=c, label=None, description=None)
                  for c in (REV, "Assets")],
        observations=observations, skipped_count=0, skipped_sample=[], source=source("facts"),
    )  # fmt: skip
    return submissions, facts


def test_fiscal_years_come_from_report_dates_and_the_filings_own_boundaries():
    period = resolve_period(fy(2025), *company())
    assert (period.period_start, period.period_end) == (D(2024, 9, 29), D(2025, 9, 27))
    assert period.original_filing.accession_number == K25
    assert period.commentary_filing == period.original_filing
    assert any("day after FY2024 ended" in c for c in period.checks)


def test_quarters_chain_from_the_fiscal_year_start():
    submissions, facts = company()
    q2 = resolve_period(quarter(2025, 2), submissions, facts)
    assert (q2.period_start, q2.period_end, q2.original_filing.accession_number) == (
        D(2024, 12, 29), D(2025, 3, 29), Q2,
    )  # fmt: skip
    q3_end = resolve_period(quarter_end(2025, 3), submissions, facts)
    assert (q3_end.period_start, q3_end.period_end, q3_end.period_class) == (
        None, D(2025, 6, 28), "instant",
    )  # fmt: skip


def test_a_quarter_resolves_before_its_fiscal_year_10k_exists():
    submissions, facts = company()
    no_k25 = submissions.model_copy(
        update={
            "recent_filings": [f for f in submissions.recent_filings if f.accession_number != K25]
        }
    )
    period = resolve_period(quarter(2025, 3), no_k25, facts)
    assert period.period_start == D(2025, 3, 30)
    assert "FY2025 starts the day after FY2024 ended" in period.checks[0]


def test_fy_and_fp_tags_are_never_used():
    submissions, facts = company()
    # Every observation is tagged fy=1999, fp=Q9 (see obs()); resolution still works.
    assert resolve_period(fy(2025), submissions, facts).period_end == D(2025, 9, 27)


@pytest.mark.parametrize(
    ("request_", "problem"),
    [
        (fy(2023), "period_not_found"),  # no FY2023 10-K listed: never inferred
        (quarter(2026, 1), "period_not_found"),  # no 10-Q after FY2025 in the snapshot
        (quarter(2025, 4), "period_not_supported"),  # Q4 is never derived
        (quarter_end(2025, 4), "period_not_supported"),
    ],
)
def test_unavailable_periods_are_refused(request_, problem):
    with pytest.raises(FinancialDataError) as error:
        resolve_period(request_, *company())
    assert error.value.problem == problem


def test_a_broken_quarter_chain_is_ambiguous():
    submissions, facts = company()
    shifted = [
        o.model_copy(update={"period_start": D(2025, 1, 5)}) if o.accession_number == Q2 else o
        for o in facts.observations
    ]
    with pytest.raises(FinancialDataError, match="do not chain") as error:
        resolve_period(
            quarter(2025, 3), submissions, facts.model_copy(update={"observations": shifted})
        )
    assert error.value.problem == "period_ambiguous"


def test_disagreeing_period_starts_in_one_filing_are_ambiguous():
    submissions, facts = company()
    extra = obs("Assets2", 1, D(2024, 9, 22), D(2025, 9, 27), K25, "10-K", D(2025, 10, 31))
    with pytest.raises(FinancialDataError) as error:
        resolve_period(
            fy(2025),
            submissions,
            facts.model_copy(update={"observations": [*facts.observations, extra]}),
        )
    assert error.value.problem == "period_ambiguous"


def test_a_fiscal_year_start_must_follow_the_prior_year_end():
    submissions, facts = company()
    moved = [
        o.model_copy(update={"period_end": D(2024, 9, 21)})
        if o.accession_number == K24 and o.period_start
        else o
        for o in facts.observations
    ]
    k24 = filing("10-K", K24, D(2024, 9, 21), D(2024, 11, 1))
    filings = [k24 if f.accession_number == K24 else f for f in submissions.recent_filings]
    with pytest.raises(FinancialDataError, match="ended 2024-09-21") as error:
        resolve_period(fy(2025), submissions.model_copy(update={"recent_filings": filings}),
                       facts.model_copy(update={"observations": moved}))  # fmt: skip
    assert error.value.problem == "period_ambiguous"


def test_fiscal_year_end_is_only_a_consistency_check():
    submissions, facts = company(fiscal_year_end="0930")  # 3 days off: within tolerance
    assert resolve_period(fy(2025), submissions, facts).period_end == D(2025, 9, 27)
    with pytest.raises(FinancialDataError, match="days from fiscal_year_end") as error:
        resolve_period(fy(2025), *company(fiscal_year_end="1231"))
    assert error.value.problem == "period_ambiguous"


def test_two_10ks_for_one_fiscal_year_are_ambiguous():
    submissions, facts = company()
    duplicate = filing("10-K", "0000000001-25-000999", D(2025, 9, 27), D(2025, 12, 1))
    doubled = submissions.model_copy(
        update={"recent_filings": [*submissions.recent_filings, duplicate]}
    )
    with pytest.raises(FinancialDataError) as error:
        resolve_period(fy(2025), doubled, facts)
    assert error.value.problem == "period_ambiguous"


def test_mismatched_snapshots_are_refused():
    submissions, facts = company()
    other = facts.model_copy(update={"cik": JPM})
    with pytest.raises(FinancialDataError) as error:
        resolve_period(fy(2025), submissions, other)
    assert error.value.problem == "snapshot_mismatch"


def test_policies_select_exactly_and_never_mix_filings():
    submissions, facts = company()
    fy24 = resolve_period(fy(2024), submissions, facts)
    latest = get_fact(facts, "revenue", fy24, "latest_filed")
    original = get_fact(facts, "revenue", fy24, "original")
    in_k24 = get_fact(facts, "revenue", fy24, "same_filing", K24)
    in_k25 = get_fact(facts, "revenue", fy24, "same_filing", K25)
    assert (latest.value, latest.observation.accession_number) == (390, K25)
    assert (original.value, original.observation.accession_number) == (391, K24)
    assert (in_k24.value, in_k25.value) == (391, 390)
    assert latest.conflicting_values and [o.value for o in latest.other_values] == [391]
    with pytest.raises(FinancialDataError) as error:  # Q1 does not report fiscal 2024
        get_fact(facts, "revenue", fy24, "same_filing", Q1)
    assert error.value.problem == "observation_not_in_filing"


@pytest.mark.parametrize(
    ("metric", "request_", "policy", "accession", "problem"),
    [
        ("total_assets", fy(2025), "latest_filed", None, "period_kind_mismatch"),
        ("revenue", year_end(2025), "latest_filed", None, "period_kind_mismatch"),
        ("revenue", fy(2025), "same_filing", None, "invalid_request"),
        ("revenue", fy(2025), "latest_filed", K25, "invalid_request"),
        ("revenue", fy(2025), "same_filing", "not-an-accession", "invalid_request"),
        ("net_income", fy(2025), "latest_filed", None, "concept_not_reported"),
    ],
)
def test_invalid_fact_requests_are_refused(metric, request_, policy, accession, problem):
    submissions, facts = company()
    period = resolve_period(request_, submissions, facts)
    with pytest.raises(FinancialDataError) as error:
        get_fact(facts, metric, period, policy, accession)
    assert error.value.problem == problem


def test_same_day_disagreement_is_not_resolved():
    submissions, facts = company()
    rival = obs(
        REV, 417, D(2024, 9, 29), D(2025, 9, 27), "0000000001-25-000555", "10-K/A", D(2025, 10, 31)
    )
    facts = facts.model_copy(update={"observations": [*facts.observations, rival]})
    period = resolve_period(fy(2025), submissions, facts)
    with pytest.raises(FinancialDataError) as error:
        get_fact(facts, "revenue", period)
    assert error.value.problem == "observation_ambiguous"


def test_unmapped_companies_and_metrics_are_refused():
    with pytest.raises(FinancialDataError) as error:
        concept_mapping("0000789019", "revenue")
    assert error.value.problem == "metric_not_mapped"


def test_identities_are_deterministic_and_separate_logic_from_observation():
    submissions, facts = company()
    fy24 = resolve_period(fy(2024), submissions, facts)
    a, b = get_fact(facts, "revenue", fy24), get_fact(facts, "revenue", fy24)
    assert (a.fact_key, a.observation_id) == (b.fact_key, b.observation_id)
    assert a.fact_key.startswith("fact:") and a.observation_id.startswith("obs:")
    original = get_fact(facts, "revenue", fy24, "original")
    assert original.fact_key != a.fact_key  # a different question: the original value
    # Same logical fact from a different snapshot: same key, different observation id.
    other_snapshot = facts.model_copy(update={"source": source("facts-later")})
    fy24_other = resolve_period(fy(2024), submissions, other_snapshot)
    c = get_fact(other_snapshot, "revenue", fy24_other)
    assert c.fact_key == a.fact_key and c.observation_id != a.observation_id
    assert c.snapshot.content_sha256 == hashlib.sha256(b"facts-later").hexdigest()


# --- Pinned snapshots and cached filings ----------------------------------------------------


def _pinned():
    if not MANIFEST.exists():
        return None
    manifest = load_snapshot_manifest(MANIFEST)
    try:
        return manifest, {
            cik: (
                load_submissions_snapshot(CACHE, cik, manifest.pin("submissions", cik).sha256),
                load_company_facts_snapshot(CACHE, cik, manifest.pin("company_facts", cik).sha256),
            )
            for cik in (APPLE, JPM)
        }
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None


PINNED = _pinned()
pinned = pytest.mark.skipif(PINNED is None, reason="pinned SEC snapshots not in the local cache")


def resolve(cik, request_):
    submissions, facts = PINNED[1][cik]
    return resolve_period(request_, submissions, facts), facts


@pinned
@pytest.mark.parametrize(
    ("cik", "request_", "start", "end", "accession"),
    [
        (APPLE, fy(2026), D(2025, 9, 28), D(2026, 9, 26), "0000320193-26-000026"),
        (APPLE, fy(2025), D(2024, 9, 29), D(2025, 9, 27), "0000320193-25-000079"),
        (APPLE, quarter(2026, 3), D(2026, 3, 29), D(2026, 6, 27), "0000320193-26-000020"),
        (APPLE, year_end(2026), None, D(2026, 9, 26), "0000320193-26-000026"),
        (JPM, quarter(2026, 3), D(2026, 7, 1), D(2026, 9, 30), "0001628280-26-069523"),
        (JPM, quarter_end(2026, 3), None, D(2026, 9, 30), "0001628280-26-069523"),
        (JPM, fy(2025), D(2025, 1, 1), D(2025, 12, 31), "0001628280-26-008131"),
    ],
)
def test_pinned_periods_resolve_to_exact_boundaries(cik, request_, start, end, accession):
    period, _ = resolve(cik, request_)
    assert (period.period_start, period.period_end) == (start, end)
    assert period.original_filing.accession_number == accession
    assert period.facts_sha256 == PINNED[0].pin("company_facts", cik).sha256
    assert period.submissions_sha256 == PINNED[0].pin("submissions", cik).sha256


@pinned
@pytest.mark.parametrize(
    ("cik", "request_", "problem"),
    [
        (JPM, quarter(2025, 3), "period_not_found"),  # its 10-Q predates the snapshot's list
        (JPM, fy(2024), "period_not_found"),
        (APPLE, fy(2014), "period_not_found"),
        (APPLE, quarter(2026, 4), "period_not_supported"),
    ],
)
def test_pinned_unavailable_periods_are_refused(cik, request_, problem):
    with pytest.raises(FinancialDataError) as error:
        resolve(cik, request_)
    assert error.value.problem == problem


@functools.cache
def _blocks(filing: FilingRef):
    document = load_filing_document(CACHE, filing)
    assert document is not None, f"{filing.accession_number} is not in the local filing cache"
    return extract_filing(document).blocks


def _statement_value(filing: FilingRef, title: str, line: str) -> Decimal:
    """The first figure on `line` in the first table after the heading `title`, in USD
    (the statements are in millions)."""
    blocks = _blocks(filing)
    heading = next(i for i, b in enumerate(blocks) if b.kind == "heading" and b.text == title)
    table = next(b for b in blocks[heading:] if b.kind == "table")
    row = next(r for r in table.text.splitlines() if r.startswith(line))
    figure = next(c for c in row.split(" | ") if re.fullmatch(r"[\d,]+", c))
    return Decimal(figure.replace(",", "")) * 1_000_000


APPLE_10K_IS, APPLE_10K_BS = "CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED BALANCE SHEETS"
APPLE_10Q_IS = "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)"
APPLE_10Q_BS = "CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)"
JPM_IS, JPM_BS = (
    "Consolidated statements of income (unaudited)",
    "Consolidated balance sheets (unaudited)",
)


@pinned
@pytest.mark.parametrize(
    ("cik", "request_", "metric", "title", "value"),
    [
        (APPLE, fy(2026), "revenue", APPLE_10K_IS, 478_834),
        (APPLE, fy(2026), "net_income", APPLE_10K_IS, 132_175),
        (APPLE, year_end(2026), "total_assets", APPLE_10K_BS, 404_548),
        (APPLE, quarter(2026, 3), "revenue", APPLE_10Q_IS, 109_417),
        (APPLE, quarter(2026, 3), "net_income", APPLE_10Q_IS, 29_789),
        (APPLE, quarter_end(2026, 3), "total_assets", APPLE_10Q_BS, 383_266),
        (JPM, quarter(2026, 3), "revenue", JPM_IS, 50_172),
        (JPM, quarter(2026, 3), "net_income", JPM_IS, 15_305),
        (JPM, quarter_end(2026, 3), "total_assets", JPM_BS, 5_091_545),
    ],
)
def test_each_mapping_matches_the_face_statement_of_the_cached_filing(
    cik, request_, metric, title, value
):
    period, facts = resolve(cik, request_)
    fact = get_fact(facts, metric, period, "same_filing", period.original_filing.accession_number)
    statement = _statement_value(
        period.original_filing, title, CONCEPT_MAPPINGS[(cik, metric)].statement_line
    )
    assert fact.value == statement == value * 1_000_000
    assert fact.observation.accession_number == period.original_filing.accession_number


@pinned
@pytest.mark.parametrize(
    ("cik", "request_", "rejected"),
    [
        (APPLE, fy(2026), "Revenues"),  # last reported by Apple in 2018
        (JPM, quarter(2026, 3), "Revenues"),  # absent from JPMorgan's 10-Qs since 2014
    ],
)
def test_rejected_revenue_concepts_cannot_serve_the_period(cik, request_, rejected):
    period, facts = resolve(cik, request_)
    selection = select_fact(
        facts, "us-gaap", rejected, "USD", period.period_start, period.period_end
    )
    assert selection.selected is None


@pinned
def test_a_wrong_mapping_is_never_rescued_by_substitution(monkeypatch):
    wrong = CONCEPT_MAPPINGS[(JPM, "revenue")].model_copy(update={"concept": "Revenues"})
    monkeypatch.setitem(financial_tools.CONCEPT_MAPPINGS, (JPM, "revenue"), wrong)
    period, facts = resolve(JPM, quarter(2026, 3))
    with pytest.raises(FinancialDataError) as error:
        get_fact(facts, "revenue", period)
    assert error.value.problem == "observation_not_found"


@pinned
def test_jpmorgan_10k_reports_both_revenue_concepts_with_equal_values():
    period, facts = resolve(JPM, fy(2025))
    mapped = get_fact(facts, "revenue", period)
    other = select_fact(facts, "us-gaap", "Revenues", "USD", period.period_start, period.period_end)
    assert mapped.value == other.selected.value == Decimal("182447000000")


@pinned
def test_a_real_restatement_keeps_policies_and_filings_apart():
    # Apple's total assets at the end of FY2015: 290,479M in the FY2015 10-K, 290,345M in
    # the FY2016 10-K.
    period, facts = resolve(APPLE, year_end(2015))
    original_10k, later_10k = "0001193125-15-356351", "0001628280-16-020309"
    assert period.original_filing.accession_number == original_10k
    latest = get_fact(facts, "total_assets", period, "latest_filed")
    original = get_fact(facts, "total_assets", period, "original")
    as_originally_filed = get_fact(facts, "total_assets", period, "same_filing", original_10k)
    assert (latest.value, latest.observation.accession_number) == (
        Decimal("290345000000"),
        later_10k,
    )
    assert (original.value, original.observation.accession_number) == (
        Decimal("290479000000"),
        original_10k,
    )
    assert as_originally_filed.value == original.value
    assert latest.conflicting_values and latest.other_values[0].accession_number == original_10k


@pinned
def test_source_filing_and_original_filing_are_distinct_roles():
    period, facts = resolve(APPLE, fy(2025))
    latest = get_fact(facts, "revenue", period)  # the FY2026 10-K repeats FY2025 last
    assert period.original_filing.accession_number == "0000320193-25-000079"
    assert latest.observation.accession_number == "0000320193-26-000026"
    in_original = get_fact(facts, "revenue", period, "same_filing", "0000320193-25-000079")
    assert in_original.value == latest.value == Decimal("416161000000")
    assert in_original.observation_id != latest.observation_id  # different source filing
    with pytest.raises(FinancialDataError) as error:  # the Q3 10-Q never reports FY2025
        get_fact(facts, "revenue", period, "same_filing", "0000320193-26-000020")
    assert error.value.problem == "observation_not_in_filing"


@pinned
def test_pinned_fact_provenance_names_the_snapshot():
    period, facts = resolve(JPM, quarter(2026, 3))
    fact = get_fact(facts, "net_income", period)
    assert fact.snapshot.content_sha256 == PINNED[0].pin("company_facts", JPM).sha256
    assert (fact.concept, fact.unit, fact.mapping_version) == ("NetIncomeLoss", "USD", "1")
    again = get_fact(
        resolve(JPM, quarter(2026, 3))[1], "net_income", resolve(JPM, quarter(2026, 3))[0]
    )
    assert (again.fact_key, again.observation_id) == (fact.fact_key, fact.observation_id)
    assert period.period_end - period.period_start == timedelta(days=91)

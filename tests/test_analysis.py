"""Evidence-backed analysis over the pinned snapshots and cached filings. Offline, sockets
blocked. Corruption tests copy one cached filing into a temporary directory; the real cache
is only read.

Pinned tests are skipped when the local (git-ignored) cache is not present.
"""

import shutil
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financial_analyst import analysis
from financial_analyst.analysis import (
    PinnedData,
    default_query,
    explain_change,
    format_percent,
    load_pinned_data,
    main,
    parse_period,
)
from financial_analyst.config import Settings
from financial_analyst.financial_tools import PeriodRequest, compare_periods, percent_change
from financial_analyst.passages import parse_citation_id

APPLE, JPM = "0000320193", "0000019617"
APPLE_10K, APPLE_10Q = "0000320193-26-000026", "0000320193-26-000020"
JPM_10Q = "0001628280-26-069523"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
MANIFEST = Path(__file__).parent.parent / "evals" / "sec_snapshots_v1.json"
FILINGS = {
    APPLE_10K: CACHE / "filings" / APPLE / "000032019326000026" / "aapl-20260926.htm",
    APPLE_10Q: CACHE / "filings" / APPLE / "000032019326000020" / "aapl-20260627.htm",
    JPM_10Q: CACHE / "filings" / JPM / "000162828026069523" / "jpm-20260930.htm",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def fy(year):
    return PeriodRequest(kind="fiscal_year", fiscal_year=year)


def quarter(year, q):
    return PeriodRequest(kind="fiscal_quarter", fiscal_year=year, quarter=q)


def _pinned() -> PinnedData | None:
    try:
        data = load_pinned_data(MANIFEST, CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if all(path.exists() for path in FILINGS.values()) else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


def apple_revenue(cache_dir=CACHE, query=None, data=None, k=5):
    return explain_change("AAPL", "revenue", fy(2026), fy(2025), query,
                          data=data or DATA, cache_dir=cache_dir, k=k)  # fmt: skip


@pytest.fixture(scope="module")
def apple():
    return apple_revenue()


@pytest.fixture(scope="module")
def jpm():
    return explain_change(
        "JPM", "net_income", quarter(2026, 3), quarter(2025, 3), data=DATA, cache_dir=CACHE
    )


def copy_filing(accession: str, cache_dir: Path, into: str | None = None) -> Path:
    """Copy one cached filing (bytes and metadata) into cache_dir, optionally into another
    filing's slot. Returns the copied document path."""
    source = FILINGS[accession]
    target = cache_dir / FILINGS[into or accession].relative_to(CACHE)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    shutil.copyfile(f"{source}.json", f"{target}.json")
    return target


def cited(result) -> list:
    ids = [e.citation_id for e in result.evidence] + [r.citation_id for r in result.rejected]
    return [parse_citation_id(c) for c in ids]


# --- The two required examples -------------------------------------------------------------


@pinned
def test_apple_fy2026_revenue_is_complete_with_md_and_a_candidates(apple):
    assert apple.status == "complete" and apple.company.cik == APPLE
    change = apple.comparison.change
    assert (change.current, change.prior) == (Decimal("478834000000"), Decimal("416161000000"))
    assert change.delta == Decimal("62673000000") and change.percent.display == "15.1%"
    assert apple.comparison.commentary_filing.accession_number == APPLE_10K
    assert apple.retrieval.section == "Item 7" and apple.retrieval.query_source == "default"
    assert apple.retrieval.query == "net sales increased decreased compared"
    assert apple.evidence and all(
        e.accession_number == APPLE_10K and e.section == "Item 7" and e.support == "candidate"
        and e.verification_status == "verified" and "net sales" in e.text.lower()
        and e.source_sha256 == apple.retrieval.document_sha256
        for e in apple.evidence
    )  # fmt: skip
    assert "iPhone net sales increased during 2026" in apple.evidence[0].text
    assert apple.summary == (
        "Apple Inc. total net sales (us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
        ", USD): FY2026 (2025-09-28 to 2026-09-26) 478,834,000,000 vs FY2025 (2024-09-29 to "
        "2025-09-27) 416,161,000,000; change +62,673,000,000 USD (+15.1%). Both values are "
        f"from 10-K {APPLE_10K} filed 2026-11-03."
    )
    assert apple.reasons == []


@pinned
def test_jpmorgan_q3_net_income_is_complete_from_the_whole_10q(jpm):
    assert jpm.status == "complete" and jpm.company.cik == JPM
    change = jpm.comparison.change
    assert (change.current, change.prior) == (Decimal("15305000000"), Decimal("14393000000"))
    assert change.delta == Decimal("912000000") and change.percent.display == "6.3%"
    prior = jpm.comparison.prior.period
    assert prior.basis == "comparative_in_filing" and prior.original_filing is None
    assert "original reporting filing is not in the pinned submissions" in jpm.summary
    # The 10-Q has no reliable section labels, so the whole verified filing is searched.
    assert jpm.retrieval.section is None and "whole filing" in jpm.retrieval.section_note
    assert jpm.retrieval.passages_searched > 700
    assert all(e.accession_number == JPM_10Q and e.section is None for e in jpm.evidence)
    assert "reported net income of $15.3 billion, up 6%" in jpm.evidence[0].text


@pinned
def test_the_numbers_are_exactly_compare_periods(apple, jpm):
    for result, cik, metric, current, prior in [
        (apple, APPLE, "revenue", fy(2026), fy(2025)),
        (jpm, JPM, "net_income", quarter(2026, 3), quarter(2025, 3)),
    ]:
        assert result.comparison == compare_periods(*DATA.companies[cik], metric, current, prior)


@pinned
def test_retrieved_passages_that_do_not_mention_the_metric_are_rejected(apple, jpm):
    for result, phrase in [(apple, "net sales"), (jpm, "net income")]:
        assert result.rejected  # both examples retrieve some passages without the phrase
        for item in result.rejected:
            assert item.reason == "does_not_mention_metric"
            assert item.verification_status == "verified"  # valid, but not about the metric
        assert all(phrase in " ".join(e.text.lower().split()) for e in result.evidence)
        ranks = [e.rank for e in result.evidence] + [r.rank for r in result.rejected]
        assert sorted(ranks) == list(range(1, result.retrieval.k + 1))


# --- Scoping -------------------------------------------------------------------------------


@pinned
def test_only_the_commentary_filing_is_searched(apple, jpm):
    # The cache holds three filings of two companies; each result cites only its own.
    for result, cik, accession in [(apple, APPLE, APPLE_10K), (jpm, JPM, JPM_10Q)]:
        assert {(c.cik, c.accession_number) for c in cited(result)} == {(cik, accession)}


@pinned
def test_a_quarterly_comparison_searches_its_10q_md_and_a_not_the_10k():
    result = explain_change("AAPL", "revenue", quarter(2026, 3), quarter(2025, 3),
                            data=DATA, cache_dir=CACHE)  # fmt: skip
    assert result.comparison.commentary_filing.accession_number == APPLE_10Q
    assert result.retrieval.section == "Part I, Item 2"
    assert {c.accession_number for c in cited(result)} == {APPLE_10Q}
    assert all(e.section == "Part I, Item 2" for e in result.evidence)


@pinned
@pytest.mark.parametrize("impostor", [APPLE_10Q, JPM_10Q])  # wrong accession, wrong company
def test_another_filing_in_the_commentary_filings_slot_is_never_used(tmp_path, impostor):
    copy_filing(impostor, tmp_path, into=APPLE_10K)
    result = apple_revenue(cache_dir=tmp_path)
    assert result.status == "partial" and result.evidence == [] and result.retrieval is None
    assert "not in the filing cache or failed its integrity checks" in result.reasons[0]


# --- Citation verification and missing sources ----------------------------------------------


@pinned
def test_a_missing_cached_filing_gives_a_partial_result(tmp_path):
    result = apple_revenue(cache_dir=tmp_path)
    assert result.status == "partial" and result.comparison is not None
    assert result.summary.startswith("Apple Inc. total net sales")
    assert result.evidence == [] and result.retrieval is None


@pinned
def test_corrupted_source_bytes_are_never_searched(tmp_path):
    document = copy_filing(APPLE_10K, tmp_path)
    document.write_bytes(document.read_bytes().replace(b"increased", b"decreased", 1))
    result = apple_revenue(cache_dir=tmp_path)
    assert result.status == "partial" and result.retrieval is None


@pinned
def test_a_source_corrupted_before_verification_is_rejected(tmp_path, monkeypatch):
    document = copy_filing(APPLE_10K, tmp_path)
    real_verify = analysis.verify_citations

    def corrupt_then_verify(pairs, cache_dir):
        document.write_bytes(document.read_bytes()[:-1])
        return real_verify(pairs, cache_dir)

    monkeypatch.setattr(analysis, "verify_citations", corrupt_then_verify)
    result = apple_revenue(cache_dir=tmp_path)
    assert result.status == "partial" and result.evidence == []
    assert {(r.reason, r.verification_status) for r in result.rejected} == {
        ("citation_not_verified", "source_unavailable")
    }
    assert "5 citation_not_verified" in result.reasons[0]


@pinned
def test_altered_passage_text_fails_verification(monkeypatch):
    real_build = analysis.build_passages

    def tampered(extracted):
        return [p.model_copy(update={"text": p.text + " (altered)"})
                for p in real_build(extracted)]  # fmt: skip

    monkeypatch.setattr(analysis, "build_passages", tampered)
    result = apple_revenue()
    assert result.status == "partial" and result.evidence == []
    statuses = {r.verification_status for r in result.rejected}
    assert statuses == {"text_changed"}  # the cited text is not what the filing says


@pinned
def test_evidence_keeps_full_citation_provenance(apple):
    for item in apple.evidence:
        citation = parse_citation_id(item.citation_id)
        assert citation.accession_number == item.accession_number
        assert citation.content_sha256 == item.source_sha256
        assert item.text_sha256.startswith(citation.text_sha256_prefix)
        assert item.document_url.endswith("/000032019326000026/aapl-20260926.htm")


# --- Partial and abstained ---------------------------------------------------------------


@pinned
@pytest.mark.parametrize("query", ["the of and", "zzqxv"])
def test_empty_retrieval_gives_a_partial_result(query):
    result = apple_revenue(query=query)
    assert result.status == "partial" and result.evidence == [] and result.rejected == []
    assert result.retrieval.query_source == "explicit"
    assert result.reasons == [f"no passage in 10-K {APPLE_10K} matched the query"]


@pinned
def test_retrieval_without_a_metric_mention_gives_a_partial_result():
    result = apple_revenue(query="research development headcount", k=1)
    assert result.status == "partial" and result.evidence == []
    assert [r.reason for r in result.rejected] == ["does_not_mention_metric"]


@pinned
def test_missing_financial_observations_abstain():
    submissions, facts = DATA.companies[APPLE]
    # Drop only the 10-K's FY2025 revenue row: the periods still resolve, the value does not.
    concept = "RevenueFromContractWithCustomerExcludingAssessedTax"
    kept = [
        o for o in facts.observations
        if not (o.accession_number == APPLE_10K and o.concept == concept
                and o.period_end.year == 2025)
    ]  # fmt: skip
    facts = facts.model_copy(update={"observations": kept})
    data = DATA.model_copy(update={"companies": {APPLE: (submissions, facts)}})
    result = apple_revenue(data=data)
    assert result.status == "abstained" and result.company.cik == APPLE
    assert (result.comparison, result.summary, result.retrieval) == (None, None, None)
    assert result.reasons[0].startswith("comparison not established (observation_not_in_filing)")


@pinned
@pytest.mark.parametrize(
    ("identifier", "prior", "reason"),
    [
        ("Microsoft", quarter(2025, 3), "company not resolved (company_not_found)"),
        ("JPM", quarter(2025, 2), "comparison not established (period_not_found)"),
    ],
)
def test_unestablished_comparisons_abstain(identifier, prior, reason):
    result = explain_change(identifier, "net_income", quarter(2026, 3), prior,
                            data=DATA, cache_dir=CACHE)  # fmt: skip
    assert result.status == "abstained" and result.reasons[0].startswith(reason)
    assert result.evidence == [] and result.comparison is None


@pinned
def test_a_company_without_pinned_data_abstains():
    data = DATA.model_copy(update={"companies": {APPLE: DATA.companies[APPLE]}})
    result = explain_change("JPM", "net_income", quarter(2026, 3), quarter(2025, 3),
                            data=data, cache_dir=CACHE)  # fmt: skip
    assert result.status == "abstained" and result.company.cik == JPM
    assert "no pinned submissions" in result.reasons[0]


@pinned
def test_an_unavailable_percentage_is_explained_not_formatted(apple):
    undefined = apple.comparison.change.model_copy(
        update={"percent": percent_change(Decimal(5), Decimal(0))}
    )
    comparison = apple.comparison.model_copy(update={"change": undefined})
    assert format_percent(comparison) == "percentage change undefined: the prior value is zero"


@pinned
def test_repeated_runs_are_identical(apple):
    assert apple_revenue().model_dump() == apple.model_dump()


@pinned
def test_malformed_arguments_are_rejected():
    for kwargs in ({"query": "  "}, {"query": "x" * 501}, {"k": 0}):
        with pytest.raises(ValueError):
            apple_revenue(**kwargs)


def test_default_queries_use_each_companys_terms():
    assert default_query(APPLE, "revenue") == "net sales increased decreased compared"
    assert default_query(JPM, "net_income") == "net income firmwide reported up down"


# --- CLI ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FY2026", fy(2026)),
        ("fy2026q3", quarter(2026, 3)),
        ("FY2026-end", PeriodRequest(kind="fiscal_year_end", fiscal_year=2026)),
        ("FY2026Q3-END", PeriodRequest(kind="fiscal_quarter_end", fiscal_year=2026, quarter=3)),
    ],
)
def test_periods_parse(text, expected):
    assert parse_period(text) == expected


@pytest.mark.parametrize("text", ["2026", "FY26", "FY2026Q5", "Q3 2026"])
def test_malformed_periods_are_rejected(text):
    with pytest.raises(ValueError):
        parse_period(text)


@pinned
def test_cli_prints_both_examples_compactly(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    for expected in [
        "== AAPL revenue: COMPLETE", "Current:  FY2026 (2025-09-28 to 2026-09-26): 478,834,000,000",
        "Change:   +62,673,000,000 (+15.1%", f"10-K {APPLE_10K}",
        "== JPM net_income: COMPLETE", "Prior:    Q3 FY2025", "Change:   +912,000,000 (+6.3%",
        f"10-Q {JPM_10Q}", "[verified]", f"sec-psg:{JPM}:{JPM_10Q}:",
    ]:  # fmt: skip
        assert expected in out
    assert len(out) < 12_000 and "{" not in out  # previews only, no raw JSON


@pinned
@pytest.mark.parametrize(
    ("argv", "code"),
    [(["Microsoft", "revenue", "FY2026", "FY2025"], 1), (["AAPL", "revenue", "2026", "FY2025"], 2),
     (["AAPL", "ebitda", "FY2026", "FY2025"], 2)],
)  # fmt: skip
def test_cli_reports_abstentions_and_bad_input(capsys, argv, code):
    assert main(argv) == code

"""Deterministic financial tools (M3): company identity, fiscal periods, concept mapping and
fact selection, all from pinned snapshots and with no network access.

Identities resolve only against pinned, already-loaded snapshots, so resolution is offline
and reproducible: the company_tickers.json snapshot (a TickerMap), and for a CIK that map
does not list, optionally a verified submissions snapshot for that CIK. Matching is exact:
a CIK, a ticker compared case-insensitively, or one of a few fixed aliases for the
supported companies. There is no fuzzy, prefix or substring matching, and an identifier
that could mean more than one company is rejected rather than guessed (D-030).
"""

import hashlib
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from financial_analyst.company_facts import (
    ELIGIBLE_FORMS,
    PeriodClass,
    find_concept,
    period_class,
    select_fact,
)
from financial_analyst.sec_client import normalize_cik, snapshot_url
from financial_analyst.sec_models import (
    CompanyFacts,
    CompanySubmissions,
    FactObservation,
    FilingRef,
    SourceMetadata,
    TickerEntry,
    TickerMap,
)

MAX_IDENTIFIER_LENGTH = 64

# Exact aliases for the supported companies. Matched after case-folding and collapsing
# whitespace only, so "JPMorgan  Chase" matches but "JP Morgan" and "Apple Computer" do not.
COMPANY_ALIASES: dict[str, str] = {
    "apple": "0000320193",
    "apple inc.": "0000320193",
    "jpmorgan": "0000019617",
    "jpmorgan chase": "0000019617",
    "jpmorgan chase & co.": "0000019617",
}

IdentityProblem = Literal["invalid_identifier", "company_not_found", "company_ambiguous"]
MatchedBy = Literal["cik", "ticker", "alias"]
VerifiedBy = Literal["company_tickers", "submissions"]


class IdentityError(Exception):
    """The identifier does not name exactly one company in the pinned ticker snapshot."""

    def __init__(self, problem: IdentityProblem, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.problem = problem
        self.candidates = candidates  # CIKs that matched; non-empty only when ambiguous


class CompanyIdentity(BaseModel):
    """One resolved company, with how it was matched and the snapshot it came from."""

    model_config = ConfigDict(frozen=True)

    cik: str
    name: str  # SEC's title in the ticker snapshot, or the company name in submissions
    tickers: list[str]  # every ticker the verifying snapshot lists for this CIK, sorted
    matched_by: MatchedBy
    verified_by: VerifiedBy  # which snapshot confirmed the company exists
    source: SourceMetadata  # that snapshot's provenance (URL, retrieved_at, SHA-256)


def resolve_company(
    identifier: str, tickers: TickerMap, submissions: CompanySubmissions | None = None
) -> CompanyIdentity:
    """Resolve a CIK, ticker or exact alias to one company. Raises IdentityError.

    - All digits: a CIK (normalized to 10 digits). It is verified by the ticker map when
      listed there. Otherwise it is verified by `submissions`, if given: a submissions
      snapshot the caller already loaded and verified (load_submissions_snapshot) for that
      exact CIK. Nothing is ever fetched. Unverified by either: company_not_found.
    - Otherwise: a ticker (exact, case-insensitive) and/or an alias (exact after
      case-folding and collapsing whitespace), against the ticker map only. Every match is
      collected, and an identifier matching more than one CIK is ambiguous.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise IdentityError("invalid_identifier", "the identifier is empty", [])
    text = identifier.strip()
    if len(text) > MAX_IDENTIFIER_LENGTH:
        raise IdentityError(
            "invalid_identifier", f"identifiers are at most {MAX_IDENTIFIER_LENGTH} characters", []
        )

    by_cik: dict[str, list[TickerEntry]] = {}
    for entry in tickers.entries:
        by_cik.setdefault(entry.cik, []).append(entry)

    if text.isascii() and text.isdigit():
        try:
            cik = normalize_cik(text)
        except ValueError as exc:
            raise IdentityError("invalid_identifier", str(exc), []) from exc
        if cik in by_cik:
            return _identity(cik, by_cik[cik], "cik", tickers)
        if _verifies(submissions, cik):
            return CompanyIdentity(
                cik=cik,
                name=submissions.name,
                tickers=sorted(set(submissions.tickers)),
                matched_by="cik",
                verified_by="submissions",
                source=submissions.source,
            )
        raise IdentityError(
            "company_not_found",
            f"CIK {cik} is not in the ticker snapshot or a submissions snapshot for that CIK",
            [],
        )

    ticker_ciks = {e.cik for e in tickers.entries if e.ticker.casefold() == text.casefold()}
    alias_cik = COMPANY_ALIASES.get(" ".join(text.split()).casefold())
    candidates = sorted(ticker_ciks | ({alias_cik} if alias_cik else set()))
    if len(candidates) > 1:
        raise IdentityError(
            "company_ambiguous", f"{text!r} matches {len(candidates)} companies", candidates
        )
    if not candidates or candidates[0] not in by_cik:
        raise IdentityError("company_not_found", f"no company matches {text!r} exactly", [])
    cik = candidates[0]
    return _identity(cik, by_cik[cik], "ticker" if ticker_ciks else "alias", tickers)


def _identity(
    cik: str, entries: list[TickerEntry], matched_by: MatchedBy, tickers: TickerMap
) -> CompanyIdentity:
    # A CIK with several tickers (share classes) normally has one title; if SEC lists more
    # than one, the first in sorted order is used so the result never depends on row order.
    return CompanyIdentity(
        cik=cik,
        name=sorted(e.title for e in entries)[0],
        tickers=sorted({e.ticker for e in entries}),
        matched_by=matched_by,
        verified_by="company_tickers",
        source=tickers.source,
    )


def _verifies(submissions: CompanySubmissions | None, cik: str) -> bool:
    """A submissions snapshot verifies a CIK only if it is that CIK's own response."""
    return (
        submissions is not None
        and submissions.cik == cik
        and str(submissions.source.source_url) == snapshot_url("submissions", cik)
    )


# --- Exact XBRL concept mapping (M3.3, D-032) -------------------------------------------

MAPPING_VERSION = "1"
Metric = Literal["revenue", "net_income", "total_assets"]


class ConceptMapping(BaseModel):
    """The one XBRL concept a company's metric is read from. There are no fallbacks."""

    model_config = ConfigDict(frozen=True)

    cik: str
    metric: Metric
    taxonomy: str
    concept: str
    unit: str
    instant: bool  # True: a balance at a period end; False: a fiscal year or quarter
    statement_line: str  # the company's own label on its face financial statement
    rationale: str


def _usd(cik: str, metric: Metric, concept: str, line: str, rationale: str) -> ConceptMapping:
    return ConceptMapping(
        cik=cik,
        metric=metric,
        taxonomy="us-gaap",
        concept=concept,
        unit="USD",
        instant=metric == "total_assets",
        statement_line=line,
        rationale=rationale,
    )


APPLE, JPMORGAN = "0000320193", "0000019617"
# Each mapping was checked against the face financial statements of the cached filings and
# against the pinned Company Facts snapshot (D-032, architecture §3.2j).
_MAPPINGS = (
    _usd(APPLE, "revenue", "RevenueFromContractWithCustomerExcludingAssessedTax",
         "Total net sales",
         "Apple's 'Total net sales' in every 10-K and 10-Q since fiscal 2018. "
         "us-gaap:Revenues was last reported in 2018 and is not used."),
    _usd(APPLE, "net_income", "NetIncomeLoss", "Net income",
         "Apple's 'Net income' on the statement of operations."),
    _usd(APPLE, "total_assets", "Assets", "Total assets",
         "Apple's 'Total assets' on the balance sheet."),
    _usd(JPMORGAN, "revenue", "RevenuesNetOfInterestExpense", "Total net revenue",
         "JPMorgan's 'Total net revenue': net of interest expense, so not comparable to an "
         "industrial company's revenue. Reported in every 10-K and 10-Q since 2015. "
         "us-gaap:Revenues carries the same values in 10-Ks but has not appeared in a 10-Q "
         "since 2014, so it cannot serve quarters and is not used."),
    _usd(JPMORGAN, "net_income", "NetIncomeLoss", "Net income",
         "JPMorgan's 'Net income' on the consolidated statement of income."),
    _usd(JPMORGAN, "total_assets", "Assets", "Total assets",
         "JPMorgan's 'Total assets' on the consolidated balance sheet."),
)  # fmt: skip
CONCEPT_MAPPINGS: dict[tuple[str, Metric], ConceptMapping] = {
    (m.cik, m.metric): m for m in _MAPPINGS
}


def concept_mapping(cik: str, metric: Metric) -> ConceptMapping:
    """The mapping for one company and metric. Raises FinancialDataError if unmapped."""
    mapping = CONCEPT_MAPPINGS.get((normalize_cik(cik), metric))
    if mapping is None:
        raise FinancialDataError(
            "metric_not_mapped", f"no {metric} mapping for CIK {normalize_cik(cik)}"
        )
    return mapping


# --- Financial period resolution (M3.3, D-031, D-032) -----------------------------------

PeriodKind = Literal["fiscal_year", "fiscal_quarter", "fiscal_year_end", "fiscal_quarter_end"]
FinancialProblem = Literal[
    "snapshot_mismatch",
    "metric_not_mapped",
    "period_not_supported",
    "period_not_found",
    "period_ambiguous",
    "period_kind_mismatch",
    "concept_not_reported",
    "observation_not_found",
    "observation_ambiguous",
    "observation_not_in_filing",
    "period_class_mismatch",
    "unit_mismatch",
    "policy_not_supported",
    "invalid_request",
]
FYE_TOLERANCE_DAYS = 7  # 52/53-week fiscal years end within a week of the nominal date
_QUARTER_SEARCH_DAYS = 371  # a 53-week year, when the fiscal year's own end is not known


class FinancialDataError(Exception):
    """The requested period or fact cannot be determined exactly from the pinned data."""

    def __init__(self, problem: FinancialProblem, message: str) -> None:
        super().__init__(message)
        self.problem = problem


class PeriodRequest(BaseModel):
    """A fiscal period as a person names it, e.g. Apple fiscal 2026 Q3."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PeriodKind
    fiscal_year: int = Field(ge=1990, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)  # Q4 is refused as unsupported

    @model_validator(mode="after")
    def _check_quarter(self) -> "PeriodRequest":
        if self.kind.startswith("fiscal_quarter") != (self.quarter is not None):
            raise ValueError("quarter is required for quarter kinds and not allowed otherwise")
        return self


PeriodBasis = Literal["original_filing", "comparative_in_filing"]


class ResolvedPeriod(BaseModel):
    """An exact economic period and the filings it is tied to.

    - period_start / period_end: the requested economic period (period_start is None for
      an instant).
    - basis "original_filing": the boundaries come from the original reporting filing (the
      10-K or 10-Q whose report date is period_end, listed in the submissions snapshot).
    - basis "comparative_in_filing": the original filing is not in the snapshot; the
      boundaries come from a later filing's own prior-year comparative observations, for a
      year-over-year comparison only. original_filing is then None: it was not verified.
    - commentary_filing: the filing whose MD&A discusses this period: the original filing,
      or for a comparative, the later filing that reports it.
    The observation source filing belongs to each selected fact (Fact.observation).
    """

    model_config = ConfigDict(frozen=True)

    cik: str
    request: PeriodRequest
    period_start: date | None
    period_end: date
    period_class: PeriodClass  # "12_months", "3_months" or "instant"
    label: str  # e.g. "FY2026 (2025-09-28 to 2026-09-26)"
    fiscal_year_start: date | None  # None when no listed filing establishes it
    basis: PeriodBasis = "original_filing"
    original_filing: FilingRef | None  # None only for basis "comparative_in_filing"
    commentary_filing: FilingRef
    checks: list[str]  # the consistency checks made, or why one was not possible
    submissions_sha256: str
    facts_sha256: str


def resolve_period(
    request: PeriodRequest, submissions: CompanySubmissions, facts: CompanyFacts
) -> ResolvedPeriod:
    """Resolve a fiscal period to exact dates and its original reporting filing.

    - Fiscal year N: the 10-K whose report date falls in calendar year N (the labeling rule
      of the supported companies). It ends on that report date; it starts on the single
      start date of that 10-K's own 12-month observations ending then. The start must be
      the day after the prior 10-K's report date when that 10-K is listed.
    - Quarter q of fiscal year N: the q-th 10-Q after the fiscal year start. Its 3-month
      observations ending on its report date must start exactly where the previous quarter
      ended (Q1 at the fiscal year start). Q4 is never derived.
    - Instants: the period end of the fiscal year or quarter, same filing.
    SEC's fy/fp tags are never used; fiscal_year_end is a consistency check only; filings
    missing from the submissions snapshot are never inferred. Raises FinancialDataError.
    """
    if submissions.cik != facts.cik:
        raise FinancialDataError(
            "snapshot_mismatch", f"submissions are for {submissions.cik}, facts for {facts.cik}"
        )
    if request.quarter == 4:
        raise FinancialDataError(
            "period_not_supported", "Q4 is not reported directly and is never derived"
        )
    checks: list[str] = []
    year = request.fiscal_year
    ten_k = _annual_filing(submissions, year)
    if request.kind in ("fiscal_year", "fiscal_year_end"):
        if ten_k is None:
            raise FinancialDataError(
                "period_not_found", f"no 10-K for fiscal {year} in the submissions snapshot"
            )
        fy_start = _fiscal_year_start(submissions, facts, year, ten_k, checks)
        _check_fiscal_year_end(ten_k.report_date, submissions.fiscal_year_end, checks)
        if request.kind == "fiscal_year":
            return _resolved(request, submissions, facts, fy_start, ten_k, f"FY{year}",
                             "12_months", fy_start, checks)  # fmt: skip
        return _resolved(request, submissions, facts, None, ten_k, f"end of FY{year}",
                         "instant", fy_start, checks)  # fmt: skip

    if ten_k is not None:
        fy_start = _fiscal_year_start(submissions, facts, year, ten_k, checks)
        search_end = ten_k.report_date
    else:
        prior = _annual_filing(submissions, year - 1)
        if prior is None:
            raise FinancialDataError(
                "period_not_found",
                f"neither the fiscal {year} nor the fiscal {year - 1} 10-K is in the "
                f"submissions snapshot, so fiscal {year} cannot be located",
            )
        fy_start = prior.report_date + timedelta(days=1)
        search_end = fy_start + timedelta(days=_QUARTER_SEARCH_DAYS)
        checks.append(f"FY{year} starts the day after FY{year - 1} ended ({prior.report_date})")
    quarters = sorted(
        (f for f in submissions.recent_filings
         if f.form == "10-Q" and f.report_date is not None
         and fy_start <= f.report_date < search_end),
        key=lambda f: f.report_date,
    )  # fmt: skip
    if len({f.report_date for f in quarters}) != len(quarters) or len(quarters) > 3:
        raise FinancialDataError(
            "period_ambiguous",
            f"FY{year} has {len(quarters)} 10-Qs, some sharing a report date or more than three",
        )
    if request.quarter > len(quarters):
        raise FinancialDataError(
            "period_not_found",
            f"the submissions snapshot lists {len(quarters)} 10-Q(s) for fiscal {year}; "
            f"Q{request.quarter} is not available",
        )
    expected = fy_start
    for number, filing in enumerate(quarters[: request.quarter], start=1):
        start = _own_period_start(facts, filing, "3_months")
        if start != expected:
            raise FinancialDataError(
                "period_ambiguous",
                f"Q{number} FY{year} ({filing.accession_number}) starts {start}, not {expected}: "
                "the quarters do not chain from the fiscal year start",
            )
        expected = filing.report_date + timedelta(days=1)
    checks.append(f"Q1 to Q{request.quarter} chain from {fy_start} without gaps")
    filing = quarters[request.quarter - 1]
    label = f"Q{request.quarter} FY{year}"
    if request.kind == "fiscal_quarter":
        start = _own_period_start(facts, filing, "3_months")
        return _resolved(request, submissions, facts, start, filing, label, "3_months",
                         fy_start, checks)  # fmt: skip
    return _resolved(request, submissions, facts, None, filing, f"end of {label}", "instant",
                     fy_start, checks)  # fmt: skip


def _annual_filing(submissions: CompanySubmissions, year: int) -> FilingRef | None:
    """The original 10-K (not an amendment) for fiscal year `year`, or None."""
    filings = [
        f
        for f in submissions.recent_filings
        if f.form == "10-K" and f.report_date is not None and f.report_date.year == year
    ]
    if len(filings) > 1:
        raise FinancialDataError("period_ambiguous", f"{len(filings)} 10-Ks for fiscal {year}")
    return filings[0] if filings else None


def _fiscal_year_start(submissions, facts, year: int, ten_k: FilingRef, checks) -> date:
    start = _own_period_start(facts, ten_k, "12_months")
    prior = _annual_filing(submissions, year - 1)
    if prior is None:
        checks.append(
            f"the FY{year - 1} 10-K is not in the submissions snapshot; FY{year}'s start comes "
            f"only from the FY{year} 10-K's own observations"
        )
    elif start != prior.report_date + timedelta(days=1):
        raise FinancialDataError(
            "period_ambiguous",
            f"FY{year} starts {start} in its 10-K, but FY{year - 1} ended {prior.report_date}",
        )
    else:
        checks.append(f"FY{year} starts the day after FY{year - 1} ended ({prior.report_date})")
    return start


def _own_period_start(facts: CompanyFacts, filing: FilingRef, expected: PeriodClass) -> date:
    """The single start of a filing's own current-period observations: those it reports
    with period_end equal to its report date and the expected duration."""
    starts = {
        obs.period_start
        for obs in facts.observations
        if obs.accession_number == filing.accession_number
        and obs.period_end == filing.report_date
        and period_class(obs) == expected
    }
    if len(starts) != 1:
        raise FinancialDataError(
            "period_not_found" if not starts else "period_ambiguous",
            f"{filing.form} {filing.accession_number} reports {len(starts)} distinct "
            f"{expected} period starts ending {filing.report_date} in Company Facts",
        )
    return starts.pop()


def _check_fiscal_year_end(report_date: date, fiscal_year_end: str | None, checks) -> None:
    """The company's fiscal_year_end (MMDD) is a consistency check, never the authority."""
    if fiscal_year_end is None:
        checks.append("the submissions give no fiscal_year_end to check against")
        return
    nominal = date(report_date.year, int(fiscal_year_end[:2]), int(fiscal_year_end[2:]))
    gap = abs((report_date - nominal).days)
    if gap > FYE_TOLERANCE_DAYS:
        raise FinancialDataError(
            "period_ambiguous",
            f"the period end {report_date} is {gap} days from fiscal_year_end {fiscal_year_end}",
        )
    checks.append(f"{report_date} is {gap} day(s) from fiscal_year_end {fiscal_year_end}")


def _resolved(request, submissions, facts, start, filing, label, period_class_, fy_start, checks):
    end = filing.report_date
    return ResolvedPeriod(
        cik=submissions.cik,
        request=request,
        period_start=start,
        period_end=end,
        period_class=period_class_,
        label=f"{label} ({start} to {end})" if start else f"{label} ({end})",
        fiscal_year_start=fy_start,
        basis="original_filing",
        original_filing=filing,
        commentary_filing=filing,
        checks=list(checks),
        submissions_sha256=submissions.source.content_sha256,
        facts_sha256=facts.source.content_sha256,
    )


# --- Fact selection (M3.3, D-031, D-032) --------------------------------------------------

SelectionPolicy = Literal["latest_filed", "original", "same_filing"]
_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")


class OtherValue(BaseModel):
    """A different value another eligible filing reported for the same exact period."""

    model_config = ConfigDict(frozen=True)

    value: Decimal
    accession_number: str
    form: str
    filed: date


class Fact(BaseModel):
    """One selected observation, with everything needed to trace and reproduce it."""

    model_config = ConfigDict(frozen=True)

    cik: str
    metric: Metric
    mapping_version: str
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    period: ResolvedPeriod
    policy: SelectionPolicy
    required_accession: str | None  # same_filing only: the filing the value must come from
    value: Decimal
    observation: FactObservation  # the selected row, unchanged: accession, form, filed
    conflicting_values: bool  # eligible filings report different values for this period
    other_values: list[OtherValue]  # those different values, oldest first
    selection_reason: str
    snapshot: SourceMetadata  # the pinned Company Facts snapshot (full SHA-256)

    @computed_field
    @property
    def fact_key(self) -> str:
        """Logical identity: which number is meant. Stable across snapshots."""
        parts = (self.cik, self.metric, self.mapping_version, self.taxonomy, self.concept,
                 self.unit, self.period_start, self.period_end, self.policy,
                 self.required_accession)  # fmt: skip
        return "fact:" + _digest(parts)

    @computed_field
    @property
    def observation_id(self) -> str:
        """Immutable identity: exactly which value, from which filing and snapshot."""
        obs = self.observation
        parts = (self.fact_key, self.value, obs.accession_number, obs.form, obs.filed,
                 self.snapshot.content_sha256)  # fmt: skip
        return "obs:" + _digest(parts)


def get_fact(
    facts: CompanyFacts,
    metric: Metric,
    period: ResolvedPeriod,
    policy: SelectionPolicy = "latest_filed",
    accession_number: str | None = None,
) -> Fact:
    """Select one observation for the company's mapped concept, exact unit and exact
    period boundaries. Raises FinancialDataError rather than guess.

    - latest_filed / original: company_facts.select_fact across all eligible filings.
    - same_filing: only observations from `accession_number`; never another filing's.
    """
    if facts.cik != period.cik or facts.source.content_sha256 != period.facts_sha256:
        raise FinancialDataError(
            "snapshot_mismatch", "the period was resolved from a different Company Facts snapshot"
        )
    mapping = concept_mapping(facts.cik, metric)
    if mapping.instant != (period.period_class == "instant"):
        needed = "a period-end instant" if mapping.instant else "a fiscal year or quarter"
        raise FinancialDataError("period_kind_mismatch", f"{metric} needs {needed}")
    if (policy == "same_filing") != (accession_number is not None):
        raise FinancialDataError(
            "invalid_request", "an accession number is required for, and only for, same_filing"
        )
    if accession_number is not None and not _ACCESSION.fullmatch(accession_number):
        raise FinancialDataError("invalid_request", f"invalid accession {accession_number!r}")
    if find_concept(facts, mapping.taxonomy, mapping.concept) is None:
        raise FinancialDataError(
            "concept_not_reported", f"{mapping.taxonomy}:{mapping.concept} is not reported"
        )

    selection = select_fact(
        facts,
        mapping.taxonomy,
        mapping.concept,
        mapping.unit,
        period.period_start,
        period.period_end,
        "original" if policy == "original" else "latest_filed",
    )
    eligible = [obs for obs in selection.candidates if obs.form in ELIGIBLE_FORMS]
    if policy == "same_filing":
        in_filing = [obs for obs in eligible if obs.accession_number == accession_number]
        if not in_filing:
            raise FinancialDataError(
                "observation_not_in_filing",
                f"{mapping.concept} for {period.label} is not reported in {accession_number}",
            )
        if len({obs.value for obs in in_filing}) > 1:
            raise FinancialDataError(
                "observation_ambiguous",
                f"{accession_number} reports different values for {mapping.concept} "
                f"in {period.label}",
            )
        selected = in_filing[0]
        reason = f"same_filing: reported in {selected.form} {accession_number}"
    else:
        if selection.selected is None:
            problem = "observation_ambiguous" if eligible else "observation_not_found"
            raise FinancialDataError(problem, selection.reason)
        selected = selection.selected
        reason = selection.reason

    others: dict[Decimal, FactObservation] = {}
    for obs in sorted(eligible, key=lambda o: (o.filed, o.accession_number)):
        if obs.value != selected.value:
            others.setdefault(obs.value, obs)
    return Fact(
        cik=facts.cik,
        metric=metric,
        mapping_version=MAPPING_VERSION,
        taxonomy=mapping.taxonomy,
        concept=mapping.concept,
        unit=mapping.unit,
        period_start=period.period_start,
        period_end=period.period_end,
        period=period,
        policy=policy,
        required_accession=accession_number,
        value=selected.value,
        observation=selected,
        conflicting_values=selection.conflicting_values,
        other_values=[
            OtherValue(
                value=o.value, accession_number=o.accession_number, form=o.form, filed=o.filed
            )
            for o in others.values()
        ],
        selection_reason=reason,
        snapshot=facts.source,
    )


# --- Comparisons and percentage change (M3.3 B2, D-033) -----------------------------------

COMPARISON_VERSION = "1"
_EXACT = 34  # significant digits for the unrounded percentage (Decimal division)
PercentStatus = Literal["computed", "undefined_zero_base", "not_meaningful_negative_base"]
SignChange = Literal["none", "positive_to_negative", "negative_to_positive"]
ComparisonPolicy = Literal["same_filing", "latest_filed", "original"]
_YEAR_DAYS = (357, 371)  # one fiscal year back, allowing for 52/53-week years


class PercentChange(BaseModel):
    """((current - prior) / prior) * 100 and how it may be presented."""

    model_config = ConfigDict(frozen=True)

    status: PercentStatus
    exact: Decimal | None  # unrounded, to 34 significant digits; None unless computed
    display: str | None  # rounded half-even to one decimal, e.g. "15.1%"; None unless computed
    note: str


class Change(BaseModel):
    """The deterministic arithmetic of one comparison."""

    model_config = ConfigDict(frozen=True)

    current: Decimal
    prior: Decimal
    delta: Decimal  # current - prior, exact
    percent: PercentChange
    sign_change: SignChange


class Revision(BaseModel):
    """A different value another eligible filing reported for one side's exact period.
    Reported next to the comparison; it never replaces the same-filing value."""

    model_config = ConfigDict(frozen=True)

    side: Literal["current", "prior"]
    value: Decimal
    accession_number: str
    form: str
    filed: date
    relation: Literal["earlier", "later", "same_date"]  # filed relative to the source filing


class Comparison(BaseModel):
    """Two facts for exact periods, read from one filing, and the change between them."""

    model_config = ConfigDict(frozen=True)

    cik: str
    metric: Metric
    policy: ComparisonPolicy
    commentary_filing: FilingRef  # the filing both values come from, whose MD&A discusses them
    current: Fact
    prior: Fact
    change: Change
    revisions: list[Revision]  # other values reported elsewhere, oldest first
    version: str = COMPARISON_VERSION

    @computed_field
    @property
    def comparison_id(self) -> str:
        return "cmp:" + _digest(
            (self.version, self.current.observation_id, self.prior.observation_id)
        )


def percent_change(current: Decimal, prior: Decimal) -> PercentChange:
    """The percentage policy (D-033). The formula is exact; presentation is separate."""
    if prior == 0:
        return PercentChange(
            status="undefined_zero_base",
            exact=None,
            display=None,
            note="percentage change undefined: the prior value is zero",
        )
    if prior < 0:
        return PercentChange(
            status="not_meaningful_negative_base",
            exact=None,
            display=None,
            note="percentage change not meaningful under this policy: the prior value is negative",
        )
    with localcontext() as context:
        context.prec = _EXACT
        exact = (current - prior) / prior * 100
        display = exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_EVEN)
    return PercentChange(
        status="computed", exact=exact, display=f"{display}%", note="(current - prior) / prior"
    )


def sign_change(current: Decimal, prior: Decimal) -> SignChange:
    if prior > 0 > current:
        return "positive_to_negative"
    if prior < 0 < current:
        return "negative_to_positive"
    return "none"


def calculate_change(current: Fact, prior: Fact) -> Change:
    """Delta, percentage and sign change between two facts. Refuses incompatible facts:
    different company, metric, concept or unit, different period classes, or a prior
    period that does not end before the current one."""
    if (current.cik, current.metric, current.concept) != (prior.cik, prior.metric, prior.concept):
        raise FinancialDataError("invalid_request", "the facts are not the same company and metric")
    if current.unit != prior.unit:
        raise FinancialDataError("unit_mismatch", f"{current.unit} vs {prior.unit}")
    if current.period.period_class != prior.period.period_class:
        raise FinancialDataError(
            "period_class_mismatch",
            f"{current.period.period_class} vs {prior.period.period_class}",
        )
    if prior.period_end >= current.period_end:
        raise FinancialDataError("invalid_request", "the prior period must end before the current")
    return Change(
        current=current.value,
        prior=prior.value,
        delta=current.value - prior.value,  # exact: Decimal subtraction does not round here
        percent=percent_change(current.value, prior.value),
        sign_change=sign_change(current.value, prior.value),
    )


def compare_periods(
    submissions: CompanySubmissions,
    facts: CompanyFacts,
    metric: Metric,
    current: PeriodRequest,
    prior: PeriodRequest,
    policy: ComparisonPolicy = "same_filing",
    accession_number: str | None = None,
) -> Comparison:
    """Compare one metric between two exact periods, both read from one filing.

    The filing defaults to the current period's original reporting filing (the commentary
    filing); `accession_number` may name another listed 10-K or 10-Q instead. Both
    observations must come from that filing, for the mapped concept and unit and the exact
    period boundaries; if either is missing the comparison is refused. Separately sourced
    comparisons (latest_filed, original) are not implemented and are refused.

    If the prior period's own original filing is not in the submissions snapshot, and only
    for a year-over-year comparison (same kind and quarter, one fiscal year earlier), its
    boundaries are taken from the filing's own comparative observations (see
    _comparative_period). Raises FinancialDataError.
    """
    if policy != "same_filing":
        raise FinancialDataError(
            "policy_not_supported",
            "only same_filing comparisons are implemented; values are never mixed across filings",
        )
    current_period = resolve_period(current, submissions, facts)
    if accession_number is None:
        filing = current_period.original_filing
    else:
        listed = [f for f in submissions.recent_filings if f.accession_number == accession_number]
        if len(listed) != 1 or listed[0].form not in ("10-K", "10-Q"):
            raise FinancialDataError(
                "invalid_request", f"{accession_number} is not a listed 10-K or 10-Q"
            )
        filing = listed[0]
    try:
        prior_period = resolve_period(prior, submissions, facts)
    except FinancialDataError as exc:
        year_over_year = (prior.kind, prior.quarter, prior.fiscal_year) == (
            current.kind, current.quarter, current.fiscal_year - 1,
        )  # fmt: skip
        if exc.problem != "period_not_found" or not year_over_year:
            raise
        prior_period = _comparative_period(prior, current_period, filing, submissions, facts)
    if prior_period.period_class != current_period.period_class:
        raise FinancialDataError(
            "period_class_mismatch",
            f"{current_period.label} vs {prior_period.label}: different kinds of period",
        )
    current_fact = get_fact(facts, metric, current_period, "same_filing", filing.accession_number)
    prior_fact = get_fact(facts, metric, prior_period, "same_filing", filing.accession_number)
    change = calculate_change(current_fact, prior_fact)
    return Comparison(
        cik=facts.cik,
        metric=metric,
        policy=policy,
        commentary_filing=filing,
        current=current_fact,
        prior=prior_fact,
        change=change,
        revisions=_revisions(current_fact, "current") + _revisions(prior_fact, "prior"),
    )


def _comparative_period(
    request: PeriodRequest,
    current: ResolvedPeriod,
    filing: FilingRef,
    submissions: CompanySubmissions,
    facts: CompanyFacts,
) -> ResolvedPeriod:
    """The prior-year period as the filing itself reports it, when its own original filing
    is not in the submissions snapshot. Accepted only if:
    - the filing reports exactly one period of the same class ending one fiscal year
      (357-371 days) before the current period, starting one fiscal year before it too;
    - it fits the company's known fiscal calendar: inside the prior fiscal year when that
      year's 10-K is listed (a quarter at the same offset from the fiscal year start),
      otherwise within FYE_TOLERANCE_DAYS of fiscal_year_end for year-end periods.
    The original reporting filing is recorded as unavailable, never guessed."""
    low, high = (current.period_end - timedelta(days=d) for d in reversed(_YEAR_DAYS))
    boundaries = {
        (obs.period_start, obs.period_end)
        for obs in facts.observations
        if obs.accession_number == filing.accession_number
        and period_class(obs) == current.period_class
        and low <= obs.period_end <= high
    }
    if len(boundaries) != 1:
        raise FinancialDataError(
            "period_not_found" if not boundaries else "period_ambiguous",
            f"{filing.accession_number} reports {len(boundaries)} {current.period_class} "
            f"period(s) one fiscal year before {current.label}",
        )
    start, end = boundaries.pop()
    checks = [
        f"FY{request.fiscal_year}'s original filing is not in the submissions snapshot; the "
        f"period is the prior-year comparative reported in {filing.form} {filing.accession_number}",
        f"ends {(current.period_end - end).days} days before the current period",
    ]
    if start is not None:
        offset = (current.period_start - start).days
        if not _YEAR_DAYS[0] <= offset <= _YEAR_DAYS[1]:
            raise FinancialDataError(
                "period_ambiguous", f"the comparative starts {offset} days before the current"
            )
        checks.append(f"starts {offset} days before the current period")
    fy_start = _check_comparative_calendar(request, current, start, end, submissions, facts, checks)
    others = sorted(
        {obs.accession_number for obs in facts.observations
         if obs.period_end == end and obs.period_start == start
         and obs.accession_number != filing.accession_number
         and not any(f.accession_number == obs.accession_number
                     for f in submissions.recent_filings)}
    )  # fmt: skip
    if others:
        checks.append(
            f"Company Facts also has observations of this period from {', '.join(others)}, "
            "which the submissions snapshot does not list; none is treated as a verified "
            "original filing"
        )
    name = (
        f"Q{request.quarter} FY{request.fiscal_year}"
        if request.quarter
        else f"FY{request.fiscal_year}"
    )
    if request.kind.endswith("_end"):
        name = f"end of {name}"
    dates = f"{start} to {end}" if start else f"{end}"
    return ResolvedPeriod(
        cik=facts.cik,
        request=request,
        period_start=start,
        period_end=end,
        period_class=current.period_class,
        label=f"{name} ({dates}, comparative in {filing.accession_number})",
        fiscal_year_start=fy_start,
        basis="comparative_in_filing",
        original_filing=None,
        commentary_filing=filing,
        checks=checks,
        submissions_sha256=submissions.source.content_sha256,
        facts_sha256=facts.source.content_sha256,
    )


def _check_comparative_calendar(request, current, start, end, submissions, facts, checks):
    """Check a comparative period against the known fiscal calendar; return the fiscal
    year start when a listed filing establishes it."""
    prior_year = request.fiscal_year
    try:
        fiscal_year = resolve_period(
            PeriodRequest(kind="fiscal_year", fiscal_year=prior_year), submissions, facts
        )
    except FinancialDataError as exc:
        if exc.problem != "period_not_found":
            raise
        fiscal_year = None
    if fiscal_year is not None:
        fy_start, fy_end = fiscal_year.period_start, fiscal_year.period_end
        if not (fy_start <= (start or end) and end <= fy_end):
            raise FinancialDataError(
                "period_ambiguous",
                f"the comparative period is not inside FY{prior_year} ({fy_start} to {fy_end})",
            )
        if request.kind.startswith("fiscal_year") and end != fy_end:
            raise FinancialDataError(
                "period_ambiguous", f"the comparative year does not end with FY{prior_year}"
            )
        if request.kind == "fiscal_year" and start != fy_start:
            raise FinancialDataError(
                "period_ambiguous", f"the comparative year does not start with FY{prior_year}"
            )
        if request.kind.startswith("fiscal_quarter") and current.fiscal_year_start is not None:
            current_anchor = current.period_start or current.period_end
            prior_anchor = start or end
            gap = abs((current_anchor - current.fiscal_year_start).days
                      - (prior_anchor - fy_start).days)  # fmt: skip
            if gap > FYE_TOLERANCE_DAYS:
                raise FinancialDataError(
                    "period_ambiguous",
                    f"the comparative quarter sits {gap} days away from Q{request.quarter}'s "
                    "position in its fiscal year",
                )
        checks.append(f"inside FY{prior_year} ({fy_start} to {fy_end}) from its listed 10-K")
        return fy_start
    if request.kind.startswith("fiscal_quarter"):
        raise FinancialDataError(
            "period_not_found",
            f"FY{prior_year}'s boundaries are not established by a listed 10-K, so a "
            "comparative quarter cannot be placed in its fiscal year",
        )
    _check_fiscal_year_end(end, submissions.fiscal_year_end, checks)
    return start


def _revisions(fact: Fact, side: Literal["current", "prior"]) -> list[Revision]:
    source = fact.observation

    def relation(filed: date):
        return (
            "earlier" if filed < source.filed else "later" if filed > source.filed else "same_date"
        )

    return [
        Revision(side=side, value=o.value, accession_number=o.accession_number, form=o.form,
                 filed=o.filed, relation=relation(o.filed))
        for o in fact.other_values
    ]  # fmt: skip


def _digest(parts: tuple) -> str:
    text = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(text.encode()).hexdigest()

"""Evidence-backed financial analysis (M3.4): a deterministic comparison plus candidate
management commentary, with no LLM and no network access.

explain_change joins two existing parts and adds no new financial or retrieval logic:
- the numbers come only from financial_tools.compare_periods (periods, concept mapping,
  same-filing observation selection, Decimal arithmetic, revisions);
- the commentary comes only from the comparison's commentary filing, read from the filing
  cache, split by the existing extractor and passage builder, ranked with KeywordIndex
  (BM25), and checked with passages.verify_citations.

Two kinds of provenance are kept apart (D-034). Numerical provenance is the Company Facts
observation behind each value (accession, concept, unit, exact period, snapshot SHA-256).
A narrative citation pins a passage of the filing document's bytes. A verified citation
proves that the passage is exactly what the cached filing says; it does not prove that the
passage explains the change. Passages are therefore returned as candidates only, and no
causal statement is generated from them.
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from financial_analyst.config import Settings
from financial_analyst.filing_cache import (
    load_company_facts_snapshot,
    load_company_tickers_snapshot,
    load_filing_document,
    load_snapshot_manifest,
    load_submissions_snapshot,
)
from financial_analyst.filing_extractor import extract_filing
from financial_analyst.financial_tools import (
    APPLE,
    JPMORGAN,
    CompanyIdentity,
    Comparison,
    FinancialDataError,
    IdentityError,
    Metric,
    PeriodRequest,
    compare_periods,
    concept_mapping,
    resolve_company,
)
from financial_analyst.passages import Passage, build_passages, verify_citations
from financial_analyst.retrieval import KeywordIndex, tokenize
from financial_analyst.sec_models import CompanyFacts, CompanySubmissions, FilingRef, TickerMap

# Bump whenever a change could alter a GroundedResult for the same inputs: the default
# query terms, the section rule, the metric-mention check, k, or the summary format.
ANALYSIS_VERSION = "1"
DEFAULT_K = 5
MAX_QUERY_CHARS = 500
# Where each form discusses results: 10-K Item 7 and 10-Q Part I, Item 2 (MD&A).
MDA_SECTIONS = {"10-K": "Item 7", "10-Q": "Part I, Item 2"}
# The phrase each company's MD&A uses for a metric. A candidate passage must contain it.
METRIC_TERMS: dict[tuple[str, Metric], str] = {
    (APPLE, "revenue"): "net sales",
    (APPLE, "net_income"): "net income",
    (APPLE, "total_assets"): "total assets",
    (JPMORGAN, "revenue"): "net revenue",
    (JPMORGAN, "net_income"): "net income",
    (JPMORGAN, "total_assets"): "total assets",
}
# Words each company's MD&A uses to describe a change, observed in its filings: Apple writes
# "increased during 2026 compared to 2025", JPMorgan "Firmwide ... reported ... up 6%".
CHANGE_TERMS = {APPLE: "increased decreased compared", JPMORGAN: "firmwide reported up down"}
LIMITATION = (
    "Candidate passages come from the filing that reports both values and carry verified "
    "citations. A verified citation proves the passage text is exactly what the cached "
    "filing says; it does not establish that the passage explains the change."
)

Status = Literal["complete", "partial", "abstained"]
RejectionReason = Literal["citation_not_verified", "out_of_scope", "does_not_mention_metric"]


class PinnedData(BaseModel):
    """Structured data loaded from pinned snapshots: the ticker map, and per CIK its
    submissions and Company Facts."""

    model_config = ConfigDict(frozen=True)

    tickers: TickerMap
    companies: dict[str, tuple[CompanySubmissions, CompanyFacts]]


class RetrievalScope(BaseModel):
    """Exactly what was searched, and how."""

    model_config = ConfigDict(frozen=True)

    query: str
    query_source: Literal["default", "explicit"]
    filing: FilingRef  # the commentary filing; no other filing is searched
    document_sha256: str  # the cached document bytes the passages come from
    section: str | None  # the MD&A label searched, or None for the whole filing
    section_note: str
    passages_searched: int
    k: int


class CandidatePassage(BaseModel):
    """A verified passage from the commentary filing that mentions the metric. A candidate
    for commentary on the change, not proof that it explains the change."""

    model_config = ConfigDict(frozen=True)

    rank: int  # 1-based position in the BM25 results
    support: Literal["candidate"] = "candidate"
    citation_id: str
    accession_number: str
    form: str
    document_url: str | None
    section: str | None
    kind: Literal["text", "table"]
    text: str
    source_sha256: str  # SHA-256 of the cited document bytes
    text_sha256: str
    score: float
    matched_terms: list[str]
    verification_status: Literal["verified"]
    verification_detail: str


class RejectedPassage(BaseModel):
    """A retrieved passage that is not returned as evidence, and why."""

    model_config = ConfigDict(frozen=True)

    rank: int
    citation_id: str
    reason: RejectionReason
    verification_status: str
    detail: str


class GroundedResult(BaseModel):
    """One auditable analysis. Every number is in `comparison`; `summary` only formats it."""

    model_config = ConfigDict(frozen=True)

    status: Status
    identifier: str
    metric: Metric
    current_period: PeriodRequest
    prior_period: PeriodRequest
    company: CompanyIdentity | None
    comparison: Comparison | None
    summary: str | None
    retrieval: RetrievalScope | None
    evidence: list[CandidatePassage]
    rejected: list[RejectedPassage]
    reasons: list[str]  # missing evidence, unavailable calculations, or why it abstained
    limitation: str = LIMITATION
    version: str = ANALYSIS_VERSION


def load_pinned_data(manifest_path: Path, cache_dir: Path) -> PinnedData:
    """Load every snapshot a manifest pins, verifying each by SHA-256. Raises SnapshotError
    for anything missing or corrupted, and never fetches."""
    manifest = load_snapshot_manifest(manifest_path)
    companies = {}
    for pin in manifest.snapshots:
        if pin.kind == "submissions":
            companies[pin.cik] = (
                load_submissions_snapshot(cache_dir, pin.cik, pin.sha256),
                load_company_facts_snapshot(
                    cache_dir, pin.cik, manifest.pin("company_facts", pin.cik).sha256
                ),
            )
    tickers = load_company_tickers_snapshot(cache_dir, manifest.pin("company_tickers").sha256)
    return PinnedData(tickers=tickers, companies=companies)


def explain_change(
    identifier: str,
    metric: Metric,
    current_period: PeriodRequest,
    prior_period: PeriodRequest,
    query: str | None = None,
    *,
    data: PinnedData,
    cache_dir: Path,
    k: int = DEFAULT_K,
) -> GroundedResult:
    """Compare a metric between two periods and attach candidate commentary from the filing
    that reports both values.

    abstained: the company, data or comparison cannot be established exactly.
    partial: the comparison succeeded, but no verified passage from the commentary filing
        mentions the metric (filing not cached, nothing retrieved, or not verified).
    complete: the comparison succeeded and at least one such candidate passage exists.

    Raises ValueError only for malformed arguments (an empty or overlong query, a bad k).
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be a positive integer, got {k!r}")
    if query is not None and (not query.strip() or len(query) > MAX_QUERY_CHARS):
        raise ValueError(f"query must be non-blank and at most {MAX_QUERY_CHARS} characters")
    request = {
        "identifier": identifier,
        "metric": metric,
        "current_period": current_period,
        "prior_period": prior_period,
    }

    def abstain(reason: str, company: CompanyIdentity | None = None) -> GroundedResult:
        return GroundedResult(
            status="abstained", company=company, comparison=None, summary=None,
            retrieval=None, evidence=[], rejected=[], reasons=[reason], **request,
        )  # fmt: skip

    try:
        company = resolve_company(identifier, data.tickers)
    except IdentityError as exc:
        return abstain(f"company not resolved ({exc.problem}): {exc}")
    if company.cik not in data.companies:
        return abstain(f"no pinned submissions and Company Facts for CIK {company.cik}", company)
    submissions, facts = data.companies[company.cik]
    try:
        comparison = compare_periods(submissions, facts, metric, current_period, prior_period)
    except FinancialDataError as exc:
        return abstain(f"comparison not established ({exc.problem}): {exc}", company)

    reasons = []
    if comparison.change.percent.status != "computed":
        reasons.append(comparison.change.percent.note)
    retrieval, evidence, rejected, why_not = retrieve_commentary(comparison, query, cache_dir, k)
    reasons.extend(why_not)
    return GroundedResult(
        status="complete" if evidence else "partial",
        company=company,
        comparison=comparison,
        summary=format_summary(company, comparison),
        retrieval=retrieval,
        evidence=evidence,
        rejected=rejected,
        reasons=reasons,
        **request,
    )


def default_query(cik: str, metric: Metric) -> str:
    """The company's own term for the metric followed by its words for a change."""
    return f"{METRIC_TERMS[(cik, metric)]} {CHANGE_TERMS[cik]}"


def retrieve_commentary(
    comparison: Comparison, query: str | None, cache_dir: Path, k: int = DEFAULT_K
) -> tuple[RetrievalScope | None, list[CandidatePassage], list[RejectedPassage], list[str]]:
    """Candidate commentary for a comparison, from its commentary filing only: the search
    scope (None if the filing is unavailable), evidence, rejected hits, and reasons when
    there is no evidence. Used by explain_change and by the agent's search tool."""
    filing = comparison.commentary_filing
    name = f"{filing.form} {filing.accession_number}"
    document = load_filing_document(cache_dir, filing)  # never downloads
    if document is None:
        return (
            None,
            [],
            [],
            [
                f"commentary filing {name} is not in the filing cache or failed its integrity "
                "checks; no commentary was retrieved (no network access is attempted)"
            ],
        )
    extracted = extract_filing(document)
    # Only passages of exactly this document are indexed, so the BM25 statistics and the
    # results never involve another company or filing.
    passages = [p for p in build_passages(extracted) if _same_document(p, filing)]
    section = MDA_SECTIONS.get(filing.form)
    if section in extracted.diagnostics.sections_found:
        note = f"MD&A ({section}) only: the section label was detected reliably"
    else:
        note = f"whole filing: no reliable MD&A section label in this {filing.form}"
        section = None
    retrieval = RetrievalScope(
        query=query if query is not None else default_query(comparison.cik, comparison.metric),
        query_source="default" if query is None else "explicit",
        filing=filing,
        document_sha256=document.source.content_sha256,
        section=section,
        section_note=note,
        passages_searched=sum(section is None or p.section == section for p in passages),
        k=k,
    )
    hits = KeywordIndex(passages).search(
        retrieval.query, k, section=section, accession_number=filing.accession_number
    )
    if not hits:
        return retrieval, [], [], [f"no passage in {name} matched the query"]

    verifications = verify_citations([(h.citation_id, h.passage) for h in hits], cache_dir)
    phrase = tokenize(METRIC_TERMS[(comparison.cik, comparison.metric)])
    evidence, rejected = [], []
    for rank, (hit, check) in enumerate(zip(hits, verifications, strict=True), start=1):
        passage = hit.passage
        reason: RejectionReason | None = None
        if check.status != "verified":
            reason, detail = "citation_not_verified", check.detail
        elif not _same_document(passage, filing) or (section and passage.section != section):
            reason, detail = "out_of_scope", "not from the commentary filing's searched section"
        elif not _contains(tokenize(passage.text), phrase):
            reason, detail = "does_not_mention_metric", f"does not contain {' '.join(phrase)!r}"
        if reason is not None:
            rejected.append(
                RejectedPassage(rank=rank, citation_id=hit.citation_id, reason=reason,
                                verification_status=check.status, detail=detail)
            )  # fmt: skip
            continue
        evidence.append(
            CandidatePassage(
                rank=rank,
                citation_id=hit.citation_id,
                accession_number=passage.source.filing.accession_number,
                form=passage.source.filing.form,
                document_url=passage.source.document_url,
                section=passage.section,
                kind=passage.kind,
                text=passage.text,
                source_sha256=passage.source.content_sha256,
                text_sha256=passage.text_sha256,
                score=hit.score,
                matched_terms=hit.matched_terms,
                verification_status="verified",
                verification_detail=check.detail,
            )
        )
    why_not = []
    if not evidence:
        counts = Counter(item.reason for item in rejected)
        why_not.append(
            f"none of the {len(hits)} retrieved passages is usable commentary: "
            + ", ".join(f"{counts[r]} {r}" for r in sorted(counts))
        )
    return retrieval, evidence, rejected, why_not


def _same_document(passage: Passage, filing: FilingRef) -> bool:
    cited = passage.source.filing
    return (cited.cik, cited.accession_number, cited.primary_document, cited.form) == (
        filing.cik, filing.accession_number, filing.primary_document, filing.form,
    )  # fmt: skip


def _contains(tokens: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(tokens[i : i + n] == phrase for i in range(len(tokens) - n + 1))


# --- Deterministic formatting ------------------------------------------------------------


def format_summary(company: CompanyIdentity, comparison: Comparison) -> str:
    """A fixed-format statement of the comparison. Every figure comes from `comparison`."""
    mapping = concept_mapping(comparison.cik, comparison.metric)
    current, prior, change = comparison.current, comparison.prior, comparison.change
    filing = comparison.commentary_filing
    lines = [
        f"{company.name} {mapping.statement_line.lower()} "
        f"({mapping.taxonomy}:{mapping.concept}, {mapping.unit}): "
        f"{current.period.label} {change.current:,} vs {prior.period.label} {change.prior:,}; "
        f"change {change.delta:+,} {mapping.unit} ({format_percent(comparison)}).",
        f"Both values are from {filing.form} {filing.accession_number} filed "
        f"{filing.filing_date.isoformat()}.",
    ]
    if change.sign_change != "none":
        lines.append(f"Sign change: {change.sign_change.replace('_', ' ')}.")
    if prior.period.basis == "comparative_in_filing":
        lines.append(
            f"The {prior.period.request.fiscal_year} period's boundaries come from this "
            "filing's comparative figures; its original reporting filing is not in the pinned "
            "submissions and is not identified."
        )
    for revision in comparison.revisions:
        lines.append(
            f"Revision (reported separately, not used): {revision.side} value "
            f"{revision.value:,} in {revision.form} {revision.accession_number} filed "
            f"{revision.filed.isoformat()} ({revision.relation})."
        )
    return " ".join(lines)


def format_percent(comparison: Comparison) -> str:
    percent = comparison.change.percent
    if percent.status != "computed":
        return percent.note
    sign = "+" if percent.exact > 0 else ""
    return f"{sign}{percent.display}"


# --- Offline CLI demonstration -----------------------------------------------------------

DEFAULT_MANIFEST = Path("evals/sec_snapshots_v1.json")
EXAMPLES = (
    ("AAPL", "revenue", "FY2026", "FY2025"),
    ("JPM", "net_income", "FY2026Q3", "FY2025Q3"),
)
_PERIOD = re.compile(r"FY(\d{4})(?:Q([1-4]))?(-END)?")
_PREVIEW_CHARS = 320


def parse_period(text: str) -> PeriodRequest:
    """FY2026, FY2026Q3, FY2026-end (a balance at year end) or FY2026Q3-end."""
    match = _PERIOD.fullmatch(text.strip().upper())
    if match is None:
        raise ValueError(f"period must look like FY2026, FY2026Q3 or FY2026Q3-end, got {text!r}")
    year, quarter, end = int(match[1]), match[2], match[3]
    kind = ("fiscal_quarter" if quarter else "fiscal_year") + ("_end" if end else "")
    return PeriodRequest(kind=kind, fiscal_year=year, quarter=int(quarter) if quarter else None)


def render(result: GroundedResult) -> str:
    """Human-readable result. Shows passage previews, never whole filings or raw JSON."""
    lines = [f"== {result.identifier} {result.metric}: {result.status.upper()}"]
    if result.company:
        lines.append(f"Company:  {result.company.name} (CIK {result.company.cik})")
    comparison = result.comparison
    if comparison:
        mapping = concept_mapping(comparison.cik, comparison.metric)
        change, filing = comparison.change, comparison.commentary_filing
        lines += [
            f"Metric:   {mapping.statement_line} ({mapping.taxonomy}:{mapping.concept}, "
            f"{mapping.unit})",
            f"Current:  {comparison.current.period.label}: {change.current:,}",
            f"Prior:    {comparison.prior.period.label}: {change.prior:,}",
            f"Change:   {change.delta:+,} ({format_percent(comparison)}"
            + (f"; exact {change.percent.exact}%" if change.percent.exact is not None else "")
            + ")",
            f"Source:   {filing.form} {filing.accession_number} filed "
            f"{filing.filing_date.isoformat()} (both values) {filing.primary_document_url}",
            f"Basis:    current {comparison.current.period.basis}, "
            f"prior {comparison.prior.period.basis}; {comparison.comparison_id}",
            f"Summary:  {result.summary}",
        ]
    if result.retrieval:
        scope = result.retrieval
        lines.append(
            f"Search:   {scope.query_source} query {scope.query!r}, top {scope.k} of "
            f"{scope.passages_searched} passages; {scope.section_note}"
        )
    if result.evidence:
        lines.append("Candidate commentary (verified citations; not proof of explanation):")
    for item in result.evidence:
        preview = " ".join(item.text.split())
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[:_PREVIEW_CHARS].rsplit(" ", 1)[0] + " ..."
        lines += [
            f"  {item.rank}. [{item.verification_status}] {item.section or 'unlabeled'}, "
            f"{item.kind}, score {item.score:.2f}, matched {', '.join(item.matched_terms)}",
            f"     {item.citation_id}",
            f"     {preview}",
        ]
    for item in result.rejected:
        lines.append(
            f"  rejected #{item.rank} ({item.reason}, {item.verification_status}): {item.detail}"
        )
    lines += [f"Reason:   {reason}" for reason in result.reasons]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.analysis [COMPANY METRIC CURRENT PRIOR] [--query Q]

    With no positional arguments, runs the two pinned examples. Offline: reads only the
    pinned snapshots and the filing cache.
    """
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.analysis")
    parser.add_argument("request", nargs="*", metavar="COMPANY METRIC CURRENT PRIOR")
    parser.add_argument("--query")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    args = parser.parse_args(argv)
    if args.request and len(args.request) != 4:
        parser.error("give COMPANY METRIC CURRENT PRIOR, or nothing for the examples")
    requests = [tuple(args.request)] if args.request else list(EXAMPLES)
    try:
        data = load_pinned_data(args.manifest, args.cache_dir)
        parsed = [(c, m, parse_period(cur), parse_period(pri)) for c, m, cur, pri in requests]
        if any(m not in {metric for _, metric in METRIC_TERMS} for _, m, _, _ in parsed):
            raise ValueError(f"metric must be one of {sorted({m for _, m in METRIC_TERMS})}")
        results = [
            explain_change(c, m, cur, pri, args.query, data=data, cache_dir=args.cache_dir,
                           k=args.k)
            for c, m, cur, pri in parsed
        ]  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - report the problem and stop
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print("\n\n".join(render(result) for result in results))
    return 0 if all(r.status != "abstained" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

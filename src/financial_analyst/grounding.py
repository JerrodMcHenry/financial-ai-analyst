"""Deterministic grounding and server-side rendering of agent drafts (M3.5 B2).

validate_draft checks an AnswerDraft only against what the run's own successful tool calls
established (AgentRunResult.evidence), never against what the model says tools returned.
render_answer then builds the published answer from that evidence alone: every number,
period, label and provenance field is formatted here, not by the model.

What can be checked mechanically, and is:
- each value_id was produced in this run and belongs to the answer's subject comparison
  (same company, metric and periods); the narrative contains no digits of its own;
- each citation is a verified passage from this run's search of the subject's commentary
  filing, and the cached source still matches it (bytes, citation identity; optionally a
  full rebuild);
- each quotation is an exact substring of such a passage, attributed to management only
  when the passage is in the filing's MD&A section.

What cannot be, and so is never published as verified: whether a passage explains the
change, and any interpretation or cause the model proposes. The model's own wording is
withheld; explanations are withheld and their passages shown only as candidate commentary.
"""

import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from financial_analyst.agent import PLACEHOLDER, AgentRunResult, RunPassage
from financial_analyst.analysis import MDA_SECTIONS, PinnedData, format_percent
from financial_analyst.filing_cache import load_filing_document
from financial_analyst.financial_tools import Comparison, Fact, concept_mapping
from financial_analyst.passages import (
    Passage,
    PassageSource,
    parse_citation_id,
    verify_citations,
)
from financial_analyst.request_scope import RequestScope, capture_scope, check_subject

GROUNDING_VERSION = "2"  # 2: commentary required only when asked; request scope
_PREVIEW_CHARS = 240

IssueCode = Literal[
    # The whole draft is rejected (the answer abstains):
    "run_not_drafted",  # the run ended without a structurally valid draft
    "missing_subject",  # the draft names no comparison
    "subject_not_in_run",  # the named comparison was not produced in this run
    "value_not_in_run",  # unknown, invented, stale or other-run value_id
    "value_context_mismatch",  # a value for another company, metric or period
    "raw_number_in_text",  # digits in the narrative instead of placeholders
    "scope_unresolved",  # the question's company, metric or periods cannot be read reliably
    "scope_mismatch",  # the subject comparison answers another question
    # Only the item is rejected (the answer is at most partial):
    "citation_not_in_run",  # not a verified passage returned by search_commentary here
    "citation_out_of_scope",  # from another comparison's (company's, filing's) search
    "citation_not_verified",  # the cached source no longer matches the citation
    "quote_citation_invalid",  # the quote's passage is not a grounded citation
    "quote_not_verbatim",  # not an exact substring of the passage
    "attribution_unsupported",  # "management" for a passage not in MD&A
]
_ABSTAIN = frozenset(
    {"run_not_drafted", "missing_subject", "subject_not_in_run", "value_not_in_run",
     "value_context_mismatch", "raw_number_in_text", "scope_unresolved", "scope_mismatch"}
)  # fmt: skip

LIMITATIONS = (
    "Figures, periods and provenance are rendered by the server from the pinned SEC data; "
    "the model chose which tools to call but wrote none of them.",
    "Citations prove that the passage text is exactly what the cached filing says. They do "
    "not establish that the passage explains the change.",
    "Commentary passages are candidates selected by keyword retrieval, not verified causes.",
    "Model-written wording and explanations are withheld because they cannot be verified "
    "deterministically.",
)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: IssueCode
    ref: str | None  # the value_id, citation id or placeholder concerned
    detail: str


class WithheldClaim(BaseModel):
    """Model content that is not published, and why."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["model_narrative", "explanation"]
    text: str
    citation_id: str | None
    reason: str


class GroundedQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citation_id: str
    attribution: Literal["filing", "management"]


class GroundingReport(BaseModel):
    """The result of validating one draft against its own run."""

    model_config = ConfigDict(frozen=True)

    subject: Comparison | None
    scope: RequestScope | None  # what the question asks, read from its text (request_scope)
    citations: list[RunPassage]  # grounded, re-checked candidate passages, in draft order
    quotes: list[GroundedQuote]
    issues: list[ValidationIssue]
    withheld: list[WithheldClaim]

    @property
    def abstains(self) -> bool:
        return self.subject is None or any(i.code in _ABSTAIN for i in self.issues)


def validate_draft(
    run: AgentRunResult,
    cache_dir: Path,
    *,
    full_reverify: bool = False,
    scope: RequestScope | None = None,
) -> GroundingReport:
    """Check a run's draft against the run's evidence. Never raises for bad drafts.

    Citations are re-checked against the cache: the document must still hash to the cited
    source, and the citation id must still describe the passage text the run returned.
    The run's search already rebuilt and verified every passage in this process;
    full_reverify=True repeats that rebuild (verify_citations).

    With a scope (ground always passes one), the subject comparison must match the company,
    metric and periods read independently from the question; otherwise the answer abstains.
    """
    draft, evidence = run.draft, run.evidence
    issues: list[ValidationIssue] = []

    def flag(code: IssueCode, ref: str | None, detail: str) -> None:
        issues.append(ValidationIssue(code=code, ref=ref, detail=detail))

    def report(subject=None, citations=(), quotes=(), withheld=()) -> GroundingReport:
        return GroundingReport(
            subject=subject, scope=scope, citations=list(citations), quotes=list(quotes),
            issues=issues, withheld=list(withheld),
        )  # fmt: skip

    if run.status != "drafted" or draft is None:
        flag("run_not_drafted", None, f"run status {run.status}: {run.error}")
        return report()
    withheld = [
        WithheldClaim(
            kind="model_narrative", text=draft.answer, citation_id=None,
            reason="model wording is not published: only server-rendered figures, verified "
            "quotations and candidate commentary are",
        )
    ]  # fmt: skip
    if draft.subject is None:
        flag("missing_subject", None, "the draft names no comparison")
        return report(withheld=withheld)
    subject = evidence.comparisons.get(draft.subject)
    if subject is None:
        flag("subject_not_in_run", draft.subject, "no compare call in this run produced it")
        return report(withheld=withheld)
    if scope is not None:
        problems = check_subject(scope, subject)
        if problems:
            code = "scope_unresolved" if scope.status != "resolved" else "scope_mismatch"
            flag(code, subject.comparison_id, "; ".join(problems))

    for value in draft.values:
        problem = _value_problem(value.value_id, subject, evidence.comparisons, evidence.facts)
        if problem:
            flag(problem[0], value.value_id, problem[1])
    if any(ch.isdigit() for ch in PLACEHOLDER.sub("", draft.answer)):
        flag("raw_number_in_text", None,
             "digits in the narrative; numbers must be placeholders for tool values")  # fmt: skip

    wanted = list(dict.fromkeys(
        draft.citations + [q.citation_id for q in draft.quotes]
        + [e.citation_id for e in draft.explanations if e.citation_id]
    ))  # fmt: skip
    grounded = _ground_citations(wanted, subject, evidence.passages, cache_dir, full_reverify, flag)

    quotes = []
    for quote in draft.quotes:
        item = grounded.get(quote.citation_id)
        if item is None:
            flag("quote_citation_invalid", quote.citation_id,
                 "the quoted passage is not a grounded citation")  # fmt: skip
        elif quote.text not in item.passage.text:
            flag("quote_not_verbatim", quote.citation_id,
                 f"not an exact substring of the passage: {quote.text[:80]!r}")  # fmt: skip
        elif quote.attribution == "management" and not _in_mda(item, subject):
            flag("attribution_unsupported", quote.citation_id,
                 "management attribution needs a passage in the filing's MD&A section")  # fmt: skip
        else:
            quotes.append(GroundedQuote(text=quote.text, citation_id=quote.citation_id,
                                        attribution=quote.attribution))  # fmt: skip

    for explanation in draft.explanations:
        withheld.append(WithheldClaim(
            kind="explanation", text=explanation.text, citation_id=explanation.citation_id,
            reason="interpretations and causes cannot be verified deterministically; a cited, "
            "verified passage is shown only as candidate commentary",
        ))  # fmt: skip
    return report(subject, grounded.values(), quotes, withheld)


def _value_problem(value_id, subject, comparisons, facts) -> tuple[IssueCode, str] | None:
    if value_id.startswith("cmp:"):
        comparison_id = value_id.split("/")[0]
        if comparison_id not in comparisons:
            return "value_not_in_run", "no compare call in this run produced this value"
        if comparison_id != subject.comparison_id:
            other = comparisons[comparison_id]
            return "value_context_mismatch", (
                f"from comparison {other.cik} {other.metric} {other.current.period.label} vs "
                f"{other.prior.period.label}, not the subject"
            )
        return None
    fact = facts.get(value_id)
    if fact is None:
        return "value_not_in_run", "no get_fact call in this run produced this value"
    periods = {_period(subject.current), _period(subject.prior)}
    if (fact.cik, fact.metric) != (subject.cik, subject.metric) or _period(fact) not in periods:
        return "value_context_mismatch", (
            f"{fact.cik} {fact.metric} {fact.period.label} is not the subject's company, "
            "metric or periods"
        )
    return None


def _period(fact: Fact) -> tuple:
    return fact.period_start, fact.period_end


def _ground_citations(citation_ids, subject, passages, cache_dir, full_reverify, flag):
    """The draft's citations that are this run's verified passages for the subject and
    still match the cached source. Problems are reported through `flag`."""
    filing = subject.commentary_filing
    candidates: dict[str, RunPassage] = {}
    for citation_id in citation_ids:
        item = passages.get(citation_id)
        if item is None:
            flag("citation_not_in_run", citation_id, "not returned by search_commentary here")
            continue
        cited = parse_citation_id(citation_id)
        in_filing = (cited.cik, cited.accession_number) == (filing.cik, filing.accession_number)
        if item.comparison_id != subject.comparison_id or not in_filing:
            flag("citation_out_of_scope", citation_id,
                 f"from {cited.cik} {cited.accession_number}, not the subject's commentary "
                 f"filing {filing.cik} {filing.accession_number}")  # fmt: skip
            continue
        candidates[citation_id] = item

    document = load_filing_document(cache_dir, filing) if candidates else None
    sha256 = hashlib.sha256(document.content).hexdigest() if document else None
    rebuilt = {cid: _as_passage(cid, item, subject) for cid, item in candidates.items()}
    grounded: dict[str, RunPassage] = {}
    for citation_id, item in candidates.items():
        if sha256 != item.passage.source_sha256:
            flag("citation_not_verified", citation_id,
                 "the cached source is missing, corrupted or changed")  # fmt: skip
        elif rebuilt[citation_id].citation_id != citation_id:
            flag("citation_not_verified", citation_id,
                 "the citation id does not describe the passage text the run returned")  # fmt: skip
        else:
            grounded[citation_id] = item
    if full_reverify and grounded:
        for check in verify_citations([(cid, rebuilt[cid]) for cid in grounded], cache_dir):
            if check.status != "verified":
                grounded.pop(check.citation_id)
                flag("citation_not_verified", check.citation_id, f"{check.status}: {check.detail}")
    return grounded


def _as_passage(citation_id: str, item: RunPassage, subject: Comparison) -> Passage:
    """The Passage the run's candidate describes, rebuilt from its own fields."""
    cited = parse_citation_id(citation_id)
    candidate = item.passage
    return Passage(
        source=PassageSource(
            filing=subject.commentary_filing,
            content_sha256=candidate.source_sha256,
            extractor_version=cited.extractor_version,
            builder_version=cited.builder_version,
        ),
        section=candidate.section,
        kind=candidate.kind,
        block_start=cited.block_start,
        char_start=cited.char_start,
        block_end=cited.block_end,
        char_end=cited.char_end,
        text=candidate.text,
    )


def _in_mda(item: RunPassage, subject: Comparison) -> bool:
    section = MDA_SECTIONS.get(subject.commentary_filing.form)
    return section is not None and item.passage.section == section


# --- Server-side rendering ---------------------------------------------------------------


class RenderedValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    value: Decimal
    formatted: str
    value_id: str
    observation_id: str
    fact_key: str
    accession_number: str
    form: str
    filed: str
    period_basis: str


class RenderedPercent(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    display: str | None  # signed, e.g. "+15.1%"; None when unavailable
    exact: Decimal | None
    note: str


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    comparison_id: str
    policy: str
    concept: str
    unit: str
    commentary_filing: str  # "10-K 0000320193-26-000026 filed 2026-11-03"
    commentary_filing_url: str | None
    company_facts_sha256: str
    mapping_version: str
    comparison_version: str


class RenderedCitation(BaseModel):
    model_config = ConfigDict(frozen=True)

    citation_id: str
    accession_number: str
    form: str
    section: str | None
    document_url: str | None
    source_sha256: str
    support: Literal["candidate"] = "candidate"
    preview: str


class RenderedQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    citation_id: str
    attribution: str  # rendered here from the filing, e.g. "Apple Inc. management, 10-K ..."


class GroundedAnswer(BaseModel):
    """The published answer. Only this, never the draft, may be shown as an answer."""

    model_config = ConfigDict(frozen=True)

    status: Literal["complete", "partial", "abstained"]
    question: str
    company: str | None  # "Apple Inc. (CIK 0000320193)"
    cik: str | None
    metric: str | None
    statement_line: str | None
    current: RenderedValue | None
    prior: RenderedValue | None
    delta: Decimal | None
    delta_formatted: str | None
    percent: RenderedPercent | None
    request_scope: RequestScope | None  # what the question asks, read from its text
    statement: str | None  # the server-rendered financial statement
    provenance: Provenance | None
    citations: list[RenderedCitation]  # candidate commentary
    quotations: list[RenderedQuote]
    warnings: list[str]
    issues: list[ValidationIssue]
    withheld: list[WithheldClaim]
    limitations: tuple[str, ...] = LIMITATIONS
    run_status: str
    tool_calls: int
    model_turns: int
    version: str = GROUNDING_VERSION


def ground(run: AgentRunResult, *, data: PinnedData, cache_dir: Path) -> GroundedAnswer:
    """Read the request scope from the question, validate the run's draft against the run's
    evidence and that scope, and render the answer."""
    scope = capture_scope(run.question, data)
    return render_answer(run, validate_draft(run, cache_dir, scope=scope), data)


def render_answer(run: AgentRunResult, report: GroundingReport, data: PinnedData) -> GroundedAnswer:
    """Build the published answer from the run's evidence and the validation report.

    abstained: no valid subject, a numeric or request-scope problem; no figures are shown.
    complete: valid figures and no issues; when the question asks for commentary, also a
        commentary search for this comparison and at least one grounded citation; when it
        asks for a quotation, also a verified quotation.
    partial: valid figures, but required commentary or quotation missing, or some citations
        or quotations rejected. Citations shown are always grounded, whether or not asked.
    """
    base = {
        "question": run.question, "issues": report.issues, "withheld": report.withheld,
        "request_scope": report.scope,
        "run_status": run.status, "tool_calls": len(run.tool_results),
        "model_turns": run.model_turns,
    }  # fmt: skip
    if report.abstains:
        reasons = [f"{i.code}: {i.detail}" for i in report.issues if i.code in _ABSTAIN]
        return GroundedAnswer(
            status="abstained", company=None, cik=None, metric=None, statement_line=None,
            current=None, prior=None, delta=None, delta_formatted=None, percent=None,
            statement=None, provenance=None, citations=[], quotations=[],
            warnings=reasons or ["no comparison could be established"], **base,
        )  # fmt: skip

    comparison = report.subject
    mapping = concept_mapping(comparison.cik, comparison.metric)
    name = data.companies[comparison.cik][0].name
    filing = comparison.commentary_filing
    change, percent = comparison.change, comparison.change.percent
    cid = comparison.comparison_id
    filing_label = f"{filing.form} {filing.accession_number} filed {filing.filing_date}"

    citations = [
        RenderedCitation(
            citation_id=item.passage.citation_id, accession_number=item.passage.accession_number,
            form=item.passage.form, section=item.passage.section,
            document_url=item.passage.document_url, source_sha256=item.passage.source_sha256,
            preview=_preview(item.passage.text),
        )
        for item in report.citations
    ]  # fmt: skip
    section = MDA_SECTIONS.get(filing.form)
    quotations = [
        RenderedQuote(
            text=quote.text, citation_id=quote.citation_id,
            attribution=(f"{name} management, {filing.form} {filing.accession_number}, "
                         f"{section} (MD&A)") if quote.attribution == "management"
            else f"{name} {filing.form} {filing.accession_number}",
        )
        for quote in report.quotes
    ]  # fmt: skip

    warnings = []
    scope = report.scope
    needs_commentary = bool(scope and scope.commentary_required)
    needs_quotation = bool(scope and scope.quotation_requested)
    searched = comparison.comparison_id in run.evidence.searches
    if needs_commentary and not searched:
        warnings.append("the question asks for commentary, but no commentary search was made "
                        "for this comparison")  # fmt: skip
    elif needs_commentary and not citations:
        warnings.append("the question asks for commentary, but no verified passage was found "
                        "in the commentary filing")  # fmt: skip
    if needs_quotation and not quotations:
        warnings.append("the question asks for a quotation, but none was verified")
    if percent.status != "computed":
        warnings.append(percent.note)
    if change.sign_change != "none":
        warnings.append(f"sign change: {change.sign_change.replace('_', ' ')}")
    if comparison.prior.period.basis == "comparative_in_filing":
        warnings.append(
            "the prior period comes from this filing's comparative figures; its original "
            "reporting filing is not in the pinned submissions and is not identified"
        )
    for revision in comparison.revisions:
        warnings.append(
            f"revision reported separately (not used): {revision.side} value "
            f"{format_amount(revision.value, mapping.unit)} in {revision.form} "
            f"{revision.accession_number} filed {revision.filed} ({revision.relation})"
        )
    rejected = [i for i in report.issues if i.code not in _ABSTAIN]
    if rejected:
        warnings.append(f"{len(rejected)} citation or quotation item(s) failed validation and "
                        "are not shown")  # fmt: skip
    if any(w.kind == "explanation" for w in report.withheld):
        warnings.append("the model's explanations are withheld: causes are not verified")

    return GroundedAnswer(
        status="complete"
        if _complete(report, needs_commentary, citations, needs_quotation, quotations)
        else "partial",  # fmt: skip
        company=f"{name} (CIK {comparison.cik})",
        cik=comparison.cik,
        metric=comparison.metric,
        statement_line=mapping.statement_line,
        current=_rendered(comparison.current, f"{cid}/current", mapping.unit),
        prior=_rendered(comparison.prior, f"{cid}/prior", mapping.unit),
        delta=change.delta,
        delta_formatted=format_amount(change.delta, mapping.unit, signed=True),
        percent=RenderedPercent(
            status=percent.status,
            display=format_percent(comparison) if percent.status == "computed" else None,
            exact=percent.exact,
            note=percent.note,
        ),
        statement=format_statement(name, comparison),
        provenance=Provenance(
            comparison_id=cid,
            policy=comparison.policy,
            concept=f"{mapping.taxonomy}:{mapping.concept}",
            unit=mapping.unit,
            commentary_filing=filing_label,
            commentary_filing_url=filing.primary_document_url,
            company_facts_sha256=comparison.current.snapshot.content_sha256,
            mapping_version=comparison.current.mapping_version,
            comparison_version=comparison.version,
        ),
        citations=citations,
        quotations=quotations,
        warnings=warnings,
        **base,
    )


def _complete(report, needs_commentary, citations, needs_quotation, quotations) -> bool:
    """Valid figures and no issue; plus, only when the question asks for them, at least one
    grounded citation and a verified quotation. A grounded citation exists only if this run
    searched the subject comparison's commentary filing, so it also proves the search."""
    if report.issues:
        return False
    if needs_commentary and not citations:
        return False
    return not (needs_quotation and not quotations)


def format_amount(value: Decimal, unit: str, signed: bool = False) -> str:
    """Exact amount with thousands separators: "$478,834,000,000", "-$1,000"."""
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    if unit == "USD":
        return f"{sign}${abs(value):,}"
    return f"{sign}{abs(value):,} {unit}"


def format_statement(name: str, comparison: Comparison) -> str:
    """The one financial statement the server publishes for a comparison."""
    mapping = concept_mapping(comparison.cik, comparison.metric)
    change, unit = comparison.change, mapping.unit
    if change.delta > 0:
        movement = (f"an increase of {format_amount(change.delta, unit)} "
                    f"({format_percent(comparison)})")  # fmt: skip
    elif change.delta < 0:
        movement = (f"a decrease of {format_amount(-change.delta, unit)} "
                    f"({format_percent(comparison)})")  # fmt: skip
    else:
        movement = "no change"
    return (
        f"{name} {mapping.statement_line.lower()} was {format_amount(change.current, unit)} for "
        f"{comparison.current.period.label}, compared with {format_amount(change.prior, unit)} "
        f"for {comparison.prior.period.label}: {movement}. Both values are from "
        f"{comparison.commentary_filing.form} {comparison.commentary_filing.accession_number}."
    )


def _rendered(fact: Fact, value_id: str, unit: str) -> RenderedValue:
    return RenderedValue(
        period=fact.period.label, value=fact.value, formatted=format_amount(fact.value, unit),
        value_id=value_id, observation_id=fact.observation_id, fact_key=fact.fact_key,
        accession_number=fact.observation.accession_number, form=fact.observation.form,
        filed=fact.observation.filed.isoformat(), period_basis=fact.period.basis,
    )  # fmt: skip


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW_CHARS else flat[:_PREVIEW_CHARS].rsplit(" ", 1)[0] + " ..."

"""Offline retrieval evaluation against pinned SEC filings.

A dataset (JSON) pins each filing by its full SHA-256 and the extractor and passage-builder
versions, and labels evidence as exact text spans in extracted blocks. The runner reads the
filings only from the filing cache (never from SEC), builds one combined BM25 index, and
measures relevance, citation integrity and latency.

Two kinds of failure are kept apart:
- Integrity failures (missing or mismatched documents, unresolvable evidence, citations that
  do not verify, filter violations, non-deterministic results) mean the evaluation itself is
  invalid. run_evaluation raises EvaluationError and produces no metrics.
- Relevance misses (evidence not retrieved) are results, reported in the metrics.

Labels are a reviewed, non-exhaustive evidence set: recall is measured over labeled
evidence only, never over all evidence a filing might contain.
"""

import argparse
import hashlib
import math
import platform
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from financial_analyst.config import Settings
from financial_analyst.filing_cache import load_filing_document
from financial_analyst.filing_extractor import EXTRACTOR_VERSION, ExtractedFiling, extract_filing
from financial_analyst.passages import BUILDER_VERSION, Passage, build_passages, verify_citation
from financial_analyst.retrieval import KeywordIndex, SearchHit
from financial_analyst.sec_models import FilingRef

# A passage is relevant to an evidence span only if it contains at least this share of the
# span's characters. Tiny overlaps (a shared boundary character) never count.
RELEVANCE_THRESHOLD = 0.8
RESULTS_PER_SEARCH = 10  # hits retrieved per search; MRR uses all of them
METRIC_K = 5  # Hit@5, labeled-evidence recall@5, combined coverage@5, section precision@5
MIN_SNIPPET_CHARS = 20

QuestionKind = Literal["direct", "paraphrase", "unanswerable"]
_KEY = r"^[a-z0-9][a-z0-9-]*$"


# --- Dataset schema ----------------------------------------------------------------------


class _Strict(BaseModel):
    # Hand-written JSON: unknown fields are typos, not extensions.
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvalDocument(_Strict):
    """One pinned filing. The pin is the full SHA-256 plus both code versions."""

    key: str = Field(pattern=_KEY)
    cik: str = Field(pattern=r"^\d{10}$")
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    form: str
    primary_document: str
    filing_date: date
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extractor_version: str
    builder_version: str

    def filing_ref(self) -> FilingRef:
        """A reference for the cache lookup. The cache matches CIK, accession, form and
        primary document, and returns the FilingRef stored at download time."""
        return FilingRef(
            cik=self.cik,
            accession_number=self.accession_number,
            form=self.form,
            filing_date=self.filing_date,
            report_date=None,
            acceptance_datetime=None,
            primary_document=self.primary_document,
            is_xbrl=False,
            is_inline_xbrl=False,
        )


class Evidence(_Strict):
    """An exact span of one extracted block, found by reading the filing (not by search)."""

    document: str  # EvalDocument.key
    snippet: str = Field(min_length=MIN_SNIPPET_CHARS)  # must occur exactly once in the block
    block_index: int = Field(ge=0)
    section: str | None  # the block's section label, as extracted


class SectionRef(_Strict):
    document: str
    section: str = Field(min_length=1)


class FilteredSearch(_Strict):
    """A secondary search with exact filters. expect_empty: the filters must match nothing."""

    section: str | None = None
    form: str | None = None
    document: str | None = None  # restricts to that document's accession number
    expect_empty: bool = False

    @model_validator(mode="after")
    def _has_filter(self) -> "FilteredSearch":
        if self.section is None and self.form is None and self.document is None:
            raise ValueError("a filtered search needs at least one filter")
        return self


class EvalQuestion(_Strict):
    id: str = Field(pattern=_KEY)
    query: str = Field(min_length=1)
    kind: QuestionKind
    topic: str
    # The question asks for a financial amount. It is still narrative evidence retrieval:
    # a number found in text is text-sourced, never an authoritative XBRL value or a
    # calculation input.
    asks_for_amount: bool = False
    expected_sections: list[SectionRef] = []
    evidence: list[Evidence] = []
    filtered_searches: list[FilteredSearch] = []

    @model_validator(mode="after")
    def _check_kind(self) -> "EvalQuestion":
        if self.kind == "unanswerable":
            if self.evidence or self.expected_sections:
                raise ValueError("an unanswerable question has no evidence or sections")
        elif not self.evidence:
            raise ValueError("an answerable question needs at least one evidence span")
        return self


class DatasetChange(_Strict):
    dataset_version: str
    date: date
    change: str = Field(min_length=1)


class EvalDataset(_Strict):
    dataset_version: str
    documents: list[EvalDocument] = Field(min_length=1)
    questions: list[EvalQuestion] = Field(min_length=1)
    changelog: list[DatasetChange] = Field(min_length=1)  # every label change, with a reason

    @model_validator(mode="after")
    def _check_references(self) -> "EvalDataset":
        keys = [d.key for d in self.documents]
        ids = [q.id for q in self.questions]
        if len(set(keys)) != len(keys):
            raise ValueError("document keys must be unique")
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        for question in self.questions:
            referenced = [e.document for e in question.evidence]
            referenced += [s.document for s in question.expected_sections]
            referenced += [f.document for f in question.filtered_searches if f.document]
            unknown = sorted(set(referenced) - set(keys))
            if unknown:
                raise ValueError(f"question {question.id} references unknown documents {unknown}")
        if self.changelog[-1].dataset_version != self.dataset_version:
            raise ValueError("the last changelog entry must describe the current dataset_version")
        return self


# --- Evidence matching and metrics -------------------------------------------------------


class ResolvedEvidence(BaseModel):
    """An evidence span located in the extracted filing: block.text[start:end]."""

    model_config = ConfigDict(frozen=True)

    document: str
    content_sha256: str
    block_index: int
    start: int
    end: int


def resolve_evidence(
    evidence: Evidence, pin: EvalDocument, extracted: ExtractedFiling
) -> ResolvedEvidence:
    """Locate a labeled span. Raises ValueError unless the label names this document, the
    extracted text is from the pinned bytes, the labeled block exists, the snippet occurs
    exactly once in that block, and the block has the labeled section.

    Uniqueness is checked within the block, not the filing: the block index already says
    which occurrence is meant, so a disclosure repeated in another block (a summary and a
    note, say) can be labeled in each place independently.
    """
    if evidence.document != pin.key:
        raise ValueError(f"evidence for {evidence.document} resolved against {pin.key}")
    if extracted.source.content_sha256 != pin.content_sha256:
        raise ValueError(f"extracted text of {pin.key} is not from the pinned SHA-256")
    if evidence.block_index >= len(extracted.blocks):
        raise ValueError(f"block {evidence.block_index} does not exist in {pin.key}")
    block = extracted.blocks[evidence.block_index]
    start = block.text.find(evidence.snippet)
    if start == -1:
        raise ValueError(f"snippet is not in block {evidence.block_index} of {pin.key}")
    # Searching again from start + 1 also catches overlapping repeats, which count() misses.
    if block.text.find(evidence.snippet, start + 1) != -1:
        raise ValueError(
            f"snippet occurs more than once in block {evidence.block_index} of {pin.key}"
        )
    if block.section != evidence.section:
        raise ValueError(
            f"block {evidence.block_index} of {pin.key} has section {block.section!r}, "
            f"labeled {evidence.section!r}"
        )
    return ResolvedEvidence(
        document=pin.key,
        content_sha256=pin.content_sha256,
        block_index=block.index,
        start=start,
        end=start + len(evidence.snippet),
    )


def evidence_overlap(passage: Passage, evidence: ResolvedEvidence) -> tuple[int, int]:
    """The (start, end) characters of the evidence span inside the passage; empty if none."""
    if passage.source.content_sha256 != evidence.content_sha256:
        return (0, 0)
    if not passage.block_start <= evidence.block_index <= passage.block_end:
        return (0, 0)
    start = passage.char_start if evidence.block_index == passage.block_start else 0
    end = passage.char_end if evidence.block_index == passage.block_end else evidence.end
    overlap_start, overlap_end = max(start, evidence.start), min(end, evidence.end)
    return (overlap_start, overlap_end) if overlap_start < overlap_end else (0, 0)


def covers(passage: Passage, evidence: ResolvedEvidence) -> bool:
    """Whether this one passage contains at least RELEVANCE_THRESHOLD of the span."""
    start, end = evidence_overlap(passage, evidence)
    return end - start >= RELEVANCE_THRESHOLD * (evidence.end - evidence.start)


def combined_coverage(passages: list[Passage], evidence: ResolvedEvidence) -> float:
    """Share of the span's characters contained in any of the passages together. Measures
    evidence split across passages, which `covers` deliberately does not count."""
    covered: set[int] = set()
    for passage in passages:
        covered.update(range(*evidence_overlap(passage, evidence)))
    return len(covered) / (evidence.end - evidence.start)


def hit_at_k(relevant: list[bool], k: int) -> float:
    """1.0 if any of the first k results is relevant, else 0.0."""
    _check_k(k)
    return 1.0 if any(relevant[:k]) else 0.0


def reciprocal_rank(relevant: list[bool], k: int) -> float:
    """1 / rank of the first relevant result among the first k, or 0.0 if there is none."""
    _check_k(k)
    for rank, is_relevant in enumerate(relevant[:k], start=1):
        if is_relevant:
            return 1.0 / rank
    return 0.0


def section_precision(hits: list[SearchHit], expected: set[tuple[str, str]]) -> float | None:
    """Share of the hits whose (content_sha256, section) is expected. Every hit counts in
    the denominator, including hits from other filings and sectionless passages.
    None when there are no hits (precision is undefined; Hit@k already scores that)."""
    if not hits:
        return None
    matching = sum(
        1 for h in hits if (h.passage.source.content_sha256, h.passage.section) in expected
    )
    return matching / len(hits)


def _check_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be a positive integer, got {k!r}")


# --- Report ------------------------------------------------------------------------------


class QuestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: QuestionKind
    topic: str
    asks_for_amount: bool
    citation_ids: list[str]  # the top RESULTS_PER_SEARCH hits, best first
    scores: list[float]
    relevant_ranks: list[int]  # 1-based ranks of relevant hits
    # Relevance metrics; None for unanswerable questions.
    hit_at_5: float | None
    reciprocal_rank_at_10: float | None
    labeled_evidence_recall_at_5: float | None
    combined_evidence_coverage_at_5: float | None
    split_evidence: int  # spans covered >= threshold only by several top-5 passages together
    section_precision_at_5: float | None  # None without expected sections or without hits


class FilteredResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    question_id: str
    search: FilteredSearch
    citation_ids: list[str]
    hit_at_5: float | None  # None for expect_empty searches and unanswerable questions
    reciprocal_rank_at_10: float | None


class RelevanceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    questions: int
    hit_at_5: float | None
    mrr_at_10: float | None
    labeled_evidence_recall_at_5: float | None
    combined_evidence_coverage_at_5: float | None
    section_precision_at_5: float | None
    section_precision_questions: int  # questions with expected sections and at least one hit


class LatencySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float


class DocumentTiming(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: str
    blocks: int
    passages: int
    load_ms: float  # cache read and SHA-256 check
    extract_ms: float
    build_passages_ms: float


class VerificationTiming(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: str
    citations: int
    total_ms: float  # each verification re-extracts the whole filing


class Timings(BaseModel):
    """Wall-clock measurements. Informational; excluded from determinism checks."""

    model_config = ConfigDict(frozen=True)

    python: str
    platform: str
    documents: list[DocumentTiming]
    index_build_ms: float
    search: LatencySummary  # primary (unfiltered) searches only, after one warm-up round
    verification: LatencySummary  # measured separately from search
    verification_by_document: list[VerificationTiming]


class EvalReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_version: str
    dataset_sha256: str
    documents: list[EvalDocument]
    passages_indexed: int
    relevance_threshold: float
    overall: RelevanceMetrics  # all answerable questions, unfiltered combined index
    direct: RelevanceMetrics
    paraphrase: RelevanceMetrics
    filtered: RelevanceMetrics  # secondary: filtered searches that expect results
    questions: list[QuestionResult]
    filtered_searches: list[FilteredResult]
    citations_verified: int  # every distinct hit of every search; all must verify
    filter_compliance_checked: int  # hits checked against their search's filters
    timings: Timings


class EvaluationError(Exception):
    """The evaluation is invalid; no metrics are produced."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__(f"{len(problems)} integrity problem(s):\n- " + "\n- ".join(problems))
        self.problems = problems


# --- Runner ------------------------------------------------------------------------------


def load_dataset(path: Path) -> tuple[EvalDataset, str]:
    """The validated dataset and the SHA-256 of the exact file bytes."""
    content = path.read_bytes()
    try:
        dataset = EvalDataset.model_validate_json(content)
    except ValidationError as exc:
        raise EvaluationError([f"invalid dataset {path}: {exc}"]) from exc
    return dataset, hashlib.sha256(content).hexdigest()


def run_evaluation(
    dataset_path: Path, cache_dir: Path, *, search_repetitions: int = 20
) -> EvalReport:
    """Evaluate retrieval on the pinned filings in `cache_dir`. Never contacts SEC.

    Raises EvaluationError, with every problem found, if the evaluation is invalid.
    """
    if search_repetitions < 1:
        raise ValueError("search_repetitions must be at least 1")
    dataset, dataset_sha256 = load_dataset(dataset_path)
    problems: list[str] = []

    # 1. Pinned documents, from the cache only.
    passages: list[Passage] = []
    extracted_by_key: dict[str, ExtractedFiling] = {}
    document_timings: list[DocumentTiming] = []
    for pin in dataset.documents:
        if pin.extractor_version != EXTRACTOR_VERSION:
            problems.append(
                f"{pin.key}: pinned extractor {pin.extractor_version}, current {EXTRACTOR_VERSION}"
            )
        if pin.builder_version != BUILDER_VERSION:
            problems.append(
                f"{pin.key}: pinned builder {pin.builder_version}, current {BUILDER_VERSION}"
            )
        started = time.perf_counter()
        document = load_filing_document(cache_dir, pin.filing_ref())
        load_ms = _ms_since(started)
        if document is None:
            problems.append(f"{pin.key}: no valid cached copy (missing or failed integrity)")
            continue
        if document.source.content_sha256 != pin.content_sha256:
            problems.append(
                f"{pin.key}: cached SHA-256 {document.source.content_sha256}, "
                f"pinned {pin.content_sha256}"
            )
            continue
        if document.filing.filing_date != pin.filing_date:
            problems.append(f"{pin.key}: cached filing date {document.filing.filing_date}")
        started = time.perf_counter()
        extracted = extract_filing(document)
        extract_ms = _ms_since(started)
        started = time.perf_counter()
        built = build_passages(extracted)
        build_ms = _ms_since(started)
        extracted_by_key[pin.key] = extracted
        passages.extend(built)
        document_timings.append(
            DocumentTiming(
                document=pin.key,
                blocks=len(extracted.blocks),
                passages=len(built),
                load_ms=load_ms,
                extract_ms=extract_ms,
                build_passages_ms=build_ms,
            )
        )

    # 2. Evidence spans, located in the extracted text.
    pins = {pin.key: pin for pin in dataset.documents}
    resolved: dict[str, list[ResolvedEvidence]] = {}
    for question in dataset.questions:
        resolved[question.id] = []
        for number, evidence in enumerate(question.evidence, start=1):
            if evidence.document not in extracted_by_key:
                continue  # the document problem is already reported
            try:
                resolved[question.id].append(
                    resolve_evidence(
                        evidence, pins[evidence.document], extracted_by_key[evidence.document]
                    )
                )
            except ValueError as exc:
                problems.append(f"{question.id} evidence {number}: {exc}")
    if problems:
        raise EvaluationError(problems)

    # 3. One combined index; primary searches are unfiltered.
    started = time.perf_counter()
    index = KeywordIndex(passages)
    index_build_ms = _ms_since(started)

    primary: dict[str, list[SearchHit]] = {}
    search_ms: list[float] = []
    for round_number in range(search_repetitions + 1):  # round 0 is a warm-up
        for question in dataset.questions:
            started = time.perf_counter()
            hits = index.search(question.query, k=RESULTS_PER_SEARCH)
            elapsed = _ms_since(started)
            if round_number == 0:
                primary[question.id] = hits
                continue
            search_ms.append(elapsed)
            if hits != primary[question.id]:
                problems.append(f"{question.id}: search results differ between repetitions")

    # 4. Secondary filtered searches, checked against their filters independently.
    filtered: list[tuple[EvalQuestion, FilteredSearch, list[SearchHit]]] = []
    compliance_checked = 0
    for question in dataset.questions:
        for search in question.filtered_searches:
            accession = pins[search.document].accession_number if search.document else None
            arguments = {"section": search.section, "form": search.form}
            hits = index.search(
                question.query, k=RESULTS_PER_SEARCH, accession_number=accession, **arguments
            )
            if hits != index.search(
                question.query, k=RESULTS_PER_SEARCH, accession_number=accession, **arguments
            ):
                problems.append(f"{question.id}: filtered search results are not repeatable")
            for hit in hits:
                compliance_checked += 1
                filing = hit.passage.source.filing
                if (
                    (search.section is not None and hit.passage.section != search.section)
                    or (search.form is not None and filing.form != search.form)
                    or (accession is not None and filing.accession_number != accession)
                ):
                    problems.append(f"{question.id}: {hit.citation_id} violates filters {search}")
            if search.expect_empty and hits:
                problems.append(
                    f"{question.id}: {len(hits)} hits where none were expected {search}"
                )
            filtered.append((question, search, hits))

    # 5. Every distinct returned citation must verify against the cache.
    key_by_sha = {pin.content_sha256: pin.key for pin in dataset.documents}
    distinct: dict[str, SearchHit] = {}
    for hits in [*primary.values(), *(hits for _, _, hits in filtered)]:
        for hit in hits:
            distinct.setdefault(hit.citation_id, hit)
    verification_ms: list[float] = []
    by_document: dict[str, list[float]] = {}
    for citation_id, hit in distinct.items():
        started = time.perf_counter()
        result = verify_citation(citation_id, hit.passage, cache_dir)
        elapsed = _ms_since(started)
        verification_ms.append(elapsed)
        by_document.setdefault(key_by_sha[hit.passage.source.content_sha256], []).append(elapsed)
        if result.status != "verified":
            problems.append(f"citation {citation_id}: {result.status} ({result.detail})")
    if problems:
        raise EvaluationError(problems)

    # 6. Relevance metrics (misses are results, not errors).
    question_results = [
        _question_result(question, primary[question.id], resolved[question.id], pins)
        for question in dataset.questions
    ]
    filtered_results = []
    for question, search, hits in filtered:
        relevant = [any(covers(h.passage, e) for e in resolved[question.id]) for h in hits]
        scored = question.kind != "unanswerable" and not search.expect_empty
        filtered_results.append(
            FilteredResult(
                question_id=question.id,
                search=search,
                citation_ids=[h.citation_id for h in hits],
                hit_at_5=hit_at_k(relevant, METRIC_K) if scored else None,
                reciprocal_rank_at_10=reciprocal_rank(relevant, RESULTS_PER_SEARCH)
                if scored
                else None,
            )
        )

    answerable = [r for r in question_results if r.kind != "unanswerable"]
    return EvalReport(
        dataset_version=dataset.dataset_version,
        dataset_sha256=dataset_sha256,
        documents=dataset.documents,
        passages_indexed=len(index),
        relevance_threshold=RELEVANCE_THRESHOLD,
        overall=_aggregate(answerable),
        direct=_aggregate([r for r in answerable if r.kind == "direct"]),
        paraphrase=_aggregate([r for r in answerable if r.kind == "paraphrase"]),
        filtered=_aggregate_filtered([r for r in filtered_results if r.hit_at_5 is not None]),
        questions=question_results,
        filtered_searches=filtered_results,
        citations_verified=len(distinct),
        filter_compliance_checked=compliance_checked,
        timings=Timings(
            python=platform.python_version(),
            platform=platform.platform(),
            documents=document_timings,
            index_build_ms=index_build_ms,
            search=_summarize(search_ms),
            verification=_summarize(verification_ms),
            verification_by_document=[
                VerificationTiming(document=key, citations=len(times), total_ms=sum(times))
                for key, times in by_document.items()
            ],
        ),
    )


def _question_result(
    question: EvalQuestion,
    hits: list[SearchHit],
    evidence: list[ResolvedEvidence],
    pins: dict[str, EvalDocument],
) -> QuestionResult:
    relevant = [any(covers(h.passage, e) for e in evidence) for h in hits]
    top = [h.passage for h in hits[:METRIC_K]]
    answerable = question.kind != "unanswerable"
    single = [any(covers(p, e) for p in top) for e in evidence]
    combined = [combined_coverage(top, e) for e in evidence]
    expected = {(pins[s.document].content_sha256, s.section) for s in question.expected_sections}
    return QuestionResult(
        id=question.id,
        kind=question.kind,
        topic=question.topic,
        asks_for_amount=question.asks_for_amount,
        citation_ids=[h.citation_id for h in hits],
        scores=[h.score for h in hits],
        relevant_ranks=[rank for rank, r in enumerate(relevant, start=1) if r],
        hit_at_5=hit_at_k(relevant, METRIC_K) if answerable else None,
        reciprocal_rank_at_10=reciprocal_rank(relevant, RESULTS_PER_SEARCH) if answerable else None,
        labeled_evidence_recall_at_5=sum(single) / len(evidence) if answerable else None,
        combined_evidence_coverage_at_5=statistics.fmean(combined) if answerable else None,
        split_evidence=sum(
            1 for s, c in zip(single, combined, strict=True) if not s and c >= RELEVANCE_THRESHOLD
        ),
        section_precision_at_5=section_precision(hits[:METRIC_K], expected) if expected else None,
    )


def _aggregate(results: list[QuestionResult]) -> RelevanceMetrics:
    def mean(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return statistics.fmean(present) if present else None

    precision = [r.section_precision_at_5 for r in results if r.section_precision_at_5 is not None]
    return RelevanceMetrics(
        questions=len(results),
        hit_at_5=mean([r.hit_at_5 for r in results]),
        mrr_at_10=mean([r.reciprocal_rank_at_10 for r in results]),
        labeled_evidence_recall_at_5=mean([r.labeled_evidence_recall_at_5 for r in results]),
        combined_evidence_coverage_at_5=mean([r.combined_evidence_coverage_at_5 for r in results]),
        section_precision_at_5=mean(precision),
        section_precision_questions=len(precision),
    )


def _aggregate_filtered(results: list[FilteredResult]) -> RelevanceMetrics:
    return RelevanceMetrics(
        questions=len(results),
        hit_at_5=statistics.fmean([r.hit_at_5 for r in results]) if results else None,
        mrr_at_10=statistics.fmean([r.reciprocal_rank_at_10 for r in results]) if results else None,
        labeled_evidence_recall_at_5=None,
        combined_evidence_coverage_at_5=None,
        section_precision_at_5=None,  # filtered hits must match exactly; checked as integrity
        section_precision_questions=0,
    )


def _summarize(values_ms: list[float]) -> LatencySummary:
    if not values_ms:
        return LatencySummary(count=0, mean_ms=0.0, p50_ms=0.0, p95_ms=0.0, max_ms=0.0)
    ordered = sorted(values_ms)

    def percentile(p: float) -> float:  # nearest-rank method
        return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

    return LatencySummary(
        count=len(ordered),
        mean_ms=statistics.fmean(ordered),
        p50_ms=percentile(50),
        p95_ms=percentile(95),
        max_ms=ordered[-1],
    )


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000


# --- CLI ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.evaluation DATASET [--cache-dir DIR] [--output FILE]

    Exit codes: 0 evaluated, 2 evaluation invalid (integrity problems are printed).
    """
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.evaluation")
    parser.add_argument("dataset", type=Path)
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    parser.add_argument("--output", type=Path, help="write the full JSON report here")
    parser.add_argument("--repetitions", type=int, default=20, help="timed rounds per search")
    args = parser.parse_args(argv)

    try:
        report = run_evaluation(args.dataset, args.cache_dir, search_repetitions=args.repetitions)
    except EvaluationError as exc:
        print(f"Evaluation invalid: {exc}", file=sys.stderr)
        return 2
    if args.output:
        args.output.write_text(report.model_dump_json(indent=2) + "\n")

    def show(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    print(f"dataset {report.dataset_version} sha256={report.dataset_sha256}")
    for name in ("overall", "direct", "paraphrase", "filtered"):
        m: RelevanceMetrics = getattr(report, name)
        print(
            f"{name:<10} n={m.questions:<3} Hit@5={show(m.hit_at_5)} MRR@10={show(m.mrr_at_10)} "
            f"labeled-evidence recall@5={show(m.labeled_evidence_recall_at_5)} "
            f"combined coverage@5={show(m.combined_evidence_coverage_at_5)} "
            f"section precision@5={show(m.section_precision_at_5)}"
        )
    t = report.timings
    print(
        f"citations verified {report.citations_verified}; "
        f"search p50 {t.search.p50_ms:.2f} ms, p95 {t.search.p95_ms:.2f} ms; "
        f"verification p50 {t.verification.p50_ms:.1f} ms, max {t.verification.max_ms:.1f} ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

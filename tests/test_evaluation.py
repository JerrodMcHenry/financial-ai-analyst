"""Offline evaluation harness: metrics, evidence matching, integrity checks and the runner.

Everything runs against small fixtures in a temporary cache, with network access blocked.
"""

import hashlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst import evaluation
from financial_analyst.evaluation import (
    EvalDataset,
    EvaluationError,
    Evidence,
    ResolvedEvidence,
    combined_coverage,
    covers,
    evidence_overlap,
    hit_at_k,
    load_dataset,
    main,
    reciprocal_rank,
    resolve_evidence,
    run_evaluation,
    section_precision,
)
from financial_analyst.filing_cache import save_filing_document
from financial_analyst.filing_extractor import extract_filing
from financial_analyst.passages import (
    Passage,
    PassageSource,
    PassageVerification,
    verify_citations,
)
from financial_analyst.retrieval import SearchHit
from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata

FIXTURES = Path(__file__).parent / "fixtures"
DATASET = FIXTURES / "eval_sample.json"
FIXTURE_FILES = {
    "sample-10k": "filing_sample.htm",
    "sample-10q": "filing_10q_sample.htm",
    "sample-sectionless": "filing_sectionless_sample.htm",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def cache_document(cache_dir: Path, key: str, content: bytes | None = None) -> None:
    dataset, _ = load_dataset(DATASET)
    pin = next(d for d in dataset.documents if d.key == key)
    content = content if content is not None else (FIXTURES / FIXTURE_FILES[key]).read_bytes()
    ref = pin.filing_ref()
    source = SourceMetadata(
        source_url=ref.primary_document_url,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    save_filing_document(cache_dir, FilingDocument(filing=ref, content=content, source=source))


@pytest.fixture
def cache(tmp_path):
    cache_dir = tmp_path / "cache"
    for key in FIXTURE_FILES:
        cache_document(cache_dir, key)
    return cache_dir


def edited_dataset(tmp_path: Path, edit) -> Path:
    data = json.loads(DATASET.read_text())
    edit(data)
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(data))
    return path


# --- Metric functions (hand-calculated) --------------------------------------------------


def test_hit_at_k_and_reciprocal_rank():
    assert hit_at_k([False, False, True], 5) == 1.0
    assert reciprocal_rank([False, False, True], 10) == pytest.approx(1 / 3)
    sixth = [False] * 5 + [True]
    assert hit_at_k(sixth, 5) == 0.0  # rank 6 is outside the top five
    assert reciprocal_rank(sixth, 10) == pytest.approx(1 / 6)
    assert reciprocal_rank(sixth, 5) == 0.0
    assert hit_at_k([], 5) == 0.0 and reciprocal_rank([], 10) == 0.0
    assert reciprocal_rank([True, True], 10) == 1.0  # only the first relevant rank counts
    for k in (0, -1, True, 2.0):
        with pytest.raises(ValueError):
            hit_at_k([True], k)


SHA_A, SHA_B = "a" * 64, "b" * 64
EVIDENCE = ResolvedEvidence(document="a", content_sha256=SHA_A, block_index=3, start=10, end=60)


def passage(block_start, char_start, block_end, char_end, sha=SHA_A) -> Passage:
    filing = FilingRef(
        cik="0000320193",
        accession_number="0000320193-26-000026",
        form="10-K",
        filing_date="2026-11-03",
        report_date=None,
        acceptance_datetime=None,
        primary_document="aapl-20260926.htm",
        is_xbrl=True,
        is_inline_xbrl=True,
    )
    source = PassageSource(
        filing=filing, content_sha256=sha, extractor_version="2", builder_version="1"
    )
    return Passage(
        source=source,
        section="Item 1A",
        kind="text",
        block_start=block_start,
        char_start=char_start,
        block_end=block_end,
        char_end=char_end,
        text="x",  # matching uses only the source and range
    )


@pytest.mark.parametrize(
    ("candidate", "overlap", "relevant"),
    [
        (passage(3, 10, 3, 60), (10, 60), True),  # exact span boundaries
        (passage(3, 0, 3, 500), (10, 60), True),  # whole block
        (passage(2, 0, 4, 9), (10, 60), True),  # span inside a multi-block passage
        (passage(3, 0, 3, 50), (10, 50), True),  # 40 of 50 characters: exactly 80%
        (passage(3, 0, 3, 49), (10, 49), False),  # 39 of 50: below the threshold
        (passage(3, 59, 3, 300), (59, 60), False),  # one shared character never counts
        (passage(2, 0, 3, 10), (0, 0), False),  # ends where the span starts
        (passage(4, 0, 4, 60), (0, 0), False),  # unrelated block
        (passage(3, 10, 3, 60, sha=SHA_B), (0, 0), False),  # same range, other document
    ],
)
def test_relevance_requires_80_percent_of_the_span_in_one_passage(candidate, overlap, relevant):
    assert evidence_overlap(candidate, EVIDENCE) == overlap
    assert covers(candidate, EVIDENCE) is relevant


def test_split_evidence_counts_only_in_combined_coverage():
    # A long block cut at a sentence end inside the span: the cut drops one space (char 35).
    first, second = passage(3, 0, 3, 35), passage(3, 36, 3, 400)
    assert not covers(first, EVIDENCE) and not covers(second, EVIDENCE)
    assert combined_coverage([first, second], EVIDENCE) == pytest.approx(49 / 50)
    assert combined_coverage([first], EVIDENCE) == pytest.approx(25 / 50)
    assert combined_coverage([first, first], EVIDENCE) == pytest.approx(25 / 50)  # no double count
    assert combined_coverage([passage(4, 0, 4, 9)], EVIDENCE) == 0.0


def hit(p: Passage) -> SearchHit:
    return SearchHit(passage=p, score=1.0, matched_terms=["x"])


def test_section_precision_counts_every_returned_hit():
    def labeled(section, sha=SHA_A, block=0):
        return passage(block, 0, block, 5, sha=sha).model_copy(update={"section": section})

    hits = [
        hit(labeled("Item 1A", block=0)),
        hit(labeled("Item 1A", block=1)),
        hit(labeled(None, block=2)),  # sectionless passage: counted, not excluded
        hit(labeled("Item 1A", sha=SHA_B)),  # same label in another filing: not expected
        hit(labeled("Item 7", block=3)),
    ]
    assert section_precision(hits, {(SHA_A, "Item 1A")}) == pytest.approx(2 / 5)
    assert section_precision([], {(SHA_A, "Item 1A")}) is None


# --- Dataset schema ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d["questions"][0]["evidence"][0].update(document="nope"), "unknown documents"),
        (lambda d: d["questions"][1].update(id="q-products"), "question ids must be unique"),
        (lambda d: d["documents"][1].update(key="sample-10k"), "document keys must be unique"),
        (
            lambda d: d["questions"][6].update(evidence=d["questions"][0]["evidence"]),
            "unanswerable",
        ),
        (lambda d: d["questions"][0].update(evidence=[]), "at least one evidence"),
        (lambda d: d["questions"][0]["evidence"][0].update(snippet="too short"), "at least 20"),
        (lambda d: d["questions"][3]["filtered_searches"][0].clear(), "at least one filter"),
        (lambda d: d["questions"][0].update(answer="typo field"), "Extra inputs"),
        (lambda d: d.update(dataset_version="2"), "last changelog entry"),
        (lambda d: d["documents"][0].update(content_sha256="abc"), "content_sha256"),
    ],
)
def test_invalid_datasets_are_refused(tmp_path, cache, edit, message):
    with pytest.raises(EvaluationError, match=message):
        run_evaluation(edited_dataset(tmp_path, edit), cache)


def test_fixture_dataset_is_valid():
    dataset, sha = load_dataset(DATASET)
    assert isinstance(dataset, EvalDataset)
    assert sha == hashlib.sha256(DATASET.read_bytes()).hexdigest()


# --- Runner: relevance results -----------------------------------------------------------


@pytest.fixture
def report(cache):
    return run_evaluation(DATASET, cache, search_repetitions=2)


def test_runner_reports_relevance_per_question_and_aggregates(report):
    by_id = {q.id: q for q in report.questions}
    # Recorded ranks of the first relevant hit on the fixtures. q-products ranks 6th:
    # "design"/"manufacture" do not match "designs"/"manufactures" (no stemming), a miss
    # that is reported, not an error.
    assert {q.id: q.relevant_ranks for q in report.questions} == {
        "q-products": [6],
        "q-net-sales": [1],
        "q-political": [7],
        "q-10q-risk-updates": [3],
        "q-credit-losses": [1],
        "q-capital": [1],
        "q-oil-gas": [],
    }
    assert by_id["q-products"].hit_at_5 == 0.0
    assert by_id["q-products"].reciprocal_rank_at_10 == pytest.approx(1 / 6)
    assert by_id["q-net-sales"].asks_for_amount is True

    overall = report.overall
    assert overall.questions == 6  # answerable only
    assert overall.hit_at_5 == pytest.approx(4 / 6)
    assert overall.mrr_at_10 == pytest.approx((1 / 6 + 1 + 1 / 7 + 1 / 3 + 1 + 1) / 6)
    assert overall.labeled_evidence_recall_at_5 == pytest.approx(4 / 6)
    assert report.direct.questions == 4 and report.direct.hit_at_5 == pytest.approx(3 / 4)
    assert report.paraphrase.questions == 2 and report.paraphrase.hit_at_5 == pytest.approx(1 / 2)


def test_section_precision_is_computed_only_for_questions_with_expected_sections(report):
    by_id = {q.id: q for q in report.questions}
    assert by_id["q-net-sales"].section_precision_at_5 == pytest.approx(1 / 5)
    assert by_id["q-credit-losses"].section_precision_at_5 is None  # sectionless target
    assert report.overall.section_precision_questions == 4


def test_unanswerable_questions_are_reported_separately(report):
    oil = next(q for q in report.questions if q.id == "q-oil-gas")
    assert oil.hit_at_5 is None and oil.labeled_evidence_recall_at_5 is None
    assert len(oil.citation_ids) == len(oil.scores)  # what BM25 returned anyway, for review


def test_filtered_searches_are_secondary_and_checked(report):
    results = {(r.question_id, r.search.section): r for r in report.filtered_searches}
    assert results[("q-credit-losses", "Part I, Item 2")].citation_ids == []  # never broadened
    assert results[("q-credit-losses", None)].hit_at_5 == 1.0  # sectionless stays searchable
    assert results[("q-10q-risk-updates", "Part II, Item 1A")].hit_at_5 == 1.0
    assert report.filtered.questions == 2 and report.filtered.hit_at_5 == 1.0
    assert report.filter_compliance_checked > 0


def test_every_returned_citation_was_verified(report):
    returned = {c for q in report.questions for c in q.citation_ids}
    returned |= {c for f in report.filtered_searches for c in f.citation_ids}
    assert report.citations_verified == len(returned)


def test_runs_are_deterministic_apart_from_timings(cache):
    first = run_evaluation(DATASET, cache, search_repetitions=1).model_dump(exclude={"timings"})
    second = run_evaluation(DATASET, cache, search_repetitions=3).model_dump(exclude={"timings"})
    assert first == second


def test_timings_separate_search_from_verification(report):
    timings = report.timings
    assert timings.search.count == 2 * 7  # repetitions x questions, warm-up excluded
    assert timings.verification_ms > 0
    assert sum(v.citations for v in timings.verification_by_document) == report.citations_verified
    assert [d.document for d in timings.documents] == list(FIXTURE_FILES)
    assert all(d.extract_ms >= 0 and d.build_passages_ms >= 0 for d in timings.documents)
    assert timings.search.p50_ms <= timings.search.p95_ms <= timings.search.max_ms


# --- Runner: integrity failures ----------------------------------------------------------


def test_missing_document_is_refused(tmp_path):
    cache_dir = tmp_path / "partial"
    cache_document(cache_dir, "sample-10k")
    cache_document(cache_dir, "sample-10q")
    with pytest.raises(EvaluationError, match="sample-sectionless: no valid cached copy"):
        run_evaluation(DATASET, cache_dir)


def test_document_with_a_different_hash_is_refused(tmp_path):
    cache_dir = tmp_path / "changed"
    cache_document(cache_dir, "sample-10k")
    cache_document(cache_dir, "sample-10q")
    changed = (FIXTURES / "filing_sectionless_sample.htm").read_bytes() + b"\n"
    cache_document(cache_dir, "sample-sectionless", changed)
    with pytest.raises(EvaluationError, match="sample-sectionless: cached SHA-256"):
        run_evaluation(DATASET, cache_dir)


@pytest.mark.parametrize(
    ("field", "message"),
    [("extractor_version", "pinned extractor 9"), ("builder_version", "pinned builder 9")],
)
def test_version_pins_must_match_the_current_code(tmp_path, cache, field, message):
    path = edited_dataset(tmp_path, lambda d: d["documents"][0].update({field: "9"}))
    with pytest.raises(EvaluationError, match=message):
        run_evaluation(path, cache)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"snippet": "this text is not in the filing at all"}, "not in block 5"),
        ({"snippet": "Sample Company Inc. | 2026"}, "not in block 5"),  # exists elsewhere
        ({"block_index": 6}, "not in block 6"),
        ({"block_index": 99}, "block 99 does not exist"),
        ({"section": "Item 7"}, "labeled 'Item 7'"),
    ],
)
def test_unresolvable_evidence_is_refused(tmp_path, cache, change, message):
    path = edited_dataset(tmp_path, lambda d: d["questions"][0]["evidence"][0].update(change))
    with pytest.raises(EvaluationError, match=message):
        run_evaluation(path, cache)


def extracted_fixture(key: str, content: bytes | None = None):
    """A pin and the extraction of its fixture (or of `content`), without a cache."""
    dataset, _ = load_dataset(DATASET)
    pin = next(d for d in dataset.documents if d.key == key)
    content = content if content is not None else (FIXTURES / FIXTURE_FILES[key]).read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    ref = pin.filing_ref()
    source = SourceMetadata(
        source_url=ref.primary_document_url,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        http_status=200,
        content_sha256=sha,
    )
    document = FilingDocument(filing=ref, content=content, source=source)
    return pin.model_copy(update={"content_sha256": sha}), extract_filing(document)


def test_same_snippet_in_two_blocks_resolves_independently():
    # This prefix occurs in two blocks of the 10-Q fixture (market risk and risk factors).
    pin, extracted = extracted_fixture("sample-10q")
    snippet = "There have been no material changes to the"
    labels = [(10, "Part I, Item 3"), (17, "Part II, Item 1A")]
    resolved = [
        resolve_evidence(
            Evidence(document=pin.key, snippet=snippet, block_index=index, section=section),
            pin,
            extracted,
        )
        for index, section in labels
    ]
    assert [(r.block_index, r.start, r.end) for r in resolved] == [
        (10, 0, len(snippet)),
        (17, 0, len(snippet)),
    ]


def test_same_snippet_in_two_blocks_passes_the_runner(tmp_path, cache):
    evidence = {"document": "sample-10q", "snippet": "There have been no material changes to the",
                "block_index": 17, "section": "Part II, Item 1A"}  # fmt: skip
    path = edited_dataset(tmp_path, lambda d: d["questions"][3].update(evidence=[evidence]))
    report = run_evaluation(path, cache, search_repetitions=1)
    assert report.questions[3].id == "q-10q-risk-updates"


@pytest.mark.parametrize(
    ("repeated", "snippet"),
    [
        ("Revenue grew because demand rose. Revenue grew because demand rose.",
         "Revenue grew because demand rose."),
        # Overlapping occurrences at 0 and 3, which str.count would report as one.
        ("abcabcabcabcabcabcabcabcabc", "abcabcabcabcabcabcabcabc"),
    ],
)  # fmt: skip
def test_snippet_repeated_within_its_block_is_rejected(repeated, snippet):
    html = f"<html><body><p>Item 1. Business</p><p>{repeated}</p></body></html>"
    pin, extracted = extracted_fixture("sample-10k", html.encode())
    block = next(b for b in extracted.blocks if b.text == repeated)
    evidence = Evidence(
        document=pin.key, snippet=snippet, block_index=block.index, section=block.section
    )
    with pytest.raises(ValueError, match=f"more than once in block {block.index}"):
        resolve_evidence(evidence, pin, extracted)


def test_evidence_for_the_wrong_document_is_rejected():
    pin, extracted = extracted_fixture("sample-10k")
    good = Evidence(
        document="sample-10k",
        snippet="designs, manufactures and markets smartphones",
        block_index=5,
        section="Item 1",
    )
    assert resolve_evidence(good, pin, extracted).block_index == 5
    with pytest.raises(ValueError, match="evidence for sample-10q resolved against sample-10k"):
        resolve_evidence(good.model_copy(update={"document": "sample-10q"}), pin, extracted)
    # The pin names other bytes than the ones extracted.
    other_pin = pin.model_copy(update={"content_sha256": "0" * 64})
    with pytest.raises(ValueError, match="not from the pinned SHA-256"):
        resolve_evidence(good, other_pin, extracted)
    # Another document's extraction, under its own pin: the block holds different text.
    pin_10q, extracted_10q = extracted_fixture("sample-10q")
    with pytest.raises(ValueError, match="evidence for sample-10k resolved against sample-10q"):
        resolve_evidence(good, pin_10q, extracted_10q)


def test_evidence_pointing_at_another_document_in_the_dataset_is_refused(tmp_path, cache):
    # A real 10-K snippet labeled as 10-Q evidence: the 10-Q block doesn't contain it.
    path = edited_dataset(
        tmp_path, lambda d: d["questions"][0]["evidence"][0].update(document="sample-10q")
    )
    with pytest.raises(EvaluationError, match="q-products evidence 1: snippet is not in block"):
        run_evaluation(path, cache)


def test_all_integrity_problems_are_reported_together(tmp_path, cache):
    def edit(data):
        data["documents"][0]["builder_version"] = "9"
        data["questions"][4]["evidence"][0]["block_index"] = 4

    with pytest.raises(EvaluationError) as error:
        run_evaluation(edited_dataset(tmp_path, edit), cache)
    assert len(error.value.problems) == 2


def test_citation_that_fails_verification_invalidates_the_evaluation(cache, monkeypatch):
    def one_fails(pairs, cache_dir):
        results = verify_citations(pairs, cache_dir)
        failed = PassageVerification(
            status="text_changed", detail="simulated", citation_id=results[1].citation_id
        )
        return [results[0], failed, *results[2:]]

    monkeypatch.setattr(evaluation, "verify_citations", one_fails)
    with pytest.raises(EvaluationError) as error:
        run_evaluation(DATASET, cache)
    assert len(error.value.problems) == 1 and "text_changed (simulated)" in error.value.problems[0]


def test_all_returned_citations_are_verified_in_one_batch(cache, monkeypatch):
    batches = []

    def recording(pairs, cache_dir):
        batches.append([citation_id for citation_id, _ in pairs])
        return verify_citations(pairs, cache_dir)

    monkeypatch.setattr(evaluation, "verify_citations", recording)
    report = run_evaluation(DATASET, cache, search_repetitions=1)
    assert len(batches) == 1 and len(set(batches[0])) == len(batches[0])
    assert len(batches[0]) == report.citations_verified


def test_filter_violations_are_caught_independently_of_the_index(cache, monkeypatch):
    original = evaluation.KeywordIndex.search

    def ignores_sections(self, query, k=5, *, section=None, form=None, accession_number=None):
        return original(self, query, k, form=form, accession_number=accession_number)

    monkeypatch.setattr(evaluation.KeywordIndex, "search", ignores_sections)
    with pytest.raises(EvaluationError) as error:
        run_evaluation(DATASET, cache)
    assert any("violates filters" in p for p in error.value.problems)
    assert any("none were expected" in p for p in error.value.problems)


def test_non_deterministic_search_is_refused(cache, monkeypatch):
    original = evaluation.KeywordIndex.search
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        hits = original(self, *args, **kwargs)
        return list(reversed(hits)) if calls["n"] % 2 == 0 else hits

    monkeypatch.setattr(evaluation.KeywordIndex, "search", flaky)
    with pytest.raises(EvaluationError) as error:
        run_evaluation(DATASET, cache, search_repetitions=1)
    assert any("differ between repetitions" in p for p in error.value.problems)  # primary
    assert any("not repeatable" in p for p in error.value.problems)  # filtered


def test_relevance_misses_are_not_integrity_failures(tmp_path, cache):
    # Evidence for a query that will never retrieve it: a valid evaluation with a miss.
    path = edited_dataset(tmp_path, lambda d: d["questions"][0].update(query="zebra quantum"))
    result = run_evaluation(path, cache, search_repetitions=1)
    products = next(q for q in result.questions if q.id == "q-products")
    assert products.citation_ids == [] and products.hit_at_5 == 0.0


# --- CLI ---------------------------------------------------------------------------------


def test_cli_writes_report_and_summary(tmp_path, cache, capsys):
    output = tmp_path / "report.json"
    code = main(
        [str(DATASET), "--cache-dir", str(cache), "--output", str(output), "--repetitions", "1"]
    )
    assert code == 0
    assert json.loads(output.read_text())["overall"]["questions"] == 6
    assert "Hit@5=0.667" in capsys.readouterr().out


def test_cli_exits_2_on_integrity_failure(tmp_path, capsys):
    assert main([str(DATASET), "--cache-dir", str(tmp_path / "empty")]) == 2
    assert "no valid cached copy" in capsys.readouterr().err


# --- The real dataset (schema only; its filings are in the local cache, not in the repo) ---

REAL_DATASET = Path(__file__).parent.parent / "evals" / "retrieval_v1.json"


def test_real_dataset_shape():
    dataset, _ = load_dataset(REAL_DATASET)  # strict schema and cross-references
    assert len(dataset.documents) == 3
    assert len(dataset.questions) == 20
    kinds = [q.kind for q in dataset.questions]
    assert [kinds.count(k) for k in ("direct", "paraphrase", "unanswerable")] == [13, 5, 2]
    ids = [q.id for q in dataset.questions]
    assert len(set(ids)) == len(ids)
    keys = {d.key for d in dataset.documents}
    for question in dataset.questions:
        referenced = {e.document for e in question.evidence}
        referenced |= {s.document for s in question.expected_sections}
        referenced |= {f.document for f in question.filtered_searches if f.document}
        assert referenced <= keys, question.id
        assert question.topic in {"risk", "mda", "business", "accounting", "unanswerable"}
        if question.kind == "unanswerable":
            assert not question.evidence and not question.expected_sections, question.id

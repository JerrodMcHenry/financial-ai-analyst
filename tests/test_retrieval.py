"""Keyword retrieval and filing selection. In-memory only; nothing touches the network."""

import hashlib
import math
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst.filing_cache import save_filing_document
from financial_analyst.filing_extractor import extract_filing
from financial_analyst.passages import Passage, PassageSource, build_passages, verify_citation
from financial_analyst.retrieval import (
    BM25_B,
    BM25_K1,
    KeywordIndex,
    select_latest_filing,
    tokenize,
)
from financial_analyst.sec_models import (
    CompanySubmissions,
    FilingDocument,
    FilingRef,
    SourceMetadata,
)

SAMPLE_10Q = (Path(__file__).parent / "fixtures" / "filing_10q_sample.htm").read_bytes()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def make_filing(**overrides) -> FilingRef:
    fields = {
        "cik": "0000320193",
        "accession_number": "0000320193-26-000026",
        "form": "10-K",
        "filing_date": "2026-11-03",
        "report_date": "2026-09-26",
        "acceptance_datetime": None,
        "primary_document": "aapl-20260926.htm",
        "is_xbrl": True,
        "is_inline_xbrl": True,
    }
    return FilingRef(**(fields | overrides))


def make_passages(texts: list[str], section: str | None = None, **filing) -> list[Passage]:
    """One single-block passage per text, from one (made-up) filing."""
    source = PassageSource(
        filing=make_filing(**filing),
        content_sha256=hashlib.sha256(repr(filing).encode()).hexdigest(),
        extractor_version="2",
        builder_version="1",
    )
    return [
        Passage(
            source=source,
            section=section,
            kind="text",
            block_start=i,
            char_start=0,
            block_end=i,
            char_end=len(text),
            text=text,
        )
        for i, text in enumerate(texts)
    ]


def make_document(content: bytes, **filing) -> FilingDocument:
    ref = make_filing(**filing)
    source = SourceMetadata(
        source_url=ref.primary_document_url,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return FilingDocument(filing=ref, content=content, source=source)


# --- Tokenization ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("Net Sales ROSE", ["net", "sales", "rose"]),
        ("Revenue, net; (unaudited) -- total!", ["revenue", "net", "unaudited", "total"]),
        ("$416,161 million, up 3.5% from 2025", ["416161", "million", "up", "3.5", "2025"]),
        ("Items 1, 2", ["items", "1", "2"]),  # a comma and space separate numbers
        ("single-source, third-party 10-K/A", ["single", "source", "third", "party", "10", "k"]),
        ("FY2026 Q3", ["fy2026", "q3"]),
        ("Apple’s company's don't suppliers'", ["apple", "company", "dont", "suppliers"]),
        ("CAFÉ naïve Straße 中国", ["café", "naïve", "strasse", "中国"]),
        ("ＡＢＣ１ ﬁnancing costs", ["abc1", "financing", "costs"]),
        ("The risks of the Company are not limited", ["risks", "company", "not", "limited"]),
        ("", []),
        ("the and of to", []),
        ("--- ... ;;", []),
    ],
)
def test_tokenize(text, tokens):
    assert tokenize(text) == tokens


def test_tokenize_is_deterministic_and_does_not_stem():
    assert tokenize("Suppliers supplier supplied") == ["suppliers", "supplier", "supplied"]
    assert tokenize("Café") == tokenize("Café")  # composed and decomposed forms agree


# --- BM25 --------------------------------------------------------------------------------

CORPUS = [
    "Apple supply chain risk",
    "Apple apple revenue",
    "Supply supply supply chain constraints delay shipments",
    "Foreign currency exchange rates",
]


def bm25(term_counts: list[dict[str, int]], lengths: list[int], query: list[str]) -> list[float]:
    """Reference Okapi BM25, written out independently of the implementation."""
    n = len(lengths)
    average = sum(lengths) / n
    scores = []
    for counts, length in zip(term_counts, lengths, strict=True):
        score = 0.0
        for term in set(query):
            df = sum(1 for c in term_counts if term in c)
            if term not in counts:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            tf = counts[term]
            score += (
                idf * tf * (BM25_K1 + 1) / (tf + BM25_K1 * (1 - BM25_B + BM25_B * length / average))
            )
        scores.append(score)
    return scores


def test_bm25_scores_match_the_reference_formula():
    passages = make_passages(CORPUS)
    index = KeywordIndex(passages)
    tokenized = [tokenize(text) for text in CORPUS]
    counts = [{t: tokens.count(t) for t in tokens} for tokens in tokenized]
    expected = bm25(counts, [len(t) for t in tokenized], ["supply", "chain"])

    hits = index.search("supply chain", k=10)
    # Passage 2 says "supply" three times; that outweighs passage 0 being shorter.
    assert [h.passage.block_start for h in hits] == [2, 0]
    for hit in hits:
        assert hit.score == pytest.approx(expected[hit.passage.block_start], rel=1e-12)
    assert [h.matched_terms for h in hits] == [["chain", "supply"], ["chain", "supply"]]


def test_rarer_terms_and_shorter_passages_score_higher():
    index = KeywordIndex(make_passages(CORPUS))
    # "apple" is in 2 passages, "currency" in 1: the rarer term carries more weight.
    assert index.search("currency")[0].score > index.search("apple", k=1)[0].score
    # Same tf, the shorter passage wins: 4 tokens vs 7 tokens for "chain".
    assert [h.passage.block_start for h in index.search("chain")] == [0, 2]


def test_repeated_query_terms_count_once_and_word_order_does_not_matter():
    index = KeywordIndex(make_passages(CORPUS))
    base = index.search("supply chain", k=10)
    for query in ("supply supply chain", "chain supply", "SUPPLY, chain chain!"):
        assert index.search(query, k=10) == base  # identical scores, exactly


def test_only_matching_passages_are_returned_even_when_k_is_larger():
    index = KeywordIndex(make_passages(CORPUS))
    hits = index.search("currency", k=10)
    assert len(hits) == 1 and hits[0].score > 0
    assert index.search("blockchain", k=10) == []


def test_top_k_limits_results():
    index = KeywordIndex(make_passages(CORPUS))
    assert len(index.search("apple supply", k=1)) == 1
    assert len(index.search("apple supply", k=10)) == 3


@pytest.mark.parametrize("query", ["", "   ", "the of and", "--- !!!"])
def test_queries_without_searchable_terms_return_nothing(query):
    assert KeywordIndex(make_passages(CORPUS)).search(query) == []


@pytest.mark.parametrize("k", [0, -1, 1.5, True, "3", None])
def test_invalid_k_is_rejected(k):
    with pytest.raises(ValueError, match="k must be a positive integer"):
        KeywordIndex(make_passages(CORPUS)).search("apple", k=k)


def test_empty_index_and_duplicate_passages():
    assert len(KeywordIndex([])) == 0
    assert KeywordIndex([]).search("apple") == []
    passages = make_passages(CORPUS)
    with pytest.raises(ValueError, match="distinct"):
        KeywordIndex(passages + passages[:1])


# --- Ties and multiple filings -----------------------------------------------------------


def test_ties_break_by_document_identity_then_location():
    later = make_passages(["supply chain", "supply chain"], accession_number="0000320193-26-000099")
    earlier = make_passages(["supply chain"], accession_number="0000320193-25-000010")
    expected = [
        ("0000320193-25-000010", 0),
        ("0000320193-26-000099", 0),
        ("0000320193-26-000099", 1),
    ]
    for order in (later + earlier, earlier + later, list(reversed(later + earlier))):
        hits = KeywordIndex(order).search("supply chain", k=10)
        assert len({h.score for h in hits}) == 1  # a genuine tie
        assert [
            (h.passage.source.filing.accession_number, h.passage.block_start) for h in hits
        ] == expected


def test_search_spans_filings_and_filters_by_accession_and_exact_form():
    annual = make_passages(["supply chain risk"], form="10-K")
    amended = make_passages(
        ["supply chain risk restated"], form="10-K/A", accession_number="0000320193-26-000050"
    )
    quarterly = make_passages(
        ["supply chain update"], form="10-Q", accession_number="0000320193-26-000020"
    )
    index = KeywordIndex(annual + amended + quarterly)

    assert {h.passage.source.filing.form for h in index.search("supply", k=10)} == {
        "10-K",
        "10-K/A",
        "10-Q",
    }
    assert [h.passage for h in index.search("supply", k=10, form="10-K")] == annual
    assert [h.passage for h in index.search("supply", k=10, form="10-K/A")] == amended
    hits = index.search("supply", k=10, accession_number="0000320193-26-000020")
    assert [h.passage for h in hits] == quarterly
    assert index.search("supply", form="10-K", accession_number="0000320193-26-000020") == []


def test_filters_never_change_scores():
    index = KeywordIndex(
        make_passages(["supply chain", "supply"], form="10-K")
        + make_passages(
            ["supply chain delays"], form="10-Q", accession_number="0000320193-26-000020"
        )
    )
    unfiltered = {h.citation_id: h.score for h in index.search("supply chain", k=10)}
    for hit in index.search("supply chain", k=10, form="10-Q"):
        assert hit.score == unfiltered[hit.citation_id]


# --- Sections ----------------------------------------------------------------------------


@pytest.fixture
def section_index():
    """A labeled 10-Q (the fixture) and an unlabeled 10-Q from another filer."""
    labeled = build_passages(extract_filing(make_document(SAMPLE_10Q, form="10-Q")))
    unlabeled_html = (
        b"<html><body><p>Item 1A. Risk Factors.</p><p>There have been no material changes"
        b" to the risk factors.</p></body></html>"
    )
    unlabeled = build_passages(
        extract_filing(
            make_document(
                unlabeled_html,
                cik="0000019617",
                accession_number="0001628280-26-069523",
                form="10-Q",
                primary_document="jpm-20260930.htm",
            )
        )
    )
    assert all(p.section is None for p in unlabeled)
    return KeywordIndex(labeled + unlabeled), labeled, unlabeled


def test_section_filter_returns_only_exactly_labeled_passages(section_index):
    index, _, _ = section_index
    hits = index.search("risk factors material changes", k=10, section="Part II, Item 1A")
    assert hits and all(h.passage.section == "Part II, Item 1A" for h in hits)
    # Unqualified or differently written labels are not "close enough".
    assert index.search("risk factors", k=10, section="Item 1A") == []
    assert index.search("risk factors", k=10, section="part ii, item 1a") == []


def test_section_filter_is_never_broadened_to_unlabeled_passages(section_index):
    index, _, unlabeled = section_index
    jpm = unlabeled[0].source.filing.accession_number
    # The unlabeled filing matches the words, but has no section labels: nothing comes back.
    assert (
        index.search("risk factors", k=10, section="Part II, Item 1A", accession_number=jpm) == []
    )


def test_sectionless_filing_is_searchable_without_a_section_filter(section_index):
    index, _, unlabeled = section_index
    jpm = unlabeled[0].source.filing.accession_number
    hits = index.search("risk factors", k=10, accession_number=jpm)
    assert [h.passage for h in hits] == unlabeled
    assert {h.passage.source.filing.cik for h in index.search("risk factors", k=10)} == {
        "0000320193",
        "0000019617",
    }


# --- Citation identity -------------------------------------------------------------------


def test_hits_carry_the_unchanged_passage_and_its_citation_id(tmp_path):
    document = make_document(SAMPLE_10Q, form="10-Q")
    passages = build_passages(extract_filing(document))
    snapshot = [p.model_dump() for p in passages]
    hit = KeywordIndex(passages).search("repurchased shares", k=1)[0]

    assert hit.passage in passages
    assert hit.citation_id == hit.passage.citation_id
    assert hit.model_dump()["citation_id"] == hit.passage.citation_id
    assert [p.model_dump() for p in passages] == snapshot  # indexing changed nothing
    # A search hit is citable exactly like the passage itself.
    save_filing_document(tmp_path, document)
    assert verify_citation(hit.citation_id, hit.passage, tmp_path).status == "verified"


# --- Filing selection --------------------------------------------------------------------


def make_submissions(filings: list[FilingRef]) -> CompanySubmissions:
    return CompanySubmissions(
        cik="0000320193",
        name="Apple Inc.",
        tickers=["AAPL"],
        exchanges=["Nasdaq"],
        sic=None,
        sic_description=None,
        fiscal_year_end="0926",
        recent_filings=filings,
        has_more_filings=False,
        source=SourceMetadata(
            source_url="https://data.sec.gov/submissions/CIK0000320193.json",
            retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
            http_status=200,
            content_sha256="0" * 64,
        ),
    )


def filing(accession: str, form: str, filed: str, report: str | None = None, accepted=None):
    return make_filing(
        accession_number=accession,
        form=form,
        filing_date=filed,
        report_date=report,
        acceptance_datetime=accepted,
    )


def test_latest_filing_uses_filing_date_and_excludes_amendments_by_default():
    filings = [
        filing("0000320193-25-000079", "10-K", "2025-10-31", report="2025-09-27"),
        filing("0000320193-26-000026", "10-K", "2026-11-03", report="2026-09-26"),
        filing("0000320193-26-000090", "10-K/A", "2026-12-15", report="2026-09-26"),
        filing("0000320193-26-000020", "10-Q", "2026-08-01", report="2026-06-27"),
        # Filed earlier, but reports a later period: report dates are not used.
        filing("0000320193-24-000001", "10-K", "2024-11-01", report="2030-01-01"),
        filing("0000320193-26-000030", "8-K", "2026-12-20"),
    ]
    submissions = make_submissions(filings)
    assert select_latest_filing(submissions).accession_number == "0000320193-26-000026"
    amended = select_latest_filing(submissions, include_amendments=True)
    assert amended.accession_number == "0000320193-26-000090"
    assert select_latest_filing(submissions, "10-Q").accession_number == "0000320193-26-000020"


def test_latest_filing_ties_break_by_acceptance_time_then_accession():
    morning = datetime(2026, 11, 3, 9, tzinfo=UTC)
    evening = datetime(2026, 11, 3, 18, tzinfo=UTC)
    same_day = [
        filing("0000320193-26-000099", "10-Q", "2026-11-03", accepted=morning),
        filing("0000320193-26-000010", "10-Q", "2026-11-03", accepted=evening),
        filing("0000320193-26-000050", "10-Q", "2026-11-03", accepted=None),
    ]
    for order in (same_day, list(reversed(same_day))):
        chosen = select_latest_filing(make_submissions(order), "10-Q")
        assert chosen.accession_number == "0000320193-26-000010"

    no_times = [
        filing("0000320193-26-000010", "10-Q", "2026-11-03"),
        filing("0000320193-26-000099", "10-Q", "2026-11-03"),
    ]
    chosen = select_latest_filing(make_submissions(no_times), "10-Q")
    assert chosen.accession_number == "0000320193-26-000099"


def test_latest_filing_none_and_invalid_form():
    submissions = make_submissions([filing("0000320193-26-000090", "10-K/A", "2026-12-15")])
    assert select_latest_filing(submissions) is None  # only an amendment exists
    assert select_latest_filing(make_submissions([]), "10-Q") is None
    for form in ("10-K/A", "8-K", "10-k"):
        with pytest.raises(ValueError, match="form must be one of"):
            select_latest_filing(submissions, form)

"""Semantic retrieval and Reciprocal Rank Fusion. Offline; most tests use a stand-in embedder.

Tests marked with the real model run only when the pinned model is installed locally
(python -m financial_analyst.semantic_retrieval install); they never download it.
"""

import hashlib
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="needs the 'semantic' extra")

from financial_analyst import evaluation  # noqa: E402
from financial_analyst import semantic_retrieval as semantic  # noqa: E402
from financial_analyst.evaluation import EvaluationError, load_dataset, run_evaluation  # noqa: E402
from financial_analyst.filing_cache import save_filing_document  # noqa: E402
from financial_analyst.filing_extractor import extract_filing  # noqa: E402
from financial_analyst.passages import Passage, build_passages, verify_citations  # noqa: E402
from financial_analyst.retrieval import KeywordIndex  # noqa: E402
from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata  # noqa: E402
from financial_analyst.semantic_retrieval import (  # noqa: E402
    HybridIndex,
    ModelIntegrityError,
    SemanticIndex,
    fuse_rrf,
    verify_model_files,
)

FIXTURES = Path(__file__).parent / "fixtures"
DATASET = FIXTURES / "eval_sample.json"
FIXTURE_FILES = {
    "sample-10k": "filing_sample.htm",
    "sample-10q": "filing_10q_sample.htm",
    "sample-sectionless": "filing_sectionless_sample.htm",
}


def document(content: bytes, form: str, accession: str, primary: str) -> FilingDocument:
    filing = FilingRef(
        cik="0000320193",
        accession_number=accession,
        form=form,
        filing_date="2026-11-03",
        report_date=None,
        acceptance_datetime=None,
        primary_document=primary,
        is_xbrl=True,
        is_inline_xbrl=True,
    )
    source = SourceMetadata(
        source_url=filing.primary_document_url,
        retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return FilingDocument(filing=filing, content=content, source=source)


def tenk(content: bytes) -> FilingDocument:
    return document(content, "10-K", "0000320193-26-000026", "aapl-20260926.htm")


def tenq() -> FilingDocument:
    content = (FIXTURES / "filing_10q_sample.htm").read_bytes()
    return document(content, "10-Q", "0000320193-26-000020", "aapl-20260627.htm")


def html(*paragraphs: str) -> bytes:
    """'#Text' is a bold heading; anything else a paragraph."""
    parts = [
        f'<div><span style="font-weight:700">{p[1:]}</span></div>'
        if p.startswith("#")
        else f"<div><span>{p}</span></div>"
        for p in paragraphs
    ]
    return f"<html><body>{''.join(parts)}</body></html>".encode()


SAMPLE = (FIXTURES / "filing_sample.htm").read_bytes()

# A stand-in embedding: each concept is one dimension, and synonyms share it, so "vendor"
# and "supplier" are close while sharing no word. Unknown words add a small, fixed
# per-word component, so texts are never all-zero.
CONCEPTS = {
    "supplier": 0, "suppliers": 0, "vendor": 0, "vendors": 0, "components": 0,
    "credit": 1, "loan": 1, "loans": 1, "borrowers": 1, "allowance": 1,
    "tax": 2, "taxes": 2, "levy": 2,
    "risk": 3, "risks": 3, "hazard": 3,
}  # fmt: skip
DIM = 16


def fake_embed(texts: list[str]) -> np.ndarray:
    rows = []
    for text in texts:
        vector = np.zeros(DIM, dtype=np.float32)
        for word in text.lower().replace(".", " ").replace(",", " ").split():
            if word in CONCEPTS:
                vector[CONCEPTS[word]] += 1.0
            else:
                digest = hashlib.sha256(word.encode()).digest()
                vector[4 + digest[0] % (DIM - 4)] += 0.1
        rows.append(vector / np.linalg.norm(vector))
    return np.asarray(rows, dtype=np.float32)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def build(content: bytes) -> list[Passage]:
    return build_passages(extract_filing(tenk(content)))


@pytest.fixture
def topical():
    """Four passages about different topics, in one filing (each heading starts a passage)."""
    passages = build(
        html(
            "#Supply",
            "The Company relies on suppliers for components.",
            "#Credit",
            "The allowance covers expected credit losses on loans.",
            "#Taxes",
            "Changes in tax law could raise taxes.",
            "#Security",
            "Cybersecurity risks affect the business.",
        )
    )
    assert len(passages) == 4
    return passages


@pytest.fixture
def corpus():
    """Real passages from the 10-K and 10-Q fixtures: two filings, sections, a table."""
    return build(SAMPLE) + build_passages(extract_filing(tenq()))


# --- Semantic ranking ---------------------------------------------------------------------


def test_semantic_ranking_finds_meaning_without_shared_words(topical):
    index = SemanticIndex(topical, fake_embed)
    hits = index.search("vendor concentration", k=4)
    assert hits[0].passage is topical[0]  # "suppliers": no word in common with the query
    assert KeywordIndex(topical).search("vendor concentration") == []  # BM25 finds nothing
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].score == pytest.approx(float(fake_embed(["vendor concentration"])[0] @
                                                 fake_embed([topical[0].text])[0]))  # fmt: skip


def test_equal_scores_break_by_document_position():
    passages = build(html("#Supply", "Suppliers matter.", "#Supply", "Suppliers matter."))
    assert len(passages) == 2 and passages[0].text == passages[1].text
    hits = SemanticIndex(passages, fake_embed).search("vendors", k=2)
    assert hits[0].score == hits[1].score
    assert [h.passage.block_start for h in hits] == sorted(h.passage.block_start for h in hits)


def test_ranking_is_repeatable_and_fingerprinted(corpus):
    first, second = SemanticIndex(corpus, fake_embed), SemanticIndex(corpus, fake_embed)
    assert first.fingerprint() == second.fingerprint()
    assert first.search("tax risk", k=10) == first.search("tax risk", k=10)
    assert first.search("tax risk", k=10) == second.search("tax risk", k=10)
    other = SemanticIndex(corpus[:-1], fake_embed)
    assert other.fingerprint() != first.fingerprint()


@pytest.mark.parametrize(
    "filters",
    [
        {"section": "Item 1A"},
        {"section": "Part II, Item 1A"},
        {"form": "10-Q"},
        {"accession_number": "0000320193-26-000026"},
        {"section": "Item 1A", "form": "10-K"},
    ],
)
def test_filters_are_exact_and_the_same_as_bm25(corpus, filters):
    index = SemanticIndex(corpus, fake_embed)
    hits = index.search("risk", k=len(corpus), **filters)

    def allowed(p: Passage) -> bool:
        filing = p.source.filing
        return (
            filters.get("section", p.section) == p.section
            and filters.get("form", filing.form) == filing.form
            and filters.get("accession_number", filing.accession_number) == filing.accession_number
        )

    expected = {p.citation_id for p in corpus if allowed(p)}
    assert expected and {h.citation_id for h in hits} == expected
    # BM25 never returns a passage the same filters exclude.
    assert {h.citation_id for h in KeywordIndex(corpus).search("risk", 50, **filters)} <= expected


def test_empty_results_for_unmatched_filters_and_blank_queries(corpus):
    for index in (
        SemanticIndex(corpus, fake_embed),
        HybridIndex(SemanticIndex(corpus, fake_embed)),
    ):
        assert index.search("risk", section="Item 99") == []
        assert index.search("risk", form="10-K/A") == []  # never broadened to "10-K"
        assert index.search("   ") == []
        for k in (0, -1, True, 2.5):
            with pytest.raises(ValueError):
                index.search("risk", k=k)


def test_duplicate_passages_are_rejected(topical):
    with pytest.raises(ValueError, match="distinct"):
        SemanticIndex(topical + topical[:1], fake_embed)


# --- Reciprocal Rank Fusion -----------------------------------------------------------------


def test_rrf_scores_are_hand_calculated(topical):
    a, b, c, d = topical
    fused = fuse_rrf([a, b, c], [b, d], k=60)
    assert [h.passage for h in fused] == [b, a, d, c]
    assert fused[0].score == 1 / 62 + 1 / 61  # b: rank 2 in BM25, rank 1 in semantic
    assert (fused[0].bm25_rank, fused[0].semantic_rank) == (2, 1)
    assert fused[1].score == 1 / 61 and (fused[1].bm25_rank, fused[1].semantic_rank) == (1, None)
    assert fused[2].score == 1 / 62 and (fused[2].bm25_rank, fused[2].semantic_rank) == (None, 2)
    assert fused[3].score == 1 / 63


def test_rrf_ties_break_by_document_position_not_by_ranker(topical):
    a, b, c, _ = topical
    # c is first in BM25 and a first in semantic: equal scores, so document order decides.
    assert [h.passage for h in fuse_rrf([c], [a])] == [a, c]
    assert [h.passage for h in fuse_rrf([a], [c])] == [a, c]
    assert fuse_rrf([], []) == []


def test_hybrid_uses_both_rankers_with_the_same_filters(corpus):
    hybrid = HybridIndex(SemanticIndex(corpus, fake_embed))
    hits = hybrid.search("tax risk", k=5, form="10-Q")
    assert hits and all(h.passage.source.filing.form == "10-Q" for h in hits)
    assert all(h.bm25_rank is not None or h.semantic_rank is not None for h in hits)
    assert hybrid.search("tax risk", k=5, form="10-Q") == hits
    # Something only the semantic ranker finds still reaches the fused list.
    topical_hybrid = HybridIndex(
        SemanticIndex(build(html("Suppliers matter.", "Tax.")), fake_embed)
    )
    found = topical_hybrid.search("vendors", k=1)
    assert found[0].bm25_rank is None and found[0].semantic_rank == 1


# --- Citation identity --------------------------------------------------------------------


def test_hits_are_the_indexed_passages_and_their_citations_verify(tmp_path):
    documents = [tenk(SAMPLE), tenq()]
    passages = []
    for filing_document in documents:
        save_filing_document(tmp_path, filing_document)
        passages += build_passages(extract_filing(filing_document))
    semantic_index = SemanticIndex(passages, fake_embed)
    hits = semantic_index.search("tax risk", k=10) + HybridIndex(semantic_index).search(
        "tax risk", k=10
    )
    by_id = {p.citation_id: p for p in passages}
    for hit in hits:
        assert hit.passage is by_id[hit.citation_id]  # the same object, not a copy
    results = verify_citations([(h.citation_id, h.passage) for h in hits], tmp_path)
    assert {r.status for r in results} == {"verified"}


# --- The pinned model -----------------------------------------------------------------------


@pytest.fixture
def pinned_files(tmp_path, monkeypatch):
    """Two small files pinned in place of the real model files."""
    contents = {"onnx/model.onnx": b"weights", "tokenizer.json": b"{}"}
    pins = {name: (len(data), hashlib.sha256(data).hexdigest()) for name, data in contents.items()}
    monkeypatch.setattr(semantic, "MODEL_FILES", pins)
    return contents


def write_files(directory: Path, contents: dict[str, bytes]) -> None:
    for name, data in contents.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)


def test_model_files_must_match_their_pins(tmp_path, pinned_files):
    write_files(tmp_path, pinned_files)
    assert verify_model_files(tmp_path) == semantic.model_fingerprint()

    (tmp_path / "tokenizer.json").write_bytes(b"{ }")
    with pytest.raises(ModelIntegrityError, match="tokenizer.json: SHA-256"):
        verify_model_files(tmp_path)
    (tmp_path / "onnx/model.onnx").unlink()
    with pytest.raises(ModelIntegrityError, match="onnx/model.onnx: missing"):
        verify_model_files(tmp_path)


def test_install_moves_only_verified_files_into_place(tmp_path, pinned_files, monkeypatch):
    import huggingface_hub

    served = dict(pinned_files)
    requests = []

    def fake_download(repo_id, filename, *, revision, local_dir):
        requests.append((repo_id, filename, revision))
        write_files(Path(local_dir), {filename: served[filename]})
        return str(Path(local_dir) / filename)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    model_dir = tmp_path / "models" / "pinned"
    assert semantic.install_model(model_dir) == semantic.model_fingerprint()
    assert {r[2] for r in requests} == {semantic.MODEL_REVISION}  # never a floating revision
    assert verify_model_files(model_dir)

    served["tokenizer.json"] = b"tampered"
    with pytest.raises(ModelIntegrityError):
        semantic.install_model(model_dir)
    assert verify_model_files(model_dir)  # the verified install was left untouched


@pytest.fixture
def cache(tmp_path):
    """The evaluation fixture dataset's three filings, cached."""
    dataset, _ = load_dataset(DATASET)
    cache_dir = tmp_path / "cache"
    for pin in dataset.documents:
        content = (FIXTURES / FIXTURE_FILES[pin.key]).read_bytes()
        ref = pin.filing_ref()
        source = SourceMetadata(
            source_url=ref.primary_document_url,
            retrieved_at=datetime(2026, 11, 12, tzinfo=UTC),
            http_status=200,
            content_sha256=hashlib.sha256(content).hexdigest(),
        )
        save_filing_document(cache_dir, FilingDocument(filing=ref, content=content, source=source))
    return cache_dir


def test_evaluation_refuses_a_missing_or_changed_model(tmp_path, cache):
    for retriever in ("semantic", "hybrid"):
        with pytest.raises(EvaluationError, match="do not match BAAI/bge-small-en-v1.5"):
            run_evaluation(DATASET, cache, retriever=retriever, model_dir=tmp_path / "none")


# --- Evaluation integration (stand-in model) ------------------------------------------------


class FakeEmbedder:
    """Stands in for LocalEmbedder in the harness. Texts over 1,500 characters "truncate"."""

    fingerprint = "f" * 64
    counted: list[str] = []

    def __init__(self, model_dir):
        pass

    embed = staticmethod(fake_embed)

    @classmethod
    def token_counts(cls, texts):
        cls.counted = list(texts)
        return [600 if len(text) > 1500 else 20 for text in texts]


@pytest.mark.parametrize("retriever", ["semantic", "hybrid"])
def test_evaluation_records_the_retriever_and_stays_deterministic(cache, monkeypatch, retriever):
    monkeypatch.setattr(semantic, "LocalEmbedder", FakeEmbedder)
    report = run_evaluation(DATASET, cache, search_repetitions=2, retriever=retriever)
    config = report.retriever
    assert config.name == retriever
    assert config.model_revision == semantic.MODEL_REVISION
    assert config.model_fingerprint == "f" * 64
    assert (config.rrf_k, config.rrf_depth) == ((60, 50) if retriever == "hybrid" else (None, None))
    assert len(config.embedding_sha256) == 64
    assert len(FakeEmbedder.counted) == report.passages_indexed
    assert config.passages_truncated == sum(len(t) > 1500 for t in FakeEmbedder.counted)
    assert report.timings.model_load_ms is not None
    assert report.citations_verified > 0  # every returned citation verified, or no report
    again = run_evaluation(DATASET, cache, search_repetitions=1, retriever=retriever)
    assert again.model_dump(exclude={"timings"}) == report.model_dump(exclude={"timings"})


def test_bm25_remains_the_default_and_records_no_model(cache):
    report = run_evaluation(DATASET, cache, search_repetitions=1)
    assert report.retriever == evaluation.RetrieverConfig(name="bm25")
    assert report.timings.model_load_ms is None


# --- The real pinned model (only when installed locally; never downloaded here) -------------

real_model = pytest.mark.skipif(
    not semantic.DEFAULT_MODEL_DIR.is_dir(), reason="pinned model not installed locally"
)


@real_model
def test_real_model_embeds_offline_deterministically_and_counts_truncation():
    embedder = semantic.LocalEmbedder()  # sockets are blocked by the autouse fixture
    texts = ["Who builds Apple's devices?", "word " * 700]
    vectors = embedder.embed(texts)
    assert vectors.shape == (2, semantic.EMBEDDING_DIM) and vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert np.array_equal(vectors, embedder.embed(texts))
    counts = embedder.token_counts(texts)
    assert counts[0] < semantic.MAX_TOKENS < counts[1]
    assert embedder.fingerprint == semantic.model_fingerprint()


@real_model
def test_real_model_ranks_a_paraphrase_above_an_unrelated_passage():
    passages = build(
        html(
            "The Company relies on outsourcing partners in Asia for final assembly.",
            "The allowance for credit losses increased in the wholesale portfolio.",
        )
    )
    index = SemanticIndex(passages, semantic.LocalEmbedder().embed)
    assert index.search("Who builds the company's devices?", k=1)[0].passage is passages[0]

"""Local semantic retrieval, and Reciprocal Rank Fusion with BM25, over filing passages.

An experiment against the BM25 baseline (D-028, D-029). Both indexes rank the same, unchanged
Passage objects, so every hit keeps its source metadata and citation id, and citations are
verified exactly as for BM25. Embeddings only rank passages; they are never evidence.

The embedding model is BAAI/bge-small-en-v1.5 at a pinned commit, run on CPU with FastEmbed
and ONNX Runtime from a local directory whose files are checked against the SHA-256 values
below before every load. Loading and searching never touch the network; only `install_model`
downloads, and it verifies what it downloaded the same way. Needs the "semantic" extra.
"""

import argparse
import hashlib
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, computed_field

from financial_analyst.passages import Passage
from financial_analyst.retrieval import KeywordIndex

MODEL_REPO = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
MODEL_FILE = "onnx/model.onnx"  # BAAI's own fp32 export, not FastEmbed's default fp16 copy
# Every file the model needs at MODEL_REVISION: exact size and SHA-256. Checked against
# Hugging Face's published LFS hash (the ONNX file) and git blob ids (the rest) on download.
MODEL_FILES: dict[str, tuple[int, str]] = {
    "onnx/model.onnx": (
        133_093_490,
        "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
    ),
    "tokenizer.json": (
        711_396,
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    ),
    "tokenizer_config.json": (
        366,
        "9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3",
    ),
    "special_tokens_map.json": (
        125,
        "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    ),
    "config.json": (
        743,
        "094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750",
    ),
}
EMBEDDING_DIM = 384
MAX_TOKENS = 512  # the model's limit; longer passages are truncated (right side) and counted
DEFAULT_MODEL_DIR = Path("data/models/BAAI--bge-small-en-v1.5") / MODEL_REVISION
# Fixed so that vectors are reproducible: thread count and batch composition can change the
# order of floating-point sums. No query instruction prefix is used (fixed in D-029).
THREADS = 1
BATCH_SIZE = 32

RRF_K = 60  # the constant from the original RRF paper; fixed, not tuned (D-028)
RRF_DEPTH = 50  # hits taken from each ranker before fusion

# The name FastEmbed knows this exact model by. Distinct from its built-in
# "BAAI/bge-small-en-v1.5", which would load a different (fp16) file.
_FASTEMBED_NAME = f"{MODEL_REPO}@{MODEL_REVISION[:12]}-fp32"


class ModelIntegrityError(Exception):
    """The local model files are missing or differ from the pinned ones."""


class RankedHit(BaseModel):
    """One ranked passage. `passage` is the indexed Passage itself, unchanged."""

    model_config = ConfigDict(frozen=True)

    passage: Passage
    score: float  # cosine similarity (semantic) or RRF score (hybrid); comparable within a run
    bm25_rank: int | None = None  # 1-based rank in each input ranking, hybrid only
    semantic_rank: int | None = None

    @computed_field
    @property
    def citation_id(self) -> str:
        return self.passage.citation_id


# --- The pinned model --------------------------------------------------------------------


def verify_model_files(model_dir: Path) -> str:
    """Check every pinned file's size and SHA-256. Returns a fingerprint of the whole set.

    Raises ModelIntegrityError naming each missing or different file.
    """
    problems = []
    for name, (size, sha256) in MODEL_FILES.items():
        path = model_dir / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stat().st_size != size or actual != sha256:
            problems.append(f"{name}: SHA-256 {actual}, pinned {sha256}")
    if problems:
        raise ModelIntegrityError(
            f"model files in {model_dir} do not match {MODEL_REPO}@{MODEL_REVISION}: "
            + "; ".join(problems)
        )
    return model_fingerprint()


def model_fingerprint() -> str:
    """SHA-256 over the pinned file names and hashes: one id for the exact model files."""
    lines = "".join(f"{name}\t{sha}\n" for name, (_, sha) in sorted(MODEL_FILES.items()))
    return hashlib.sha256(lines.encode()).hexdigest()


def install_model(model_dir: Path = DEFAULT_MODEL_DIR) -> str:
    """Download the pinned files from Hugging Face, verify them, then move them into place.

    The only function here that uses the network. Nothing reaches `model_dir` unless every
    file matches its pinned size and hash. Returns the model fingerprint.
    """
    from huggingface_hub import hf_hub_download

    model_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=model_dir.parent) as staging:
        for name in MODEL_FILES:
            hf_hub_download(MODEL_REPO, name, revision=MODEL_REVISION, local_dir=staging)
        fingerprint = verify_model_files(Path(staging))
        target = model_dir.parent / f".{model_dir.name}.installing"
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(staging, target, ignore=shutil.ignore_patterns(".cache"))
        shutil.rmtree(model_dir, ignore_errors=True)  # only ever replaced by verified files
        target.rename(model_dir)
    return fingerprint


class LocalEmbedder:
    """The pinned model, loaded from a verified local directory. Never downloads."""

    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        self.fingerprint = verify_model_files(model_dir)
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType
        from tokenizers import Tokenizer

        if not any(m["model"] == _FASTEMBED_NAME for m in TextEmbedding.list_supported_models()):
            # BGE's sentence embedding is the [CLS] token's final hidden state, L2-normalized.
            TextEmbedding.add_custom_model(
                model=_FASTEMBED_NAME,
                pooling=PoolingType.CLS,
                normalization=True,
                sources=ModelSource(hf=MODEL_REPO),  # never used: specific_model_path is set
                dim=EMBEDDING_DIM,
                model_file=MODEL_FILE,
            )
        self._model = TextEmbedding(
            model_name=_FASTEMBED_NAME,
            specific_model_path=str(model_dir),
            threads=THREADS,
            providers=["CPUExecutionProvider"],
        )
        # A second tokenizer, without truncation, only to count tokens.
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()

    def embed(self, texts: list[str]) -> np.ndarray:
        """Unit-length float32 vectors, one row per text, in order."""
        vectors = list(self._model.embed(texts, batch_size=BATCH_SIZE))
        return np.asarray(vectors, dtype=np.float32).reshape(len(texts), EMBEDDING_DIM)

    def token_counts(self, texts: list[str]) -> list[int]:
        """Tokens per text, including [CLS] and [SEP], before truncation."""
        return [len(encoding.ids) for encoding in self._tokenizer.encode_batch(texts)]


# --- Semantic index ----------------------------------------------------------------------


class SemanticIndex:
    """Exact cosine-similarity search over a fixed list of passages.

    `embed` maps texts to unit-length float32 vectors (LocalEmbedder.embed, or a stand-in in
    tests). Filters behave exactly as in KeywordIndex.search: exact matches, applied before
    ranking, never broadened. Equal scores break ties as in BM25: document identity, then
    position in the document.
    """

    def __init__(self, passages: list[Passage], embed: Callable[[list[str]], np.ndarray]) -> None:
        ids = [p.citation_id for p in passages]
        if len(set(ids)) != len(ids):
            raise ValueError("passages must be distinct; a citation id appears more than once")
        self._passages = list(passages)
        self._embed = embed
        matrix = embed([p.text for p in self._passages]) if passages else np.empty((0, 0))
        self._matrix = np.ascontiguousarray(matrix, dtype=np.float32)

    def __len__(self) -> int:
        return len(self._passages)

    @property
    def passages(self) -> list[Passage]:
        return list(self._passages)

    def fingerprint(self) -> str:
        """SHA-256 of the citation ids and the exact vector bytes, in index order."""
        digest = hashlib.sha256()
        for citation_id in (p.citation_id for p in self._passages):
            digest.update(citation_id.encode() + b"\n")
        digest.update(self._matrix.tobytes())
        return digest.hexdigest()

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        section: str | None = None,
        form: str | None = None,
        accession_number: str | None = None,
    ) -> list[RankedHit]:
        """The top `k` passages by cosine similarity, best first. [] for a blank query.
        Raises ValueError if k is not a positive integer."""
        _check_k(k)
        allowed = [
            position
            for position, passage in enumerate(self._passages)
            if _allowed(passage, section, form, accession_number)
        ]
        if not query.strip() or not allowed:
            return []
        query_vector = self._embed([query])[0].astype(np.float32)
        scores = self._matrix[allowed] @ query_vector
        ranked = sorted(
            zip(allowed, scores.tolist(), strict=True),
            key=lambda item: (-item[1], _order(self._passages[item[0]])),
        )
        return [
            RankedHit(passage=self._passages[position], score=score)
            for position, score in ranked[:k]
        ]


# --- Hybrid: Reciprocal Rank Fusion ------------------------------------------------------


def fuse_rrf(bm25: list[Passage], semantic: list[Passage], k: int = RRF_K) -> list[RankedHit]:
    """Fuse two rankings: score = sum over rankings of 1 / (k + rank), rank 1-based.

    A passage missing from one ranking gets nothing from it. Equal scores (common: rank 1 in
    one list ties with rank 1 in the other) break by document identity, then position.
    """
    ranks: dict[str, dict[str, int]] = {}
    passages: dict[str, Passage] = {}
    for name, ranking in (("bm25", bm25), ("semantic", semantic)):
        for rank, passage in enumerate(ranking, start=1):
            passages.setdefault(passage.citation_id, passage)
            ranks.setdefault(passage.citation_id, {}).setdefault(name, rank)
    # Terms summed in a fixed order (bm25, then semantic) so the float result never varies.
    scores = {
        cid: sum(1 / (k + r[name]) for name in ("bm25", "semantic") if name in r)
        for cid, r in ranks.items()
    }
    ordered = sorted(scores, key=lambda cid: (-scores[cid], _order(passages[cid])))
    return [
        RankedHit(
            passage=passages[cid],
            score=scores[cid],
            bm25_rank=ranks[cid].get("bm25"),
            semantic_rank=ranks[cid].get("semantic"),
        )
        for cid in ordered
    ]


class HybridIndex:
    """BM25 and semantic search over the same passages, fused with RRF (k=60, depth 50).
    The BM25 index is built here from the semantic index's own passages, so both rankers
    always see exactly the same passages."""

    def __init__(self, semantic: SemanticIndex) -> None:
        self._keyword = KeywordIndex(semantic.passages)
        self._semantic = semantic

    def __len__(self) -> int:
        return len(self._semantic)

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        section: str | None = None,
        form: str | None = None,
        accession_number: str | None = None,
    ) -> list[RankedHit]:
        """The top `k` fused passages. Both rankers get the same filters, so the fused list
        obeys them too. Raises ValueError if k is not a positive integer."""
        _check_k(k)
        filters = {"section": section, "form": form, "accession_number": accession_number}
        bm25 = self._keyword.search(query, RRF_DEPTH, **filters)
        semantic = self._semantic.search(query, RRF_DEPTH, **filters)
        return fuse_rrf([h.passage for h in bm25], [h.passage for h in semantic])[:k]


# --- Shared rules (the same as KeywordIndex's, so results are comparable) -----------------


def _check_k(k: int) -> None:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError(f"k must be a positive integer, got {k!r}")


def _allowed(
    passage: Passage, section: str | None, form: str | None, accession: str | None
) -> bool:
    filing = passage.source.filing
    return (
        (section is None or passage.section == section)
        and (form is None or filing.form == form)
        and (accession is None or filing.accession_number == accession)
    )


def _order(passage: Passage) -> tuple:
    source = passage.source
    return (
        source.filing.cik,
        source.filing.accession_number,
        source.filing.primary_document or "",
        source.content_sha256,
        source.extractor_version,
        source.builder_version,
        passage.block_start,
        passage.char_start,
    )


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.semantic_retrieval install|verify [--model-dir DIR]"""
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.semantic_retrieval")
    parser.add_argument("command", choices=["install", "verify"])
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            fingerprint = install_model(args.model_dir)
        else:
            fingerprint = verify_model_files(args.model_dir)
    except ModelIntegrityError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{MODEL_REPO}@{MODEL_REVISION} in {args.model_dir}: verified, fingerprint {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

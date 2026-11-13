"""Deterministic in-memory keyword retrieval over filing passages, plus filing selection.

KeywordIndex ranks Passages from one or more filings with Okapi BM25. Results carry the
passage unchanged, so every hit keeps its source metadata and citation id. No external
search library, no stemming, no synonyms: what matched is always visible in matched_terms.
"""

import math
import re
import unicodedata
from collections import Counter

from pydantic import BaseModel, ConfigDict, computed_field

from financial_analyst.passages import Passage
from financial_analyst.sec_models import CompanySubmissions, FilingRef

# Okapi BM25 parameters, the common defaults (also Lucene's). k1 limits how much repeated
# occurrences of a term in one passage add; b sets how strongly long passages are penalized.
BM25_K1 = 1.2
BM25_B = 0.75

# Very common English function words. Kept short on purpose: words such as "not", "no",
# "may" or "risk" can matter in filings and are searchable.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was",
        "were", "which", "will", "with",
    }
)  # fmt: skip

# A number with "." or "," between digits ("3.5", "416,161"), or else a run of letters and
# digits in any script, possibly with apostrophes inside ("company's").
_NUMBER = r"\d+(?:[.,]\d+)+(?![^\W_])"
_TOKEN = re.compile(rf"{_NUMBER}|[^\W_]+(?:'[^\W_]+)*")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})

LATEST_FILING_FORMS = frozenset({"10-K", "10-Q"})


class SearchHit(BaseModel):
    """One ranked passage. `passage` is the indexed Passage itself, unchanged."""

    model_config = ConfigDict(frozen=True)

    passage: Passage
    score: float  # BM25 score; always > 0, comparable only within one index
    matched_terms: list[str]  # distinct query tokens found in the passage, sorted

    @computed_field
    @property
    def citation_id(self) -> str:
        return self.passage.citation_id


def tokenize(text: str) -> list[str]:
    """Split text into search tokens, in order. Used for both passages and queries.

    1. Unicode NFKC normalization (full-width forms, ligatures, non-breaking spaces), then
       casefold (lowercase that also handles e.g. German ß).
    2. Tokens are runs of letters or digits in any script. Everything else separates them,
       so hyphenated words become their parts ("single-source" -> "single", "source").
    3. Numbers keep their decimal point and drop thousands separators: "416,161" -> "416161",
       "3.5%" -> "3.5".
    4. Apostrophes: a possessive "'s" is dropped ("apple's" -> "apple"); other apostrophes
       are removed ("don't" -> "dont"). Curly apostrophes count as apostrophes.
    5. STOPWORDS are removed. Nothing is stemmed.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold().translate(_APOSTROPHES)
    tokens = []
    for match in _TOKEN.finditer(normalized):
        token = match.group()
        if "'" in token:
            token = token.removesuffix("'s").replace("'", "")
        else:
            token = token.replace(",", "")  # only numbers can contain "," or "."
        if token and token not in STOPWORDS:
            tokens.append(token)
    return tokens


class KeywordIndex:
    """BM25 index over a fixed list of passages. Statistics are computed once, here.

    Filtering policy: corpus statistics (document frequencies, average length, passage
    count) always describe the whole index. Filters only decide which passages may be
    returned, before ranking. So a passage's score for a query is the same with or without
    filters, and filtering never changes the order of the passages it keeps.
    """

    def __init__(self, passages: list[Passage]) -> None:
        ids = [p.citation_id for p in passages]
        if len(set(ids)) != len(ids):
            raise ValueError("passages must be distinct; a citation id appears more than once")
        self._passages = list(passages)
        self._lengths: list[int] = []
        self._postings: dict[str, list[tuple[int, int]]] = {}  # term -> [(position, tf)]
        for position, passage in enumerate(self._passages):
            counts = Counter(tokenize(passage.text))
            self._lengths.append(sum(counts.values()))
            for term, tf in counts.items():
                self._postings.setdefault(term, []).append((position, tf))
        count = len(self._passages)
        self._average_length = sum(self._lengths) / count if count else 0.0
        # Non-negative BM25 IDF (as in Lucene): ln(1 + (N - df + 0.5) / (df + 0.5)).
        self._idf = {
            term: math.log(1 + (count - len(postings) + 0.5) / (len(postings) + 0.5))
            for term, postings in self._postings.items()
        }

    def __len__(self) -> int:
        return len(self._passages)

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        section: str | None = None,
        form: str | None = None,
        accession_number: str | None = None,
    ) -> list[SearchHit]:
        """The top `k` passages for `query`, best first. Fewer, or none, when fewer match.

        Filters are exact matches on Passage.section, the filing's form ("10-K" does not
        match "10-K/A") and accession number. None means "do not filter"; it never means
        "unlabeled". A section filter returns only passages with exactly that label, so it
        returns nothing for a filing without section labels; it is never broadened.

        A query term counts once, however often it is repeated in the query. A query with no
        tokens after tokenization (empty, punctuation, stopwords only) returns [].
        Raises ValueError if k is not a positive integer.
        """
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be a positive integer, got {k!r}")
        # Sorted so the floating-point sum is identical whatever the query's word order.
        terms = sorted(set(tokenize(query)))
        scores: dict[int, float] = {}
        matched: dict[int, list[str]] = {}
        for term in terms:
            for position, tf in self._postings.get(term, ()):
                if not self._allowed(position, section, form, accession_number):
                    continue
                scores[position] = scores.get(position, 0.0) + self._term_score(term, tf, position)
                matched.setdefault(position, []).append(term)

        ranked = sorted(scores, key=lambda position: (-scores[position], self._order(position)))
        return [
            SearchHit(
                passage=self._passages[position],
                score=scores[position],
                matched_terms=matched[position],
            )
            for position in ranked[:k]
        ]

    def _term_score(self, term: str, tf: int, position: int) -> float:
        """BM25 contribution: idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len / avg_len))."""
        relative_length = self._lengths[position] / self._average_length
        norm = BM25_K1 * (1 - BM25_B + BM25_B * relative_length)
        return self._idf[term] * tf * (BM25_K1 + 1) / (tf + norm)

    def _allowed(
        self, position: int, section: str | None, form: str | None, accession: str | None
    ) -> bool:
        passage = self._passages[position]
        filing = passage.source.filing
        return (
            (section is None or passage.section == section)
            and (form is None or filing.form == form)
            and (accession is None or filing.accession_number == accession)
        )

    def _order(self, position: int) -> tuple:
        """Tie-break for equal scores: document identity, then location in the document."""
        passage = self._passages[position]
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


def select_latest_filing(
    submissions: CompanySubmissions, form: str = "10-K", include_amendments: bool = False
) -> FilingRef | None:
    """The most recently filed 10-K or 10-Q in the submissions' recent filings, or None.

    Chooses by filing_date (when the filing was made), never by report date or fiscal-year
    labels. Ties on the same date go to the later acceptance time, then the larger accession
    number. Amendments ("10-K/A") are excluded unless include_amendments is True.
    Only `recent_filings` is searched; older pages are not fetched (has_more_filings).
    """
    if form not in LATEST_FILING_FORMS:
        raise ValueError(f"form must be one of {sorted(LATEST_FILING_FORMS)}, got {form!r}")
    forms = {form, f"{form}/A"} if include_amendments else {form}
    candidates = [f for f in submissions.recent_filings if f.form in forms]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda f: (
            f.filing_date,
            f.acceptance_datetime.timestamp() if f.acceptance_datetime else -math.inf,
            f.accession_number,
        ),
    )

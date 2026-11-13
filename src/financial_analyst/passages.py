"""Deterministic passages built from extracted filing blocks, and offline citation checks.

A passage is a contiguous run of extracted text: whole blocks, or exact character ranges of
a block that was too long to keep whole. Passages never cross a known section boundary and
together cover every block, so no source content (page footers included) is dropped.

Each passage has a citation id that pins the exact source bytes, the extractor and builder
versions, and its block/character range. `verify_citation` re-derives a passage from the
cached document and reports why it no longer matches, instead of resolving to other text.
"""

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from financial_analyst.filing_cache import load_filing_document
from financial_analyst.filing_extractor import (
    EXTRACTOR_VERSION,
    ContentBlock,
    ExtractedFiling,
    extract_filing,
)
from financial_analyst.sec_models import FilingRef

# Bump whenever a change could alter passage boundaries, text, sections or citation ids for
# the same extracted blocks, including any change to TARGET_CHARS or the sentence rules.
BUILDER_VERSION = "1"
# Passages are filled with blocks up to about this many characters. A text block longer than
# this is split into pieces no longer than this. Tables are never split.
TARGET_CHARS = 2000
# Joins the block texts (or block pieces) that make up one passage.
BLOCK_SEPARATOR = "\n\n"

# Words ending in "." that rarely end a sentence in filings. Not splitting there is safe:
# it only means a piece is cut at an earlier boundary.
_ABBREVIATIONS = frozenset(
    {"inc.", "corp.", "co.", "ltd.", "no.", "nos.", "vs.", "mr.", "ms.", "dr.", "st.", "jr."}
)
# A sentence ends at ., ! or ? (optionally followed by closing quotes or brackets), then one
# space, then something that can start a sentence. Block text has single spaces only.
_SENTENCE_END = re.compile(r"[.!?][\"'”’)\]]* (?=[\"'“‘(\[]?[A-Z0-9$])")
_INITIALS = re.compile(r"(?:[A-Za-z]\.)+")  # "U.S.", "e.g.", "J."

_SHA256 = r"[0-9a-f]{64}"
_VERSION = r"[A-Za-z0-9._-]+"
_CITATION_ID = re.compile(
    r"sec-psg"
    r":(?P<cik>\d{10})"
    r":(?P<accession_number>\d{10}-\d{2}-\d{6})"
    r":(?P<primary_document>[A-Za-z0-9][A-Za-z0-9._-]*\.html?)"
    rf":(?P<content_sha256>{_SHA256})"
    rf":x(?P<extractor_version>{_VERSION})"
    rf":p(?P<builder_version>{_VERSION})"
    r":b(?P<block_start>\d+)\.(?P<char_start>\d+)-(?P<block_end>\d+)\.(?P<char_end>\d+)"
    r":t(?P<text_sha256_prefix>[0-9a-f]{16})"
)

VerificationStatus = Literal[
    "verified",
    "invalid_citation",  # malformed id, or id/metadata that do not describe the same passage
    "source_unavailable",  # document not in the cache (verification never downloads)
    "source_changed",  # cached bytes have a different SHA-256 than the cited source
    "extractor_changed",  # cited with another EXTRACTOR_VERSION; cannot be reproduced
    "builder_changed",  # cited with another BUILDER_VERSION; cannot be reproduced
    "boundaries_changed",  # rebuilding produces no passage with this block/character range
    "text_changed",  # a passage with this range exists, but its text is different
]


class PassageSource(BaseModel):
    """The exact document and code versions a passage was derived from."""

    model_config = ConfigDict(frozen=True)

    filing: FilingRef
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")  # full hash of the document bytes
    extractor_version: str = Field(pattern=rf"^{_VERSION}$")
    builder_version: str = Field(pattern=rf"^{_VERSION}$")

    @computed_field
    @property
    def document_url(self) -> str | None:
        return self.filing.primary_document_url


class Passage(BaseModel):
    """A contiguous span of extracted text, from (block_start, char_start) to
    (block_end, char_end). Character offsets index ContentBlock.text, not the raw HTML;
    char_end is exclusive and refers to block_end's text."""

    model_config = ConfigDict(frozen=True)

    source: PassageSource
    section: str | None  # the shared section of every block, or None when unlabeled
    kind: Literal["text", "table"]  # "table": one whole table, possibly after its headings
    block_start: int = Field(ge=0)
    char_start: int = Field(ge=0)
    block_end: int = Field(ge=0)
    char_end: int = Field(ge=1)
    text: str = Field(min_length=1)  # the block texts or pieces, joined by BLOCK_SEPARATOR

    @model_validator(mode="after")
    def _check_range(self) -> "Passage":
        if self.block_start > self.block_end or (
            self.block_start == self.block_end and self.char_start >= self.char_end
        ):
            raise ValueError("passage range is empty or reversed")
        return self

    # Both are derived, never stored, so a passage cannot carry an id that disagrees with it.
    @computed_field
    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    @computed_field
    @property
    def citation_id(self) -> str:
        """Every part is validated and cannot contain ":", so the id parses unambiguously."""
        filing = self.source.filing
        return (
            f"sec-psg:{filing.cik}:{filing.accession_number}:{filing.primary_document}"
            f":{self.source.content_sha256}"
            f":x{self.source.extractor_version}:p{self.source.builder_version}"
            f":b{self.block_start}.{self.char_start}-{self.block_end}.{self.char_end}"
            f":t{self.text_sha256[:16]}"
        )


class CitationId(BaseModel):
    """The parsed parts of a passage citation id."""

    model_config = ConfigDict(frozen=True)

    cik: str
    accession_number: str
    primary_document: str
    content_sha256: str
    extractor_version: str
    builder_version: str
    block_start: int
    char_start: int
    block_end: int
    char_end: int
    text_sha256_prefix: str  # first 16 hex digits of Passage.text_sha256


class PassageVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: VerificationStatus
    detail: str
    citation_id: str
    # Only when verified: the source document URL and the blocks the passage was built from.
    document_url: str | None = None
    blocks: list[ContentBlock] = []


def build_passages(extracted: ExtractedFiling) -> list[Passage]:
    """Group an extracted filing's blocks into passages. Deterministic for the same input.

    Rules, applied in document order:
    - A passage never contains blocks from different sections (None counts as a section,
      so unlabeled filings are grouped by headings and size alone).
    - A heading starts a new passage, except that consecutive headings stay together, and
      the headings are kept with the block that follows them.
    - A table is one passage with any headings directly before it, and is never split.
    - Otherwise blocks are added while the passage stays within TARGET_CHARS.
    - A text block longer than TARGET_CHARS is split into pieces (see _split_text).
    """
    source = PassageSource(
        filing=extracted.filing,
        content_sha256=extracted.source.content_sha256,
        extractor_version=extracted.extractor_version,
        builder_version=BUILDER_VERSION,
    )
    passages: list[Passage] = []
    current: list[_Piece] = []
    for block in extracted.blocks:
        for piece in _pieces(block):
            if current and _starts_new_passage(current, piece):
                passages.append(_make_passage(source, current))
                current = []
            current.append(piece)
    if current:
        passages.append(_make_passage(source, current))
    return passages


def parse_citation_id(citation_id: str) -> CitationId:
    """Parse a passage citation id. Raises ValueError if it is not exactly in that format."""
    match = _CITATION_ID.fullmatch(citation_id)
    if match is None:
        raise ValueError(f"not a passage citation id: {citation_id[:200]!r}")
    return CitationId.model_validate(match.groupdict())


def verify_citation(citation_id: str, passage: Passage, cache_dir: Path) -> PassageVerification:
    """Check, offline, that `citation_id` identifies `passage` and that the passage still
    says exactly what it said when it was cited.

    `passage` is the record the citation claims to refer to, for example a retrieval
    result. Uses only the filing cache and never contacts SEC. A source_unavailable result
    can be retried after the caller fetches the document. Every other failure is final for
    this citation: it is never re-pointed to different bytes, versions or text.

    A batch of one: see verify_citations.
    """
    return verify_citations([(citation_id, passage)], cache_dir)[0]


def verify_citations(
    pairs: Iterable[tuple[str, Passage]], cache_dir: Path
) -> list[PassageVerification]:
    """Verify many (citation_id, passage) pairs; one result per pair, in input order.

    Each result is the one verify_citation defines, but the work is shared per filing:
    each distinct cached filing is loaded and hashed once, and extracted and rebuilt at
    most once (only if some citation passes the earlier checks). Every citation of a filing
    is checked against that one snapshot of its bytes; the file is not read again within
    the batch. Nothing derived is kept after the call returns.
    """
    pairs = list(pairs)
    results: list[PassageVerification | None] = [None] * len(pairs)
    by_filing: dict[tuple[str, str, str | None, str], list[int]] = {}
    for position, (citation_id, passage) in enumerate(pairs):
        failure = _check_identity(citation_id, passage)
        if failure is not None:
            results[position] = failure
            continue
        filing = passage.source.filing
        # The cache's own identity: these fields alone choose and validate the cache entry.
        key = (filing.cik, filing.accession_number, filing.primary_document, filing.form)
        by_filing.setdefault(key, []).append(position)

    for positions in by_filing.values():
        group = [pairs[position] for position in positions]
        for position, result in zip(positions, _verify_filing(group, cache_dir), strict=True):
            results[position] = result
    assert all(result is not None for result in results)
    return results


def _failure(status: VerificationStatus, detail: str, citation_id: str) -> PassageVerification:
    return PassageVerification(status=status, detail=detail, citation_id=citation_id)


def _check_identity(citation_id: str, passage: Passage) -> PassageVerification | None:
    """The checks that need no source: the id parses and names exactly this passage."""
    try:
        cited = parse_citation_id(citation_id)
    except ValueError as exc:
        return _failure("invalid_citation", str(exc), citation_id)
    if citation_id != passage.citation_id:
        expected = parse_citation_id(passage.citation_id)
        differing = [
            name
            for name in CitationId.model_fields
            if getattr(cited, name) != getattr(expected, name)
        ]
        return _failure(
            "invalid_citation",
            f"citation id does not match the passage in: {differing}",
            citation_id,
        )
    return None


def _verify_filing(group: list[tuple[str, Passage]], cache_dir: Path) -> list[PassageVerification]:
    """Verify citations that all name the same cached filing, against one read of it."""
    # None covers both a missing entry and one that failed the cache's integrity checks
    # (logged by filing_cache). Either way there is no trustworthy copy, and a damaged
    # entry's recorded hash cannot be used to claim the source changed.
    document = load_filing_document(cache_dir, group[0][1].source.filing)
    # The hash of the exact bytes that get extracted below, not only the recorded one.
    sha256 = hashlib.sha256(document.content).hexdigest() if document else None
    extracted: ExtractedFiling | None = None
    rebuilt_by_span: dict[tuple[int, int, int, int], Passage] = {}
    results = []
    for citation_id, passage in group:
        source = passage.source
        if document is None:
            results.append(
                _failure(
                    "source_unavailable",
                    "no valid cached copy of the filing document: the cache entry is missing "
                    "or failed its integrity checks; fetch the document and verify again",
                    citation_id,
                )
            )
            continue
        if sha256 != source.content_sha256:
            results.append(
                _failure(
                    "source_changed",
                    f"cached document has SHA-256 {sha256}, "
                    f"citation requires {source.content_sha256}",
                    citation_id,
                )
            )
            continue
        if source.extractor_version != EXTRACTOR_VERSION:
            results.append(
                _failure(
                    "extractor_changed",
                    f"cited with extractor {source.extractor_version}, "
                    f"current is {EXTRACTOR_VERSION}",
                    citation_id,
                )
            )
            continue
        if source.builder_version != BUILDER_VERSION:
            results.append(
                _failure(
                    "builder_changed",
                    f"cited with passage builder {source.builder_version}, "
                    f"current is {BUILDER_VERSION}",
                    citation_id,
                )
            )
            continue
        if extracted is None:  # at most once per group, and only when a citation gets here
            extracted = extract_filing(document)
            # Ranges are unique within one build: passages cover the blocks once, in order.
            rebuilt_by_span = {_span(p): p for p in build_passages(extracted)}
        results.append(_compare_rebuilt(citation_id, passage, extracted, rebuilt_by_span))
    return results


def _compare_rebuilt(
    citation_id: str,
    passage: Passage,
    extracted: ExtractedFiling,
    rebuilt_by_span: dict[tuple[int, int, int, int], Passage],
) -> PassageVerification:
    rebuilt = rebuilt_by_span.get(_span(passage))
    if rebuilt is None:
        return _failure(
            "boundaries_changed", "no rebuilt passage has this block/character range", citation_id
        )
    if rebuilt.text_sha256 != passage.text_sha256:
        return _failure(
            "text_changed", "the rebuilt passage with this range has different text", citation_id
        )
    if rebuilt != passage:
        differing = [
            name
            for name in Passage.model_fields
            if getattr(rebuilt, name) != getattr(passage, name)
        ]
        return _failure(
            "invalid_citation",
            f"passage metadata differs from the source: {differing}",
            citation_id,
        )
    return PassageVerification(
        status="verified",
        detail="re-extracted from the cached document and rebuilt identically",
        citation_id=citation_id,
        document_url=passage.source.document_url,
        blocks=extracted.blocks[passage.block_start : passage.block_end + 1],
    )


# --- Building ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Piece:
    """A whole block, or an exact character range of one."""

    block: ContentBlock
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.block.text[self.start : self.end]


def _pieces(block: ContentBlock) -> list[_Piece]:
    if block.kind == "table" or len(block.text) <= TARGET_CHARS:
        return [_Piece(block, 0, len(block.text))]
    return [_Piece(block, start, end) for start, end in _split_text(block.text)]


def _split_text(text: str) -> list[tuple[int, int]]:
    """Cut a long text into (start, end) ranges of at most TARGET_CHARS characters.

    Each cut is at the last sentence end that fits; failing that, at the last space; and
    only for a run of TARGET_CHARS characters without a space, at exactly TARGET_CHARS.
    A cut at a sentence end or space drops just that one space from the pieces.
    """
    sentence_ends = [m.start() + len(m.group()) - 1 for m in _SENTENCE_END.finditer(text)]
    sentence_ends = [i for i in sentence_ends if not _is_abbreviation(text, i)]
    ranges = []
    start = 0
    while len(text) - start > TARGET_CHARS:
        limit = start + TARGET_CHARS
        fitting = [i for i in sentence_ends if start < i <= limit]
        if fitting:
            cut, next_start = fitting[-1], fitting[-1] + 1
        else:
            space = text.rfind(" ", start + 1, limit + 1)
            cut, next_start = (space, space + 1) if space != -1 else (limit, limit)
        ranges.append((start, cut))
        start = next_start
    ranges.append((start, len(text)))
    return ranges


def _is_abbreviation(text: str, space_index: int) -> bool:
    """Whether the word before this candidate sentence end is an abbreviation or initials."""
    word = text[:space_index].rsplit(" ", 1)[-1].rstrip("\"'”’)]")
    return word.lower() in _ABBREVIATIONS or bool(_INITIALS.fullmatch(word))


def _starts_new_passage(current: list[_Piece], piece: _Piece) -> bool:
    if piece.block.section != current[0].block.section:
        return True
    if any(p.block.kind == "table" for p in current):
        return True
    only_headings = all(p.block.kind == "heading" for p in current)
    if piece.block.kind == "table":
        return not only_headings
    length = sum(len(p.text) for p in current) + len(BLOCK_SEPARATOR) * len(current)
    if piece.block.kind == "heading":
        return not only_headings or length + len(piece.text) > TARGET_CHARS
    # Text after headings always joins them, so a heading is never cut off from its text.
    return not only_headings and length + len(piece.text) > TARGET_CHARS


def _make_passage(source: PassageSource, pieces: list[_Piece]) -> Passage:
    return Passage(
        source=source,
        section=pieces[0].block.section,
        kind="table" if any(p.block.kind == "table" for p in pieces) else "text",
        block_start=pieces[0].block.index,
        char_start=pieces[0].start,
        block_end=pieces[-1].block.index,
        char_end=pieces[-1].end,
        text=BLOCK_SEPARATOR.join(p.text for p in pieces),
    )


def _span(passage: Passage) -> tuple[int, int, int, int]:
    return passage.block_start, passage.char_start, passage.block_end, passage.char_end

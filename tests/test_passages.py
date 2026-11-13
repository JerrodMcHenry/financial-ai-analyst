"""Passage building and offline citation verification. Nothing touches the network."""

import hashlib
import json
import logging
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst import passages as passages_module
from financial_analyst.filing_cache import save_filing_document
from financial_analyst.filing_extractor import extract_filing
from financial_analyst.passages import (
    BLOCK_SEPARATOR,
    BUILDER_VERSION,
    TARGET_CHARS,
    Passage,
    build_passages,
    parse_citation_id,
    verify_citation,
    verify_citations,
)
from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = (FIXTURES / "filing_sample.htm").read_bytes()
SAMPLE_10Q = (FIXTURES / "filing_10q_sample.htm").read_bytes()
ENTRY = "filings/0000320193/000032019326000026/aapl-20260926.htm"


def make_document(content: bytes, form: str = "10-K") -> FilingDocument:
    filing = FilingRef(
        cik="0000320193",
        accession_number="0000320193-26-000026",
        form=form,
        filing_date="2026-11-03",
        report_date="2026-09-26",
        acceptance_datetime=None,
        primary_document="aapl-20260926.htm",
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


def html(*blocks: str) -> bytes:
    """Body blocks: '#Text' is a bold heading, '|a|b' a one-row table, anything else a paragraph."""
    parts = []
    for block in blocks:
        if block.startswith("#"):
            parts.append(f'<div><span style="font-weight:700">{block[1:]}</span></div>')
        elif block.startswith("|"):
            cells = "".join(f"<td>{cell}</td>" for cell in block[1:].split("|"))
            parts.append(f"<table><tr>{cells}</tr></table>")
        else:
            parts.append(f"<div><span>{block}</span></div>")
    return f"<html><body>{''.join(parts)}</body></html>".encode()


def build(content: bytes, form: str = "10-K") -> list[Passage]:
    return build_passages(extract_filing(make_document(content, form)))


def sentences(count: int, length: int = 99) -> str:
    """`count` sentences of exactly `length` characters each, joined by single spaces."""
    return " ".join(f"Sentence {i:03d} " + "x" * (length - 14) + "." for i in range(count))


def covered_text(passage: Passage, blocks) -> list[str]:
    """The passage's text, re-derived from its block/character range."""
    pieces = []
    for block in blocks[passage.block_start : passage.block_end + 1]:
        start = passage.char_start if block.index == passage.block_start else 0
        end = passage.char_end if block.index == passage.block_end else len(block.text)
        pieces.append(block.text[start:end])
    return pieces


# --- Building ----------------------------------------------------------------------------


def test_passages_are_deterministic():
    first = build(SAMPLE)
    second = build(SAMPLE)
    assert first == second
    assert [p.citation_id for p in first] == [p.citation_id for p in second]


def test_10k_passages_follow_sections_headings_and_tables():
    result = [(p.kind, p.section, p.block_start, p.block_end) for p in build(SAMPLE)]
    assert result == [
        ("text", None, 0, 1),  # cover heading + company name
        ("table", None, 2, 2),  # table of contents
        ("text", None, 3, 3),  # "PART I": unlabeled, so not merged into Item 1
        ("text", "Item 1", 4, 6),
        ("text", "Item 1A", 7, 9),  # item heading + risk-factor heading + text
        ("text", None, 10, 10),  # "PART II"
        ("text", "Item 7", 11, 12),
        ("table", "Item 7", 13, 13),
        ("text", "Item 7", 14, 14),  # page footer, kept as content
    ]


def test_10q_passages_never_cross_part_qualified_sections():
    passages = build(SAMPLE_10Q, form="10-Q")
    extracted = extract_filing(make_document(SAMPLE_10Q, form="10-Q"))
    for passage in passages:
        blocks = extracted.blocks[passage.block_start : passage.block_end + 1]
        assert {b.section for b in blocks} == {passage.section}
    assert [p.section for p in passages if p.section and "Item 1" in p.section] == [
        "Part I, Item 1",
        "Part I, Item 1",
        "Part II, Item 1",
        "Part II, Item 1A",
    ]
    mdna = [p for p in passages if p.section == "Part I, Item 2"]
    assert len(mdna) == 1 and mdna[0].text.startswith("Item 2. Management")


def test_every_block_is_covered_once_in_order_including_footers():
    content = html("#Item 7. MD&A", sentences(45), "Apple Inc. | Q3 2026 Form 10-Q | 5", "|a|b")
    extracted = extract_filing(make_document(content, form="8-K"))
    passages = build_passages(extracted)
    joined = BLOCK_SEPARATOR.join(p.text for p in passages)
    expected = BLOCK_SEPARATOR.join(b.text for b in extracted.blocks)
    # Each cut inside a block replaces exactly one space with the separator; nothing else is
    # lost, repeated or reordered.
    cuts = sum(1 for p in passages if p.char_start > 0)
    assert cuts == 2
    assert len(joined) == len(expected) + cuts * (len(BLOCK_SEPARATOR) - 1)
    assert joined.replace(BLOCK_SEPARATOR, " ") == expected.replace(BLOCK_SEPARATOR, " ")
    assert "Apple Inc. | Q3 2026 Form 10-Q | 5" in joined
    for passage in passages:
        assert BLOCK_SEPARATOR.join(covered_text(passage, extracted.blocks)) == passage.text


def test_sectionless_filings_still_get_passages():
    for form in ("8-K", "10-Q"):  # unsupported form; 10-Q without Part headings
        content = html("#Item 2. Discussion", "Revenue grew.", "#Other", "Costs fell.")
        passages = build(content, form)
        assert [(p.section, p.text) for p in passages] == [
            (None, "Item 2. Discussion\n\nRevenue grew."),
            (None, "Other\n\nCosts fell."),
        ]


def test_headings_stay_with_the_text_they_introduce():
    passages = build(
        html("Intro.", "#Risk Factors", "#Supply chain", "Suppliers may fail.", "More.", "#Next"),
        form="8-K",
    )
    assert [p.text for p in passages] == [
        "Intro.",
        "Risk Factors\n\nSupply chain\n\nSuppliers may fail.\n\nMore.",
        "Next",  # a final heading with nothing after it stands alone
    ]


def test_heading_attaches_even_when_the_following_block_fills_the_passage():
    long_paragraph = sentences(20)  # 1,999 characters: fits on its own, not with the heading
    assert len(long_paragraph) <= TARGET_CHARS
    passages = build(html("#Liquidity", long_paragraph, "Short."), form="8-K")
    assert [p.text for p in passages] == [f"Liquidity\n\n{long_paragraph}", "Short."]


def test_paragraphs_are_grouped_up_to_the_target_size():
    paragraph = "y" * 600 + "."
    passages = build(html(*[paragraph] * 7), form="8-K")
    assert [p.block_end - p.block_start + 1 for p in passages] == [3, 3, 1]
    assert all(len(p.text) <= TARGET_CHARS for p in passages)


def test_oversized_paragraph_is_split_at_sentence_ends_with_exact_offsets():
    text = sentences(45)  # 4,499 characters, 100 per sentence including the space
    extracted = extract_filing(make_document(html(text), form="8-K"))
    passages = build_passages(extracted)
    assert [(p.block_start, p.char_start, p.block_end, p.char_end) for p in passages] == [
        (0, 0, 0, 1999),
        (0, 2000, 0, 3999),
        (0, 4000, 0, 4499),
    ]
    for passage in passages:
        assert passage.text == text[passage.char_start : passage.char_end]
        assert passage.text.startswith("Sentence") and passage.text.endswith(".")


def test_split_skips_abbreviations_and_falls_back_to_spaces_then_hard_cuts():
    # "U.S." and "Inc." are not sentence ends, so the cut goes at the earlier real one.
    text = "A" * 1500 + ". " + "B" * 470 + " U.S. Apple Inc. Sales grew strongly. " + "c" * 600
    cuts = passages_module._split_text(text)
    assert cuts[0] == (0, 1501)

    no_sentences = " ".join(["word"] * 1000)  # 4,999 characters, no sentence ends
    ranges = passages_module._split_text(no_sentences)
    assert all(end - start <= TARGET_CHARS for start, end in ranges)
    assert all(no_sentences[end] == " " for _, end in ranges[:-1])

    no_spaces = "z" * 4500
    assert passages_module._split_text(no_spaces) == [(0, 2000), (2000, 4000), (4000, 4500)]


def test_tables_are_never_split_and_keep_their_headings():
    big_row = "|" + "|".join(f"{i:,}" for i in range(1000, 1700))  # one row, over 3,000 chars
    passages = build(html("Intro.", "#Segment results", big_row, "After the table."), "8-K")
    assert [p.kind for p in passages] == ["text", "table", "text"]
    table = passages[1]
    assert table.text.startswith("Segment results\n\n1,000 | 1,001")
    assert len(table.text) > TARGET_CHARS and table.block_end == 2


# --- Character offsets -------------------------------------------------------------------


def assert_exact_slices(extracted, passages):
    """Every passage segment is exactly a slice of its block's text, and the slices of each
    block cover it in order, leaving out at most one space at each cut."""
    covered = {}
    for passage in passages:
        segments = passage.text.split(BLOCK_SEPARATOR)
        indexes = range(passage.block_start, passage.block_end + 1)
        assert len(segments) == len(indexes)
        for index, segment in zip(indexes, segments, strict=True):
            text = extracted.blocks[index].text
            start = passage.char_start if index == passage.block_start else 0
            end = passage.char_end if index == passage.block_end else len(text)
            assert text[start:end] == segment
            covered.setdefault(index, []).append((start, end))
    for block in extracted.blocks:
        ranges = covered[block.index]
        assert ranges[0][0] == 0 and ranges[-1][1] == len(block.text)
        for (_, end), (next_start, _) in zip(ranges, ranges[1:], strict=False):
            assert block.text[end:next_start] in ("", " ")
            assert not block.text[end - 1].isspace() and not block.text[next_start].isspace()


def test_offsets_are_exact_with_unicode_and_collapsed_whitespace():
    sentence = (
        "Apple’s net sales  rose 3.5%   in the U.S. — “strong” demand."
        " “Quoted sentence.” (Parenthetical one.) Café résumé 中文"
        " 🙂 e.g. Inc. No. 1. Why? Growth!"
    )
    paragraph = " ".join([sentence] * 40)  # long, with repeated and non-breaking spaces
    content = html("#Results — “Overview”", paragraph, "Short  one.", "|a|é")
    extracted = extract_filing(make_document(content, form="8-K"))
    passages = build_passages(extracted)
    assert len(passages) > 3 and "  " not in extracted.blocks[1].text
    assert_exact_slices(extracted, passages)
    for passage in passages[1:-2]:  # middle pieces of the long paragraph end at a sentence end
        assert passage.text.endswith((".", "!", "?", ".”", ".)"))


def test_cuts_never_land_after_abbreviations_or_initials():
    filler = "Revenue grew in every region. "
    paragraph = (filler * 66).strip() + " U.S. Apple Inc. No. 5 J. Smith e.g. Sales. " + filler * 10
    extracted = extract_filing(make_document(html(paragraph), form="8-K"))
    passages = build_passages(extracted)
    assert_exact_slices(extracted, passages)
    for passage in passages[:-1]:
        assert passage.text.endswith("region.") or passage.text.endswith("Sales.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A sentence end exactly at the limit is used (not the earlier one at 11): the piece
        # is 2,000 characters.
        ("A" * 10 + ". " + "B" * 1987 + ". Next", [(0, 2000), (2001, 2005)]),
        # One character past the limit does not fit; the earlier sentence end is used.
        ("A" * 10 + ". " + "C" * 1988 + ". Next", [(0, 11), (12, 2006)]),
        # A space exactly at the limit is the fallback cut.
        ("z" * 2000 + " " + "y" * 10, [(0, 2000), (2001, 2011)]),
        # 2,001 characters without a space: a hard cut at exactly 2,000, nothing omitted.
        ("z" * 2001 + " yy", [(0, 2000), (2000, 2004)]),
        # 2,000 characters fit without any cut.
        ("q" * 2000, [(0, 2000)]),
    ],
)
def test_splits_near_the_2000_character_boundary(text, expected):
    assert passages_module._split_text(text) == expected
    extracted = extract_filing(make_document(html(text), form="8-K"))
    assert [(p.char_start, p.char_end) for p in build_passages(extracted)] == expected
    assert_exact_slices(extracted, build_passages(extracted))


def test_multi_block_passages_join_exact_block_texts():
    content = html("#Heading “one”", "First   paragraph.", "Second paragraph.", "|x|1")
    extracted = extract_filing(make_document(content, form="8-K"))
    passages = build_passages(extracted)
    assert passages[0].text == "Heading “one”\n\nFirst paragraph.\n\nSecond paragraph."
    assert_exact_slices(extracted, passages)


def test_split_passages_verify_against_the_real_cache(tmp_path, no_network):
    paragraph = " ".join(["Apple’s U.S. sales rose 3.5%   — “strongly.”"] * 120)
    document = make_document(html("#Liquidity", paragraph, "Tail."), form="10-Q")
    save_filing_document(tmp_path, document)
    passages = build_passages(extract_filing(document))
    assert sum(1 for p in passages if p.char_start > 0) >= 2  # really split
    for passage in passages:
        assert verify_citation(passage.citation_id, passage, tmp_path).status == "verified"


# --- Citation identity -------------------------------------------------------------------


def test_citation_ids_are_unique_even_for_repeated_text():
    footer = "Sample Company Inc. | 2026 Form 10-K | 21"
    passages = build(html("#One", "Text.", "|x|1", footer, "|y|2", footer), form="8-K")
    repeated = [p for p in passages if p.text == footer]
    assert len(repeated) == 2 and repeated[0].citation_id != repeated[1].citation_id
    assert len({p.citation_id for p in passages}) == len(passages)


def test_citation_id_changes_with_source_bytes_and_versions():
    original = build(SAMPLE)[3]
    other_bytes = build(SAMPLE + b"\n")[3]
    assert other_bytes.text == original.text
    assert other_bytes.citation_id != original.citation_id

    source = original.source
    for change in ({"extractor_version": "99"}, {"builder_version": "99"}):
        changed = original.model_copy(update={"source": source.model_copy(update=change)})
        assert changed.citation_id != original.citation_id


def test_citation_id_parses_back_to_the_passage_identity():
    passage = build(SAMPLE)[3]
    parsed = parse_citation_id(passage.citation_id)
    assert parsed.cik == "0000320193"
    assert parsed.accession_number == "0000320193-26-000026"
    assert parsed.primary_document == "aapl-20260926.htm"
    assert parsed.content_sha256 == hashlib.sha256(SAMPLE).hexdigest()  # full hash
    assert (parsed.extractor_version, parsed.builder_version) == ("2", BUILDER_VERSION)
    assert (parsed.block_start, parsed.char_start, parsed.block_end, parsed.char_end) == (
        4,
        0,
        6,
        41,
    )
    assert parsed.text_sha256_prefix == passage.text_sha256[:16]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s + ":extra",
        lambda s: s.replace("sec-psg:", "sec-doc:"),
        lambda s: s.replace(":x2:", ":"),
        lambda s: s.replace(":b4.0-6.41:", ":b4-6:"),
        lambda s: s.replace(":0000320193:", ":320193:", 1),
        lambda s: s.replace("aapl-20260926.htm", "../x.htm"),
        lambda s: s[:-1],  # truncated text hash
        lambda s: s.upper(),
        lambda s: " " + s,
    ],
)
def test_malformed_citation_ids_are_rejected(mutate):
    with pytest.raises(ValueError):
        parse_citation_id(mutate(build(SAMPLE)[3].citation_id))


def test_stored_passages_cannot_carry_a_stale_citation_id():
    passage = build(SAMPLE)[3]
    stored = passage.model_dump(mode="json")
    assert Passage.model_validate(stored) == passage

    stored["text"] = "Tampered text."
    tampered = Passage.model_validate(stored)  # the stored id and hash are ignored, not trusted
    assert tampered.citation_id != stored["citation_id"]
    assert tampered.text_sha256 == hashlib.sha256(b"Tampered text.").hexdigest()


def test_empty_or_reversed_ranges_are_rejected():
    stored = build(SAMPLE)[3].model_dump()
    for change in ({"block_end": 3}, {"char_start": 41, "block_end": 4, "char_end": 41}):
        with pytest.raises(ValueError, match="empty or reversed"):
            Passage.model_validate(stored | change)


# --- Verification ------------------------------------------------------------------------


@pytest.fixture
def cached(tmp_path):
    """A cache holding the 10-K sample, and the passages built from it."""
    document = make_document(SAMPLE)
    save_filing_document(tmp_path, document)
    return tmp_path, build_passages(extract_filing(document))


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted during verification")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_verified_citation_returns_source_blocks_and_document_url(cached, no_network):
    cache_dir, passages = cached
    passage = passages[3]
    result = verify_citation(passage.citation_id, passage, cache_dir)
    assert result.status == "verified"
    assert result.document_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/aapl-20260926.htm"
    )
    assert [b.index for b in result.blocks] == [4, 5, 6]
    assert result.blocks[0].text == "Item 1. Business" and result.blocks[0].dom_path


def test_every_passage_of_a_filing_verifies(cached, no_network):
    cache_dir, passages = cached
    for passage in passages:
        assert verify_citation(passage.citation_id, passage, cache_dir).status == "verified"


def test_source_unavailable_never_downloads(tmp_path, no_network):
    passage = build(SAMPLE)[3]
    result = verify_citation(passage.citation_id, passage, tmp_path)
    assert result.status == "source_unavailable"
    assert result.blocks == [] and result.document_url is None


def test_source_changed_when_cached_bytes_differ(tmp_path, no_network):
    passage = build(SAMPLE)[3]
    save_filing_document(tmp_path, make_document(SAMPLE.replace(b"2026", b"2027")))
    result = verify_citation(passage.citation_id, passage, tmp_path)
    assert result.status == "source_changed"


def test_source_changed_when_entry_is_rewritten_consistently(cached, no_network):
    # Bytes and recorded hash replaced together pass the cache's own checks; the citation
    # still pins the original full hash.
    cache_dir, passages = cached
    content_path, metadata_path = cache_dir / ENTRY, cache_dir / (ENTRY + ".json")
    content = content_path.read_bytes() + b"<p>appended</p>"
    metadata = json.loads(metadata_path.read_text())
    metadata["source"]["content_sha256"] = hashlib.sha256(content).hexdigest()
    content_path.write_bytes(content)
    metadata_path.write_text(json.dumps(metadata))
    result = verify_citation(passages[3].citation_id, passages[3], cache_dir)
    assert result.status == "source_changed"


def _remove(path):
    path.unlink()


def _corrupt(path):
    path.write_bytes(path.read_bytes() + b"x")


def _edit_metadata(change):
    def edit(path):
        metadata = json.loads(path.read_text())
        change(metadata)
        path.write_text(json.dumps(metadata))

    return edit


@pytest.mark.parametrize(
    ("damage", "file", "logged"),
    [
        (_remove, ENTRY + ".json", False),  # metadata is written last: an incomplete entry
        (_remove, ENTRY, True),
        (_corrupt, ENTRY, True),  # bytes no longer match the recorded hash
        (_corrupt, ENTRY + ".json", True),  # not valid JSON
        (_edit_metadata(lambda m: m["filing"].update(form="10-Q")), ENTRY + ".json", True),
        (
            _edit_metadata(lambda m: m["source"].update(content_sha256="0" * 64)),
            ENTRY + ".json",
            True,
        ),
    ],
)
def test_missing_or_damaged_cache_entries_are_unavailable_never_changed(
    cached, no_network, caplog, damage, file, logged
):
    cache_dir, passages = cached
    damage(cache_dir / file)
    with caplog.at_level(logging.WARNING, logger="financial_analyst.filing_cache"):
        result = verify_citation(passages[3].citation_id, passages[3], cache_dir)
    assert result.status == "source_unavailable"
    assert "missing or failed its integrity checks" in result.detail
    assert result.blocks == [] and result.document_url is None
    # The cache log is the only place a damaged entry is told apart from a missing one.
    assert any("Ignoring invalid cache entry" in r.message for r in caplog.records) == logged


@pytest.mark.parametrize(
    ("field", "status"),
    [("extractor_version", "extractor_changed"), ("builder_version", "builder_changed")],
)
def test_version_mismatches_are_reported(cached, no_network, field, status):
    cache_dir, passages = cached
    passage = passages[3]
    old = passage.model_copy(update={"source": passage.source.model_copy(update={field: "0"})})
    result = verify_citation(old.citation_id, old, cache_dir)
    assert result.status == status
    assert result.blocks == []


def test_text_changed_and_boundaries_changed(cached, no_network):
    cache_dir, passages = cached
    passage = passages[3]

    edited = passage.model_copy(update={"text": passage.text.replace("2026", "2027")})
    assert verify_citation(edited.citation_id, edited, cache_dir).status == "text_changed"

    shifted = passage.model_copy(update={"char_end": 40, "text": passage.text[:-1]})
    assert verify_citation(shifted.citation_id, shifted, cache_dir).status == "boundaries_changed"


def test_citation_must_match_the_passage_it_claims(cached, no_network):
    cache_dir, passages = cached
    passage, other = passages[3], passages[4]

    result = verify_citation(other.citation_id, passage, cache_dir)
    assert result.status == "invalid_citation"
    assert "block_start" in result.detail and "text_sha256_prefix" in result.detail

    malformed = verify_citation("sec-psg:not-an-id", passage, cache_dir)
    assert malformed.status == "invalid_citation"

    # A consistent id cannot launder wrong metadata: the rebuilt passage must match exactly.
    relabeled = passage.model_copy(update={"section": "Item 7"})
    result = verify_citation(relabeled.citation_id, relabeled, cache_dir)
    assert result.status == "invalid_citation" and "section" in result.detail


# --- Batch verification ------------------------------------------------------------------

ENTRY_10Q = "filings/0000320193/000032019326000020/aapl-20260627.htm"


def make_10q_document(content: bytes = SAMPLE_10Q) -> FilingDocument:
    """A second filing, with its own cache entry."""
    document = make_document(content, form="10-Q")
    filing = document.filing.model_copy(
        update={"accession_number": "0000320193-26-000020", "primary_document": "aapl-20260627.htm"}
    )
    source = SourceMetadata.model_validate(
        {**document.source.model_dump(), "source_url": filing.primary_document_url}
    )
    return FilingDocument(filing=filing, content=content, source=source)


@pytest.fixture
def two_filings(tmp_path):
    """A cache with the 10-K and 10-Q samples (two filings), and passages from each."""
    tenk, tenq = make_document(SAMPLE), make_10q_document()
    save_filing_document(tmp_path, tenk)
    save_filing_document(tmp_path, tenq)
    return tmp_path, build_passages(extract_filing(tenk)), build_passages(extract_filing(tenq))


@pytest.fixture
def counts(monkeypatch):
    """Counts cache loads and extractions done by verification."""
    calls = {"load": 0, "extract": 0}
    load, extract = passages_module.load_filing_document, passages_module.extract_filing

    def counting_load(*args):
        calls["load"] += 1
        return load(*args)

    def counting_extract(*args):
        calls["extract"] += 1
        return extract(*args)

    monkeypatch.setattr(passages_module, "load_filing_document", counting_load)
    monkeypatch.setattr(passages_module, "extract_filing", counting_extract)
    return calls


def pair(passage: Passage) -> tuple[str, Passage]:
    return passage.citation_id, passage


def with_source(passage: Passage, **changes) -> Passage:
    return passage.model_copy(update={"source": passage.source.model_copy(update=changes)})


def rewrite_entry(cache_dir: Path, entry: str, content: bytes) -> None:
    """Replace a cache entry's bytes and recorded hash together: still a valid entry."""
    metadata_path = cache_dir / (entry + ".json")
    metadata = json.loads(metadata_path.read_text())
    metadata["source"]["content_sha256"] = hashlib.sha256(content).hexdigest()
    (cache_dir / entry).write_bytes(content)
    metadata_path.write_text(json.dumps(metadata))


def mixed_batch(tenk: list[Passage], tenq: list[Passage]) -> list[tuple[str, Passage]]:
    """Every status, from two filings, interleaved, with a duplicate."""
    passage = tenk[3]
    return [
        pair(tenk[0]),
        pair(tenq[2]),
        ("sec-psg:not-an-id", passage),  # invalid_citation: malformed
        (tenk[4].citation_id, passage),  # invalid_citation: names another passage
        pair(passage.model_copy(update={"section": "Item 7"})),  # invalid_citation: metadata
        pair(with_source(passage, content_sha256="0" * 64)),  # source_changed
        pair(with_source(passage, extractor_version="0")),  # extractor_changed
        pair(with_source(passage, builder_version="0")),  # builder_changed
        pair(passage.model_copy(update={"char_end": 40, "text": passage.text[:-1]})),
        pair(passage.model_copy(update={"text": passage.text.replace("2026", "2027")})),
        pair(tenq[0]),
        pair(tenk[0]),  # duplicate of the first pair
    ]


MIXED_STATUSES = [
    "verified",
    "verified",
    "invalid_citation",
    "invalid_citation",
    "invalid_citation",
    "source_changed",
    "extractor_changed",
    "builder_changed",
    "boundaries_changed",
    "text_changed",
    "verified",
    "verified",
]


def test_batch_results_equal_single_item_results(two_filings, no_network):
    cache_dir, tenk, tenq = two_filings
    pairs = mixed_batch(tenk, tenq) + [pair(p) for p in tenk + tenq]
    batch = verify_citations(pairs, cache_dir)
    assert batch == [verify_citation(c, p, cache_dir) for c, p in pairs]
    assert [r.status for r in batch[: len(MIXED_STATUSES)]] == MIXED_STATUSES
    assert all(r.status == "verified" for r in batch[len(MIXED_STATUSES) :])


def test_output_follows_input_order_and_keeps_duplicates(two_filings, no_network):
    cache_dir, tenk, tenq = two_filings
    pairs = mixed_batch(tenk, tenq)
    forward = verify_citations(pairs, cache_dir)
    assert [r.citation_id for r in forward] == [c for c, _ in pairs]
    assert forward[0] == forward[-1]  # the duplicate gets its own, identical result
    assert verify_citations(list(reversed(pairs)), cache_dir) == list(reversed(forward))
    assert verify_citations([], cache_dir) == []


def test_each_filing_is_loaded_and_extracted_once_per_batch(two_filings, no_network, counts):
    cache_dir, tenk, tenq = two_filings
    pairs = [pair(p) for p in tenk + tenq]
    pairs += pairs  # every citation twice, both filings interleaved by position
    results = verify_citations(pairs, cache_dir)
    assert all(r.status == "verified" for r in results)
    assert counts == {"load": 2, "extract": 2}


def test_citations_rejected_before_extraction_extract_nothing(two_filings, no_network, counts):
    cache_dir, tenk, _ = two_filings
    passage = tenk[3]
    pairs = [
        ("sec-psg:not-an-id", passage),
        (tenk[4].citation_id, passage),
        pair(with_source(passage, content_sha256="0" * 64)),
        pair(with_source(passage, extractor_version="0")),
        pair(with_source(passage, builder_version="0")),
    ]
    statuses = [r.status for r in verify_citations(pairs, cache_dir)]
    assert statuses == [
        "invalid_citation",
        "invalid_citation",
        "source_changed",
        "extractor_changed",
        "builder_changed",
    ]
    # The two malformed or mismatched ids never reach the cache at all.
    assert counts == {"load": 1, "extract": 0}


def test_unavailable_filing_extracts_nothing(tmp_path, no_network, counts):
    passages = build(SAMPLE)
    results = verify_citations([pair(p) for p in passages], tmp_path)
    assert {r.status for r in results} == {"source_unavailable"}
    assert counts == {"load": 1, "extract": 0}


def test_repeated_batches_share_no_derived_state(two_filings, no_network, counts):
    cache_dir, tenk, _ = two_filings
    first = verify_citations([pair(tenk[3])], cache_dir)
    second = verify_citations([pair(tenk[3])], cache_dir)
    assert first == second and first[0].status == "verified"
    assert counts == {"load": 2, "extract": 2}


def test_source_mutation_between_batches_is_detected(two_filings, no_network):
    cache_dir, tenk, tenq = two_filings
    pairs = [pair(tenk[3]), pair(tenq[2])]
    assert [r.status for r in verify_citations(pairs, cache_dir)] == ["verified", "verified"]
    rewrite_entry(cache_dir, ENTRY, SAMPLE + b"<p>appended</p>")
    assert [r.status for r in verify_citations(pairs, cache_dir)] == ["source_changed", "verified"]


@pytest.mark.parametrize(
    "damage",
    [
        _corrupt,  # not valid JSON
        _edit_metadata(lambda m: m["source"].update(content_sha256="0" * 64)),
        _edit_metadata(lambda m: m["filing"].update(form="10-Q")),
    ],
)
def test_invalid_cache_metadata_never_verifies(two_filings, no_network, damage):
    cache_dir, tenk, tenq = two_filings
    damage(cache_dir / (ENTRY + ".json"))
    results = verify_citations([pair(tenk[3]), pair(tenq[2]), pair(tenk[0])], cache_dir)
    assert [r.status for r in results] == ["source_unavailable", "verified", "source_unavailable"]
    assert results[0].blocks == [] and results[0].document_url is None


def test_different_claimed_hashes_for_the_same_filing(two_filings, no_network, counts):
    # Both passages name the same cache entry; only the cached bytes' hash verifies.
    cache_dir, tenk, _ = two_filings
    edited = build(SAMPLE.replace(b"2026", b"2027"))[3]
    assert edited.source.filing == tenk[3].source.filing
    results = verify_citations([pair(edited), pair(tenk[3]), pair(edited)], cache_dir)
    assert [r.status for r in results] == ["source_changed", "verified", "source_changed"]
    assert hashlib.sha256(SAMPLE).hexdigest() in results[0].detail
    assert counts == {"load": 1, "extract": 1}


def test_a_batch_never_mixes_source_bytes(two_filings, no_network, monkeypatch):
    """The cache entry is replaced right after the batch reads it: every citation in that
    batch is still checked against the bytes first read, and the next batch sees the change."""
    cache_dir, tenk, _ = two_filings
    load = passages_module.load_filing_document
    loads = []

    def load_then_replace(*args):
        document = load(*args)
        loads.append(document.source.content_sha256)
        rewrite_entry(cache_dir, ENTRY, SAMPLE + b"<p>replaced</p>")
        return document

    monkeypatch.setattr(passages_module, "load_filing_document", load_then_replace)
    results = verify_citations([pair(p) for p in tenk], cache_dir)
    assert {r.status for r in results} == {"verified"}
    assert loads == [hashlib.sha256(SAMPLE).hexdigest()]  # read once, before the change

    monkeypatch.setattr(passages_module, "load_filing_document", load)
    assert verify_citations([pair(tenk[3])], cache_dir)[0].status == "source_changed"

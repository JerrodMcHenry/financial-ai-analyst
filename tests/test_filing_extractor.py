"""Offline extraction tests against small hand-written HTML. Nothing touches the network."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst.filing_extractor import EXTRACTOR_VERSION, extract_filing
from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = (FIXTURES / "filing_sample.htm").read_bytes()
SAMPLE_10Q = (FIXTURES / "filing_10q_sample.htm").read_bytes()


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
        retrieved_at=datetime.now(UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return FilingDocument(filing=filing, content=content, source=source)


@pytest.fixture(scope="module")
def sample():
    return extract_filing(make_document(SAMPLE))


def test_blocks_are_extracted_in_document_order(sample):
    assert [(b.kind, b.text) for b in sample.blocks] == [
        ("heading", "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
        ("paragraph", "Sample Company Inc."),
        ("table", sample.blocks[2].text),  # table of contents, checked below
        ("heading", "PART I"),
        ("heading", "Item 1. Business"),
        ("paragraph", "The Company designs, manufactures and markets smartphones and "
                      "personal computers."),
        ("paragraph", "Net sales for 2026 were $416,161 million."),
        ("heading", "Item 1A. Risk Factors"),
        ("heading", "The Company’s business can be affected by political events."),
        ("paragraph", "See Item 7 of this Form 10-K for further discussion."),
        ("heading", "PART II"),
        ("heading", "Item 7. Management’s Discussion and Analysis"),
        ("paragraph", "Segment net sales were as follows (in millions):"),
        ("table", sample.blocks[13].text),
        ("paragraph", "Sample Company Inc. | 2026 Form 10-K | 21"),
    ]  # fmt: skip
    assert [b.index for b in sample.blocks] == list(range(15))


def test_hidden_xbrl_scripts_and_styles_are_not_extracted(sample):
    all_text = "\n".join(b.text for b in sample.blocks)
    for hidden in ("HIDDEN XBRL FACT", "CONTEXT TEXT", "SCRIPT TEXT", "color: blue", "sample-10k"):
        assert hidden not in all_text
    # Visible values wrapped in inline XBRL tags are kept.
    assert "$416,161 million" in all_text
    assert "178,353" in all_text


def test_ix_header_and_display_none_are_each_skipped_on_their_own():
    html = b"""<html><body>
    <ix:header><ix:hidden>XBRL FACT</ix:hidden></ix:header>
    <div style="DISPLAY: none">Hidden note</div>
    <p>Visible</p></body></html>"""

    result = extract_filing(make_document(html))

    assert [b.text for b in result.blocks] == ["Visible"]


def test_table_keeps_rows_cells_and_column_alignment(sample):
    table = sample.blocks[13]
    assert table.rows == [
        ["Segment", "", "", "2026", "", "2025", ""],
        ["Americas", "", "", "$", "178,353", "$", "167,045"],
        # the spacer row with only whitespace is dropped
        ["Europe", "", "", "$", "111,032", "$", "101,328"],
    ]
    assert table.text.splitlines() == [
        "Segment | 2026 | 2025",
        "Americas | $ | 178,353 | $ | 167,045",
        "Europe | $ | 111,032 | $ | 101,328",
    ]
    assert all(b.rows is None for b in sample.blocks if b.kind != "table")


def test_sections_ignore_table_of_contents_and_cross_references(sample):
    sections = [b.section for b in sample.blocks]
    assert sections == [
        None, None, None, None,             # cover, TOC table (lists every item), PART I
        "Item 1", "Item 1", "Item 1",
        "Item 1A", "Item 1A", "Item 1A",    # includes the "See Item 7" cross-reference
        None,                               # PART II ends Item 1A
        "Item 7", "Item 7", "Item 7", "Item 7",
    ]  # fmt: skip
    d = sample.diagnostics
    assert d.sections_found == ["Item 1", "Item 1A", "Item 7"]
    assert d.missing_sections == ["Item 8"]
    assert d.uncertain_sections == []


def test_diagnostics_counts(sample):
    d = sample.diagnostics
    assert (d.block_count, d.heading_count, d.paragraph_count, d.table_count) == (15, 7, 6, 2)
    assert d.encoding == "utf-8"
    assert d.warnings == []


def test_source_locations_point_to_original_elements(sample):
    item_1 = sample.blocks[4]
    assert item_1.dom_path == "/html[1]/body[1]/div[5]"  # the hidden div still counts as div[1]
    assert item_1.element_id == "item1"
    segments = sample.blocks[13]
    assert segments.dom_path == "/html[1]/body[1]/ix:nonnumeric[1]/table[1]"
    assert segments.element_id == "segments"
    assert sample.source.content_sha256 == hashlib.sha256(SAMPLE).hexdigest()
    # Same bytes, same locations.
    assert extract_filing(make_document(SAMPLE)).blocks == sample.blocks


def test_duplicate_item_heading_is_reported_uncertain_not_assigned():
    html = b"""<html><body>
    <div><b>Item 1. Business</b></div><div>About us.</div>
    <div><b>Item 7. MD&amp;A</b></div><div>First MD&amp;A text.</div>
    <div><b>Item 7. MD&amp;A</b></div><div>Second MD&amp;A text.</div>
    <div><b>Item 8. Financial Statements</b></div><div>Statements.</div>
    </body></html>"""

    result = extract_filing(make_document(html))

    by_text = {b.text: b.section for b in result.blocks}
    assert by_text["About us."] == "Item 1"
    assert by_text["First MD&A text."] is None
    assert by_text["Second MD&A text."] is None
    assert by_text["Statements."] == "Item 8"
    assert result.diagnostics.uncertain_sections == ["Item 7"]
    assert result.diagnostics.missing_sections == ["Item 1A"]
    assert any("Item 7" in w for w in result.diagnostics.warnings)


def test_malformed_html_is_recovered():
    html = b"<html><body><div><b>Item 1. Business<div>Unclosed paragraph<p>Another one</body>"

    result = extract_filing(make_document(html))

    texts = [b.text for b in result.blocks]
    assert texts == ["Item 1. Business", "Unclosed paragraph", "Another one"]


def test_non_utf8_content_falls_back_to_windows_1252_with_warning():
    html = b"<html><body><p>The Company\x92s results</p></body></html>"

    result = extract_filing(make_document(html))

    assert result.blocks[0].text == "The Company’s results"
    assert result.diagnostics.encoding == "windows-1252"
    assert any("not valid UTF-8" in w for w in result.diagnostics.warnings)


def test_original_document_is_not_modified():
    document = make_document(SAMPLE)
    before = document.model_copy(deep=True)

    extract_filing(document)

    assert document == before
    assert document.content == SAMPLE


def test_bullets_separated_only_by_css_get_a_space():
    html = """<html><body>
    <div><span>•</span><span style="padding-left:18pt">iPhone</span></div>
    <div><span>◦</span><span>iPad</span></div>
    <div><span>▪</span><span>Mac</span></div>
    <div><span>• Watch</span></div>
    </body></html>""".encode()

    result = extract_filing(make_document(html))

    assert [b.text for b in result.blocks] == ["• iPhone", "◦ iPad", "▪ Mac", "• Watch"]


def test_10k_amendment_keeps_10k_section_detection(sample):
    result = extract_filing(make_document(SAMPLE, form="10-K/A"))

    assert [b.section for b in result.blocks] == [b.section for b in sample.blocks]
    assert result.diagnostics.sections_found == ["Item 1", "Item 1A", "Item 7"]


def test_extractor_version_is_recorded_and_stable(sample):
    assert EXTRACTOR_VERSION == "2"
    assert sample.extractor_version == EXTRACTOR_VERSION
    assert extract_filing(make_document(SAMPLE)).extractor_version == sample.extractor_version


# --- 10-Q sections (M2.1) ---------------------------------------------------------------


def html(*headings_and_paragraphs: str) -> bytes:
    """Body of bold heading divs ("#text") and plain paragraph divs ("text")."""
    divs = []
    for text in headings_and_paragraphs:
        if text.startswith("#"):
            divs.append(f'<div><span style="font-weight:700">{text[1:]}</span></div>')
        else:
            divs.append(f"<div><span>{text}</span></div>")
    return f"<html><body>{''.join(divs)}</body></html>".encode()


def sections_by_text(result) -> dict[str, str | None]:
    return {b.text: b.section for b in result.blocks if b.kind != "table"}


@pytest.mark.parametrize("form", ["10-Q", "10-Q/A"])
def test_10q_items_are_labeled_by_part(form):
    result = extract_filing(make_document(SAMPLE_10Q, form=form))

    assert [b.section for b in result.blocks] == [
        None, None, None,                                       # cover, TOC table, PART I
        "Part I, Item 1", "Part I, Item 1", "Part I, Item 1",   # heading, statements table, note
        "Part I, Item 2", "Part I, Item 2", "Part I, Item 2",   # MD&A incl. cross-reference
        "Part I, Item 3", "Part I, Item 3",
        "Part I, Item 4", "Part I, Item 4",
        None,                                                   # PART II
        "Part II, Item 1", "Part II, Item 1",
        "Part II, Item 1A", "Part II, Item 1A",
        "Part II, Item 2", "Part II, Item 2",
        "Part II, Item 6", "Part II, Item 6",
    ]  # fmt: skip
    d = result.diagnostics
    assert d.sections_found == [
        "Part I, Item 1", "Part I, Item 2", "Part I, Item 3", "Part I, Item 4",
        "Part II, Item 1", "Part II, Item 1A", "Part II, Item 2", "Part II, Item 6",
    ]  # fmt: skip
    assert (d.missing_sections, d.uncertain_sections, d.warnings) == ([], [], [])


def test_repeated_item_numbers_are_distinguished_by_part():
    result = extract_filing(make_document(SAMPLE_10Q, form="10-Q"))

    by_text = sections_by_text(result)
    assert by_text["Net sales increased during the third quarter compared to the same "
                   "quarter a year ago."] == "Part I, Item 2"  # fmt: skip
    assert by_text["The Company repurchased shares during the quarter."] == "Part II, Item 2"
    assert by_text["Disclosure controls and procedures were effective."] == "Part I, Item 4"


def test_table_of_contents_and_part_references_in_paragraphs_are_not_boundaries():
    result = extract_filing(make_document(SAMPLE_10Q, form="10-Q"))

    toc = result.blocks[1]
    assert (toc.kind, toc.element_id, toc.section) == ("table", "toc", None)
    reference = next(b for b in result.blocks if b.text.startswith("Part II, Item 1A of the"))
    assert (reference.kind, reference.section) == ("paragraph", "Part I, Item 2")


def test_10q_blocks_are_identical_to_10k_extraction_apart_from_sections():
    as_10q = extract_filing(make_document(SAMPLE_10Q, form="10-Q"))
    as_10k = extract_filing(make_document(SAMPLE_10Q, form="10-K"))

    def without_section(result):
        return [b.model_dump(exclude={"section"}) for b in result.blocks]

    assert without_section(as_10q) == without_section(as_10k)


def test_items_without_a_part_heading_are_never_inferred():
    result = extract_filing(make_document(html(
        "#Item 1. Financial Statements", "Statements.",
        "#Item 2. MD&amp;A", "Discussion.",
    ), form="10-Q"))  # fmt: skip

    assert all(b.section is None for b in result.blocks)
    d = result.diagnostics
    assert d.sections_found == []
    assert d.uncertain_sections == ["Item 1 (Part unknown)", "Item 2 (Part unknown)"]
    assert d.missing_sections == ["Part II, Item 1A"]  # Part I items are uncertain, not missing


def test_duplicate_items_in_a_part_are_unassigned_but_others_are_kept():
    result = extract_filing(make_document(html(
        "#PART I", "#Item 1. Financial Statements", "Statements.",
        "#Item 2. MD&amp;A", "First MD&amp;A.", "#Item 2. MD&amp;A", "Second MD&amp;A.",
        "#PART II", "#Item 2. Unregistered Sales", "Repurchases.",
    ), form="10-Q"))  # fmt: skip

    by_text = sections_by_text(result)
    assert by_text["Statements."] == "Part I, Item 1"
    assert by_text["First MD&A."] is None and by_text["Second MD&A."] is None
    assert by_text["Repurchases."] == "Part II, Item 2"  # same number, other Part: not a duplicate
    assert result.diagnostics.uncertain_sections == ["Part I, Item 2"]


def test_out_of_order_and_invalid_items_are_unassigned():
    result = extract_filing(make_document(html(
        "#PART I", "#Item 2. MD&amp;A", "Discussion.", "#Item 1. Financial Statements",
        "Statements.", "#Item 1A. Risk Factors", "Risks in the wrong Part.",
    ), form="10-Q"))  # fmt: skip

    by_text = sections_by_text(result)
    assert by_text["Discussion."] == "Part I, Item 2"
    assert by_text["Statements."] is None
    assert by_text["Risks in the wrong Part."] is None
    assert result.diagnostics.uncertain_sections == ["Part I, Item 1", "Part I, Item 1A"]


def test_part_headings_out_of_order_stop_all_later_labeling():
    # A table of contents written as bold divs (not a <table>) repeats the Part headings.
    result = extract_filing(make_document(html(
        "#PART I", "#Item 2. MD&amp;A", "#PART II", "#Item 1A. Risk Factors",
        "#PART I", "#Item 2. MD&amp;A", "Real discussion.",
        "#PART II", "#Item 1A. Risk Factors", "Real risks.",
    ), form="10-Q"))  # fmt: skip

    # Not even the table-of-contents lines before the break keep a label.
    assert all(b.section is None for b in result.blocks)
    assert result.diagnostics.sections_found == []
    assert {"Part I, Item 2", "Part II, Item 1A"} <= set(result.diagnostics.uncertain_sections)
    assert any("Unexpected 10-Q Part heading" in w for w in result.diagnostics.warnings)


def test_forms_without_section_rules_get_none_and_a_warning():
    result = extract_filing(make_document(SAMPLE, form="8-K"))

    assert all(b.section is None for b in result.blocks)
    d = result.diagnostics
    assert (d.sections_found, d.missing_sections, d.uncertain_sections) == ([], [], [])
    assert d.warnings == [
        "Section detection is not yet supported for 8-K filings; no sections assigned"
    ]

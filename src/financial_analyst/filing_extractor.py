"""Deterministic extraction of readable, traceable content from SEC filing HTML.

Turns a FilingDocument's bytes into ordered heading, paragraph and table blocks. Each
block records where it came from in the parsed HTML, and blocks are labeled with their
Form 10-K item ("Item 1A") only when the section boundaries are unambiguous.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.element import PreformattedString
from pydantic import BaseModel, ConfigDict

from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata

# Bump whenever a change could alter block text, kinds, order, indexes or locations for the
# same input bytes. Stored chunk/citation ids must include this and the source SHA-256.
EXTRACTOR_VERSION = "2"  # 2: Part-aware 10-Q section labels (M2.1)

# Elements whose content is never displayed as filing text. `ix:header` holds inline
# XBRL's hidden facts and contexts, which would otherwise read as duplicate narrative.
_SKIP_TAGS = frozenset(
    {"head", "script", "style", "noscript", "template", "iframe", "object", "embed", "ix:header"}
)
# Elements that start a new block of text. Anything else (span, a, b, font, ix:nonnumeric,
# ix:nonfraction, ...) is treated as inline, so its visible text stays in the surrounding block.
_BLOCK_TAGS = frozenset(
    {
        "html", "body", "div", "p", "li", "ul", "ol", "dl", "dt", "dd", "blockquote",
        "center", "pre", "section", "article", "header", "footer", "main", "nav", "aside",
        "hr", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)  # fmt: skip
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# A fully bold block longer than this is treated as an emphasized paragraph, not a heading.
_MAX_HEADING_CHARS = 300

_HIDDEN_STYLE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_FONT_WEIGHT = re.compile(r"font-weight\s*:\s*([a-z0-9]+)", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
# Filings often separate a bullet from its text with CSS padding rather than a space.
_BULLET_WITHOUT_SPACE = re.compile(r"([\u2022\u25e6\u25aa])(?=\S)")  # • ◦ ▪

# Forms whose section structure the extractor understands. 10-K items are numbered once
# across the document; a 10-Q numbers items separately within Part I and Part II.
_10K_FORMS = frozenset({"10-K", "10-K/A"})
_10Q_FORMS = frozenset({"10-Q", "10-Q/A"})
SECTION_DETECTION_FORMS = _10K_FORMS | _10Q_FORMS
# Form 10-K items in their required order.
_FORM_10K_ITEMS = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
)  # fmt: skip
PRIORITY_SECTIONS = ("Item 1", "Item 1A", "Item 7", "Item 8")
# Form 10-Q items allowed in each Part, in their required order.
_FORM_10Q_ITEMS = {"I": ("1", "2", "3", "4"), "II": ("1", "1A", "2", "3", "4", "5", "6")}
PRIORITY_10Q_SECTIONS = ("Part I, Item 1", "Part I, Item 2", "Part II, Item 1A")
_ITEM_HEADING = re.compile(r"item\s*(\d{1,2}[a-c]?)\b", re.IGNORECASE)
_PART_HEADING = re.compile(r"part\s+(iv|i{1,3})\b", re.IGNORECASE)

BlockKind = Literal["heading", "paragraph", "table"]


class ContentBlock(BaseModel):
    """One heading, paragraph or table, in document order."""

    model_config = ConfigDict(frozen=True)

    index: int  # 0-based position in document order
    kind: BlockKind
    # Normalized text. For tables: non-empty cells joined by " | ", one line per row.
    text: str
    # Tables only: the authoritative cell grid, rows and cells in original order.
    # A cell with colspan=n is followed by n-1 "" placeholders so columns stay aligned.
    # Placeholders look the same as genuinely empty cells, and rowspan is not expanded.
    rows: list[list[str]] | None = None
    # Form 10-K item such as "Item 1A", or None when unknown or uncertain.
    section: str | None
    # Location of the source element in the parsed HTML tree, e.g. "/html[1]/body[1]/div[3]".
    # Only meaningful for the exact bytes identified by ExtractedFiling.source.content_sha256.
    dom_path: str
    element_id: str | None  # the element's own HTML id attribute, if it has one


class ExtractionDiagnostics(BaseModel):
    """Counts and warnings about the extraction. Not proof of semantic correctness."""

    model_config = ConfigDict(frozen=True)

    encoding: str
    block_count: int
    paragraph_count: int
    heading_count: int
    table_count: int
    sections_found: list[str]
    # Priority 10-K sections with no heading found; empty when detection does not apply.
    missing_sections: list[str]
    uncertain_sections: list[str]  # headings found, but boundaries are ambiguous
    warnings: list[str]


class ExtractedFiling(BaseModel):
    model_config = ConfigDict(frozen=True)

    filing: FilingRef
    source: SourceMetadata  # identifies the exact bytes the dom_paths refer to
    extractor_version: str  # with source.content_sha256, identifies these exact blocks
    blocks: list[ContentBlock]
    diagnostics: ExtractionDiagnostics


def extract_filing(document: FilingDocument) -> ExtractedFiling:
    """Extract ordered content blocks from a filing's HTML. Does not modify `document`."""
    html, encoding, warnings = _decode(document.content)
    # html.parser is Python's built-in parser: it builds a tree only, never runs scripts
    # or fetches external resources, and recovers from malformed markup.
    soup = BeautifulSoup(html, "html.parser")

    raw_blocks: list[_RawBlock] = []
    root = _Container(dom_path="", element_id=None, is_heading_tag=False)
    _walk(soup, "", False, root, raw_blocks)
    _flush(root, raw_blocks)

    form = document.filing.form
    if form in _10K_FORMS:
        sections, (found, uncertain) = _assign_sections(raw_blocks, warnings)
        missing = [s for s in PRIORITY_SECTIONS if s not in found and s not in uncertain]
    elif form in _10Q_FORMS:
        sections, (found, uncertain) = _assign_10q_sections(raw_blocks, warnings)
        missing = _missing_10q_sections(found, uncertain)
    else:
        sections, found, uncertain, missing = [None] * len(raw_blocks), [], [], []
        warnings.append(
            f"Section detection is not yet supported for {form} filings; no sections assigned"
        )
    blocks = [
        ContentBlock(
            index=i,
            kind=raw.kind,
            text=raw.text,
            rows=raw.rows,
            section=sections[i],
            dom_path=raw.dom_path,
            element_id=raw.element_id,
        )
        for i, raw in enumerate(raw_blocks)
    ]
    if not blocks:
        warnings.append("No content blocks were extracted")

    kinds = Counter(block.kind for block in blocks)
    diagnostics = ExtractionDiagnostics(
        encoding=encoding,
        block_count=len(blocks),
        paragraph_count=kinds["paragraph"],
        heading_count=kinds["heading"],
        table_count=kinds["table"],
        sections_found=found,
        missing_sections=missing,
        uncertain_sections=uncertain,
        warnings=warnings,
    )
    return ExtractedFiling(
        filing=document.filing,
        source=document.source,
        extractor_version=EXTRACTOR_VERSION,
        blocks=blocks,
        diagnostics=diagnostics,
    )


def _decode(content: bytes) -> tuple[str, str, list[str]]:
    """Decode as UTF-8 (which also covers the ASCII that SEC filings usually declare).

    If the bytes are not valid UTF-8, fall back to windows-1252, the most common encoding
    of older EDGAR HTML, and say so in a warning rather than guessing silently.
    """
    try:
        return content.decode("utf-8-sig"), "utf-8", []
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
        warning = "Content is not valid UTF-8; decoded as windows-1252"
        replaced = text.count("�")
        if replaced:
            warning += f" with {replaced} undecodable bytes replaced"
        return text, "windows-1252", [warning]


# --- HTML walking ------------------------------------------------------------------------


@dataclass
class _RawBlock:
    kind: BlockKind
    text: str
    dom_path: str
    element_id: str | None
    rows: list[list[str]] | None = None


@dataclass
class _Container:
    """The block-level element whose inline text is currently being collected."""

    dom_path: str
    element_id: str | None
    is_heading_tag: bool
    fragments: list[tuple[str, bool]] = field(default_factory=list)  # (text, is_bold)


def _walk(node: Tag, path: str, bold: bool, container: _Container, out: list[_RawBlock]) -> None:
    """Visit `node`'s children in document order, appending finished blocks to `out`.

    Paths are built here as the tree is walked, from the original parse tree, so skipping
    hidden elements never shifts the positions recorded for visible ones.
    """
    tag_counts: Counter[str] = Counter()
    for child in node.children:
        if isinstance(child, PreformattedString):  # comments, <?xml ?>, doctype, CDATA
            continue
        if isinstance(child, NavigableString):
            container.fragments.append((str(child), bold))
            continue

        tag_counts[child.name] += 1
        child_path = f"{path}/{child.name}[{tag_counts[child.name]}]"
        if child.name in _SKIP_TAGS or _is_hidden(child):
            continue

        child_bold = _is_bold(child, bold)
        if child.name == "table":
            _flush(container, out)
            table = _table_block(child, child_path)
            if table is not None:
                out.append(table)
        elif child.name == "br":
            container.fragments.append((" ", child_bold))
        elif child.name in _BLOCK_TAGS:
            _flush(container, out)
            inner = _Container(child_path, child.get("id"), child.name in _HEADING_TAGS)
            _walk(child, child_path, child_bold, inner, out)
            _flush(inner, out)
        else:
            _walk(child, child_path, child_bold, container, out)


def _flush(container: _Container, out: list[_RawBlock]) -> None:
    """Turn the container's collected inline text into a heading or paragraph block."""
    text = _normalize("".join(text for text, _ in container.fragments))
    text = _BULLET_WITHOUT_SPACE.sub(r"\1 ", text)
    visible_fragments = [is_bold for text, is_bold in container.fragments if text.strip()]
    container.fragments.clear()
    if not text:
        return
    all_bold = all(visible_fragments)
    is_heading = container.is_heading_tag or (all_bold and len(text) <= _MAX_HEADING_CHARS)
    out.append(
        _RawBlock(
            kind="heading" if is_heading else "paragraph",
            text=text,
            dom_path=container.dom_path,
            element_id=container.element_id,
        )
    )


def _table_block(table: Tag, path: str) -> _RawBlock | None:
    rows = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:  # row of a nested table
            continue
        cells: list[str] = []
        for cell in tr.find_all(["td", "th"], recursive=False):
            if _is_hidden(cell):
                continue
            cells.append(_normalize(_visible_text(cell)))
            cells.extend([""] * (_colspan(cell) - 1))
        if any(cells):  # drop spacer rows with no text at all
            rows.append(cells)
    if not rows:
        return None
    text = "\n".join(" | ".join(cell for cell in row if cell) for row in rows)
    return _RawBlock("table", text, path, table.get("id"), rows)


def _visible_text(element: Tag) -> str:
    parts = []
    for child in element.children:
        if isinstance(child, PreformattedString):
            continue
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif child.name in _SKIP_TAGS or _is_hidden(child):
            continue
        elif child.name == "br" or child.name in _BLOCK_TAGS or child.name == "table":
            parts.append(f" {_visible_text(child)} ")
        else:
            parts.append(_visible_text(child))
    return "".join(parts)


def _is_hidden(tag: Tag) -> bool:
    return tag.has_attr("hidden") or bool(_HIDDEN_STYLE.search(tag.get("style", "")))


def _is_bold(tag: Tag, inherited: bool) -> bool:
    if tag.name in ("b", "strong") or tag.name in _HEADING_TAGS:
        return True
    match = _FONT_WEIGHT.search(tag.get("style", ""))
    if match is None:
        return inherited
    weight = match.group(1).lower()
    return weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600)


def _colspan(cell: Tag) -> int:
    try:
        return max(1, min(int(cell.get("colspan", 1)), 100))
    except ValueError:
        return 1


def _normalize(text: str) -> str:
    """Collapse whitespace runs (including non-breaking spaces) to single spaces."""
    return _WHITESPACE.sub(" ", text).strip()


# --- Section detection -------------------------------------------------------------------


def _assign_sections(
    blocks: list[_RawBlock], warnings: list[str]
) -> tuple[list[str | None], tuple[list[str], list[str]]]:
    """Label each block with its Form 10-K item, or None when that cannot be done reliably.

    Only heading blocks that *start* with "Item N" or "Part N" are boundaries. Tables of
    contents (tables) and cross-references inside sentences (paragraphs) are therefore
    ignored. Each boundary starts a region that runs until the next boundary. A region is
    labeled only if its item heading appears exactly once and in the expected order;
    otherwise it is left as None and reported as uncertain, never given to a neighbor.
    """
    boundaries: list[tuple[int, str | None]] = []  # (block index, item number or None)
    for i, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        item = _ITEM_HEADING.match(block.text)
        if item and item.group(1).upper() in _FORM_10K_ITEMS:
            boundaries.append((i, item.group(1).upper()))
        elif _PART_HEADING.match(block.text):
            boundaries.append((i, None))

    counts = Counter(item for _, item in boundaries if item)
    labels: dict[int, str | None] = {}
    found: list[str] = []
    uncertain: list[str] = []
    last_position = -1
    for i, item in boundaries:
        labels[i] = None
        if item is None:  # "PART II" ends the previous item without starting a new one
            continue
        name = f"Item {item}"
        position = _FORM_10K_ITEMS.index(item)
        if counts[item] > 1:
            if name not in uncertain:
                uncertain.append(name)
                warnings.append(
                    f"{name} heading appears {counts[item]} times; its content is unassigned"
                )
        elif position <= last_position:
            uncertain.append(name)
            warnings.append(f"{name} heading is out of order; its content is unassigned")
        else:
            labels[i] = name
            found.append(name)
            last_position = position

    if not boundaries:
        warnings.append("No Item headings detected; no content was assigned to sections")
    return _label_regions(len(blocks), labels), (found, uncertain)


def _assign_10q_sections(
    blocks: list[_RawBlock], warnings: list[str]
) -> tuple[list[str | None], tuple[list[str], list[str]]]:
    """Label 10-Q blocks such as "Part I, Item 2", or None when the evidence is insufficient.

    A 10-Q numbers items separately in Part I (financial information) and Part II (other
    information), so an item only means something together with the Part heading above it.
    Boundaries are heading blocks that start with "Part I"/"Part II" or "Item N"; tables
    (tables of contents) and paragraphs (cross-references) are ignored.

    Conservative rules: a Part heading is never inferred, so an item with no Part heading
    above it is unassigned. Parts must run I then II; any other Part heading (going back to
    Part I, or a Part III) means the Part structure cannot be trusted, and nothing in the
    document is labeled (a table of contents written as headings would otherwise take the
    labels). An item is labeled only if it is valid for its Part, occurs once in that Part,
    and comes in order. Everything else is unassigned and reported as uncertain.
    """
    boundaries: list[tuple[int, str | None, str | None]] = []  # (block index, part, item)
    current_part: str | None = None
    parts_trusted = True
    for i, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        part = _PART_HEADING.match(block.text)
        item = _ITEM_HEADING.match(block.text)
        if part:
            roman = part.group(1).upper()
            if parts_trusted and (
                roman not in _FORM_10Q_ITEMS or (current_part, roman) == ("II", "I")
            ):
                parts_trusted = False
                warnings.append(
                    f"Unexpected 10-Q Part heading {block.text[:40]!r}; "
                    "content from here on is unassigned"
                )
            current_part = roman if parts_trusted else None
            boundaries.append((i, None, None))  # a Part heading ends the previous item
        elif item:
            boundaries.append((i, current_part, item.group(1).upper()))

    counts = Counter((part, item) for _, part, item in boundaries if part and item)
    labels: dict[int, str | None] = {}
    found: list[str] = []
    uncertain: list[str] = []

    def mark_uncertain(name: str, problem: str) -> None:
        if name not in uncertain:
            uncertain.append(name)
            warnings.append(f"{name} heading {problem}; its content is unassigned")

    last_position = (-1, -1)
    for i, part, item in boundaries:
        labels[i] = None
        if item is None:
            continue
        if part is None:
            mark_uncertain(f"Item {item} (Part unknown)", "has no trusted Part heading above it")
            continue
        name = f"Part {part}, Item {item}"
        if item not in _FORM_10Q_ITEMS[part]:
            mark_uncertain(name, f"is not a valid Part {part} item in a 10-Q")
        elif counts[(part, item)] > 1:
            mark_uncertain(name, f"appears {counts[(part, item)]} times")
        else:
            position = (0 if part == "I" else 1, _FORM_10Q_ITEMS[part].index(item))
            if position <= last_position:
                mark_uncertain(name, "is out of order")
            else:
                labels[i] = name
                found.append(name)
                last_position = position

    if not parts_trusted:  # discard every label, including ones given before the break
        for name in found:
            mark_uncertain(name, "may belong to an untrusted Part structure")
        labels, found = {}, []
    if not any(item for _, _, item in boundaries):
        warnings.append("No Item headings detected; no content was assigned to sections")
    return _label_regions(len(blocks), labels), (found, uncertain)


def _missing_10q_sections(found: list[str], uncertain: list[str]) -> list[str]:
    """Priority 10-Q sections with no heading at all. An item seen without a Part heading
    is uncertain rather than missing, for whichever Part it might belong to."""
    missing = []
    for name in PRIORITY_10Q_SECTIONS:
        item = name.split("Item ")[1]
        if name not in found and name not in uncertain:
            if f"Item {item} (Part unknown)" not in uncertain:
                missing.append(name)
    return missing


def _label_regions(block_count: int, labels: dict[int, str | None]) -> list[str | None]:
    """Each boundary's label applies to it and every following block until the next one."""
    sections: list[str | None] = []
    current = None
    for i in range(block_count):
        if i in labels:
            current = labels[i]
        sections.append(current)
    return sections

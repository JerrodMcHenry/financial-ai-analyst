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

# Form 10-K items in their required order.
_FORM_10K_ITEMS = (
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
)  # fmt: skip
PRIORITY_SECTIONS = ("Item 1", "Item 1A", "Item 7", "Item 8")
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
    missing_sections: list[str]  # priority sections with no heading found
    uncertain_sections: list[str]  # headings found, but boundaries are ambiguous
    warnings: list[str]


class ExtractedFiling(BaseModel):
    model_config = ConfigDict(frozen=True)

    filing: FilingRef
    source: SourceMetadata  # identifies the exact bytes the dom_paths refer to
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

    sections, section_report = _assign_sections(raw_blocks, warnings)
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
    found, uncertain = section_report
    diagnostics = ExtractionDiagnostics(
        encoding=encoding,
        block_count=len(blocks),
        paragraph_count=kinds["paragraph"],
        heading_count=kinds["heading"],
        table_count=kinds["table"],
        sections_found=found,
        missing_sections=[s for s in PRIORITY_SECTIONS if s not in found and s not in uncertain],
        uncertain_sections=uncertain,
        warnings=warnings,
    )
    return ExtractedFiling(
        filing=document.filing, source=document.source, blocks=blocks, diagnostics=diagnostics
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

    sections: list[str | None] = []
    current = None
    for i in range(len(blocks)):
        if i in labels:
            current = labels[i]
        sections.append(current)
    return sections, (found, uncertain)

"""Typed contracts for data retrieved from SEC EDGAR, including provenance."""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    computed_field,
    model_validator,
)

ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"


class SourceMetadata(BaseModel):
    """Provenance for a single HTTP retrieval."""

    model_config = ConfigDict(frozen=True)

    source_url: HttpUrl
    retrieved_at: AwareDatetime
    http_status: int
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FilingRef(BaseModel):
    """One filing as listed in a company's EDGAR submissions history."""

    model_config = ConfigDict(frozen=True)

    cik: str = Field(pattern=r"^\d{10}$")
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    form: str = Field(min_length=1)  # amendments such as "10-K/A" are kept distinct
    filing_date: date
    report_date: date | None
    acceptance_datetime: datetime | None
    primary_document: str | None
    is_xbrl: bool
    is_inline_xbrl: bool

    @computed_field
    @property
    def primary_document_url(self) -> str | None:
        """Canonical archive URL, or None when EDGAR lists no primary document."""
        if not self.primary_document:
            return None
        folder = self.accession_number.replace("-", "")
        return f"{ARCHIVES_BASE_URL}/{int(self.cik)}/{folder}/{self.primary_document}"


class CompanySubmissions(BaseModel):
    """Company profile and recent filings from data.sec.gov/submissions."""

    model_config = ConfigDict(frozen=True)

    cik: str = Field(pattern=r"^\d{10}$")
    name: str
    tickers: list[str]
    exchanges: list[str | None]
    sic: str | None
    sic_description: str | None
    fiscal_year_end: str | None = Field(pattern=r"^\d{4}$")  # "MMDD"
    recent_filings: list[FilingRef]
    # EDGAR pages older filings into separate files; fetching them is Milestone 1.
    has_more_filings: bool
    source: SourceMetadata


class FilingDocument(BaseModel):
    """A filing's primary document, exactly as SEC served it.

    The link to the filing comes from trusted submissions metadata and the archive URL
    derived from it; the HTML content itself does not prove which filing it is.

    `content` is the response body after httpx undoes HTTP compression (SEC serves gzip),
    not the compressed bytes sent over the network. `source.content_sha256` hashes these
    same decompressed bytes. They are not decoded to text or parsed.
    """

    model_config = ConfigDict(frozen=True)

    filing: FilingRef
    content: bytes = Field(repr=False)
    source: SourceMetadata

    @model_validator(mode="after")
    def _check_provenance(self) -> "FilingDocument":
        if str(self.source.source_url) != self.filing.primary_document_url:
            raise ValueError("source URL is not this filing's primary document URL")
        if hashlib.sha256(self.content).hexdigest() != self.source.content_sha256:
            raise ValueError("content does not match its SHA-256 hash")
        return self


class FactConcept(BaseModel):
    """SEC's label and description for one XBRL concept, stored once per concept."""

    model_config = ConfigDict(frozen=True)

    taxonomy: str  # "us-gaap", "dei", "srt", "ifrs-full", ...
    concept: str  # "Revenues"
    label: str | None
    description: str | None


class FactObservation(BaseModel):
    """One Company Facts row exactly as SEC reported it. Never merged with other rows."""

    model_config = ConfigDict(frozen=True)

    cik: str = Field(pattern=r"^\d{10}$")
    taxonomy: str
    concept: str
    unit: str  # exactly as SEC keys it: "USD", "EUR", "shares", "USD/shares", "pure", ...
    value: Decimal  # exact; never a binary float
    period_start: date | None  # None means an instant fact (e.g. a balance on period_end)
    period_end: date
    # SEC's "fy"/"fp" describe the FILING that reported this row, not the row's own period.
    # A 10-K for fiscal 2026 reports fiscal 2024 comparatives with filing_fiscal_year=2026.
    filing_fiscal_year: int | None
    filing_fiscal_period: str | None
    form: str = Field(min_length=1)
    filed: date
    accession_number: str = Field(pattern=r"^\d{10}-\d{2}-\d{6}$")
    frame: str | None  # SEC's calendar-period tag; informational, not used for selection

    @model_validator(mode="after")
    def _check_period(self) -> "FactObservation":
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start is after period_end")
        return self

    @computed_field
    @property
    def filing_index_url(self) -> str:
        """EDGAR archive folder of the filing that reported this row."""
        return f"{ARCHIVES_BASE_URL}/{int(self.cik)}/{self.accession_number.replace('-', '')}/"


class SkippedObservation(BaseModel):
    """Where a malformed Company Facts row was found and why it was not loaded."""

    model_config = ConfigDict(frozen=True)

    taxonomy: str
    concept: str
    unit: str
    row_index: int  # position within SEC's list for that concept and unit
    reason: str


class CompanyFacts(BaseModel):
    """All XBRL facts SEC publishes for one company, from data.sec.gov/api/xbrl/companyfacts."""

    model_config = ConfigDict(frozen=True)

    cik: str = Field(pattern=r"^\d{10}$")
    entity_name: str
    concepts: list[FactConcept]
    observations: list[FactObservation]  # every valid row, in SEC's order
    skipped_count: int  # total malformed rows not loaded
    skipped_sample: list[SkippedObservation]  # the first few, for diagnosis
    source: SourceMetadata


# --- Structured-data snapshots (M3.2) ----------------------------------------------------

# The SEC JSON resources that are stored as immutable snapshots.
SnapshotKind = Literal["submissions", "company_facts", "company_tickers"]


class SnapshotMetadata(BaseModel):
    """Provenance of one stored SEC JSON response. Never changes once written."""

    model_config = ConfigDict(frozen=True)

    kind: SnapshotKind
    cik: str | None = Field(pattern=r"^\d{10}$")  # None only for company_tickers (all companies)
    source: SourceMetadata  # URL, original retrieved_at, HTTP status, full SHA-256 of the bytes

    @model_validator(mode="after")
    def _check_cik(self) -> "SnapshotMetadata":
        if (self.kind == "company_tickers") != (self.cik is None):
            raise ValueError("company_tickers snapshots have no CIK; every other kind needs one")
        return self


class Snapshot(BaseModel):
    """The exact bytes of one SEC JSON response, with its metadata.

    The bytes are kept as served (after HTTP decompression), never re-serialized, so the
    SHA-256 identifies exactly what SEC returned.
    """

    model_config = ConfigDict(frozen=True)

    metadata: SnapshotMetadata
    content: bytes = Field(repr=False)

    @model_validator(mode="after")
    def _check_hash(self) -> "Snapshot":
        if hashlib.sha256(self.content).hexdigest() != self.metadata.source.content_sha256:
            raise ValueError("content does not match its SHA-256 hash")
        return self


class TickerEntry(BaseModel):
    """One row of SEC's company_tickers.json."""

    model_config = ConfigDict(frozen=True)

    cik: str = Field(pattern=r"^\d{10}$")
    ticker: str = Field(min_length=1)  # exactly as SEC lists it
    title: str  # SEC's company title, e.g. "Apple Inc."


class TickerMap(BaseModel):
    """SEC's ticker-to-CIK mapping, in SEC's order. A ticker can appear more than once."""

    model_config = ConfigDict(frozen=True)

    entries: list[TickerEntry]
    source: SourceMetadata

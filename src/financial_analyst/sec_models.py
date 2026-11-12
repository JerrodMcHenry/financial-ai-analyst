"""Typed contracts for data retrieved from SEC EDGAR, including provenance."""

from datetime import date, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, computed_field

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

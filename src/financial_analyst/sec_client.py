"""HTTP client for SEC EDGAR.

Implements SEC fair-access requirements: an identifying User-Agent, conservative
request pacing, explicit timeouts, and bounded retries with backoff.
"""

import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import ValidationError

from financial_analyst.config import Settings
from financial_analyst.sec_models import (
    CompanyFacts,
    CompanySubmissions,
    FactConcept,
    FactObservation,
    FilingDocument,
    FilingRef,
    SkippedObservation,
    SourceMetadata,
)

logger = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
MAX_SKIPPED_SAMPLE = 20

CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Document retrieval is limited to the periodic reports this product analyzes.
DOCUMENT_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
# The only URL shape get_filing_document will request: https, www.sec.gov, a numeric CIK,
# an 18-digit accession folder, and a plain .htm/.html filename (no "/", "..", "?" or "#").
_ARCHIVE_URL_PATTERN = re.compile(
    r"https://www\.sec\.gov/Archives/edgar/data/[0-9]{1,10}/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9._-]*\.html?"
)
# Phrases from SEC's block pages, which can be served as HTML instead of a filing.
_SEC_ERROR_PAGE_MARKERS = (b"Request Rate Threshold Exceeded", b"Undeclared Automated Tool")

_CIK_PATTERN = re.compile(r"[0-9]{1,10}")


class SecError(Exception):
    """Any failure talking to SEC. Callers never see raw httpx exceptions.

    `status_code` is the last HTTP status received, or None for timeouts and
    connection failures.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SecNotFoundError(SecError):
    """EDGAR has no resource at this URL (e.g. unknown CIK)."""


class SecResponseError(SecError):
    """EDGAR responded, but the body was not in the expected shape."""


def normalize_cik(cik: str | int) -> str:
    """Validate a CIK and zero-pad it to the 10 digits EDGAR URLs require."""
    text = str(cik).strip()
    if isinstance(cik, bool) or not _CIK_PATTERN.fullmatch(text):
        raise ValueError(f"CIK must be 1-10 ASCII digits, got {cik!r}")
    return text.zfill(10)


class SecClient:
    """Synchronous EDGAR client. Create one per process so pacing is shared."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        # `transport` is httpx's hook for swapping the network layer; tests pass a MockTransport.
        self._min_interval = 1.0 / settings.sec_max_requests_per_second
        self._next_request_at = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(
            headers={"User-Agent": settings.sec_user_agent},
            timeout=httpx.Timeout(10.0, connect=CONNECT_TIMEOUT_SECONDS, read=READ_TIMEOUT_SECONDS),
            transport=transport,
        )

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get_submissions(self, cik: str | int) -> CompanySubmissions:
        """Fetch a company's profile and recent filings."""
        cik = normalize_cik(cik)
        payload, source = self._get_json(SUBMISSIONS_URL.format(cik=cik))
        return _parse_submissions(payload, cik, source)

    def get_filing_document(self, filing: FilingRef) -> FilingDocument:
        """Download a filing's primary HTML document, keeping the exact response bytes."""
        url = filing_document_url(filing)
        response = self._get(url)
        _check_html_filing(response, url)
        return FilingDocument(
            filing=filing, content=response.content, source=_source_metadata(url, response)
        )

    def get_company_facts(self, cik: str | int) -> CompanyFacts:
        """Fetch every XBRL fact SEC publishes for a company. Not cached: it changes over time."""
        cik = normalize_cik(cik)
        payload, source = self._get_json(COMPANY_FACTS_URL.format(cik=cik))
        return _parse_company_facts(payload, cik, source)

    def _get_json(self, url: str) -> tuple[dict[str, Any], SourceMetadata]:
        response = self._get(url)
        try:
            # parse_float=Decimal keeps reported numbers exact; floats never enter values.
            payload = json.loads(response.content, parse_float=Decimal)
        except ValueError as exc:
            raise SecResponseError(f"Invalid JSON from {url}") from exc
        if not isinstance(payload, dict):
            raise SecResponseError(f"Expected a JSON object from {url}")
        return payload, _source_metadata(url, response)

    def _get(self, url: str) -> httpx.Response:
        """GET with pacing and bounded retries; raises SecError on failure."""
        max_attempts = MAX_RETRIES + 1
        for attempt in range(1, max_attempts + 1):
            self._pace()
            retry_after = None
            try:
                response = self._http.get(url)
            except httpx.TransportError as exc:  # includes all timeouts
                error = SecError(f"{type(exc).__name__} requesting {url}")
            else:
                status = response.status_code
                if response.is_success:
                    return response
                if status == 404:
                    raise SecNotFoundError(f"Not found: {url}", status)
                if status not in RETRYABLE_STATUS_CODES:
                    # 403 is what SEC returns for a missing User-Agent or excessive traffic.
                    raise SecError(f"Unexpected HTTP {status} from {url}", status)
                error = SecError(f"HTTP {status} from {url}", status)
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))

            if attempt == max_attempts:
                break
            if retry_after is not None and retry_after > BACKOFF_MAX_SECONDS:
                # Retrying sooner than SEC asked would violate fair access, so give up now.
                break
            delay = _backoff_delay(attempt, retry_after)
            logger.warning(
                "SEC request failed (%s); retry %d/%d in %.1fs", error, attempt, MAX_RETRIES, delay
            )
            time.sleep(delay)
        raise error

    def _pace(self) -> None:
        """Block until the minimum interval since the previous request has elapsed."""
        with self._lock:
            now = time.monotonic()
            wait = self._next_request_at - now
            if wait > 0:
                time.sleep(wait)
            self._next_request_at = max(now, self._next_request_at) + self._min_interval


def _source_metadata(url: str, response: httpx.Response) -> SourceMetadata:
    return SourceMetadata(
        source_url=url,
        retrieved_at=datetime.now(UTC),
        http_status=response.status_code,
        content_sha256=hashlib.sha256(response.content).hexdigest(),
    )


def filing_document_url(filing: FilingRef) -> str:
    """Return the filing's archive URL, or raise ValueError before any request is sent."""
    if filing.form not in DOCUMENT_FORMS:
        raise ValueError(
            f"Document retrieval supports {sorted(DOCUMENT_FORMS)}, got {filing.form!r}"
        )
    url = filing.primary_document_url
    if url is None:
        raise ValueError(f"Filing {filing.accession_number} lists no primary document")
    if not _ARCHIVE_URL_PATTERN.fullmatch(url):
        raise ValueError(f"Not an expected EDGAR archive document URL: {url!r}")
    return url


def _check_html_filing(response: httpx.Response, url: str) -> None:
    """Reject 200 responses that are plausibly not a filing. This is a sanity check,
    not proof of identity; identity comes from the URL derived from submissions data."""
    content_type = response.headers.get("Content-Type", "")
    if not content_type.lower().startswith("text/html"):
        raise SecResponseError(f"Expected HTML from {url}, got Content-Type {content_type!r}")
    body = response.content
    if not body.strip():
        raise SecResponseError(f"Empty document from {url}")
    if b"<html" not in body[:8192].lower():
        raise SecResponseError(f"Response from {url} does not start like an HTML document")
    if any(marker in body for marker in _SEC_ERROR_PAGE_MARKERS):
        raise SecResponseError(f"SEC returned an error page instead of the filing at {url}")


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, never shorter than Retry-After, capped."""
    delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1) + random.uniform(0, BACKOFF_BASE_SECONDS)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return min(delay, BACKOFF_MAX_SECONDS)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After given in seconds. The HTTP-date form is ignored."""
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _parse_submissions(
    payload: dict[str, Any], cik: str, source: SourceMetadata
) -> CompanySubmissions:
    """Convert EDGAR's submissions JSON into typed records.

    EDGAR lists recent filings column by column: one array per field, where index i
    of every array describes filing i.
    """
    try:
        if normalize_cik(payload["cik"]) != cik:
            raise SecResponseError(
                f"Response CIK {payload['cik']!r} does not match requested {cik}"
            )

        recent = payload["filings"]["recent"]
        row_count = len(recent["accessionNumber"])
        columns = [
            "accessionNumber",
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "form",
            "primaryDocument",
            "isXBRL",
            "isInlineXBRL",
        ]
        if any(len(recent[column]) != row_count for column in columns):
            raise SecResponseError("Filing arrays in submissions response have different lengths")

        filings = []
        for i in range(row_count):
            filings.append(
                FilingRef(
                    cik=cik,
                    accession_number=recent["accessionNumber"][i],
                    form=recent["form"][i],
                    filing_date=recent["filingDate"][i],
                    # EDGAR uses "" for missing values; store None rather than guess.
                    report_date=recent["reportDate"][i] or None,
                    acceptance_datetime=recent["acceptanceDateTime"][i] or None,
                    primary_document=recent["primaryDocument"][i] or None,
                    is_xbrl=recent["isXBRL"][i],
                    is_inline_xbrl=recent["isInlineXBRL"][i],
                )
            )

        return CompanySubmissions(
            cik=cik,
            name=payload["name"],
            tickers=payload.get("tickers", []),
            exchanges=payload.get("exchanges", []),
            sic=payload.get("sic") or None,
            sic_description=payload.get("sicDescription") or None,
            fiscal_year_end=payload.get("fiscalYearEnd") or None,
            recent_filings=filings,
            has_more_filings=bool(payload["filings"].get("files")),
            source=source,
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise SecResponseError(f"Malformed submissions response for CIK {cik}: {exc}") from exc


def _parse_company_facts(payload: dict[str, Any], cik: str, source: SourceMetadata) -> CompanyFacts:
    """Convert facts[taxonomy][concept]["units"][unit] = [rows] into typed records.

    The overall structure must be valid or the whole response is rejected. Individual
    malformed rows are skipped and counted; if every supplied row is malformed, the
    response is rejected rather than returned as if the company had no facts.
    """
    try:
        if normalize_cik(payload["cik"]) != cik:
            raise SecResponseError(
                f"Response CIK {payload['cik']!r} does not match requested {cik}"
            )
        entity_name = payload["entityName"]
        facts = payload["facts"]
        if not isinstance(entity_name, str) or not isinstance(facts, dict):
            raise SecResponseError("Company Facts response has no entity name or facts object")

        concepts: list[FactConcept] = []
        observations: list[FactObservation] = []
        skipped: list[SkippedObservation] = []
        skipped_count = 0
        for taxonomy, taxonomy_facts in facts.items():
            for concept, details in taxonomy_facts.items():
                concepts.append(
                    FactConcept(
                        taxonomy=taxonomy,
                        concept=concept,
                        label=details.get("label"),
                        description=details.get("description"),
                    )
                )
                for unit, rows in details["units"].items():
                    if not isinstance(rows, list):
                        raise SecResponseError(f"Rows for {taxonomy}:{concept} [{unit}] not a list")
                    for row_index, row in enumerate(rows):
                        try:
                            observations.append(_parse_fact_row(cik, taxonomy, concept, unit, row))
                        except (KeyError, TypeError, ValueError) as exc:
                            skipped_count += 1
                            if len(skipped) < MAX_SKIPPED_SAMPLE:
                                skipped.append(
                                    SkippedObservation(
                                        taxonomy=taxonomy,
                                        concept=concept,
                                        unit=unit,
                                        row_index=row_index,
                                        reason=_row_error_reason(exc),
                                    )
                                )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise SecResponseError(f"Malformed Company Facts response for CIK {cik}: {exc}") from exc

    if skipped_count and not observations:
        raise SecResponseError(
            f"All {skipped_count} Company Facts rows for CIK {cik} are malformed"
        )
    return CompanyFacts(
        cik=cik,
        entity_name=entity_name,
        concepts=concepts,
        observations=observations,
        skipped_count=skipped_count,
        skipped_sample=skipped,
        source=source,
    )


def _parse_fact_row(cik: str, taxonomy: str, concept: str, unit: str, row: Any) -> FactObservation:
    if not isinstance(row, dict):
        raise TypeError("row is not a JSON object")
    value = row["val"]
    # bool is a subclass of int in Python, and NaN/Infinity arrive as floats; reject both.
    is_integer = isinstance(value, int) and not isinstance(value, bool)
    if not (is_integer or (isinstance(value, Decimal) and value.is_finite())):
        raise ValueError(f"value is not a finite number: {value!r}")
    return FactObservation(
        cik=cik,
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        value=Decimal(value),
        period_start=row.get("start"),
        period_end=row["end"],
        filing_fiscal_year=row.get("fy"),
        filing_fiscal_period=row.get("fp"),
        form=row["form"],
        filed=row["filed"],
        accession_number=row["accn"],
        frame=row.get("frame"),
    )


def _row_error_reason(exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return f"missing field {exc.args[0]!r}"
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        return f"{location}: {error['msg']}" if location else error["msg"]
    return str(exc)

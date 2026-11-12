"""SEC client tests. All HTTP is served by httpx.MockTransport; nothing touches the network."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from financial_analyst import sec_client
from financial_analyst.sec_client import (
    MAX_RETRIES,
    SecClient,
    SecError,
    SecNotFoundError,
    SecResponseError,
    normalize_cik,
)
from financial_analyst.sec_models import FilingDocument, FilingRef

FIXTURE = Path(__file__).parent / "fixtures" / "submissions_sample.json"
APPLE_URL = "https://data.sec.gov/submissions/CIK0000320193.json"


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record requested sleeps instead of waiting, so pacing and backoff are observable."""
    recorded: list[float] = []
    monkeypatch.setattr(sec_client.time, "sleep", recorded.append)
    return recorded


def make_client(settings, responses):
    """Build a client whose transport returns `responses` in order.

    Each item is an httpx.Response or an exception to raise. Returns the client
    and the list of requests it sent.
    """
    queue = list(responses)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = SecClient(settings, transport=httpx.MockTransport(handler))
    return client, requests


def test_get_submissions_parses_filings_with_provenance(settings):
    body = FIXTURE.read_bytes()
    client, requests = make_client(settings, [httpx.Response(200, content=body)])

    result = client.get_submissions("320193")

    assert str(requests[0].url) == APPLE_URL
    assert requests[0].headers["User-Agent"] == "Test Suite test@example.com"

    assert result.cik == "0000320193"
    assert result.name == "Apple Inc."
    assert result.fiscal_year_end == "0930"
    assert result.has_more_filings is True

    ten_k = result.recent_filings[0]
    assert ten_k.form == "10-K"
    assert ten_k.accession_number == "0000320193-23-000106"
    assert ten_k.report_date.isoformat() == "2023-09-30"
    assert ten_k.primary_document_url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
    )

    # Empty strings from EDGAR become None rather than invented values.
    no_doc = result.recent_filings[2]
    assert no_doc.report_date is None
    assert no_doc.primary_document_url is None

    assert str(result.source.source_url) == APPLE_URL
    assert result.source.http_status == 200
    assert result.source.content_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("bad_cik", ["", "abc", "12345678901", "-1", "１２３", True])
def test_invalid_cik_is_rejected_before_any_request(settings, bad_cik):
    client, requests = make_client(settings, [])

    with pytest.raises(ValueError):
        client.get_submissions(bad_cik)
    assert requests == []


def test_normalize_cik_zero_pads():
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik(" 0000320193 ") == "0000320193"


def test_retries_server_error_then_succeeds(settings, sleeps):
    client, requests = make_client(
        settings, [httpx.Response(503), httpx.Response(200, content=FIXTURE.read_bytes())]
    )

    result = client.get_submissions(320193)

    assert result.name == "Apple Inc."
    assert len(requests) == 2
    assert any(delay >= sec_client.BACKOFF_BASE_SECONDS for delay in sleeps)


def test_gives_up_after_bounded_retries_on_timeout(settings):
    attempts = MAX_RETRIES + 1
    client, requests = make_client(settings, [httpx.ReadTimeout("slow")] * attempts)

    with pytest.raises(SecError) as excinfo:
        client.get_submissions(320193)
    assert excinfo.value.status_code is None
    assert len(requests) == attempts


def test_honors_retry_after_on_429(settings, sleeps):
    client, _ = make_client(
        settings,
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=FIXTURE.read_bytes()),
        ],
    )

    client.get_submissions(320193)

    assert 7 in sleeps


def test_stops_when_retry_after_exceeds_backoff_cap(settings):
    client, requests = make_client(settings, [httpx.Response(429, headers={"Retry-After": "600"})])

    with pytest.raises(SecError) as excinfo:
        client.get_submissions(320193)
    assert excinfo.value.status_code == 429
    assert len(requests) == 1


@pytest.mark.parametrize(("status", "error_type"), [(404, SecNotFoundError), (403, SecError)])
def test_non_retryable_status_raises_immediately(settings, status, error_type):
    client, requests = make_client(settings, [httpx.Response(status)])

    with pytest.raises(SecError) as excinfo:
        client.get_submissions(320193)
    assert type(excinfo.value) is error_type
    assert excinfo.value.status_code == status
    assert len(requests) == 1


def _fixture_with(**changes):
    payload = json.loads(FIXTURE.read_text())
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not json</html>",
        json.dumps(_fixture_with(cik="789019")).encode(),  # a different company
        json.dumps(
            _fixture_with(
                filings={"recent": {**_fixture_with()["filings"]["recent"], "form": ["10-K"]}}
            )
        ).encode(),  # misaligned columns
    ],
    ids=["not-json", "cik-mismatch", "misaligned-columns"],
)
def test_malformed_response_raises_response_error(settings, body):
    client, _ = make_client(settings, [httpx.Response(200, content=body)])

    with pytest.raises(SecResponseError):
        client.get_submissions(320193)


def test_requests_are_paced(settings, sleeps):
    body = FIXTURE.read_bytes()
    client, _ = make_client(
        settings, [httpx.Response(200, content=body), httpx.Response(200, content=body)]
    )

    client.get_submissions(320193)
    client.get_submissions(320193)

    min_interval = 1 / settings.sec_max_requests_per_second
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= min_interval


# --- Filing document retrieval -------------------------------------------------------

DOCUMENT_URL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/aapl-20260926.htm"
# Starts like a real inline-XBRL filing; \xa0 is a non-UTF-8 byte that must survive untouched.
FILING_HTML = b"<?xml version='1.0' encoding='ASCII'?>\n<html><body>Annual report\xa0</body></html>"
HTML = {"Content-Type": "text/html"}


def make_filing(**overrides):
    fields = {
        "cik": "0000320193",
        "accession_number": "0000320193-26-000026",
        "form": "10-K",
        "filing_date": "2026-11-03",
        "report_date": "2026-09-26",
        "acceptance_datetime": None,
        "primary_document": "aapl-20260926.htm",
        "is_xbrl": True,
        "is_inline_xbrl": True,
    }
    return FilingRef(**(fields | overrides))


def test_get_filing_document_keeps_exact_bytes_and_provenance(settings):
    filing = make_filing()
    client, requests = make_client(
        settings, [httpx.Response(200, content=FILING_HTML, headers=HTML)]
    )

    document = client.get_filing_document(filing)

    assert str(requests[0].url) == DOCUMENT_URL
    assert document.content == FILING_HTML
    assert document.filing == filing
    assert str(document.source.source_url) == DOCUMENT_URL
    assert document.source.http_status == 200
    assert document.source.content_sha256 == hashlib.sha256(FILING_HTML).hexdigest()


@pytest.mark.parametrize(
    "overrides",
    [
        {"primary_document": None},
        {"primary_document": "../../evil.htm"},
        {"primary_document": "aapl.htm?redirect=elsewhere"},
        {"primary_document": "aapl-20260926.txt"},
        {"form": "4", "primary_document": "xslF345X05/form4.xml"},
    ],
    ids=["missing", "path-traversal", "query-string", "not-html", "unsupported-form"],
)
def test_invalid_filing_is_rejected_before_any_request(settings, overrides):
    client, requests = make_client(settings, [])

    with pytest.raises(ValueError):
        client.get_filing_document(make_filing(**overrides))
    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"", headers=HTML),
        httpx.Response(
            200, content=b'{"error": "x"}', headers={"Content-Type": "application/json"}
        ),
        httpx.Response(200, content=b"Annual report as plain text", headers=HTML),
        httpx.Response(
            200,
            content=b"<html><body><h1>Request Rate Threshold Exceeded</h1></body></html>",
            headers=HTML,
        ),
    ],
    ids=["empty", "wrong-content-type", "not-html", "sec-error-page"],
)
def test_implausible_document_response_is_rejected(settings, response):
    client, _ = make_client(settings, [response])

    with pytest.raises(SecResponseError):
        client.get_filing_document(make_filing())


def test_document_retrieval_reuses_retries(settings):
    client, requests = make_client(
        settings,
        [httpx.Response(503), httpx.Response(200, content=FILING_HTML, headers=HTML)],
    )

    document = client.get_filing_document(make_filing())

    assert document.content == FILING_HTML
    assert len(requests) == 2


def test_document_redirect_is_not_followed(settings):
    redirect = httpx.Response(301, headers={"Location": "https://example.com/aapl.htm"})
    client, requests = make_client(settings, [redirect])

    with pytest.raises(SecError) as excinfo:
        client.get_filing_document(make_filing())
    assert excinfo.value.status_code == 301
    assert len(requests) == 1


def test_filing_document_rejects_content_that_does_not_match_its_hash(settings):
    client, _ = make_client(settings, [httpx.Response(200, content=FILING_HTML, headers=HTML)])
    document = client.get_filing_document(make_filing())

    with pytest.raises(ValidationError):
        FilingDocument(filing=document.filing, content=b"tampered", source=document.source)

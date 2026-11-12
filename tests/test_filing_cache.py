"""Filing cache tests: temporary directories and mocked SEC responses only."""

import hashlib
import json
import os
from datetime import UTC, datetime

import httpx
import pytest

from financial_analyst import filing_cache, sec_client
from financial_analyst.filing_cache import (
    fetch_filing_document,
    load_filing_document,
    save_filing_document,
)
from financial_analyst.sec_client import SecClient
from financial_analyst.sec_models import FilingDocument, FilingRef, SourceMetadata

HTML_A = b"<html><body>Annual report, first download</body></html>"
HTML_B = b"<html><body>Annual report, a different download</body></html>"
ENTRY = "filings/0000320193/000032019326000026/aapl-20260926.htm"


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(sec_client.time, "sleep", lambda seconds: None)


def make_filing(**overrides) -> FilingRef:
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


def make_document(content: bytes, retrieved_at: datetime | None = None) -> FilingDocument:
    filing = make_filing()
    source = SourceMetadata(
        source_url=filing.primary_document_url,
        retrieved_at=retrieved_at or datetime(2026, 11, 12, 16, 56, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    return FilingDocument(filing=filing, content=content, source=source)


def make_sec(settings, *bodies: bytes):
    """SecClient that serves `bodies` as HTML in order; returns it and the requests made."""
    requests: list[httpx.Request] = []
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=queue.pop(0), headers={"Content-Type": "text/html"})

    return SecClient(settings, transport=httpx.MockTransport(handler)), requests


def test_miss_then_hit_without_second_request_keeps_original_provenance(settings, tmp_path):
    sec, requests = make_sec(settings, HTML_A)

    first = fetch_filing_document(sec, make_filing(), tmp_path)
    second = fetch_filing_document(sec, make_filing(), tmp_path)

    assert len(requests) == 1
    assert second.content == HTML_A
    assert second.source == first.source  # same URL, retrieved_at, status and SHA-256
    assert (tmp_path / ENTRY).read_bytes() == HTML_A
    metadata = json.loads((tmp_path / f"{ENTRY}.json").read_text())
    assert metadata["schema_version"] == 1
    assert metadata["source"]["content_sha256"] == hashlib.sha256(HTML_A).hexdigest()


def test_corrupted_bytes_trigger_redownload(settings, tmp_path, caplog):
    sec, requests = make_sec(settings, HTML_A, HTML_A)
    fetch_filing_document(sec, make_filing(), tmp_path)
    (tmp_path / ENTRY).write_bytes(HTML_A.replace(b"first", b"FIRST"))

    document = fetch_filing_document(sec, make_filing(), tmp_path)

    assert len(requests) == 2
    assert document.content == HTML_A
    assert "Ignoring invalid cache entry" in caplog.text
    assert load_filing_document(tmp_path, make_filing()).content == HTML_A  # entry repaired


def _garble(metadata_path):
    metadata_path.write_text("{not json")


def _wrong_schema(metadata_path):
    metadata = json.loads(metadata_path.read_text())
    metadata["schema_version"] = 99
    metadata_path.write_text(json.dumps(metadata))


def _other_filing(metadata_path):
    metadata = json.loads(metadata_path.read_text())
    metadata["filing"]["accession_number"] = "0000320193-25-000079"
    metadata_path.write_text(json.dumps(metadata))


def _other_form(metadata_path):  # same URL and hash; only the identity check catches it
    metadata = json.loads(metadata_path.read_text())
    metadata["filing"]["form"] = "10-K/A"
    metadata_path.write_text(json.dumps(metadata))


def _missing_bytes(metadata_path):
    metadata_path.with_suffix("").unlink()


def _missing_metadata(metadata_path):
    metadata_path.unlink()


@pytest.mark.parametrize(
    "damage",
    [_garble, _wrong_schema, _other_filing, _other_form, _missing_bytes, _missing_metadata],
)
def test_bad_metadata_or_partial_entry_triggers_redownload(settings, tmp_path, damage):
    sec, requests = make_sec(settings, HTML_A, HTML_A)
    fetch_filing_document(sec, make_filing(), tmp_path)
    damage(tmp_path / f"{ENTRY}.json")

    document = fetch_filing_document(sec, make_filing(), tmp_path)

    assert len(requests) == 2
    assert document.content == HTML_A
    assert load_filing_document(tmp_path, make_filing()) is not None


def test_unsafe_filing_is_rejected_before_filesystem_access(settings, tmp_path):
    sec, requests = make_sec(settings)
    cache_dir = tmp_path / "cache"

    with pytest.raises(ValueError):
        fetch_filing_document(sec, make_filing(primary_document="../../evil.htm"), cache_dir)

    assert requests == []
    assert not cache_dir.exists()


def test_cache_path_may_not_escape_the_cache_root(settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    cache_dir = tmp_path / "cache"
    (cache_dir / "filings").mkdir(parents=True)
    (cache_dir / "filings" / "0000320193").symlink_to(outside)  # planted link to elsewhere
    sec, requests = make_sec(settings)

    with pytest.raises(ValueError, match="escapes the cache directory"):
        fetch_filing_document(sec, make_filing(), cache_dir)

    assert requests == []
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    ("existing", "failing_replace", "expected"),
    [
        (None, 1, None),  # new entry, bytes write fails: nothing cached
        (None, 2, None),  # new entry, metadata write fails: bytes without metadata
        (HTML_A, 1, HTML_A),  # replacement, bytes write fails: old entry intact
        (HTML_A, 2, None),  # replacement, metadata fails: new bytes + old metadata rejected
    ],
)
def test_interrupted_write_never_yields_a_mismatched_entry(
    tmp_path, monkeypatch, existing, failing_replace, expected
):
    if existing is not None:
        save_filing_document(tmp_path, make_document(existing))
    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(dst)
        if len(calls) == failing_replace:
            raise OSError("simulated crash")
        real_replace(src, dst)

    monkeypatch.setattr(filing_cache.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        save_filing_document(tmp_path, make_document(HTML_B))
    monkeypatch.undo()

    cached = load_filing_document(tmp_path, make_filing())
    assert (cached.content if cached else None) == expected
    assert list((tmp_path / ENTRY).parent.glob("*.tmp")) == []  # temporary files cleaned up


def test_interleaved_writers_leave_a_pair_that_is_rejected(tmp_path):
    # Writer A finishes, then writer B replaces the bytes, then A's metadata lands last.
    save_filing_document(tmp_path, make_document(HTML_A))
    metadata_a = (tmp_path / f"{ENTRY}.json").read_bytes()
    save_filing_document(tmp_path, make_document(HTML_B))
    (tmp_path / f"{ENTRY}.json").write_bytes(metadata_a)

    assert load_filing_document(tmp_path, make_filing()) is None


def test_cache_write_failure_still_returns_the_download(settings, tmp_path, caplog):
    cache_dir = tmp_path / "not-a-directory"
    cache_dir.write_text("a file where the cache directory should be")
    sec, requests = make_sec(settings, HTML_A)

    document = fetch_filing_document(sec, make_filing(), cache_dir)

    assert document.content == HTML_A
    assert len(requests) == 1
    assert "Could not cache filing" in caplog.text

"""Company identity resolution against a pinned ticker snapshot. Offline, sockets blocked."""

import hashlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst.filing_cache import (
    load_company_tickers_snapshot,
    load_submissions_snapshot,
    save_snapshot,
)
from financial_analyst.financial_tools import IdentityError, resolve_company
from financial_analyst.sec_client import COMPANY_TICKERS_URL, parse_company_tickers, snapshot_url
from financial_analyst.sec_models import Snapshot, SnapshotMetadata, SourceMetadata

FIXTURE = Path(__file__).parent / "fixtures" / "company_tickers_sample.json"
SUBMISSIONS_FIXTURE = Path(__file__).parent / "fixtures" / "submissions_sample.json"
APPLE, JPM = "0000320193", "0000019617"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def source(content: bytes) -> SourceMetadata:
    return SourceMetadata(
        source_url=COMPANY_TICKERS_URL,
        retrieved_at=datetime(2026, 11, 13, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def ticker_map(payload: dict | None = None):
    content = json.dumps(payload).encode() if payload is not None else FIXTURE.read_bytes()
    return parse_company_tickers(json.loads(content), source(content))


@pytest.fixture
def tickers():
    return ticker_map()


@pytest.mark.parametrize(
    ("identifier", "cik", "matched_by"),
    [
        ("AAPL", APPLE, "ticker"),
        ("aapl", APPLE, "ticker"),
        ("  AaPl ", APPLE, "ticker"),
        ("JPM", JPM, "ticker"),
        ("jpm-pc", JPM, "ticker"),  # a second share class of the same company
        ("320193", APPLE, "cik"),
        ("0000320193", APPLE, "cik"),
        ("19617", JPM, "cik"),
        ("Apple", APPLE, "alias"),
        ("APPLE INC.", APPLE, "alias"),
        ("  JPMorgan   Chase ", JPM, "alias"),  # whitespace is collapsed, nothing else
        ("jpmorgan chase & co.", JPM, "alias"),
    ],
)
def test_exact_identities_resolve(tickers, identifier, cik, matched_by):
    company = resolve_company(identifier, tickers)
    assert (company.cik, company.matched_by) == (cik, matched_by)
    assert company.verified_by == "company_tickers"
    assert company.source == tickers.source


def test_a_company_lists_all_its_tickers_and_sec_title(tickers):
    company = resolve_company("JPM", tickers)
    assert company.tickers == ["JPM", "JPM-PC"]
    assert company.name == "JPMORGAN CHASE & CO"


@pytest.mark.parametrize(
    "identifier",
    [
        "Apple Computer",  # not an exact alias
        "Apple Inc",  # the alias includes the period
        "JP Morgan",
        "Chase",
        "appl",  # near-miss ticker
        "AAP",  # prefix
        "Microsoft",
        "1234",  # a valid CIK that is not in the snapshot
        "３２０１９３",  # full-width digits are not a CIK and match nothing
    ],
)
def test_unknown_or_inexact_identities_are_not_found(tickers, identifier):
    with pytest.raises(IdentityError) as error:
        resolve_company(identifier, tickers)
    assert error.value.problem == "company_not_found" and error.value.candidates == []


@pytest.mark.parametrize("identifier", ["", "   ", "x" * 65, "12345678901"])
def test_invalid_identifiers_are_rejected(tickers, identifier):
    with pytest.raises(IdentityError) as error:
        resolve_company(identifier, tickers)
    assert error.value.problem == "invalid_identifier"


def test_a_ticker_listed_for_two_companies_is_ambiguous(tickers):
    for identifier in ("DUAL", "dual"):  # differs only in case in the snapshot
        with pytest.raises(IdentityError) as error:
            resolve_company(identifier, tickers)
        assert error.value.problem == "company_ambiguous"
        assert error.value.candidates == ["0001111111", "0002222222"]


def test_a_ticker_and_an_alias_naming_different_companies_is_ambiguous():
    conflicting = ticker_map(
        {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 3333333, "ticker": "APPLE", "title": "Synthetic Orchard Co"},
        }
    )
    with pytest.raises(IdentityError) as error:
        resolve_company("apple", conflicting)
    assert error.value.problem == "company_ambiguous"
    assert error.value.candidates == [APPLE, "0003333333"]


def test_an_alias_for_a_company_missing_from_the_snapshot_is_not_found():
    apple_only = ticker_map({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
    with pytest.raises(IdentityError) as error:
        resolve_company("JPMorgan", apple_only)
    assert error.value.problem == "company_not_found"


def test_resolution_from_a_pinned_snapshot_is_offline(tmp_path):
    content = FIXTURE.read_bytes()
    metadata = SnapshotMetadata(kind="company_tickers", cik=None, source=source(content))
    save_snapshot(tmp_path, Snapshot(metadata=metadata, content=content))
    pinned = load_company_tickers_snapshot(tmp_path, metadata.source.content_sha256)
    company = resolve_company("aapl", pinned)  # sockets are blocked by the autouse fixture
    assert company.cik == APPLE
    assert company.source.content_sha256 == metadata.source.content_sha256


# --- CIKs the ticker map does not list: verified by a loaded submissions snapshot ---------

JPM_ONLY = {"0": {"cik_str": 19617, "ticker": "JPM", "title": "JPMORGAN CHASE & CO"}}


@pytest.fixture
def apple_submissions(tmp_path):
    """Apple's submissions, saved as a snapshot and loaded back through the verified loader."""
    content = SUBMISSIONS_FIXTURE.read_bytes()
    url = snapshot_url("submissions", APPLE)
    snapshot_source = SourceMetadata(
        source_url=url,
        retrieved_at=datetime(2026, 11, 13, tzinfo=UTC),
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    metadata = SnapshotMetadata(kind="submissions", cik=APPLE, source=snapshot_source)
    save_snapshot(tmp_path, Snapshot(metadata=metadata, content=content))
    return load_submissions_snapshot(tmp_path, APPLE, snapshot_source.content_sha256)


def test_a_listed_cik_is_still_verified_by_the_ticker_map(tickers, apple_submissions):
    company = resolve_company("320193", tickers, apple_submissions)
    assert company.verified_by == "company_tickers" and company.source == tickers.source


def test_an_unlisted_cik_is_verified_by_its_submissions_snapshot(apple_submissions):
    company = resolve_company("320193", ticker_map(JPM_ONLY), apple_submissions)
    assert company.cik == APPLE and company.matched_by == "cik"
    assert company.verified_by == "submissions"
    assert (company.name, company.tickers) == ("Apple Inc.", ["AAPL"])
    assert company.source == apple_submissions.source  # the submissions snapshot's provenance


@pytest.mark.parametrize("identifier", ["1234", "0000001234"])
def test_a_cik_in_neither_source_is_not_found(apple_submissions, identifier):
    for submissions in (None, apple_submissions):  # Apple's submissions never vouch for 1234
        with pytest.raises(IdentityError) as error:
            resolve_company(identifier, ticker_map(JPM_ONLY), submissions)
        assert error.value.problem == "company_not_found"


def test_submissions_from_another_source_do_not_verify_a_cik(apple_submissions):
    elsewhere = apple_submissions.model_copy(
        update={
            "source": apple_submissions.source.model_copy(
                update={"source_url": "https://example.com/CIK0000320193.json"}
            )
        }
    )
    with pytest.raises(IdentityError) as error:
        resolve_company("320193", ticker_map(JPM_ONLY), elsewhere)
    assert error.value.problem == "company_not_found"


def test_an_invalid_cik_is_rejected_whatever_the_sources(apple_submissions):
    with pytest.raises(IdentityError) as error:
        resolve_company("12345678901", ticker_map(JPM_ONLY), apple_submissions)
    assert error.value.problem == "invalid_identifier"


@pytest.mark.parametrize("identifier", ["AAPL", "aapl", "Apple", "Apple Inc."])
def test_tickers_and_aliases_never_use_the_submissions_snapshot(apple_submissions, identifier):
    # Apple is missing from this ticker map; its submissions verify CIK input only.
    with pytest.raises(IdentityError) as error:
        resolve_company(identifier, ticker_map(JPM_ONLY), apple_submissions)
    assert error.value.problem == "company_not_found"


def test_submissions_recorded_for_another_cik_do_not_verify_it(apple_submissions):
    # Canonical URL for Apple, but the parsed record names a different company.
    relabeled = apple_submissions.model_copy(update={"cik": "0000001234"})
    with pytest.raises(IdentityError) as error:
        resolve_company("320193", ticker_map(JPM_ONLY), relabeled)
    assert error.value.problem == "company_not_found"

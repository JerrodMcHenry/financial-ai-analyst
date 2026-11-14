"""Budgeted snapshot acquisition and pin manifests. Mocked HTTP only; sockets blocked."""

import json
import socket
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from financial_analyst import sec_client
from financial_analyst.filing_cache import (
    SnapshotError,
    SnapshotManifest,
    load_company_facts_snapshot,
    load_company_tickers_snapshot,
    load_pinned_snapshot,
    load_snapshot_manifest,
    load_submissions_snapshot,
)
from financial_analyst.financial_tools import resolve_company
from financial_analyst.sec_client import snapshot_url
from financial_analyst.snapshot_acquisition import (
    TARGETS,
    BudgetedTransport,
    RequestRefused,
    acquire_snapshots,
    write_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures"
APPLE, JPM = "0000320193", "0000019617"
URLS = [snapshot_url(kind, cik) for kind, cik in TARGETS]


def jpm_variant(fixture: str, name_key: str) -> bytes:
    """The Apple fixture relabeled as JPMorgan, for a well-formed second company."""
    payload = json.loads((FIXTURES / fixture).read_text())
    payload["cik"], payload[name_key] = 19617, "JPMorgan Chase & Co."
    return json.dumps(payload).encode()


BODIES = {
    URLS[0]: (FIXTURES / "company_tickers_sample.json").read_bytes(),
    URLS[1]: (FIXTURES / "submissions_sample.json").read_bytes(),
    URLS[2]: (FIXTURES / "companyfacts_sample.json").read_bytes(),
    URLS[3]: jpm_variant("submissions_sample.json", "name"),
    URLS[4]: jpm_variant("companyfacts_sample.json", "entityName"),
}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(sec_client.time, "sleep", lambda seconds: None)


def sec_server(overrides: dict[str, httpx.Response] | None = None):
    """A mock SEC: serves BODIES, or an override response for a URL. Records every request."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if overrides and url in overrides:
            return overrides[url]
        return httpx.Response(200, content=BODIES[url])

    return httpx.MockTransport(handler), seen


def budgeted(inner, budget: int = 5) -> BudgetedTransport:
    return BudgetedTransport(URLS, budget=budget, inner=inner)


# --- Request budget -----------------------------------------------------------------------


def test_acquisition_makes_exactly_one_request_per_target_in_order(settings, tmp_path):
    inner, seen = sec_server()
    manifest, transport = acquire_snapshots(settings, tmp_path, transport=budgeted(inner))
    assert seen == URLS and transport.attempts == URLS and transport.statuses == [200] * 5
    assert [(p.kind, p.cik) for p in manifest.snapshots] == list(TARGETS)


@pytest.mark.parametrize("failing", [0, 1, 4])
def test_a_failed_request_stops_acquisition_without_retrying(settings, tmp_path, failing):
    inner, seen = sec_server({URLS[failing]: httpx.Response(503)})  # a retryable status
    transport = budgeted(inner)
    with pytest.raises(sec_client.SecError):
        acquire_snapshots(settings, tmp_path, transport=transport)
    assert seen == URLS[: failing + 1]  # nothing after the failure; the failure not retried
    assert transport.statuses[-1] == 503


def test_a_redirect_is_not_followed(settings, tmp_path):
    redirect = httpx.Response(301, headers={"Location": "https://example.com/elsewhere.json"})
    inner, seen = sec_server({URLS[0]: redirect})
    with pytest.raises(sec_client.SecError):
        acquire_snapshots(settings, tmp_path, transport=budgeted(inner))
    assert seen == [URLS[0]]


def test_a_response_that_does_not_parse_stops_acquisition(settings, tmp_path):
    inner, seen = sec_server({URLS[1]: httpx.Response(200, content=b"<html>rate limited</html>")})
    with pytest.raises(sec_client.SecResponseError):
        acquire_snapshots(settings, tmp_path, transport=budgeted(inner))
    assert seen == URLS[:2]


def test_the_transport_refuses_unapproved_urls_repeats_and_overspending():
    inner, seen = sec_server()
    transport = budgeted(inner, budget=2)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(RequestRefused, match="not an approved"):
            client.get("https://data.sec.gov/submissions/CIK0000789019.json")
        client.get(URLS[0])
        with pytest.raises(RequestRefused, match="no retries"):
            client.get(URLS[0])
        client.get(URLS[1])
        with pytest.raises(RequestRefused, match="budget of 2"):
            client.get(URLS[2])
    assert seen == URLS[:2]  # refused requests never reached the server
    assert transport.attempts == URLS[:2]


def test_sec_client_with_max_retries_zero_sends_one_attempt(settings):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503)

    with sec_client.SecClient(settings, httpx.MockTransport(handler), max_retries=0) as sec:
        with pytest.raises(sec_client.SecError):
            sec.get_submissions(APPLE)
    assert len(attempts) == 1
    with pytest.raises(ValueError):
        sec_client.SecClient(settings, max_retries=-1)


def test_pacing_is_capped_at_four_requests_per_second(settings, tmp_path, monkeypatch):
    rates = []
    real_init = sec_client.SecClient.__init__

    def spy(self, configured, *args, **kwargs):
        rates.append(configured.sec_max_requests_per_second)
        real_init(self, configured, *args, **kwargs)

    monkeypatch.setattr(sec_client.SecClient, "__init__", spy)
    fast = settings.model_copy(update={"sec_max_requests_per_second": 10.0})
    inner, _ = sec_server()
    acquire_snapshots(fast, tmp_path, transport=budgeted(inner))
    assert rates == [4.0]


# --- Manifest -----------------------------------------------------------------------------


@pytest.fixture
def acquired(settings, tmp_path):
    inner, _ = sec_server()
    manifest, _ = acquire_snapshots(settings, tmp_path / "cache", transport=budgeted(inner))
    path = tmp_path / "manifest.json"
    write_manifest(path, manifest)
    return tmp_path / "cache", path


def test_a_written_manifest_reloads_the_exact_snapshots_offline(acquired):
    cache_dir, path = acquired
    manifest = load_snapshot_manifest(path)
    for pin, url in zip(manifest.snapshots, URLS, strict=True):
        snapshot = load_pinned_snapshot(cache_dir, pin)
        assert snapshot.content == BODIES[url] and pin.size_bytes == len(BODIES[url])
    tickers = load_company_tickers_snapshot(cache_dir, manifest.pin("company_tickers").sha256)
    apple = load_submissions_snapshot(cache_dir, APPLE, manifest.pin("submissions", APPLE).sha256)
    jpm_facts = load_company_facts_snapshot(
        cache_dir, JPM, manifest.pin("company_facts", JPM).sha256
    )
    assert apple.cik == APPLE and jpm_facts.entity_name == "JPMorgan Chase & Co."
    assert resolve_company("AAPL", tickers).cik == APPLE
    assert resolve_company("JPMorgan", tickers).cik == JPM


def test_a_manifest_is_never_replaced(acquired):
    _, path = acquired
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_manifest(path, load_snapshot_manifest(path))
    assert path.read_bytes() == before


def test_missing_or_corrupted_pinned_snapshots_are_refused(acquired):
    cache_dir, path = acquired
    pin = load_snapshot_manifest(path).pin("company_facts", APPLE)
    stored = cache_dir / "snapshots" / "company_facts" / APPLE / f"{pin.sha256}.json"
    stored.write_bytes(stored.read_bytes()[:-1])
    with pytest.raises(SnapshotError) as error:
        load_pinned_snapshot(cache_dir, pin)
    assert error.value.problem == "corrupted"
    stored.unlink()
    with pytest.raises(SnapshotError) as error:
        load_pinned_snapshot(cache_dir, pin)
    assert error.value.problem == "corrupted"  # metadata present, bytes gone


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"sha256": "0" * 64}, "missing"),  # a wrong SHA pin names no stored snapshot
        ({"retrieved_at": "2020-01-01T00:00:00Z"}, "mismatched"),
        ({"http_status": 203}, "mismatched"),
        ({"size_bytes": 1}, "mismatched"),
    ],
)
def test_a_pin_must_match_the_stored_snapshot_exactly(acquired, change, problem):
    cache_dir, path = acquired
    pin = load_snapshot_manifest(path).pin("submissions", JPM)
    altered = pin.model_validate(pin.model_dump() | change)
    with pytest.raises(SnapshotError) as error:
        load_pinned_snapshot(cache_dir, altered)
    assert error.value.problem == problem


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d["snapshots"].append(d["snapshots"][0]),  # duplicate kind and company
        lambda d: d["snapshots"][1].update(source_url=URLS[3]),  # URL for another company
        lambda d: d["snapshots"][0].update(cik=APPLE),  # tickers carry no CIK
        lambda d: d["snapshots"][0].update(sha256="ABC"),
        lambda d: d.update(snapshot_schema_version=2),
        lambda d: d.update(snapshots=[]),
        lambda d: d.update(unexpected=True),
    ],
)
def test_an_invalid_manifest_is_rejected(acquired, damage):
    _, path = acquired
    document = json.loads(path.read_text())
    damage(document)
    with pytest.raises(ValidationError):
        SnapshotManifest.model_validate(document)


def test_a_manifest_names_one_pin_per_kind_and_company(acquired):
    _, path = acquired
    manifest = load_snapshot_manifest(path)
    assert manifest.pin("company_facts", "19617").cik == JPM
    with pytest.raises(KeyError):
        manifest.pin("company_facts", "1234")


def test_the_committed_manifest_pins_exactly_the_approved_targets():
    # Schema only: the raw snapshots live in the git-ignored cache, not the repository.
    manifest = load_snapshot_manifest(
        Path(__file__).parent.parent / "evals" / "sec_snapshots_v1.json"
    )
    assert [(p.kind, p.cik) for p in manifest.snapshots] == list(TARGETS)
    assert [p.source_url for p in manifest.snapshots] == URLS
    assert {p.http_status for p in manifest.snapshots} == {200}

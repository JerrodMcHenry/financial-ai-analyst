"""Immutable structured-data snapshots: temporary directories only, sockets blocked."""

import hashlib
import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest

from financial_analyst import filing_cache
from financial_analyst.filing_cache import (
    SnapshotError,
    load_company_facts_snapshot,
    load_company_tickers_snapshot,
    load_snapshot,
    load_submissions_snapshot,
    save_snapshot,
)
from financial_analyst.sec_client import snapshot_url
from financial_analyst.sec_models import Snapshot, SnapshotMetadata, SourceMetadata

FIXTURES = Path(__file__).parent / "fixtures"
SUBMISSIONS = (FIXTURES / "submissions_sample.json").read_bytes()
FACTS = (FIXTURES / "companyfacts_sample.json").read_bytes()
TICKERS = (FIXTURES / "company_tickers_sample.json").read_bytes()
APPLE = "0000320193"
FIRST = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def make_snapshot(kind, content, cik=None, retrieved_at=FIRST) -> Snapshot:
    source = SourceMetadata(
        source_url=snapshot_url(kind, cik),
        retrieved_at=retrieved_at,
        http_status=200,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )
    cik = None if cik is None else cik.zfill(10)
    return Snapshot(metadata=SnapshotMetadata(kind=kind, cik=cik, source=source), content=content)


def paths(cache_dir: Path, kind: str, sha256: str, cik: str | None = None) -> tuple[Path, Path]:
    folder = cache_dir / "snapshots" / kind / (cik or "all")
    return folder / f"{sha256}.json", folder / f"{sha256}.meta.json"


SNAPSHOTS = [
    ("submissions", SUBMISSIONS, APPLE),
    ("company_facts", FACTS, APPLE),
    ("company_tickers", TICKERS, None),
]


@pytest.mark.parametrize(("kind", "content", "cik"), SNAPSHOTS)
def test_round_trip_keeps_exact_bytes_and_metadata(tmp_path, kind, content, cik):
    snapshot = make_snapshot(kind, content, cik)
    sha256 = snapshot.metadata.source.content_sha256
    assert save_snapshot(tmp_path, snapshot) == snapshot.metadata
    content_path, metadata_path = paths(tmp_path, kind, sha256, cik)
    assert content_path.read_bytes() == content  # stored as served, never re-serialized
    stored = json.loads(metadata_path.read_text())
    assert stored["schema_version"] == 1 and stored["kind"] == kind and stored["cik"] == cik
    assert stored["source"]["content_sha256"] == sha256
    assert load_snapshot(tmp_path, kind, sha256, cik) == snapshot


def test_typed_loaders_parse_like_the_live_client(tmp_path):
    shas = {}
    for kind, content, cik in SNAPSHOTS:
        snapshot = make_snapshot(kind, content, cik)
        save_snapshot(tmp_path, snapshot)
        shas[kind] = snapshot.metadata.source.content_sha256
    submissions = load_submissions_snapshot(tmp_path, "320193", shas["submissions"])
    assert submissions.name == "Apple Inc." and submissions.source.retrieved_at == FIRST
    facts = load_company_facts_snapshot(tmp_path, APPLE, shas["company_facts"])
    assert facts.entity_name == "Apple Inc." and facts.skipped_count == 5  # same as live parsing
    tickers = load_company_tickers_snapshot(tmp_path, shas["company_tickers"])
    assert [e.ticker for e in tickers.entries] == ["AAPL", "JPM", "JPM-PC", "DUAL", "dual"]


def test_a_snapshot_is_never_overwritten(tmp_path):
    first = make_snapshot("submissions", SUBMISSIONS, APPLE, FIRST)
    second = make_snapshot("submissions", SUBMISSIONS, APPLE, LATER)  # same bytes, later fetch
    save_snapshot(tmp_path, first)
    content_path, metadata_path = paths(
        tmp_path, "submissions", first.metadata.source.content_sha256, APPLE
    )
    before = (
        content_path.read_bytes(),
        metadata_path.read_bytes(),
        metadata_path.stat().st_mtime_ns,
    )
    assert save_snapshot(tmp_path, second) == first.metadata  # the first retrieval wins
    assert (
        content_path.read_bytes(),
        metadata_path.read_bytes(),
        metadata_path.stat().st_mtime_ns,
    ) == before
    # Different bytes are a different snapshot, stored alongside; nothing is replaced.
    changed = make_snapshot("submissions", SUBMISSIONS + b"\n", APPLE)
    save_snapshot(tmp_path, changed)
    assert (
        load_snapshot(tmp_path, "submissions", first.metadata.source.content_sha256, APPLE) == first
    )
    assert len(list(content_path.parent.glob("*.meta.json"))) == 2


def test_a_corrupted_entry_is_refused_and_never_repaired_by_saving(tmp_path):
    snapshot = make_snapshot("company_facts", FACTS, APPLE)
    sha256 = snapshot.metadata.source.content_sha256
    save_snapshot(tmp_path, snapshot)
    content_path, _ = paths(tmp_path, "company_facts", sha256, APPLE)
    content_path.write_bytes(FACTS.replace(b"Apple", b"Apfel"))  # one changed word
    with pytest.raises(SnapshotError) as error:
        load_snapshot(tmp_path, "company_facts", sha256, APPLE)
    assert error.value.problem == "corrupted"
    with pytest.raises(SnapshotError) as error:
        save_snapshot(tmp_path, snapshot)
    assert error.value.problem == "corrupted"
    assert content_path.read_bytes() != FACTS  # left for a person to inspect and remove


def edit_metadata(change):
    def edit(metadata_path: Path, content_path: Path) -> None:
        document = json.loads(metadata_path.read_text())
        change(document)
        metadata_path.write_text(json.dumps(document))

    return edit


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (lambda m, c: m.unlink(), "missing"),
        (lambda m, c: c.unlink(), "corrupted"),
        (lambda m, c: c.write_bytes(c.read_bytes()[:-1]), "corrupted"),
        (lambda m, c: m.write_text("{not json"), "mismatched"),
        (edit_metadata(lambda d: d.update(schema_version=2)), "mismatched"),
        (edit_metadata(lambda d: d.update(kind="company_facts")), "mismatched"),
        (edit_metadata(lambda d: d.update(cik="0000019617")), "mismatched"),
        (edit_metadata(lambda d: d["source"].update(content_sha256="0" * 64)), "mismatched"),
        (
            edit_metadata(lambda d: d["source"].update(source_url="https://example.com/x.json")),
            "mismatched",
        ),
    ],
)
def test_damaged_or_inconsistent_entries_are_refused(tmp_path, damage, problem):
    snapshot = make_snapshot("submissions", SUBMISSIONS, APPLE)
    sha256 = snapshot.metadata.source.content_sha256
    save_snapshot(tmp_path, snapshot)
    content_path, metadata_path = paths(tmp_path, "submissions", sha256, APPLE)
    damage(metadata_path, content_path)
    with pytest.raises(SnapshotError) as error:
        load_snapshot(tmp_path, "submissions", sha256, APPLE)
    assert error.value.problem == problem


def test_a_load_must_name_the_exact_hash_kind_and_company(tmp_path):
    snapshot = make_snapshot("submissions", SUBMISSIONS, APPLE)
    sha256 = snapshot.metadata.source.content_sha256
    save_snapshot(tmp_path, snapshot)
    for kind, sha, cik in [
        ("submissions", hashlib.sha256(b"other").hexdigest(), APPLE),  # wrong hash
        ("submissions", sha256, "0000019617"),  # wrong company
        ("company_facts", sha256, APPLE),  # wrong kind
    ]:
        with pytest.raises(SnapshotError) as error:
            load_snapshot(tmp_path, kind, sha, cik)
        assert error.value.problem == "missing"


def test_bytes_that_are_not_a_valid_response_are_refused_when_parsed(tmp_path):
    bogus = make_snapshot("submissions", b'{"cik": "320193"}', APPLE)
    save_snapshot(tmp_path, bogus)  # storage verifies integrity, not meaning
    with pytest.raises(SnapshotError) as error:
        load_submissions_snapshot(tmp_path, APPLE, bogus.metadata.source.content_sha256)
    assert error.value.problem == "invalid"


@pytest.mark.parametrize(
    ("kind", "sha256", "cik"),
    [
        ("submissions", "A" * 64, APPLE),  # not lowercase hex
        ("submissions", "abc", APPLE),  # not a full hash
        ("submissions", "../" + "a" * 61, APPLE),  # path traversal
        ("company_tickers", "a" * 64, APPLE),  # tickers take no CIK
        ("company_facts", "a" * 64, None),  # facts need a CIK
        ("company_facts", "a" * 64, "12345678901"),  # invalid CIK
        ("filings", "a" * 64, APPLE),  # unknown kind
    ],
)
def test_unsafe_requests_are_rejected_before_touching_the_filesystem(tmp_path, kind, sha256, cik):
    with pytest.raises(ValueError):
        load_snapshot(tmp_path, kind, sha256, cik)
    assert list(tmp_path.iterdir()) == []


def test_a_failed_write_leaves_no_entry_and_no_temporary_files(tmp_path, monkeypatch):
    snapshot = make_snapshot("company_tickers", TICKERS)

    def fail(source, target):
        raise OSError("disk full")

    monkeypatch.setattr(filing_cache.os, "link", fail)
    with pytest.raises(OSError, match="disk full"):
        save_snapshot(tmp_path, snapshot)
    folder = tmp_path / "snapshots" / "company_tickers" / "all"
    assert list(folder.iterdir()) == []


def test_an_interrupted_save_is_incomplete_until_saved_again(tmp_path, monkeypatch):
    snapshot = make_snapshot("company_tickers", TICKERS)
    sha256 = snapshot.metadata.source.content_sha256
    real_link = os.link

    def bytes_only(source, target):
        if str(target).endswith(".meta.json"):
            raise KeyboardInterrupt  # stopped between the two files
        real_link(source, target)

    monkeypatch.setattr(filing_cache.os, "link", bytes_only)
    with pytest.raises(KeyboardInterrupt):
        save_snapshot(tmp_path, snapshot)
    with pytest.raises(SnapshotError) as error:
        load_snapshot(tmp_path, "company_tickers", sha256)
    assert error.value.problem == "missing"  # no metadata: not a complete snapshot
    monkeypatch.setattr(filing_cache.os, "link", real_link)
    assert save_snapshot(tmp_path, snapshot) == snapshot.metadata
    assert load_snapshot(tmp_path, "company_tickers", sha256) == snapshot
    assert not [p for p in paths(tmp_path, "company_tickers", sha256)[0].parent.iterdir()
                if p.name.endswith(".tmp")]  # fmt: skip

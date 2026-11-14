"""Filesystem cache for SEC filing documents, and immutable snapshots of SEC JSON data.

Layout, mirroring EDGAR's archive:
    <cache_dir>/filings/<cik>/<accession without dashes>/<primary_document>        exact bytes
    <cache_dir>/filings/<cik>/<accession without dashes>/<primary_document>.json   metadata

Each file is written atomically on its own (temporary file, then os.replace): bytes first,
metadata last. The pair is NOT replaced as one transaction. An interrupted write or two
concurrent writers can leave bytes and metadata that do not belong together. That is safe
because every read rebuilds a FilingDocument, whose validator checks the bytes against the
recorded SHA-256; a mismatched or corrupted entry is treated as a miss, never returned.

Structured-data snapshots (submissions, Company Facts, company_tickers.json) are stored
differently, because those resources change over time and evaluations must pin one exact
version (D-030):
    <cache_dir>/snapshots/<kind>/<cik or "all">/<sha256>.json        exact response bytes
    <cache_dir>/snapshots/<kind>/<cik or "all">/<sha256>.meta.json   SnapshotMetadata
They are content-addressed by the full SHA-256 of the bytes, created once and never
overwritten (a second save of the same bytes keeps the first metadata). Loading needs the
expected SHA-256 and refuses, with SnapshotError, anything missing, corrupted or
inconsistent; a snapshot is never repaired or refetched implicitly.

The cache directory must be controlled by this application and not writable by untrusted
users: the checks here keep paths inside the cache root, but do not defend against an
attacker who can rewrite the directory itself.
"""

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from financial_analyst.sec_client import (
    SecClient,
    SecResponseError,
    decode_json,
    filing_document_url,
    normalize_cik,
    parse_company_facts,
    parse_company_tickers,
    parse_submissions,
    snapshot_url,
)
from financial_analyst.sec_models import (
    CompanyFacts,
    CompanySubmissions,
    FilingDocument,
    FilingRef,
    Snapshot,
    SnapshotKind,
    SnapshotMetadata,
    TickerMap,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def fetch_filing_document(sec: SecClient, filing: FilingRef, cache_dir: Path) -> FilingDocument:
    """Return the filing's primary document from the cache, downloading it on a miss."""
    cached = load_filing_document(cache_dir, filing)
    if cached is not None:
        return cached

    document = sec.get_filing_document(filing)
    try:
        save_filing_document(cache_dir, document)
    except OSError as exc:
        # The download itself succeeded and was validated, so the caller still gets it.
        logger.warning("Could not cache filing %s: %s", filing.accession_number, exc)
    return document


def load_filing_document(cache_dir: Path, filing: FilingRef) -> FilingDocument | None:
    """Return the verified cached document, or None on a miss or an invalid entry.

    A hit returns the provenance recorded at download time (original retrieved_at),
    and the FilingRef stored with it.
    """
    content_path, metadata_path = _entry_paths(cache_dir, filing)
    try:
        metadata_bytes = metadata_path.read_bytes()
    except FileNotFoundError:
        return None  # metadata is written last, so no metadata means no complete entry
    except OSError as exc:
        logger.warning("Cannot read cache entry %s: %s", metadata_path, exc)
        return None

    try:
        metadata = json.loads(metadata_bytes)
        if not isinstance(metadata, dict) or metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported cache schema version")
        cached_filing = FilingRef.model_validate(metadata["filing"])
        if _identity(cached_filing) != _identity(filing):
            raise ValueError("metadata belongs to a different filing")
        # The FilingDocument validator rejects bytes that do not match the recorded hash.
        return FilingDocument(
            filing=cached_filing, content=content_path.read_bytes(), source=metadata["source"]
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:  # includes ValidationError
        logger.warning("Ignoring invalid cache entry %s: %s", metadata_path, exc)
        return None


def save_filing_document(cache_dir: Path, document: FilingDocument) -> None:
    """Write the document's bytes, then its metadata, each atomically. Raises OSError."""
    content_path, metadata_path = _entry_paths(cache_dir, document.filing)
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "filing": document.filing.model_dump(mode="json"),
        "source": document.source.model_dump(mode="json"),
    }
    content_path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(content_path, document.content)
    _write_atomically(metadata_path, json.dumps(metadata, indent=2).encode())


def _entry_paths(cache_dir: Path, filing: FilingRef) -> tuple[Path, Path]:
    """Cache file paths for a filing. Raises ValueError for anything unsafe.

    The filing is validated with the same allowlist used before downloading, so only
    documents the client would fetch can map to cache paths, and paths are never read
    from cached files.
    """
    filing_document_url(filing)  # raises ValueError before any filesystem access
    root = cache_dir.resolve()
    folder = filing.accession_number.replace("-", "")
    content_path = (root / "filings" / filing.cik / folder / filing.primary_document).resolve()
    if not content_path.is_relative_to(root):  # e.g. a symlink inside the cache pointing out
        raise ValueError(f"Cache path escapes the cache directory: {content_path}")
    return content_path, content_path.with_name(content_path.name + ".json")


def _identity(filing: FilingRef) -> tuple[str, str, str | None, str]:
    return filing.cik, filing.accession_number, filing.primary_document, filing.form


def _write_atomically(path: Path, data: bytes) -> None:
    """Write to a temporary file in the same directory, then rename it over `path`."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(data)
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


# --- Immutable structured-data snapshots --------------------------------------------------

SNAPSHOT_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")

SnapshotProblem = Literal["missing", "corrupted", "mismatched", "invalid"]


class SnapshotError(Exception):
    """A pinned snapshot cannot be used. `problem` says why:
    missing: no complete entry for this kind, CIK and SHA-256;
    corrupted: the stored bytes are absent or do not hash to the SHA-256;
    mismatched: the metadata disagrees with the request or with the bytes;
    invalid: the bytes are not a valid response of that kind.
    """

    def __init__(self, problem: SnapshotProblem, message: str) -> None:
        super().__init__(message)
        self.problem = problem


def save_snapshot(cache_dir: Path, snapshot: Snapshot) -> SnapshotMetadata:
    """Store a snapshot once. Returns the metadata now on disk.

    Each file is created atomically and only if it does not exist: bytes first, metadata
    last, so an entry without metadata is incomplete and is completed by the next save.
    Saving bytes that are already stored changes nothing and returns the original
    metadata (first retrieval wins). An existing entry that fails verification is never
    overwritten: SnapshotError is raised and the entry must be removed by hand.
    Raises OSError if the files cannot be written.
    """
    metadata = snapshot.metadata
    sha256 = metadata.source.content_sha256
    content_path, metadata_path = _snapshot_paths(cache_dir, metadata.kind, metadata.cik, sha256)
    if metadata_path.exists():
        return load_snapshot(cache_dir, metadata.kind, sha256, metadata.cik).metadata
    content_path.parent.mkdir(parents=True, exist_ok=True)
    if not _create_once(content_path, snapshot.content):
        stored = content_path.read_bytes()
        if hashlib.sha256(stored).hexdigest() != sha256:
            raise SnapshotError(
                "corrupted", f"existing snapshot bytes at {content_path} are corrupted"
            )
    document = {"schema_version": SNAPSHOT_SCHEMA_VERSION, **metadata.model_dump(mode="json")}
    # If a concurrent save of the same bytes created the metadata first, the load below
    # verifies that file instead.
    _create_once(metadata_path, json.dumps(document, indent=2).encode())
    return load_snapshot(cache_dir, metadata.kind, sha256, metadata.cik).metadata


def load_snapshot(
    cache_dir: Path, kind: SnapshotKind, sha256: str, cik: str | int | None = None
) -> Snapshot:
    """The stored snapshot with exactly this SHA-256, verified. Raises SnapshotError."""
    content_path, metadata_path = _snapshot_paths(cache_dir, kind, cik, sha256)
    label = f"{kind} snapshot {sha256}" + (f" for CIK {normalize_cik(cik)}" if cik else "")
    try:
        metadata_bytes = metadata_path.read_bytes()
    except FileNotFoundError:
        raise SnapshotError("missing", f"no {label} in {cache_dir}") from None
    try:
        document = json.loads(metadata_bytes)
        if not isinstance(document, dict) or document.pop("schema_version", None) != (
            SNAPSHOT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported snapshot schema version")
        metadata = SnapshotMetadata.model_validate(document)
    except (ValueError, ValidationError) as exc:  # includes JSON errors
        raise SnapshotError("mismatched", f"{label}: unreadable metadata: {exc}") from exc
    expected_cik = None if cik is None else normalize_cik(cik)
    if (metadata.kind, metadata.cik) != (kind, expected_cik):
        raise SnapshotError(
            "mismatched", f"{label}: metadata is for {metadata.kind} {metadata.cik}"
        )
    if metadata.source.content_sha256 != sha256:
        raise SnapshotError(
            "mismatched", f"{label}: metadata records {metadata.source.content_sha256}"
        )
    if str(metadata.source.source_url) != snapshot_url(kind, cik):
        raise SnapshotError("mismatched", f"{label}: recorded URL {metadata.source.source_url}")
    try:
        content = content_path.read_bytes()
    except FileNotFoundError:
        raise SnapshotError("corrupted", f"{label}: the stored bytes are missing") from None
    actual = hashlib.sha256(content).hexdigest()
    if actual != sha256:
        raise SnapshotError("corrupted", f"{label}: the stored bytes are corrupted (hash {actual})")
    return Snapshot(metadata=metadata, content=content)


def load_submissions_snapshot(cache_dir: Path, cik: str | int, sha256: str) -> CompanySubmissions:
    """A verified submissions snapshot, parsed exactly as SecClient.get_submissions parses."""
    snapshot = load_snapshot(cache_dir, "submissions", sha256, cik)
    return _parsed(snapshot, lambda payload, m: parse_submissions(payload, m.cik, m.source))


def load_company_facts_snapshot(cache_dir: Path, cik: str | int, sha256: str) -> CompanyFacts:
    """A verified Company Facts snapshot, parsed exactly as SecClient.get_company_facts."""
    snapshot = load_snapshot(cache_dir, "company_facts", sha256, cik)
    return _parsed(snapshot, lambda payload, m: parse_company_facts(payload, m.cik, m.source))


def load_company_tickers_snapshot(cache_dir: Path, sha256: str) -> TickerMap:
    """A verified company_tickers.json snapshot, parsed as SecClient.get_company_tickers."""
    snapshot = load_snapshot(cache_dir, "company_tickers", sha256)
    return _parsed(snapshot, lambda payload, m: parse_company_tickers(payload, m.source))


def _parsed[T](snapshot: Snapshot, parse: Callable[[dict, SnapshotMetadata], T]) -> T:
    """Parse verified snapshot bytes with the live client's own validation."""
    metadata = snapshot.metadata
    try:
        return parse(decode_json(snapshot.content, str(metadata.source.source_url)), metadata)
    except SecResponseError as exc:
        raise SnapshotError("invalid", f"{metadata.kind} snapshot: {exc}") from exc


# --- Pin manifests -----------------------------------------------------------------------


class SnapshotPin(BaseModel):
    """One exact snapshot a manifest depends on. Everything here must match the stored
    metadata on load, not only the SHA-256."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: SnapshotKind
    cik: str | None = Field(pattern=r"^\d{10}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str
    retrieved_at: AwareDatetime
    http_status: int
    size_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_url(self) -> "SnapshotPin":
        if self.source_url != snapshot_url(self.kind, self.cik):  # also checks kind/CIK rules
            raise ValueError(f"{self.source_url} is not the canonical URL for this snapshot")
        return self


class SnapshotManifest(BaseModel):
    """A versioned list of pinned snapshots, committed to the repository (the raw JSON is
    not). Loading it and then every pin reproduces exactly the same data offline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: str = Field(min_length=1)
    snapshot_schema_version: int
    description: str
    snapshots: list[SnapshotPin] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_unique(self) -> "SnapshotManifest":
        if self.snapshot_schema_version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported snapshot schema version {self.snapshot_schema_version}")
        keys = [(pin.kind, pin.cik) for pin in self.snapshots]
        if len(set(keys)) != len(keys):
            raise ValueError("a manifest pins at most one snapshot per kind and company")
        return self

    def pin(self, kind: SnapshotKind, cik: str | int | None = None) -> SnapshotPin:
        """The pin for one kind (and company). Raises KeyError if the manifest has none."""
        key = (kind, None if cik is None else normalize_cik(cik))
        for pin in self.snapshots:
            if (pin.kind, pin.cik) == key:
                return pin
        raise KeyError(
            f"the manifest pins no {kind} snapshot" + (f" for CIK {key[1]}" if cik else "")
        )


def load_snapshot_manifest(path: Path) -> SnapshotManifest:
    """Read and validate a manifest. Raises OSError or pydantic.ValidationError."""
    return SnapshotManifest.model_validate_json(path.read_bytes())


def load_pinned_snapshot(cache_dir: Path, pin: SnapshotPin) -> Snapshot:
    """The exact snapshot a pin names: verified as by load_snapshot, and its stored
    metadata must equal the pin's URL, retrieval time, status and size. Raises SnapshotError."""
    snapshot = load_snapshot(cache_dir, pin.kind, pin.sha256, pin.cik)
    source = snapshot.metadata.source
    stored = (
        str(source.source_url),
        source.retrieved_at,
        source.http_status,
        len(snapshot.content),
    )
    if stored != (pin.source_url, pin.retrieved_at, pin.http_status, pin.size_bytes):
        raise SnapshotError("mismatched", f"{pin.kind} snapshot {pin.sha256} differs from its pin")
    return snapshot


def pin_for(snapshot: Snapshot) -> SnapshotPin:
    """The manifest pin describing a stored snapshot."""
    metadata = snapshot.metadata
    return SnapshotPin(
        kind=metadata.kind,
        cik=metadata.cik,
        sha256=metadata.source.content_sha256,
        source_url=str(metadata.source.source_url),
        retrieved_at=metadata.source.retrieved_at,
        http_status=metadata.source.http_status,
        size_bytes=len(snapshot.content),
    )


def _snapshot_paths(
    cache_dir: Path, kind: SnapshotKind, cik: str | int | None, sha256: str
) -> tuple[Path, Path]:
    """Paths for a snapshot. Raises ValueError for anything unsafe, before touching files."""
    snapshot_url(kind, cik)  # validates the kind and the CIK rules
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ValueError(f"a snapshot is identified by a full lowercase SHA-256, got {sha256!r}")
    root = cache_dir.resolve()
    folder = root / "snapshots" / kind / ("all" if cik is None else normalize_cik(cik))
    content_path = (folder / f"{sha256}.json").resolve()
    if not content_path.is_relative_to(root):
        raise ValueError(f"Snapshot path escapes the cache directory: {content_path}")
    return content_path, content_path.with_name(f"{sha256}.meta.json")


def _create_once(path: Path, data: bytes) -> bool:
    """Atomically create `path` with `data` unless it already exists. Returns False if it
    existed. The data is written to a temporary file first, then hard-linked into place,
    which fails rather than replacing an existing file."""
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as temp_file:
            temp_file.write(data)
        try:
            os.link(temp_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)

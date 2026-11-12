"""Filesystem cache for SEC filing documents.

Layout, mirroring EDGAR's archive:
    <cache_dir>/filings/<cik>/<accession without dashes>/<primary_document>        exact bytes
    <cache_dir>/filings/<cik>/<accession without dashes>/<primary_document>.json   metadata

Each file is written atomically on its own (temporary file, then os.replace): bytes first,
metadata last. The pair is NOT replaced as one transaction. An interrupted write or two
concurrent writers can leave bytes and metadata that do not belong together. That is safe
because every read rebuilds a FilingDocument, whose validator checks the bytes against the
recorded SHA-256; a mismatched or corrupted entry is treated as a miss, never returned.

The cache directory must be controlled by this application and not writable by untrusted
users: the checks here keep paths inside the cache root, but do not defend against an
attacker who can rewrite the directory itself.
"""

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path

from financial_analyst.sec_client import SecClient, filing_document_url
from financial_analyst.sec_models import FilingDocument, FilingRef

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

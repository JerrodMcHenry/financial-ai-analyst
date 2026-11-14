"""One-time, budgeted acquisition of the pinned SEC structured-data snapshots (M3.2 B2).

The only networked step for structured data. Everything after it is offline: the snapshots
are stored immutably (filing_cache.save_snapshot) and a committed manifest pins each one by
its full SHA-256 (D-030).

The request budget is enforced below SecClient, in the HTTP transport: only the exact
target URLs are allowed, each at most once, and never more than the budget in total. A
refused request raises before anything is sent. SecClient is also built with
max_retries=0, so a failed request is final and acquisition stops; nothing compensates
with another request. Redirects are not followed (SecClient's existing behavior).
"""

import argparse
import sys
from pathlib import Path

import httpx

from financial_analyst.config import Settings
from financial_analyst.filing_cache import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotManifest,
    load_pinned_snapshot,
    pin_for,
    save_snapshot,
)
from financial_analyst.sec_client import SecClient, normalize_cik, parse_snapshot, snapshot_url
from financial_analyst.sec_models import Snapshot, SnapshotKind

# The approved targets, in request order: (kind, CIK).
TARGETS: tuple[tuple[SnapshotKind, str | None], ...] = (
    ("company_tickers", None),
    ("submissions", "0000320193"),  # Apple Inc.
    ("company_facts", "0000320193"),
    ("submissions", "0000019617"),  # JPMorgan Chase & Co.
    ("company_facts", "0000019617"),
)
REQUEST_BUDGET = len(TARGETS)
MAX_REQUESTS_PER_SECOND = 4.0  # acquisition never paces faster, whatever is configured
DEFAULT_MANIFEST = Path("evals/sec_snapshots_v1.json")


class RequestRefused(Exception):
    """A request outside the approved allowlist or budget. Raised before sending; not an
    httpx error, so SecClient never treats it as retryable."""


class BudgetedTransport(httpx.BaseTransport):
    """Sends only the allowed URLs, each at most once, at most `budget` requests in total.
    Every attempt counts, whatever its outcome."""

    def __init__(
        self, allowed_urls: list[str], budget: int, inner: httpx.BaseTransport | None = None
    ) -> None:
        self._allowed = set(allowed_urls)
        self._budget = budget
        self._inner = inner or httpx.HTTPTransport()
        self.attempts: list[str] = []
        self.statuses: list[int | None] = []  # None when no response was received

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in self._allowed:
            raise RequestRefused(f"{url} is not an approved acquisition URL")
        if url in self.attempts:
            raise RequestRefused(f"{url} was already requested once; no retries")
        if len(self.attempts) >= self._budget:
            raise RequestRefused(f"the request budget of {self._budget} is spent")
        self.attempts.append(url)
        self.statuses.append(None)
        response = self._inner.handle_request(request)
        self.statuses[-1] = response.status_code
        return response

    def close(self) -> None:
        self._inner.close()


def acquire_snapshots(
    settings: Settings,
    cache_dir: Path,
    targets: tuple[tuple[SnapshotKind, str | None], ...] = TARGETS,
    transport: BudgetedTransport | None = None,
    description: str = "",
) -> tuple[SnapshotManifest, BudgetedTransport]:
    """Fetch, store and verify each target once, in order; return the manifest pinning them.

    After each save the snapshot is reloaded from disk and checked (bytes' SHA-256,
    metadata, canonical URL), parsed with the typed parser, and its parsed CIK compared
    with the target's. Any failure raises immediately and later targets are not requested.
    The manifest is returned, not written; see write_manifest.
    """
    urls = [snapshot_url(kind, cik) for kind, cik in targets]
    if transport is None:
        transport = BudgetedTransport(urls, budget=len(targets))
    rate = min(settings.sec_max_requests_per_second, MAX_REQUESTS_PER_SECOND)
    paced = settings.model_copy(update={"sec_max_requests_per_second": rate})
    pins = []
    with SecClient(paced, transport=transport, max_retries=0) as sec:
        for kind, cik in targets:
            fetched = sec.get_snapshot(kind, cik)
            # Never overwrites: identical bytes stored earlier keep their first metadata,
            # and the pin describes what is actually stored.
            stored_metadata = save_snapshot(cache_dir, fetched)
            pin = pin_for(Snapshot(metadata=stored_metadata, content=fetched.content))
            stored = load_pinned_snapshot(cache_dir, pin)  # raises if it differs in any way
            parsed = parse_snapshot(stored)
            if cik is not None and parsed.cik != normalize_cik(cik):
                raise ValueError(f"{kind} snapshot names CIK {parsed.cik}, expected {cik}")
            pins.append(pin)
    manifest = SnapshotManifest(
        manifest_version="1",
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        description=description,
        snapshots=pins,
    )
    return manifest, transport


def write_manifest(path: Path, manifest: SnapshotManifest) -> None:
    """Create the manifest file. Refuses to replace an existing one: a new acquisition
    gets a new versioned manifest."""
    with path.open("x", encoding="utf-8") as file:  # "x": fail if it exists
        file.write(manifest.model_dump_json(indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.snapshot_acquisition [--cache-dir DIR] [--manifest FILE]"""
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.snapshot_acquisition")
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    if args.manifest.exists():
        print(f"{args.manifest} already exists; refusing to replace it", file=sys.stderr)
        return 2
    try:
        settings = Settings()  # the configured User-Agent; never printed
    except Exception as exc:  # noqa: BLE001 - report the blocker without its values
        print(f"SEC settings unavailable ({type(exc).__name__}); no request sent", file=sys.stderr)
        return 2
    transport = BudgetedTransport([snapshot_url(k, c) for k, c in TARGETS], budget=REQUEST_BUDGET)
    try:
        manifest, _ = acquire_snapshots(
            settings,
            args.cache_dir,
            transport=transport,
            description="Apple and JPMorgan Chase submissions and Company Facts, and SEC's "
            "company_tickers.json, acquired once for M3 (M3.2 B2).",
        )
    except Exception as exc:  # noqa: BLE001 - report and stop; never retry
        print(f"acquisition stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        _report(transport)
        return 2
    write_manifest(args.manifest, manifest)
    _report(transport)
    for pin in manifest.snapshots:
        print(f"{pin.kind:<16} {pin.cik or '-':<10} {pin.sha256} {pin.retrieved_at.isoformat()}")
    print(f"manifest written to {args.manifest}")
    return 0


def _report(transport: BudgetedTransport) -> None:
    print(f"requests attempted: {len(transport.attempts)} of {REQUEST_BUDGET}")
    for url, status in zip(transport.attempts, transport.statuses, strict=True):
        print(f"  {status} {url}")


if __name__ == "__main__":
    raise SystemExit(main())

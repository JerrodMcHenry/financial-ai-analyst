"""Deterministic financial tools (M3). This milestone: company identity resolution.

Identities resolve only against pinned, already-loaded snapshots, so resolution is offline
and reproducible: the company_tickers.json snapshot (a TickerMap), and for a CIK that map
does not list, optionally a verified submissions snapshot for that CIK. Matching is exact:
a CIK, a ticker compared case-insensitively, or one of a few fixed aliases for the
supported companies. There is no fuzzy, prefix or substring matching, and an identifier
that could mean more than one company is rejected rather than guessed (D-030).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from financial_analyst.sec_client import normalize_cik, snapshot_url
from financial_analyst.sec_models import CompanySubmissions, SourceMetadata, TickerEntry, TickerMap

MAX_IDENTIFIER_LENGTH = 64

# Exact aliases for the supported companies. Matched after case-folding and collapsing
# whitespace only, so "JPMorgan  Chase" matches but "JP Morgan" and "Apple Computer" do not.
COMPANY_ALIASES: dict[str, str] = {
    "apple": "0000320193",
    "apple inc.": "0000320193",
    "jpmorgan": "0000019617",
    "jpmorgan chase": "0000019617",
    "jpmorgan chase & co.": "0000019617",
}

IdentityProblem = Literal["invalid_identifier", "company_not_found", "company_ambiguous"]
MatchedBy = Literal["cik", "ticker", "alias"]
VerifiedBy = Literal["company_tickers", "submissions"]


class IdentityError(Exception):
    """The identifier does not name exactly one company in the pinned ticker snapshot."""

    def __init__(self, problem: IdentityProblem, message: str, candidates: list[str]) -> None:
        super().__init__(message)
        self.problem = problem
        self.candidates = candidates  # CIKs that matched; non-empty only when ambiguous


class CompanyIdentity(BaseModel):
    """One resolved company, with how it was matched and the snapshot it came from."""

    model_config = ConfigDict(frozen=True)

    cik: str
    name: str  # SEC's title in the ticker snapshot, or the company name in submissions
    tickers: list[str]  # every ticker the verifying snapshot lists for this CIK, sorted
    matched_by: MatchedBy
    verified_by: VerifiedBy  # which snapshot confirmed the company exists
    source: SourceMetadata  # that snapshot's provenance (URL, retrieved_at, SHA-256)


def resolve_company(
    identifier: str, tickers: TickerMap, submissions: CompanySubmissions | None = None
) -> CompanyIdentity:
    """Resolve a CIK, ticker or exact alias to one company. Raises IdentityError.

    - All digits: a CIK (normalized to 10 digits). It is verified by the ticker map when
      listed there. Otherwise it is verified by `submissions`, if given: a submissions
      snapshot the caller already loaded and verified (load_submissions_snapshot) for that
      exact CIK. Nothing is ever fetched. Unverified by either: company_not_found.
    - Otherwise: a ticker (exact, case-insensitive) and/or an alias (exact after
      case-folding and collapsing whitespace), against the ticker map only. Every match is
      collected, and an identifier matching more than one CIK is ambiguous.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        raise IdentityError("invalid_identifier", "the identifier is empty", [])
    text = identifier.strip()
    if len(text) > MAX_IDENTIFIER_LENGTH:
        raise IdentityError(
            "invalid_identifier", f"identifiers are at most {MAX_IDENTIFIER_LENGTH} characters", []
        )

    by_cik: dict[str, list[TickerEntry]] = {}
    for entry in tickers.entries:
        by_cik.setdefault(entry.cik, []).append(entry)

    if text.isascii() and text.isdigit():
        try:
            cik = normalize_cik(text)
        except ValueError as exc:
            raise IdentityError("invalid_identifier", str(exc), []) from exc
        if cik in by_cik:
            return _identity(cik, by_cik[cik], "cik", tickers)
        if _verifies(submissions, cik):
            return CompanyIdentity(
                cik=cik,
                name=submissions.name,
                tickers=sorted(set(submissions.tickers)),
                matched_by="cik",
                verified_by="submissions",
                source=submissions.source,
            )
        raise IdentityError(
            "company_not_found",
            f"CIK {cik} is not in the ticker snapshot or a submissions snapshot for that CIK",
            [],
        )

    ticker_ciks = {e.cik for e in tickers.entries if e.ticker.casefold() == text.casefold()}
    alias_cik = COMPANY_ALIASES.get(" ".join(text.split()).casefold())
    candidates = sorted(ticker_ciks | ({alias_cik} if alias_cik else set()))
    if len(candidates) > 1:
        raise IdentityError(
            "company_ambiguous", f"{text!r} matches {len(candidates)} companies", candidates
        )
    if not candidates or candidates[0] not in by_cik:
        raise IdentityError("company_not_found", f"no company matches {text!r} exactly", [])
    cik = candidates[0]
    return _identity(cik, by_cik[cik], "ticker" if ticker_ciks else "alias", tickers)


def _identity(
    cik: str, entries: list[TickerEntry], matched_by: MatchedBy, tickers: TickerMap
) -> CompanyIdentity:
    # A CIK with several tickers (share classes) normally has one title; if SEC lists more
    # than one, the first in sorted order is used so the result never depends on row order.
    return CompanyIdentity(
        cik=cik,
        name=sorted(e.title for e in entries)[0],
        tickers=sorted({e.ticker for e in entries}),
        matched_by=matched_by,
        verified_by="company_tickers",
        source=tickers.source,
    )


def _verifies(submissions: CompanySubmissions | None, cik: str) -> bool:
    """A submissions snapshot verifies a CIK only if it is that CIK's own response."""
    return (
        submissions is not None
        and submissions.cik == cik
        and str(submissions.source.source_url) == snapshot_url("submissions", cik)
    )

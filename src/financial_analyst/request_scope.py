"""Deterministic request scope (M3.6 B3.1): what the user asked, read from the question alone.

The model's tool calls choose which comparison to make; they must not redefine the question.
capture_scope reads the company, metric and periods from the question text with a small set
of fixed patterns, and check_subject compares an answer's subject comparison with that scope
before anything is published (grounding.validate_draft). If the scope cannot be read, is
ambiguous or is unsupported, the answer abstains rather than risk answering another question.

This is not a natural-language parser. Supported patterns (case-insensitive):
- Company: a supported company's exact alias (financial_tools.COMPANY_ALIASES), one of its
  tickers from the pinned ticker map (upper case, e.g. AAPL, JPM), or "CIK <digits>".
- Metric: revenue ("revenue", "net revenue", "net sales", "sales", "top line"), net income
  ("net income", "profit", "profitable", "earnings", "bottom line"), total assets ("total
  assets", "assets"). Named unsupported metrics (EBITDA, EPS, margins, cash flow, operating
  income) make the scope unsupported.
- Periods: "fiscal 2026", "fiscal year 2026", "FY2026", "2026 fiscal year"; "Q3 2026",
  "Q3 FY2026", "third quarter (of) (fiscal) 2026", "third-quarter 2026"; and, only for a
  company whose fiscal year ends December 31, "the September 2026 quarter" and balance dates
  "September 30, 2026" (December 31 is the year end). Years are fiscal years, as in
  financial_tools. Bare calendar years ("in 2026") are not read.
- Comparison: two periods are current (the later) and prior (the earlier); one period is
  compared with the same period a year earlier. Total assets use the matching balance-date
  kinds. Q4 is unsupported (financial_tools does not report fourth quarters separately).
- Segments and products: only consolidated figures are supported. A question naming a
  reported segment, product or region (iPhone, Services, Greater China, CCB, ...) or the words
  "segment", "product line", "by region" is unsupported, never read as consolidated revenue.
  This is deliberately conservative: such a word anywhere in the question is enough.
- What the answer must contain: commentary is required only when the question asks for it
  ("why", "what drove / explains", "explain", what a company or filing "did/does ... say",
  "said", "describe", "discuss", "management", "MD&A", "commentary"); a quotation is required
  only when one is asked for ("quote", "quotation", "verbatim", "exact words"), which also
  requires commentary. Comparing two periods alone requires figures only.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from financial_analyst.analysis import PinnedData
from financial_analyst.financial_tools import (
    COMPANY_ALIASES,
    CONCEPT_MAPPINGS,
    Comparison,
    Metric,
    PeriodRequest,
)
from financial_analyst.sec_client import normalize_cik

SCOPE_VERSION = "2"  # 2: commentary and quotation requirements, segments unsupported
_YEAR = r"((?:19|20)\d\d)"
_QUARTER_FILLER = r"(?:\s+of)?(?:\s+fiscal(?:\s+year)?)?\s+"  # "third quarter of fiscal 2026"
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_CALENDAR_QUARTERS = {"march": 1, "june": 2, "september": 3, "december": 4}
_QUARTER_END_DATES = {"march 31": 1, "june 30": 2, "september 30": 3, "december 31": 4}

_METRIC_PHRASES: dict[str, Metric | None] = {  # None: a metric that is not supported
    "total net sales": "revenue", "net sales": "revenue", "total net revenue": "revenue",
    "net revenue": "revenue", "revenues": "revenue", "revenue": "revenue", "sales": "revenue",
    "top line": "revenue", "top-line": "revenue",
    "net income": "net_income", "bottom line": "net_income", "bottom-line": "net_income",
    "profitable": "net_income", "profitability": "net_income", "profits": "net_income",
    "profit": "net_income", "earnings": "net_income",
    "total assets": "total_assets", "assets": "total_assets",
    "ebitda": None, "earnings per share": None, "eps": None, "operating income": None,
    "operating margin": None, "gross margin": None, "net margin": None, "margin": None,
    "free cash flow": None, "cash flow": None, "net interest income": None,
    "noninterest revenue": None, "noninterest income": None, "noninterest expense": None,
}  # fmt: skip
# Reported segments, products and regions of the supported companies (from their filings),
# matched case-sensitively as names, plus generic words that ask for a breakdown.
_SEGMENT_NAMES = (
    "iPhone", "iPad", "Mac", "Wearables", "Services", "AppleCare", "Americas", "Europe",
    "Greater China", "Japan", "Rest of Asia Pacific", "CCB", "CIB", "AWM",
    "Consumer & Community Banking", "Commercial & Investment Bank", "Asset & Wealth Management",
    "Card Services", "Banking & Wealth Management",
)  # fmt: skip
_SEGMENT_WORDS = re.compile(
    r"\b(?:segments?|product lines?|by product|by region|by geography|geographic|divisions?)\b",
    re.IGNORECASE,
)
_COMMENTARY_CUES = re.compile(
    r"\bwhy\b|\bwhat\s+(?:drove|drives|explains|explained|caused|causes)\b"
    r"|\b(?:explain|explains|explained|explanation)\b|\b(?:did|does|do)\b[^?.!]{0,80}\bsay\b"
    r"|\bsaid\b|\bcommentary\b|\bmd&a\b|\bmanagement\b"
    r"|\b(?:describe|describes|described|discuss|discusses|discussed)\b",
    re.IGNORECASE,
)
_QUOTATION_CUES = re.compile(
    r"\b(?:quote|quotes|quotation|quotations|verbatim|exact words)\b", re.IGNORECASE
)

ScopeStatus = Literal["resolved", "unsupported", "ambiguous", "unparsed"]


class RequestScope(BaseModel):
    """The company, metric and periods a question asks about, or why they are unknown."""

    model_config = ConfigDict(frozen=True)

    status: ScopeStatus
    cik: str | None
    metric: Metric | None
    current: PeriodRequest | None
    prior: PeriodRequest | None
    commentary_required: bool  # the question asks what the filing or management says
    quotation_requested: bool  # the question asks for a quotation (implies commentary)
    matched: list[str]  # the question text each part was read from
    reasons: list[str]  # why the scope is not resolved, or what was implied
    version: str = SCOPE_VERSION


def capture_scope(question: str, data: PinnedData) -> RequestScope:
    """Read the request scope from the question text. Never uses the model or tools."""
    matched: list[str] = []
    reasons: list[str] = []
    quotation = bool(_QUOTATION_CUES.search(question))
    commentary = quotation or bool(_COMMENTARY_CUES.search(question))

    def result(status: ScopeStatus, cik=None, metric=None, current=None, prior=None):
        return RequestScope(
            status=status, cik=cik, metric=metric, current=current, prior=prior,
            commentary_required=commentary, quotation_requested=quotation, matched=matched,
            reasons=reasons,
        )  # fmt: skip

    ciks, unknown_cik = _companies(question, data, matched)
    if unknown_cik:
        reasons.append(f"CIK {unknown_cik} has no pinned data")
        return result("unsupported")
    if len(ciks) != 1:
        reasons.append("no supported company is named" if not ciks
                       else f"more than one company is named: {sorted(ciks)}")  # fmt: skip
        return result("unparsed" if not ciks else "ambiguous")
    cik = ciks.pop()

    segment = _segment(question)
    if segment:
        matched.append(segment)
        reasons.append(f"{segment!r}: segment, product and regional figures are not supported, "
                       "only consolidated ones")  # fmt: skip
        return result("unsupported", cik)

    metrics = _metrics(question, matched)
    if None in metrics:
        reasons.append("the question asks about a metric that is not supported")
        return result("unsupported", cik)
    if len(metrics) != 1:
        reasons.append("no supported metric is named" if not metrics
                       else f"more than one metric is named: {sorted(metrics)}")  # fmt: skip
        return result("unparsed" if not metrics else "ambiguous", cik)
    metric = metrics.pop()
    if (cik, metric) not in CONCEPT_MAPPINGS:
        reasons.append(f"{metric} is not mapped for CIK {cik}")
        return result("unsupported", cik, metric)

    calendar_year = data.companies[cik][0].fiscal_year_end == "1231"
    periods = _periods(question, calendar_year, matched)
    if not periods:
        reasons.append("no fiscal period is named in a supported form")
        return result("unparsed", cik, metric)
    if len(periods) > 2:
        reasons.append(f"more than two periods are named: {len(periods)}")
        return result("ambiguous", cik, metric)
    balance = metric == "total_assets"
    if any(p.kind.endswith("_end") for p in periods) and not balance:
        reasons.append("a balance date is named for a metric reported over a period")
        return result("ambiguous", cik, metric)
    periods = [_as_balance(p) if balance else p for p in periods]
    if len(periods) == 1:
        current = periods[0]
        prior = current.model_copy(update={"fiscal_year": current.fiscal_year - 1})
        reasons.append("one period named: compared with the same period a year earlier")
    else:
        current, prior = sorted(periods, key=_order, reverse=True)
    if 4 in (current.quarter, prior.quarter):
        reasons.append("fourth quarters are not reported separately")
        return result("unsupported", cik, metric, current, prior)
    return result("resolved", cik, metric, current, prior)


def check_subject(scope: RequestScope, comparison: Comparison) -> list[str]:
    """How the comparison differs from the requested scope; [] when it answers it."""
    if scope.status != "resolved":
        return [f"the request scope is {scope.status}: {'; '.join(scope.reasons)}"]
    problems = []
    if comparison.cik != scope.cik:
        problems.append(f"company {comparison.cik}, but the question asks about {scope.cik}")
    if comparison.metric != scope.metric:
        problems.append(f"metric {comparison.metric}, but the question asks about {scope.metric}")
    for side, asked in (("current", scope.current), ("prior", scope.prior)):
        got = getattr(comparison, side).period.request
        if _key(got) != _key(asked):
            problems.append(
                f"{side} period {_label(got)}, but the question asks for {_label(asked)}"
            )
    return problems


def _companies(question: str, data: PinnedData, matched: list[str]) -> tuple[set[str], str | None]:
    found: set[str] = set()
    for alias, cik in COMPANY_ALIASES.items():
        if cik in data.companies and _find(rf"\b{re.escape(alias)}(?![\w&])", question, matched):
            found.add(cik)
    for entry in data.tickers.entries:
        cik = normalize_cik(entry.cik)
        if cik in data.companies and re.search(rf"(?<![\w-]){re.escape(entry.ticker)}\b", question):
            matched.append(entry.ticker)
            found.add(cik)
    unknown = None
    for digits in re.findall(r"\bCIK\s*(\d{1,10})\b", question, re.IGNORECASE):
        matched.append(f"CIK {digits}")
        cik = normalize_cik(digits)
        if cik in data.companies:
            found.add(cik)
        else:
            unknown = cik
    return found, unknown


def _segment(question: str) -> str | None:
    for name in _SEGMENT_NAMES:
        if re.search(rf"(?<![\w&]){re.escape(name)}(?![\w&])", question):
            return name
    match = _SEGMENT_WORDS.search(question)
    return match.group() if match else None


def _metrics(question: str, matched: list[str]) -> set[Metric | None]:
    text = question.lower()
    taken = [False] * len(text)
    found: set[Metric | None] = set()
    for phrase in sorted(_METRIC_PHRASES, key=len, reverse=True):  # longest phrase wins
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", text):
            if any(taken[match.start() : match.end()]):
                continue
            taken[match.start() : match.end()] = [True] * (match.end() - match.start())
            matched.append(question[match.start() : match.end()])
            found.add(_METRIC_PHRASES[phrase])
    return found


def _periods(question: str, calendar_year: bool, matched: list[str]) -> list[PeriodRequest]:
    patterns: list[tuple[str, object]] = [
        (rf"\bq([1-4])\s*(?:fy\s*)?{_YEAR}\b", lambda m: _quarter(int(m[1]), int(m[2]))),
        (rf"\b(first|second|third|fourth)[ -]quarter{_QUARTER_FILLER}{_YEAR}\b",
         lambda m: _quarter(_ORDINALS[m[1]], int(m[2]))),
        (rf"\bfy\s*{_YEAR}\b", lambda m: _year(int(m[1]))),
        (rf"\bfiscal(?:\s+year)?\s+{_YEAR}\b", lambda m: _year(int(m[1]))),
        (rf"\b{_YEAR}\s+fiscal\s+year\b", lambda m: _year(int(m[1]))),
    ]  # fmt: skip
    if calendar_year:  # calendar quarters and dates mean fiscal ones only for these companies
        patterns += [
            (rf"\b(march|june|september|december)\s+{_YEAR}\s+quarter\b",
             lambda m: _quarter(_CALENDAR_QUARTERS[m[1]], int(m[2]))),
            (rf"\b(march 31|june 30|september 30|december 31),?\s+{_YEAR}\b",
             lambda m: _balance_date(_QUARTER_END_DATES[m[1]], int(m[2]))),
        ]  # fmt: skip
    text = question.lower()
    taken = [False] * len(text)
    found: list[tuple[int, PeriodRequest]] = []
    for pattern, build in patterns:
        for match in re.finditer(pattern, text):
            if any(taken[match.start() : match.end()]):
                continue
            taken[match.start() : match.end()] = [True] * (match.end() - match.start())
            matched.append(question[match.start() : match.end()])
            found.append((match.start(), build(match)))
    periods: list[PeriodRequest] = []
    for _, period in sorted(found, key=lambda item: item[0]):
        if period not in periods:
            periods.append(period)
    return periods


def _year(year: int) -> PeriodRequest:
    return PeriodRequest(kind="fiscal_year", fiscal_year=year)


def _quarter(quarter: int, year: int) -> PeriodRequest:
    return PeriodRequest(kind="fiscal_quarter", fiscal_year=year, quarter=quarter)


def _balance_date(quarter: int, year: int) -> PeriodRequest:
    if quarter == 4:  # December 31 ends a calendar fiscal year
        return PeriodRequest(kind="fiscal_year_end", fiscal_year=year)
    return PeriodRequest(kind="fiscal_quarter_end", fiscal_year=year, quarter=quarter)


def _as_balance(period: PeriodRequest) -> PeriodRequest:
    kind = {"fiscal_year": "fiscal_year_end", "fiscal_quarter": "fiscal_quarter_end"}
    return period.model_copy(update={"kind": kind.get(period.kind, period.kind)})


def _order(period: PeriodRequest) -> tuple[int, int]:
    return period.fiscal_year, period.quarter or 5


def _key(period: PeriodRequest | None) -> tuple | None:
    return None if period is None else (period.kind, period.fiscal_year, period.quarter)


def _label(period: PeriodRequest | None) -> str:
    if period is None:
        return "none"
    quarter = f"Q{period.quarter} " if period.quarter else ""
    end = " end" if period.kind.endswith("_end") else ""
    return f"{quarter}FY{period.fiscal_year}{end}"


def _find(pattern: str, question: str, matched: list[str]) -> bool:
    match = re.search(pattern, question, re.IGNORECASE)
    if match:
        matched.append(match.group())
    return match is not None

"""Company Facts retrieval, parsing, period classification and selection. Offline only."""

import copy
import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financial_analyst import sec_client
from financial_analyst.company_facts import (
    duration_days,
    find_concept,
    find_observations,
    period_class,
    select_fact,
)
from financial_analyst.sec_client import SecClient, SecNotFoundError, SecResponseError
from financial_analyst.sec_models import FactObservation

FIXTURE = Path(__file__).parent / "fixtures" / "companyfacts_sample.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
FY2024 = (date(2023, 10, 1), date(2024, 9, 28))


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(sec_client.time, "sleep", lambda seconds: None)


def fetch(settings, body: bytes, status: int = 200):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, content=body)

    with SecClient(settings, transport=httpx.MockTransport(handler)) as sec:
        return sec.get_company_facts("320193"), requests


def load_payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def facts(settings):
    return fetch(settings, FIXTURE.read_bytes())[0]


def test_retrieves_company_facts_with_provenance(settings):
    body = FIXTURE.read_bytes()
    facts, requests = fetch(settings, body)

    assert [str(r.url) for r in requests] == [FACTS_URL]
    assert (facts.cik, facts.entity_name) == ("0000320193", "Apple Inc.")
    assert len(facts.observations) == 22
    assert str(facts.source.source_url) == FACTS_URL
    assert facts.source.content_sha256 == hashlib.sha256(body).hexdigest()

    # A FY2024 comparative reported in the fiscal 2026 10-K: fy/fp belong to the filing.
    row = select_fact(facts, "us-gaap", "Revenues", "USD", *FY2024).selected
    assert (row.period_start, row.period_end) == FY2024
    assert (row.filing_fiscal_year, row.filing_fiscal_period) == (2026, "FY")
    assert (row.form, row.filed, row.accession_number) == (
        "10-K",
        date(2026, 11, 3),
        "0000320193-26-000026",
    )
    assert row.frame == "CY2024"
    assert (
        row.filing_index_url == "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/"
    )


def test_values_are_exact_decimals(facts):
    assert all(type(obs.value) is Decimal for obs in facts.observations)
    eps = find_observations(facts, "us-gaap", "EarningsPerShareDiluted", "USD/shares")[0]
    assert (
        str(eps.value) == "6.08"
    )  # a float would be 6.0800000000000000710542735760100185871124267578125
    shares = find_observations(facts, "dei", "EntityCommonStockSharesOutstanding", "shares")[0]
    assert shares.value == Decimal("14776353000")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [(404, b"", SecNotFoundError), (200, b"<html>not json</html>", SecResponseError)],
)
def test_http_and_body_errors(settings, status, body, error):
    with pytest.raises(error):
        fetch(settings, body, status)


def _cik_mismatch(p):
    p["cik"] = 789019


def _no_facts(p):
    del p["facts"]


def _facts_not_object(p):
    p["facts"] = []


def _rows_not_list(p):
    p["facts"]["us-gaap"]["Assets"]["units"]["USD"] = {"val": 1}


def _all_rows_malformed(p):
    p["facts"] = {"us-gaap": {"Revenues": {"label": "Revenues", "units": {"USD": [{"val": "x"}]}}}}


@pytest.mark.parametrize(
    "damage", [_cik_mismatch, _no_facts, _facts_not_object, _rows_not_list, _all_rows_malformed]
)
def test_invalid_overall_structure_is_rejected(settings, damage):
    payload = load_payload()
    damage(payload)

    with pytest.raises(SecResponseError):
        fetch(settings, json.dumps(payload).encode())


def test_malformed_rows_are_skipped_with_bounded_diagnostics(facts):
    assert facts.skipped_count == 5
    sample = [(s.taxonomy, s.concept, s.unit, s.row_index) for s in facts.skipped_sample]
    assert sample == [("us-gaap", "Revenues", "USD", i) for i in range(9, 14)]
    reasons = [s.reason for s in facts.skipped_sample]
    assert "not a finite number: '416161000000'" in reasons[0]  # string
    assert "not a finite number: True" in reasons[1]  # boolean
    assert reasons[2] == "missing field 'end'"
    assert "period_start is after period_end" in reasons[3]
    assert reasons[4].startswith("accession_number")


def test_nonfinite_values_are_rejected(settings):
    # NaN and Infinity are not valid JSON numbers, but Python's parser accepts them.
    row = '{"end": "2024-09-28", "val": %s, "accn": "0000320193-24-000123", "form": "10-K", '
    row += '"filed": "2024-11-01"}'
    rows = ", ".join(row % value for value in ("1", "NaN", "Infinity"))
    body = (
        '{"cik": 320193, "entityName": "Apple Inc.", '
        f'"facts": {{"us-gaap": {{"Assets": {{"units": {{"USD": [{rows}]}}}}}}}}}}'
    ).encode()

    facts, _ = fetch(settings, body)

    assert [obs.value for obs in facts.observations] == [Decimal(1)]
    assert facts.skipped_count == 2


def test_concept_labels_and_descriptions_are_kept_once_per_concept(facts):
    revenues = find_concept(facts, "us-gaap", "Revenues")
    assert revenues.label == "Revenues"
    assert revenues.description.startswith("Amount of revenue recognized")
    assert find_concept(facts, "us-gaap", "ResearchAndDevelopmentExpense").description is None
    assert find_concept(facts, "us-gaap", "NotAConcept") is None
    assert "description" not in FactObservation.model_fields
    assert len(facts.concepts) == 7


def _obs(start: str | None, end: str) -> FactObservation:
    return FactObservation(
        cik="0000320193",
        taxonomy="us-gaap",
        concept="Revenues",
        unit="USD",
        value=Decimal(1),
        period_start=start,
        period_end=end,
        filing_fiscal_year=None,
        filing_fiscal_period=None,
        form="10-K",
        filed="2026-11-03",
        accession_number="0000320193-26-000026",
        frame=None,
    )


@pytest.mark.parametrize(
    ("start", "end", "days", "expected"),
    [
        (None, "2024-09-28", None, "instant"),
        ("2023-01-01", "2023-04-01", 91, "3_months"),
        ("2022-09-25", "2022-12-31", 98, "3_months"),  # 14-week quarter
        ("2022-09-25", "2023-04-01", 189, "6_months"),
        ("2022-09-25", "2023-07-01", 280, "9_months"),
        ("2023-10-01", "2024-09-28", 364, "12_months"),  # 52-week year
        ("2022-09-25", "2023-09-30", 371, "12_months"),  # 53-week year
        ("2024-01-01", "2024-01-31", 31, "other"),
        ("2024-01-01", "2024-04-30", 121, "other"),
    ],
)
def test_period_classes_are_durations(start, end, days, expected):
    obs = _obs(start, end)
    assert duration_days(obs) == days
    assert period_class(obs) == expected


def test_repeated_and_conflicting_values_across_filings(facts):
    latest = select_fact(facts, "us-gaap", "Revenues", "USD", *FY2024, policy="latest_filed")
    original = select_fact(facts, "us-gaap", "Revenues", "USD", *FY2024, policy="original")

    assert [c.filed for c in latest.candidates] == [
        date(2024, 11, 1),
        date(2025, 10, 31),
        date(2026, 11, 3),
    ]
    assert latest.conflicting_values and original.conflicting_values
    assert latest.selected.value == Decimal("391040000000")
    assert latest.selected.accession_number == "0000320193-26-000026"
    assert original.selected.value == Decimal("391035000000")
    assert original.selected.accession_number == "0000320193-24-000123"
    assert "different value" in latest.reason


def test_amendment_is_an_eligible_later_filing(facts):
    end = date(2024, 9, 28)
    latest = select_fact(facts, "us-gaap", "Assets", "USD", None, end)
    original = select_fact(facts, "us-gaap", "Assets", "USD", None, end, policy="original")

    assert (latest.selected.form, latest.selected.value) == ("10-K/A", Decimal("365000000000"))
    assert (original.selected.form, original.selected.value) == ("10-K", Decimal("364980000000"))


def test_same_date_filings_that_disagree_are_not_resolved(facts):
    for policy in ("latest_filed", "original"):
        result = select_fact(
            facts, "us-gaap", "ResearchAndDevelopmentExpense", "USD", *FY2024, policy=policy
        )
        assert result.selected is None
        assert len(result.candidates) == 2
        assert "not choosing" in result.reason


def test_same_date_filings_that_agree_select_deterministically(facts):
    result = select_fact(
        facts,
        "us-gaap",
        "ResearchAndDevelopmentExpense",
        "USD",
        date(2022, 9, 25),
        date(2023, 9, 30),
    )
    assert result.selected.accession_number == "0000320193-24-000123"  # lowest accession
    assert not result.conflicting_values


def test_conflicting_values_within_one_filing_are_not_resolved(facts):
    result = select_fact(facts, "us-gaap", "IncomeTaxesPaidNet", "USD", *FY2024)

    assert result.selected is None
    assert {c.accession_number for c in result.candidates} == {"0000320193-24-000123"}
    assert result.conflicting_values


def test_missing_observations_stay_missing(facts):
    unknown = select_fact(facts, "us-gaap", "NotAConcept", "USD", *FY2024)
    other_period = select_fact(
        facts, "us-gaap", "Revenues", "USD", date(2019, 9, 29), date(2020, 9, 26)
    )
    eight_k_only = select_fact(
        facts, "us-gaap", "Revenues", "USD", date(2026, 6, 28), date(2026, 9, 26)
    )

    assert (unknown.selected, unknown.candidates) == (None, [])
    assert "not reported by this company" in unknown.reason
    assert (other_period.selected, other_period.candidates) == (None, [])
    assert "not reported for this exact period" in other_period.reason
    assert eight_k_only.selected is None
    assert [c.form for c in eight_k_only.candidates] == ["8-K"]
    assert "['8-K']" in eight_k_only.reason


def test_units_and_currencies_never_mix(facts):
    end = date(2024, 9, 28)
    usd = find_observations(facts, "us-gaap", "DebtInstrumentFaceAmount", "USD")
    eur = select_fact(facts, "us-gaap", "DebtInstrumentFaceAmount", "EUR", None, end)
    gbp = select_fact(facts, "us-gaap", "DebtInstrumentFaceAmount", "GBP", None, end)

    assert [(o.unit, o.value) for o in usd] == [("USD", Decimal("1000000000"))]
    assert (eur.selected.unit, eur.selected.value) == ("EUR", Decimal("2000000000"))
    assert gbp.selected is None and gbp.candidates == []


def test_selection_does_not_depend_on_sec_row_order(settings, facts):
    reversed_payload = copy.deepcopy(load_payload())
    for taxonomy in reversed_payload["facts"].values():
        for details in taxonomy.values():
            for unit, rows in details["units"].items():
                details["units"][unit] = list(reversed(rows))
    reordered, _ = fetch(settings, json.dumps(reversed_payload).encode())

    queries = [
        ("Revenues", "USD", *FY2024),
        ("Assets", "USD", None, date(2024, 9, 28)),
        ("ResearchAndDevelopmentExpense", "USD", date(2022, 9, 25), date(2023, 9, 30)),
        ("ResearchAndDevelopmentExpense", "USD", *FY2024),
    ]
    for concept, unit, start, end in queries:
        for policy in ("latest_filed", "original"):
            a = select_fact(facts, "us-gaap", concept, unit, start, end, policy)
            b = select_fact(reordered, "us-gaap", concept, unit, start, end, policy)
            assert (a.selected, a.candidates, a.reason) == (b.selected, b.candidates, b.reason)

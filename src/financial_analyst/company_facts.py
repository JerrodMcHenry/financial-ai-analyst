"""Period classification and observation selection for SEC Company Facts.

SEC lists every time a value was reported: the original filing, later filings that repeat
it as a comparative, and amendments. This module finds the observations for one exact
period and picks one under an explicit policy, or picks none when the choice would be
arbitrary. It never averages, merges or converts values.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from financial_analyst.sec_models import (
    CompanyFacts,
    FactConcept,
    FactObservation,
    SourceMetadata,
)

# Only periodic reports are eligible for selection. Other forms (8-K, S-1, ...) still
# appear in FactSelection.candidates so nothing is hidden.
ELIGIBLE_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})

SelectionPolicy = Literal["latest_filed", "original"]
# Lengths of time, not fiscal periods: a "3_months" duration is not necessarily a fiscal
# quarter, and duration alone cannot say which fiscal quarter or year it belongs to.
PeriodClass = Literal["instant", "3_months", "6_months", "9_months", "12_months", "other"]

# Inclusive day ranges wide enough for 52/53-week fiscal years and 13/14-week quarters.
_DURATION_CLASSES: tuple[tuple[int, int, PeriodClass], ...] = (
    (84, 98, "3_months"),
    (175, 189, "6_months"),
    (266, 280, "9_months"),
    (357, 371, "12_months"),
)


class FactSelection(BaseModel):
    """The result of choosing one observation for an exact concept, unit and period."""

    model_config = ConfigDict(frozen=True)

    selected: FactObservation | None  # None when not reported, ineligible, or ambiguous
    candidates: list[FactObservation]  # every observation for the exact period, any form, sorted
    # True when observations from ELIGIBLE forms (10-K/10-Q and amendments) report different
    # values for this period, e.g. a later 10-K revising a comparative. Disagreement from
    # ineligible forms (8-K, 424B2, ...) is not counted; those rows stay visible in candidates.
    conflicting_values: bool
    reason: str
    source: SourceMetadata  # the Company Facts response the observations came from


def duration_days(observation: FactObservation) -> int | None:
    """Inclusive length of a duration fact in days, or None for an instant fact."""
    if observation.period_start is None:
        return None
    return (observation.period_end - observation.period_start).days + 1


def period_class(observation: FactObservation) -> PeriodClass:
    days = duration_days(observation)
    if days is None:
        return "instant"
    for low, high, name in _DURATION_CLASSES:
        if low <= days <= high:
            return name
    return "other"


def find_concept(facts: CompanyFacts, taxonomy: str, concept: str) -> FactConcept | None:
    """SEC's label and description for a concept, or None if the company never reported it."""
    for candidate in facts.concepts:
        if candidate.taxonomy == taxonomy and candidate.concept == concept:
            return candidate
    return None


def find_observations(
    facts: CompanyFacts, taxonomy: str, concept: str, unit: str
) -> list[FactObservation]:
    """All observations of a concept in exactly one unit, in a deterministic order."""
    matches = [
        obs
        for obs in facts.observations
        if obs.taxonomy == taxonomy and obs.concept == concept and obs.unit == unit
    ]
    return sorted(matches, key=_sort_key)


def select_fact(
    facts: CompanyFacts,
    taxonomy: str,
    concept: str,
    unit: str,
    period_start: date | None,
    period_end: date,
    policy: SelectionPolicy = "latest_filed",
) -> FactSelection:
    """Choose one observation for an exact period, or None with the reason.

    - latest_filed: the most recently filed eligible value (reflects revisions).
    - original: the first filed eligible value (as originally reported).
    If the eligible observations filed on the chosen date disagree, nothing is selected:
    picking one would be arbitrary.
    """
    candidates = [
        obs
        for obs in find_observations(facts, taxonomy, concept, unit)
        if obs.period_start == period_start and obs.period_end == period_end
    ]
    eligible = [obs for obs in candidates if obs.form in ELIGIBLE_FORMS]
    conflicting = len({obs.value for obs in eligible}) > 1

    def result(selected: FactObservation | None, reason: str) -> FactSelection:
        return FactSelection(
            selected=selected,
            candidates=candidates,
            conflicting_values=conflicting,
            reason=reason,
            source=facts.source,
        )

    name = f"{taxonomy}:{concept} [{unit}]"
    if not candidates:
        if find_concept(facts, taxonomy, concept) is None:
            return result(None, f"{taxonomy}:{concept} is not reported by this company")
        return result(None, f"{name} is not reported for this exact period")

    if not eligible:
        forms = sorted({obs.form for obs in candidates})
        return result(None, f"{name} is reported for this period only in {forms}")

    pick_date = max if policy == "latest_filed" else min
    chosen_date = pick_date(obs.filed for obs in eligible)
    chosen = [obs for obs in eligible if obs.filed == chosen_date]
    values = sorted({obs.value for obs in chosen})
    if len(values) > 1:
        accessions = sorted({obs.accession_number for obs in chosen})
        return result(
            None,
            f"{len(chosen)} eligible observations filed {chosen_date} ({', '.join(accessions)}) "
            f"report different values {[str(v) for v in values]}; not choosing between them",
        )

    selected = chosen[0]  # all agree; candidates are sorted, so this is deterministic
    reason = f"{policy}: filed {selected.filed} in {selected.form} {selected.accession_number}"
    if conflicting:
        reason += "; other eligible filings report a different value for this period"
    return result(selected, reason)


def _sort_key(obs: FactObservation) -> tuple:
    # A total order over every field that can differ, so SEC's row order never matters.
    return (
        obs.filed,
        obs.accession_number,
        obs.form,
        obs.period_end,
        obs.period_start or date.min,
        obs.value,
        obs.frame or "",
        obs.filing_fiscal_year or 0,
        obs.filing_fiscal_period or "",
    )

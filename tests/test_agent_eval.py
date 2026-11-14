"""The offline agent evaluation set and its runner. Offline, sockets blocked.

Every case is run (twice, for determinism) against the pinned snapshots and cached filings.
Pinned tests are skipped when the local (git-ignored) cache is not present.
"""

import hashlib
import json
import socket
from collections import Counter
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from financial_analyst.agent import run_agent
from financial_analyst.agent_eval import (
    METRICS,
    AgentEvalDataset,
    is_v2,
    load_dataset,
    load_dataset_v2,
    main,
    score_case,
    score_case_v2,
    scripted_model,
)
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.filing_cache import load_filing_document
from financial_analyst.request_scope import capture_scope

ROOT = Path(__file__).parent.parent
DATASET = ROOT / "evals" / "agent_v1.json"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
CASES = load_dataset(DATASET).cases


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def _pinned():
    try:
        data = load_pinned_data(ROOT / "evals" / "sec_snapshots_v1.json", CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if (CACHE / "filings").exists() else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


def test_the_dataset_covers_every_category():
    counts = Counter(case.category for case in CASES)
    assert len(CASES) == 26
    assert counts == {"direct": 6, "paraphrase": 3, "commentary": 4, "unsupported": 3,
                      "unanswerable": 2, "adversarial": 8}  # fmt: skip
    assert Counter(c.expected.status for c in CASES) == {
        "complete": 10, "partial": 8, "abstained": 8
    }  # fmt: skip


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d["cases"].append(d["cases"][0]),  # duplicate id
        lambda d: d["cases"][0]["expected"].update(confidence=1),  # unknown label
        lambda d: d["cases"][0].update(category="easy"),
        lambda d: d["cases"][0].update(script=[]),
    ],
)
def test_an_invalid_dataset_is_rejected(tmp_path, damage):
    document = json.loads(DATASET.read_text())
    damage(document)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    with pytest.raises((ValidationError, ValueError)):
        load_dataset(path)


@pinned
def test_labels_match_the_filings_own_statement_tables():
    # Independent of XBRL: each labeled value, in millions, is printed in the filing.
    for case in CASES:
        expected = case.expected
        if expected.current is None or expected.commentary_accession is None:
            continue
        submissions = DATA.companies[expected.cik][0]
        [filing] = [f for f in submissions.recent_filings
                    if f.accession_number == expected.commentary_accession]  # fmt: skip
        text = load_filing_document(CACHE, filing).content.decode()
        for value in (expected.current.value, expected.prior.value):
            assert f"{int(value) // 1_000_000:,}" in text, (case.id, value)


# agent_v1.json is frozen. M3.7 changed two policies on purpose, and these cases now publish
# a different status than their v1 label (agent_v2.json labels them by information need).
V1_SUPERSEDED = {
    # Figures-only questions: no commentary is required, so no citation means complete.
    "apple-fy2026-total-assets": "complete",
    "jpm-q3-2026-total-assets": "complete",
    "apple-bottom-line-by-cik": "complete",
    "prompt-injection-in-question": "complete",
    # Product metrics ("iPhone net sales") are unsupported, never consolidated revenue.
    "apple-iphone-commentary": "abstained",
    "altered-quote": "abstained",
    # M3.8.2: digits in the unpublished note no longer abstain (raw_number_in_text removed).
    "raw-number-in-narrative": "complete",
}


# raw-number-in-narrative forbids "500 billion" (the model's invented figure) and "478,834"
# (the correct figure, forbidden only because v1 expected an abstention). Now complete, it
# publishes the correct figure and still never the invented one.
V1_CORRECT_FIGURES_FORBIDDEN = {"raw-number-in-narrative": ["478,834"]}


def test_agent_v1_is_frozen():
    digest = hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert digest == "b10ff2ae0e51c749290115365047fcbaa43b448a67b3db27ec4c3ba06faa1680"


@pinned
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_each_case_meets_its_labels(case):
    result = score_case(case, DATA, CACHE)
    assert set(result.checks) <= set(METRICS) and {"status", "budget", "deterministic"} <= set(
        result.checks
    )
    if case.id not in V1_SUPERSEDED:
        assert result.failures == [], result.failures
        return
    # Only the intended policy change differs; everything else still meets the label.
    assert result.status == V1_SUPERSEDED[case.id]
    assert result.checks["budget"] and result.checks["deterministic"]
    if result.status == "complete":
        names = [f.split(":")[0] for f in result.failures]
        assert names[:2] == ["status", "partial_abstained"]
        assert set(names) <= {"status", "partial_abstained", "unsupported_rejected"}
        # A missing issue code may differ. The only forbidden text allowed to appear is the
        # correct, server-rendered figure that a v1 abstention label also forbade.
        allowed = V1_CORRECT_FIGURES_FORBIDDEN.get(case.id, [])
        assert all(f.endswith(f"leaked {allowed}") for f in result.failures
                   if f.startswith("unsupported"))  # fmt: skip
        assert result.checks.get("numeric", True) and result.checks.get("company_period", True)


@pinned
def test_a_script_that_refers_to_a_missing_result_is_a_model_error():
    model = scripted_model([{"answer": {"subject": "$cmp:0", "answer": "x"}}])
    outcome = run_agent("q", model, data=DATA, cache_dir=CACHE)
    assert outcome.status == "model_error" and "ScriptError" in outcome.error


@pinned
def test_the_cli_reports_every_metric_with_denominators(tmp_path, capsys):
    small = AgentEvalDataset(dataset_version="t", description="subset",
                             cases=[c for c in CASES if c.id in {"apple-fy2026-revenue",
                                                                 "apple-forecast"}])  # fmt: skip
    path = tmp_path / "subset.json"
    path.write_text(small.model_dump_json())
    output = tmp_path / "report.json"
    assert main(["--dataset", str(path), "--output", str(output)]) == 0
    printed = capsys.readouterr().out
    assert "status                 2/2" in printed and "does not measure LLM reasoning" in printed
    report = json.loads(output.read_text())
    assert report["metrics"]["numeric"] == [1, 1] and report["cases"] == 2


# --- agent_v2: outcome-based labels (M3.7) ---------------------------------------------------

DATASET_V2 = ROOT / "evals" / "agent_v2.json"
CASES_V2 = load_dataset_v2(DATASET_V2).cases


def test_v2_covers_the_required_kinds_of_question():
    counts = Counter(case.category for case in CASES_V2)
    assert counts == {"figures": 7, "paraphrase": 3, "commentary": 6, "unsupported": 7,
                      "unanswerable": 1, "ambiguous": 2, "injection": 2}  # fmt: skip
    assert is_v2(DATASET_V2) and not is_v2(DATASET)
    # Labels name outcomes only: no tool-error codes or issue codes anywhere.
    assert "tool_error_codes" not in DATASET_V2.read_text()
    assert "issue_codes" not in DATASET_V2.read_text()


@pytest.mark.parametrize(
    "damage",
    [
        lambda c: c["expected"].pop("figures"),  # answerable without figures
        lambda c: c["expected"].update(acceptable_statuses=["abstained"]),  # answerable
        lambda c: c["expected"].update(tool_error_codes=["x"]),  # path labels are not allowed
    ],
)
def test_an_invalid_v2_case_is_rejected(tmp_path, damage):
    document = json.loads(DATASET_V2.read_text())
    damage(document["cases"][0])
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ValidationError):
        load_dataset_v2(path)


@pinned
def test_v2_commentary_labels_agree_with_the_request_scope():
    for case in CASES_V2:
        scope = capture_scope(case.question, DATA)
        expected = case.expected
        assert scope.commentary_required == expected.requires_commentary or not expected.answerable
        assert scope.quotation_requested == expected.requires_quotation or not expected.answerable


@pinned
@pytest.mark.parametrize("case", CASES_V2, ids=[c.id for c in CASES_V2])
def test_each_v2_case_meets_its_outcome(case):
    result = score_case_v2(case, DATA, CACHE)
    assert result.failures == [], result.failures
    assert {"status", "abstention", "reliability", "budget", "deterministic"} <= set(result.checks)


@pinned
def test_v2_never_counts_partial_as_complete_when_commentary_is_required():
    [case] = [c for c in CASES_V2 if c.id == "v2-apple-why-revenue"]
    skipping = case.model_copy(update={"script": case.script[:1] + [
        {"answer": {"subject": "$cmp:0", "answer": "{company} {metric}: {delta}.",
                    "values": [{"placeholder": "delta", "value_id": "$value:0:delta"}]}}
    ]})  # fmt: skip
    result = score_case_v2(skipping, DATA, CACHE)
    assert result.status == "partial"
    assert not result.checks["status"] and not result.checks["question_answered"]
    assert result.checks["numeric"] and result.checks["context"]  # the figures are right


@pinned
def test_v2_scores_different_safe_paths_the_same():
    ms = [c for c in CASES_V2 if c.id.startswith("v2-microsoft")]
    assert len(ms) == 2 and ms[0].question == ms[1].question and ms[0].script != ms[1].script
    for case in ms:
        assert score_case_v2(case, DATA, CACHE).failures == []

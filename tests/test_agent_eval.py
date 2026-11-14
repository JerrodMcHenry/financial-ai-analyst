"""The offline agent evaluation set and its runner. Offline, sockets blocked.

Every case is run (twice, for determinism) against the pinned snapshots and cached filings.
Pinned tests are skipped when the local (git-ignored) cache is not present.
"""

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
    load_dataset,
    main,
    score_case,
    scripted_model,
)
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.filing_cache import load_filing_document

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


@pinned
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_each_case_meets_its_labels(case):
    result = score_case(case, DATA, CACHE)
    assert result.failures == [], result.failures
    assert set(result.checks) <= set(METRICS) and {"status", "budget", "deterministic"} <= set(
        result.checks
    )


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

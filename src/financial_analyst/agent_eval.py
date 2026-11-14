"""Offline evaluation of the agent pipeline with scripted models (M3.5 B2).

Each case pairs a question with a model script (what a model might do) and independently
written labels (what the published answer must be). The pipeline under test is everything
after the model: tool validation and execution, limits, grounding and rendering.

This does not measure an LLM's reasoning. A script is fixed in advance, so the evaluation
shows whether the system answers correctly when a model behaves as scripted (well, badly or
adversarially), not how often a real model behaves that way.

Script language: a step is {"tools": [{"name", "arguments"}]}, {"answer": {...}} or
{"raw": ...} (sent unchanged, to imitate malformed output). Strings starting with "$" are
filled from earlier tool results in the conversation, as a model would read them:
    $cik              CIK from the last successful resolve_company
    $cmp:I            comparison_id of the I-th successful compare (0-based)
    $values:I         (as the whole "values" list) current, prior, delta and growth refs
    $value:I:PART     value_id PART (current, prior, delta, percent) of comparison I
    $fact:I           value_id of the I-th successful get_fact
    $cite:I:J         citation id of evidence J from the I-th successful search_commentary
    $quote:I:J        a verbatim excerpt of that passage;  $alter:I:J  the excerpt, altered
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from financial_analyst.agent import (
    MAX_MODEL_TURNS,
    MAX_TOOL_CALLS,
    AgentRunResult,
    Message,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    run_agent,
)
from financial_analyst.analysis import PinnedData, load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.grounding import GroundedAnswer, ground

DEFAULT_DATASET = Path("evals/agent_v1.json")
DEFAULT_MANIFEST = Path("evals/sec_snapshots_v1.json")
Category = Literal["direct", "paraphrase", "commentary", "unsupported", "unanswerable",
                   "adversarial"]  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExpectedValue(_Strict):
    start: date | None
    end: date
    value: str  # exact, as in the filing's statement table (in units, not millions)


class Expected(_Strict):
    status: Literal["complete", "partial", "abstained"]
    run_status: str = "drafted"
    cik: str | None = None
    metric: str | None = None
    current: ExpectedValue | None = None
    prior: ExpectedValue | None = None
    delta: str | None = None
    percent: str | None = None  # signed display, e.g. "+15.1%"
    commentary_accession: str | None = None
    quotations: int | None = None  # how many quotations must be published
    tool_error_codes: list[str] = []  # each must appear among the run's tool errors
    issue_codes: list[str] = []  # each must appear among the validation issues
    forbidden_text: list[str] = []  # must not appear anywhere in the published answer


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: Category
    question: str
    script: list[dict[str, Any]] = Field(min_length=1)
    expected: Expected


class AgentEvalDataset(_Strict):
    dataset_version: str
    description: str
    cases: list[EvalCase] = Field(min_length=1)


class CaseResult(BaseModel):
    id: str
    category: Category
    status: str
    expected_status: str
    checks: dict[str, bool]  # metric name -> passed, only for metrics that apply
    failures: list[str]


class AgentEvalReport(BaseModel):
    dataset_version: str
    cases: int
    metrics: dict[str, tuple[int, int]]  # name -> (passed, applicable)
    results: list[CaseResult]
    note: str = (
        "Scripted-model evaluation: tests the deterministic pipeline (tools, limits, "
        "grounding, rendering) under fixed model behaviors; it does not measure LLM reasoning."
    )


# --- Script interpretation --------------------------------------------------------------------


class ScriptError(Exception):
    """A script refers to a tool result that does not exist in the conversation."""


def scripted_model(script: list[dict[str, Any]]) -> ScriptedModel:
    """A ScriptedModel whose steps fill "$" references from the conversation so far."""

    def make(step: dict[str, Any], number: int):
        def turn(messages: list[Message], tools) -> ModelTurn | dict:
            outputs = _successful_outputs(messages)
            if "raw" in step:
                return step["raw"]
            if "tools" in step:
                calls = [
                    ToolCall(id=f"t{number}c{k}", name=c["name"],
                             arguments=_fill(c.get("arguments", {}), outputs))
                    for k, c in enumerate(step["tools"])
                ]  # fmt: skip
                return ModelTurn(tool_calls=calls)
            return ModelTurn(answer=_fill(step["answer"], outputs))

        return turn

    return ScriptedModel([make(step, n) for n, step in enumerate(script, start=1)])


def _successful_outputs(messages: list[Message]) -> dict[str, list[dict]]:
    names = {c.id: c.name for m in messages for c in m.tool_calls}
    outputs: dict[str, list[dict]] = {}
    for message in messages:
        if message.role == "tool":
            body = json.loads(message.content)
            if body["ok"]:
                outputs.setdefault(names[message.tool_call_id], []).append(body["output"])
    return outputs


def _fill(node: Any, outputs: dict[str, list[dict]]) -> Any:
    if isinstance(node, dict):
        if isinstance(node.get("values"), str) and node["values"].startswith("$values:"):
            cmp = _ref(outputs, "compare", node["values"].split(":")[1])["comparison_id"]
            refs = [("current", "current"), ("prior", "prior"), ("delta", "delta"),
                    ("growth", "percent")]  # fmt: skip
            node = node | {"values": [{"placeholder": p, "value_id": f"{cmp}/{part}"}
                                      for p, part in refs]}  # fmt: skip
        return {key: _fill(value, outputs) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill(value, outputs) for value in node]
    if not (isinstance(node, str) and node.startswith("$")):
        return node
    kind, *args = node[1:].split(":")
    if kind == "cik":
        return _ref(outputs, "resolve_company", "-1")["cik"]
    if kind == "cmp":
        return _ref(outputs, "compare", args[0])["comparison_id"]
    if kind == "value":
        return f"{_ref(outputs, 'compare', args[0])['comparison_id']}/{args[1]}"
    if kind == "fact":
        return _ref(outputs, "get_fact", args[0])["value_id"]
    if kind in ("cite", "quote", "alter"):
        evidence = _ref(outputs, "search_commentary", args[0])["evidence"]
        if int(args[1]) >= len(evidence):
            raise ScriptError(f"{node}: search returned {len(evidence)} passages")
        passage = evidence[int(args[1])]
        if kind == "cite":
            return passage["citation_id"]
        excerpt = _excerpt(passage["text"])
        return excerpt if kind == "quote" else excerpt.replace(" ", " not ", 1)
    raise ScriptError(f"unknown reference {node}")


def _ref(outputs: dict[str, list[dict]], tool: str, index: str) -> dict:
    found = outputs.get(tool, [])
    if not found or not -len(found) <= int(index) < len(found):
        raise ScriptError(f"no successful {tool} result {index}")
    return found[int(index)]


def _excerpt(text: str) -> str:
    """The longest paragraph of the passage that fits in a quotation, verbatim."""
    paragraphs = [p for p in text.split("\n\n") if 40 <= len(p) <= 300 and " " in p]
    if paragraphs:
        return max(paragraphs, key=len)
    return text[:200].rsplit(" ", 1)[0]


# --- Scoring --------------------------------------------------------------------------------


def run_case(
    case: EvalCase, data: PinnedData, cache_dir: Path
) -> tuple[AgentRunResult, GroundedAnswer]:
    run = run_agent(case.question, scripted_model(case.script), data=data, cache_dir=cache_dir)
    return run, ground(run, data=data, cache_dir=cache_dir)


def score_case(case: EvalCase, data: PinnedData, cache_dir: Path) -> CaseResult:
    run, answer = run_case(case, data, cache_dir)
    again_run, again_answer = run_case(case, data, cache_dir)
    checks, failures = check_case(case, run, answer)
    repeat = (run.model_dump(), answer.model_dump()) == (again_run.model_dump(),
                                                         again_answer.model_dump())  # fmt: skip
    checks["deterministic"] = repeat
    if not repeat:
        failures.append("deterministic: a second run differed")
    return CaseResult(
        id=case.id, category=case.category, status=answer.status,
        expected_status=case.expected.status, checks=checks, failures=failures,
    )  # fmt: skip


def check_case(
    case: EvalCase, run: AgentRunResult, answer: GroundedAnswer
) -> tuple[dict[str, bool], list[str]]:
    """Compare one run and its grounded answer with the case's labels. Shared by the
    scripted evaluation and the live evaluation (llm_provider / live_agent)."""
    expected = case.expected
    failures: list[str] = []
    checks: dict[str, bool] = {}

    def check(name: str, passed: bool, why: str) -> None:
        checks[name] = checks.get(name, True) and passed
        if not passed:
            failures.append(f"{name}: {why}")

    got_status, want_status = (run.status, answer.status), (expected.run_status, expected.status)
    check("status", got_status == want_status, f"{got_status}, expected {want_status}")
    if expected.status != "complete":
        check("partial_abstained", answer.status == expected.status, answer.status)
    if expected.current is not None:
        got = (answer.current, answer.prior)
        values_ok = all(got) and (
            str(answer.current.value),
            str(answer.prior.value),
            str(answer.delta),
            answer.percent.display,
        ) == (expected.current.value, expected.prior.value, expected.delta, expected.percent)
        check("numeric", values_ok, f"rendered {answer.statement}")  # fmt: skip
        context_ok = all(got) and (answer.cik, answer.metric) == (expected.cik, expected.metric)
        for rendered, label in ((answer.current, expected.current),
                                (answer.prior, expected.prior)):  # fmt: skip
            context_ok = context_ok and _period_of(run, rendered) == (label.start, label.end)
        check("company_period", context_ok, f"{answer.company} {answer.metric}")
    if answer.citations or expected.commentary_accession:
        accession_ok = bool(answer.citations) and all(
            c.accession_number == expected.commentary_accession
            and c.citation_id in run.evidence.passages
            for c in answer.citations
        )
        check("citation_provenance", accession_ok,
              f"{[c.accession_number for c in answer.citations]}")  # fmt: skip
    if expected.quotations is not None:
        check("citation_provenance", len(answer.quotations) == expected.quotations,
              f"{len(answer.quotations)} quotations")  # fmt: skip
    if expected.issue_codes or expected.forbidden_text or expected.tool_error_codes:
        codes = {i.code for i in answer.issues}
        tool_codes = {r.error.code for r in run.tool_results if r.error}
        # Published claims: everything except the audit fields and the echoed question.
        published = answer.model_dump_json(exclude={"issues", "withheld", "question"})
        missing = [c for c in expected.issue_codes if c not in codes]
        missing += [c for c in expected.tool_error_codes if c not in tool_codes]
        leaked = [t for t in expected.forbidden_text if t in published]
        check("unsupported_rejected", not missing and not leaked,
              f"missing {missing}, leaked {leaked}")  # fmt: skip
    check("budget", len(run.tool_results) <= MAX_TOOL_CALLS and run.model_turns <= MAX_MODEL_TURNS,
          f"{len(run.tool_results)} calls, {run.model_turns} turns")  # fmt: skip
    return checks, failures


def _period_of(run, rendered) -> tuple:
    fact = next(c for c in run.evidence.comparisons.values()
                if rendered.value_id.startswith(c.comparison_id))  # fmt: skip
    side = fact.current if rendered.value_id.endswith("/current") else fact.prior
    return side.period_start, side.period_end


METRICS = ("status", "numeric", "company_period", "citation_provenance",
           "unsupported_rejected", "partial_abstained", "budget", "deterministic")  # fmt: skip


def evaluate(dataset: AgentEvalDataset, data: PinnedData, cache_dir: Path) -> AgentEvalReport:
    results = [score_case(case, data, cache_dir) for case in dataset.cases]
    metrics = {
        name: (sum(r.checks[name] for r in results if name in r.checks),
               sum(name in r.checks for r in results))
        for name in METRICS
    }  # fmt: skip
    return AgentEvalReport(dataset_version=dataset.dataset_version, cases=len(results),
                           metrics=metrics, results=results)  # fmt: skip


def load_dataset(path: Path) -> AgentEvalDataset:
    dataset = AgentEvalDataset.model_validate_json(path.read_text(encoding="utf-8"))
    ids = [c.id for c in dataset.cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    return dataset


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.agent_eval [--dataset FILE] [--output FILE]"""
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.agent_eval")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    parser.add_argument("--output", type=Path, help="write the full JSON report here")
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset)
        report = evaluate(dataset, load_pinned_data(args.manifest, args.cache_dir), args.cache_dir)
    except Exception as exc:  # noqa: BLE001 - report the problem and stop
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for result in report.results:
        mark = "ok  " if not result.failures else "FAIL"
        print(f"{mark} {result.id:<34} {result.category:<12} {result.status}")
        for failure in result.failures:
            print(f"       {failure}")
    for name, (passed, total) in report.metrics.items():
        print(f"{name:<22} {passed}/{total}")
    print(report.note)
    if args.output:
        args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0 if all(not r.failures for r in report.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

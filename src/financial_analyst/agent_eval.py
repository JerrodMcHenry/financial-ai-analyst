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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from financial_analyst.financial_tools import PeriodRequest
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
    category: str
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


# --- Outcome-based evaluation v2 (M3.7) ---------------------------------------------------------
#
# agent_v1.json stays frozen. agent_v2.json labels the information need, not a model's path:
# whether the question is answerable from the pinned data, its scope and figures, whether it
# asks for commentary or a quotation, and which published statuses meet the need. Each
# dimension is scored on its own; no internal tool-error path is required.

DEFAULT_DATASET_V2 = Path("evals/agent_v2.json")
CategoryV2 = Literal["figures", "paraphrase", "commentary", "unsupported", "unanswerable",
                     "ambiguous", "injection"]  # fmt: skip
Status = Literal["complete", "partial", "abstained"]


class ScopeLabel(_Strict):
    cik: str
    metric: str
    current: PeriodRequest
    prior: PeriodRequest


class FiguresLabel(_Strict):
    current: ExpectedValue
    prior: ExpectedValue
    delta: str
    percent: str  # signed display, e.g. "+15.1%"


class ExpectedV2(_Strict):
    answerable: bool  # the pinned data can answer the question as asked
    scope: ScopeLabel | None = None
    figures: FiguresLabel | None = None
    requires_commentary: bool = False
    requires_quotation: bool = False
    acceptable_statuses: list[Status] = Field(min_length=1)
    commentary_accession: str | None = None  # where any published citation must come from
    forbidden_text: list[str] = []

    @model_validator(mode="after")
    def _consistent(self) -> "ExpectedV2":
        if self.answerable != (self.scope is not None and self.figures is not None):
            raise ValueError("answerable cases need scope and figures; others have neither")
        if not self.answerable and self.acceptable_statuses != ["abstained"]:
            raise ValueError("an unanswerable case is met only by abstaining")
        if self.answerable and "abstained" in self.acceptable_statuses:
            raise ValueError("abstaining never meets an answerable question")
        return self


class EvalCaseV2(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    category: CategoryV2
    question: str
    script: list[dict[str, Any]] = Field(min_length=1)  # offline only; ignored live
    expected: ExpectedV2
    note: str = ""


class AgentEvalDatasetV2(_Strict):
    spec: Literal["agent_v2"]
    dataset_version: str
    description: str
    cases: list[EvalCaseV2] = Field(min_length=1)


METRICS_V2 = ("question_answered", "numeric", "context", "citation_integrity", "abstention",
              "status", "reliability", "budget", "safe_content", "deterministic")  # fmt: skip


def score_v2(
    case: EvalCaseV2, run: AgentRunResult, answer: GroundedAnswer
) -> tuple[dict[str, bool], list[str]]:
    """Score one run against the information need, one dimension at a time."""
    expected = case.expected
    checks: dict[str, bool] = {}
    failures: list[str] = []

    def check(name: str, passed: bool, why: str) -> None:
        checks[name] = passed
        if not passed:
            failures.append(f"{name}: {why}")

    published = answer.current is not None
    check("status", answer.status in expected.acceptable_statuses,
          f"{answer.status}, acceptable {expected.acceptable_statuses}")  # fmt: skip
    check("abstention", (answer.status == "abstained") == (not expected.answerable),
          f"{answer.status} for {'an answerable' if expected.answerable else 'an unanswerable'} "
          "question")  # fmt: skip
    check("reliability", run.status == "drafted", f"run ended {run.status}")
    check("budget", len(run.tool_results) <= MAX_TOOL_CALLS and run.model_turns <= MAX_MODEL_TURNS,
          f"{len(run.tool_results)} calls, {run.model_turns} turns")  # fmt: skip
    if expected.answerable:
        in_scope = published and _scope_matches(run, answer, expected.scope)
        where = "for the asked scope" if in_scope else "missing or for another scope"
        check("question_answered", in_scope and answer.status in expected.acceptable_statuses,
              f"{answer.status}; figures {where}")  # fmt: skip
    if published and expected.figures is not None:
        f = expected.figures
        got = (str(answer.current.value), str(answer.prior.value), str(answer.delta),
               answer.percent.display)  # fmt: skip
        want = (f.current.value, f.prior.value, f.delta, f.percent)
        check("numeric", got == want, f"{got}, expected {want}")
        sides = ((answer.current, f.current), (answer.prior, f.prior))
        context = _scope_matches(run, answer, expected.scope) and all(
            _period_of(run, rendered) == (label.start, label.end) for rendered, label in sides
        )
        check("context", context, f"{answer.company} {answer.metric}")
    if answer.citations or answer.quotations:
        check("citation_integrity", _citations_intact(run, answer, expected.commentary_accession),
              f"{[c.accession_number for c in answer.citations]}, "
              f"{len(answer.quotations)} quotation(s)")  # fmt: skip
    if expected.forbidden_text:
        text = answer.model_dump_json(exclude={"issues", "withheld", "question"})
        leaked = [t for t in expected.forbidden_text if t in text]
        check("safe_content", not leaked, f"published {leaked}")
    return checks, failures


def _scope_matches(run: AgentRunResult, answer: GroundedAnswer, scope: ScopeLabel | None) -> bool:
    if scope is None or answer.provenance is None:
        return False
    comparison = run.evidence.comparisons[answer.provenance.comparison_id]
    return (comparison.cik, comparison.metric, comparison.current.period.request,
            comparison.prior.period.request) == (scope.cik, scope.metric, scope.current,
                                                 scope.prior)  # fmt: skip


def _citations_intact(run: AgentRunResult, answer: GroundedAnswer, accession: str | None) -> bool:
    """Every published citation is a passage this run returned, from the expected filing, and
    every published quotation is an exact substring of its cited passage."""
    passages = run.evidence.passages
    for citation in answer.citations:
        if citation.citation_id not in passages:
            return False
        if accession is not None and citation.accession_number != accession:
            return False
    return all(
        q.citation_id in passages and q.text in passages[q.citation_id].passage.text
        for q in answer.quotations
    )


def score_case_v2(case: EvalCaseV2, data: PinnedData, cache_dir: Path) -> CaseResult:
    runs = [_run_v2(case, data, cache_dir) for _ in range(2)]
    (run, answer), (again_run, again_answer) = runs
    checks, failures = score_v2(case, run, answer)
    checks["deterministic"] = (run.model_dump(), answer.model_dump()) == (
        again_run.model_dump(), again_answer.model_dump()
    )  # fmt: skip
    if not checks["deterministic"]:
        failures.append("deterministic: a second run differed")
    return CaseResult(
        id=case.id, category=case.category, status=answer.status,
        expected_status="/".join(case.expected.acceptable_statuses), checks=checks,
        failures=failures,
    )  # fmt: skip


def _run_v2(case: EvalCaseV2, data: PinnedData, cache_dir: Path):
    run = run_agent(case.question, scripted_model(case.script), data=data, cache_dir=cache_dir)
    return run, ground(run, data=data, cache_dir=cache_dir)


def evaluate_v2(dataset: AgentEvalDatasetV2, data: PinnedData, cache_dir: Path) -> AgentEvalReport:
    results = [score_case_v2(case, data, cache_dir) for case in dataset.cases]
    return AgentEvalReport(dataset_version=dataset.dataset_version, cases=len(results),
                           metrics=rates(results, METRICS_V2), results=results)  # fmt: skip


def rates(results, names) -> dict[str, tuple[int, int]]:
    return {
        name: (sum(r.checks[name] for r in results if name in r.checks),
               sum(name in r.checks for r in results))
        for name in names
    }  # fmt: skip


def load_dataset_v2(path: Path) -> AgentEvalDatasetV2:
    dataset = AgentEvalDatasetV2.model_validate_json(path.read_text(encoding="utf-8"))
    ids = [c.id for c in dataset.cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    return dataset


def is_v2(path: Path) -> bool:
    return json.loads(path.read_text(encoding="utf-8")).get("spec") == "agent_v2"


def load_dataset(path: Path) -> AgentEvalDataset:
    dataset = AgentEvalDataset.model_validate_json(path.read_text(encoding="utf-8"))
    ids = [c.id for c in dataset.cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    return dataset


def main(argv: list[str] | None = None) -> int:
    """python -m financial_analyst.agent_eval [--dataset FILE] [--output FILE]

    The dataset may be agent_v1.json (frozen, path-specific labels) or agent_v2.json
    (outcome-based labels), recognized by its "spec" field."""
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.agent_eval")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    parser.add_argument("--output", type=Path, help="write the full JSON report here")
    args = parser.parse_args(argv)
    try:
        data = load_pinned_data(args.manifest, args.cache_dir)
        if is_v2(args.dataset):
            report = evaluate_v2(load_dataset_v2(args.dataset), data, args.cache_dir)
        else:
            report = evaluate(load_dataset(args.dataset), data, args.cache_dir)
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

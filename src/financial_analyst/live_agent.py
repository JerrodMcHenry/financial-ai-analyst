"""Live-model command line (M3.6 B1): ask one question, or run the live evaluation.

Every command here sends requests to the configured LLM provider and may cost money.
Nothing in the automated test suite runs it against a real provider.

    python -m financial_analyst.live_agent ask "How much did Apple's revenue grow in FY2026?"
    python -m financial_analyst.live_agent eval            # the two smoke cases only
    python -m financial_analyst.live_agent eval --cases ID,ID
    python -m financial_analyst.live_agent eval --all      # every live-eligible case

The flow is the offline one with a live model: the pinned snapshots and cached filings,
run_agent (same tools, limits and repair), then ground. Only the server-rendered answer is
printed; the model's draft never is.
"""

import argparse
import statistics
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import BaseModel

from financial_analyst.agent import AgentRunResult, run_agent
from financial_analyst.agent_eval import DEFAULT_DATASET, EvalCase, check_case, load_dataset
from financial_analyst.analysis import PinnedData, load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.grounding import GroundedAnswer, ground
from financial_analyst.llm_provider import (
    OpenAICompatibleModel,
    ProviderConfigError,
    ProviderSettings,
    Usage,
    load_provider_settings,
)

DEFAULT_MANIFEST = Path("evals/sec_snapshots_v1.json")
SMOKE_CASES = ("apple-fy2026-revenue", "jpm-q3-2026-net-income")
# Adversarial cases script a misbehaving model; only the one whose attack is in the question
# itself means anything for a live model.
QUESTION_DRIVEN_ADVERSARIAL = frozenset({"prompt-injection-in-question"})
_MODEL_ERRORS = frozenset({"unknown_tool", "invalid_arguments"})


class LiveCaseResult(BaseModel):
    id: str
    run_status: str
    status: str
    expected_status: str
    valid_tool_sequence: bool
    grounding_pass: bool | None  # None when there was no draft to ground
    checks: dict[str, bool]
    failures: list[str]
    tool_calls: int
    model_turns: int
    usage: Usage
    error: str | None


class LiveEvalReport(BaseModel):
    model: str
    attempted: int
    metrics: dict[str, tuple[int, int]]  # name -> (passed, applicable)
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: Decimal | None
    latency_seconds_total: float
    latency_seconds_median: float
    latency_seconds_max: float
    results: list[LiveCaseResult]
    note: str = (
        "Live-model evaluation of the configured provider on these cases. Labels were written "
        "for scripted runs; a live model may legitimately take another valid path (for "
        "example, skipping commentary), which shows up as a status difference."
    )


def live_cases(cases: list[EvalCase], ids: list[str] | None, run_all: bool) -> list[EvalCase]:
    eligible = [c for c in cases
                if c.category != "adversarial" or c.id in QUESTION_DRIVEN_ADVERSARIAL]  # fmt: skip
    if run_all:
        return eligible
    wanted = list(ids or SMOKE_CASES)
    known = {c.id: c for c in eligible}
    unknown = [i for i in wanted if i not in known]
    if unknown:
        raise ValueError(f"not live-eligible case ids: {unknown}")
    return [known[i] for i in wanted]


def evaluate_live(
    cases: list[EvalCase],
    data: PinnedData,
    cache_dir: Path,
    model_factory: Callable[[], OpenAICompatibleModel],
    model_name: str,
) -> LiveEvalReport:
    """Run each case once with a fresh live model and score it with the scripted
    evaluation's checks (no determinism check: live output varies)."""
    results = []
    for case in cases:
        with model_factory() as model:
            run = run_agent(case.question, model, data=data, cache_dir=cache_dir)
            answer = ground(run, data=data, cache_dir=cache_dir)
            usage = model.usage()
        checks, failures = check_case(case, run, answer)
        results.append(LiveCaseResult(
            id=case.id, run_status=run.status, status=answer.status,
            expected_status=case.expected.status, valid_tool_sequence=_valid_sequence(run),
            grounding_pass=None if run.status != "drafted" else not answer.issues,
            checks=checks, failures=failures, tool_calls=len(run.tool_results),
            model_turns=run.model_turns, usage=usage, error=run.error,
        ))  # fmt: skip

    def rate(name: str) -> tuple[int, int]:
        applicable = [r.checks[name] for r in results if name in r.checks]
        return sum(applicable), len(applicable)

    figures = [r for r in results if "numeric" in r.checks]
    drafted = [r for r in results if r.grounding_pass is not None]
    latencies = [r.usage.latency_seconds for r in results] or [0.0]
    costs = [r.usage.estimated_cost_usd for r in results]
    return LiveEvalReport(
        model=model_name,
        attempted=len(results),
        metrics={
            "valid_tool_sequence": (sum(r.valid_tool_sequence for r in results), len(results)),
            "draft_success": (len(drafted), len(results)),
            "grounding_pass": (sum(bool(r.grounding_pass) for r in drafted), len(drafted)),
            "financial_correct": (
                sum(r.checks["numeric"] and r.checks["company_period"] for r in figures),
                len(figures),
            ),  # fmt: skip
            "status": rate("status"),
            "partial_abstained": rate("partial_abstained"),
            "unsupported_rejected": rate("unsupported_rejected"),
            "budget": rate("budget"),
        },
        prompt_tokens=sum(r.usage.prompt_tokens for r in results),
        completion_tokens=sum(r.usage.completion_tokens for r in results),
        estimated_cost_usd=None if None in costs or not costs else sum(costs, Decimal(0)),
        latency_seconds_total=sum(latencies),
        latency_seconds_median=statistics.median(latencies),
        latency_seconds_max=max(latencies),
        results=results,
    )


def _valid_sequence(run: AgentRunResult) -> bool:
    """Every call named a real tool with valid arguments, and the run stayed in budget."""
    malformed = any(r.error and r.error.code in _MODEL_ERRORS for r in run.tool_results)
    return not malformed and run.status not in ("tool_call_limit", "turn_limit")


# --- Output ------------------------------------------------------------------------------------


def format_answer(answer: GroundedAnswer) -> str:
    """The server-rendered answer as text. Never includes the model's draft or wording."""
    lines = [f"Status: {answer.status.upper()}"]
    if answer.statement:
        p = answer.provenance
        lines += [
            f"Company: {answer.company}",
            f"Metric:  {answer.statement_line} ({p.concept}, {p.unit})",
            f"Current: {answer.current.period}: {answer.current.formatted}",
            f"Prior:   {answer.prior.period}: {answer.prior.formatted}",
            f"Change:  {answer.delta_formatted} ({answer.percent.display or answer.percent.note})",
            "",
            answer.statement,
            "",
            f"Source:  {p.commentary_filing} {p.commentary_filing_url}",
            f"         {p.comparison_id}; Company Facts snapshot {p.company_facts_sha256[:16]}",
        ]
    for quote in answer.quotations:
        lines.append(f'Quote:   "{quote.text}" ({quote.attribution})')
    if answer.citations:
        lines.append("Candidate commentary (verified source text; not established as a cause):")
    for citation in answer.citations:
        lines += [f"  - {citation.section or 'unlabeled'}: {citation.preview}",
                  f"    {citation.citation_id}"]  # fmt: skip
    lines += [f"Note:    {warning}" for warning in answer.warnings]
    lines += [f"Issue:   {issue.code}: {issue.detail}" for issue in answer.issues]
    lines.append(f"Run:     {answer.run_status}, {answer.tool_calls} tool calls, "
                 f"{answer.model_turns} model turns")  # fmt: skip
    return "\n".join(lines)


def format_usage(usage: Usage) -> str:
    cost = (
        "not estimated (no prices configured)"
        if usage.estimated_cost_usd is None
        else (f"~${usage.estimated_cost_usd:.4f}")
    )
    return (f"Usage:   {usage.requests} requests, {usage.prompt_tokens} prompt + "
            f"{usage.completion_tokens} completion tokens, {usage.latency_seconds:.1f} s, "
            f"cost {cost}")  # fmt: skip


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m financial_analyst.live_agent")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir", type=Path, default=Settings.model_fields["filing_cache_dir"].default
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="answer one question with the live model")
    ask.add_argument("question")
    evaluation = commands.add_parser("eval", help="live evaluation (smoke cases by default)")
    evaluation.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    choice = evaluation.add_mutually_exclusive_group()
    choice.add_argument("--cases", help="comma-separated case ids")
    choice.add_argument("--all", action="store_true", help="every live-eligible case")
    evaluation.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        settings = load_provider_settings()
    except ProviderConfigError as exc:
        print(f"{exc}. Set them in the environment; no request was sent.", file=sys.stderr)
        return 2
    try:
        data = load_pinned_data(args.manifest, args.cache_dir)
        cases = None
        if args.command == "eval":
            ids = args.cases.split(",") if args.cases else None
            cases = live_cases(load_dataset(args.dataset).cases, ids, args.all)
    except Exception as exc:  # noqa: BLE001 - report the problem and stop
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    def new_model() -> OpenAICompatibleModel:
        return OpenAICompatibleModel(settings, transport=transport)

    print(f"Live provider: model {settings.model} at {settings.base_url}", file=sys.stderr)
    if args.command == "ask":
        try:
            with new_model() as model:
                run = run_agent(args.question, model, data=data, cache_dir=args.cache_dir)
                usage = model.usage()
        except ValueError as exc:  # a blank or overlong question
            print(f"error: {exc}", file=sys.stderr)
            return 2
        answer = ground(run, data=data, cache_dir=args.cache_dir)
        print(format_answer(answer))
        print(format_usage(usage))
        return 0 if answer.status != "abstained" else 1

    report = evaluate_live(cases, data, args.cache_dir, new_model, settings.model)
    _print_report(report, settings)
    if args.output:
        args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return 0


def _print_report(report: LiveEvalReport, settings: ProviderSettings) -> None:
    for result in report.results:
        print(f"{result.id:<34} {result.run_status:<16} {result.status:<10} "
              f"(expected {result.expected_status}) {result.tool_calls} calls, "
              f"{result.model_turns} turns, {result.usage.latency_seconds:.1f} s")  # fmt: skip
        for failure in result.failures:
            print(f"    {failure}")
    print(f"attempted              {report.attempted}")
    for name, (passed, total) in report.metrics.items():
        print(f"{name:<22} {passed}/{total}")
    cost = (
        "not estimated"
        if report.estimated_cost_usd is None
        else (f"~${report.estimated_cost_usd:.4f}")
    )
    print(f"tokens                 {report.prompt_tokens} prompt + "
          f"{report.completion_tokens} completion; cost {cost}")  # fmt: skip
    total, median = report.latency_seconds_total, report.latency_seconds_median
    print(f"latency                total {total:.1f} s, median {median:.1f} s, "
          f"max {report.latency_seconds_max:.1f} s")  # fmt: skip
    print(report.note)


if __name__ == "__main__":
    raise SystemExit(main())

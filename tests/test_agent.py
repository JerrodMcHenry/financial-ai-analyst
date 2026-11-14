"""Tool-calling orchestration with a scripted fake model. Offline, sockets blocked.

The scripted model reads tool results from the conversation, exactly as a live model would,
so every number in a draft comes from a tool message, never from the script.
Pinned tests are skipped when the local (git-ignored) cache is not present.
"""

import json
import socket
from pathlib import Path

import httpx
import pytest

from financial_analyst import agent
from financial_analyst.agent import (
    MAX_MODEL_TURNS,
    MAX_TOOL_CALLS,
    TOOL_SPECS,
    AnswerDraft,
    ModelTurn,
    ScriptedModel,
    ToolCall,
    run_agent,
)
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.grounding import ground

APPLE, JPM = "0000320193", "0000019617"
APPLE_10K, JPM_10Q = "0000320193-26-000026", "0001628280-26-069523"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
MANIFEST = Path(__file__).parent.parent / "evals" / "sec_snapshots_v1.json"
FY2026 = {"kind": "fiscal_year", "fiscal_year": 2026}
FY2025 = {"kind": "fiscal_year", "fiscal_year": 2025}
Q3_2026 = {"kind": "fiscal_quarter", "fiscal_year": 2026, "quarter": 3}
Q3_2025 = {"kind": "fiscal_quarter", "fiscal_year": 2025, "quarter": 3}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)


def _pinned():
    try:
        data = load_pinned_data(MANIFEST, CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if (CACHE / "filings" / APPLE / "000032019326000026").exists() else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


# --- Script helpers: they only read what tools returned --------------------------------------


def call(call_id: str, name: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def tools(*calls: ToolCall) -> ModelTurn:
    return ModelTurn(tool_calls=list(calls))


def result(messages, call_id: str) -> dict:
    """The tool message answering `call_id`, parsed."""
    return json.loads(next(m.content for m in messages if m.tool_call_id == call_id))


def output(messages, call_id: str) -> dict:
    body = result(messages, call_id)
    assert body["ok"], body
    return body["output"]


def growth_answer(compare_id: str, search_id: str | None, subject: str):
    """A final step that drafts an answer from the compare (and search) tool outputs."""

    def step(messages, specs):
        comparison = output(messages, compare_id)
        draft = {
            "subject": comparison["comparison_id"],
            "answer": f"{subject} was {{current}} versus {{prior}}, a change of {{delta}} "
            "({growth}).",
            "values": [
                {"placeholder": "current", "value_id": comparison["current"]["value_id"]},
                {"placeholder": "prior", "value_id": comparison["prior"]["value_id"]},
                {"placeholder": "delta", "value_id": comparison["delta"]["value_id"]},
                {"placeholder": "growth", "value_id": comparison["percent"]["value_id"]},
            ],
        }
        if search_id:
            first = output(messages, search_id)["evidence"][0]
            excerpt = min(first["text"].split("\n\n"), key=lambda s: (len(s) > 300, -len(s)))
            draft["citations"] = [first["citation_id"]]
            draft["quotes"] = [{"citation_id": first["citation_id"], "text": excerpt}]
        return ModelTurn(answer=draft)

    return step


def apple_script() -> ScriptedModel:
    return ScriptedModel([
        tools(call("c1", "resolve_company", identifier="AAPL")),
        lambda m, t: tools(call("c2", "compare", cik=output(m, "c1")["cik"], metric="revenue",
                                current_period=FY2026, prior_period=FY2025)),
        lambda m, t: tools(
            call("c3", "commentary_filing", comparison_id=output(m, "c2")["comparison_id"]),
            call("c4", "search_commentary", comparison_id=output(m, "c2")["comparison_id"]),
        ),
        growth_answer("c2", "c4", "Apple's total net sales"),
    ])  # fmt: skip


APPLE_REVENUE_Q = "How much did Apple's revenue grow in fiscal 2026 compared with fiscal 2025?"


def run(model, data=None, question=APPLE_REVENUE_Q, **kwargs):
    return run_agent(question, model, data=data or DATA, cache_dir=CACHE, **kwargs)


PLAIN = ModelTurn(answer={"answer": "No figures."})


# --- Required scripted runs ---------------------------------------------------------------


@pinned
def test_apple_fy2026_revenue_growth_scripted_run():
    model = apple_script()
    outcome = run_agent("How much did Apple's revenue grow in fiscal 2026?", model,
                        data=DATA, cache_dir=CACHE)  # fmt: skip
    assert outcome.status == "drafted" and outcome.draft_label == "unvalidated_draft"
    assert outcome.notice.startswith("Unvalidated draft")
    assert [r.call.name for r in outcome.tool_results] == [
        "resolve_company", "compare", "commentary_filing", "search_commentary"
    ]  # fmt: skip
    assert all(r.ok for r in outcome.tool_results) and outcome.model_turns == 4
    comparison = outcome.tool_results[1].output
    assert (comparison["current"]["value"], comparison["prior"]["value"]) == (
        "478834000000", "416161000000"
    )  # fmt: skip
    assert comparison["delta"]["value"] == "62673000000"
    assert comparison["percent"]["display"] == "15.1%"
    assert comparison["percent"]["exact"] == "15.05979656911627951682161471161401"
    assert comparison["commentary_filing"] == APPLE_10K
    filing = outcome.tool_results[2].output
    assert filing["cached"] is True and filing["mda_section"] == "Item 7"
    search = outcome.tool_results[3].output
    assert search["section"] == "Item 7" and search["evidence"]
    assert all(f":{APPLE_10K}:" in e["citation_id"] for e in search["evidence"])
    # The draft refers to tool values by id; it states no number of its own.
    cid = comparison["comparison_id"]
    assert [v.value_id for v in outcome.draft.values] == [
        f"{cid}/current", f"{cid}/prior", f"{cid}/delta", f"{cid}/percent"
    ]  # fmt: skip
    assert not any(ch.isdigit() for ch in outcome.draft.answer)
    quote = outcome.draft.quotes[0]
    assert quote.citation_id == search["evidence"][0]["citation_id"]
    assert quote.text in search["evidence"][0]["text"]  # verbatim (checked here, not by B1)


@pinned
def test_jpmorgan_q3_2026_net_income_growth_scripted_run():
    model = ScriptedModel([
        tools(call("r", "resolve_company", identifier="JPMorgan Chase")),
        lambda m, t: tools(call("c", "compare", cik=output(m, "r")["cik"], metric="net_income",
                                current_period=Q3_2026, prior_period=Q3_2025)),
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        growth_answer("c", "s", "JPMorgan's net income"),
    ])  # fmt: skip
    outcome = run_agent("How did JPMorgan's Q3 2026 net income change?", model,
                        data=DATA, cache_dir=CACHE)  # fmt: skip
    assert outcome.status == "drafted" and len(outcome.tool_results) == 3
    comparison = outcome.tool_results[1].output
    assert comparison["current"]["value"] == "15305000000"
    assert comparison["prior"]["value"] == "14393000000"
    assert comparison["prior"]["period_basis"] == "comparative_in_filing"
    assert (comparison["delta"]["value"], comparison["percent"]["display"]) == ("912000000", "6.3%")
    search = outcome.tool_results[2].output
    assert search["section"] is None  # no reliable MD&A label: the whole verified 10-Q
    assert "reported net income of $15.3 billion, up 6%" in search["evidence"][0]["text"]
    assert all(f":{JPM_10Q}:" in e["citation_id"] for e in search["evidence"])


@pinned
def test_several_tools_in_one_turn_and_get_fact():
    model = ScriptedModel([
        tools(call("a", "get_fact", cik=APPLE, metric="net_income", period=FY2026),
              call("b", "get_fact", cik=APPLE, metric="total_assets",
                   period={"kind": "fiscal_year_end", "fiscal_year": 2026}, policy="original")),
        lambda m, t: ModelTurn(answer={
            "answer": "Net income {ni}; total assets {ta}.",
            "values": [{"placeholder": "ni", "value_id": output(m, "a")["value_id"]},
                       {"placeholder": "ta", "value_id": output(m, "b")["value_id"]}],
        }),
    ])  # fmt: skip
    outcome = run(model)
    assert outcome.status == "drafted" and outcome.model_turns == 2
    net_income, assets = (r.output for r in outcome.tool_results)
    assert assets["value"] == "404548000000" and assets["policy"] == "original"
    assert net_income["concept"] == "us-gaap:NetIncomeLoss"
    assert net_income["value_id"].startswith("obs:")


@pinned
def test_repeated_runs_are_identical():
    first, second = run(apple_script()), run(apple_script())
    assert first.model_dump() == second.model_dump()


@pinned
def test_the_model_sees_the_tools_and_the_conversation_but_no_data_sources():
    model = apple_script()
    run(model)
    first = model.received[0]
    assert [m.role for m in first] == ["system", "user"]
    assert [m.role for m in model.received[-1]].count("tool") == 4
    assert {s.name for s in TOOL_SPECS} == {
        "resolve_company", "get_fact", "compare", "commentary_filing", "search_commentary"
    }  # fmt: skip

    def keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from keys(value)
        elif isinstance(node, list):
            for value in node:
                yield from keys(value)

    names = {k.lower() for spec in TOOL_SPECS for k in keys(spec.parameters["properties"])}
    assert not {n for n in names if any(w in n for w in ("path", "url", "dir", "file"))}


# --- Tool errors are returned to the model --------------------------------------------------


def one_call_then_answer(name, arguments) -> ScriptedModel:
    return ScriptedModel([tools(ToolCall(id="x", name=name, arguments=arguments)), PLAIN])


@pinned
@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("delete_files", {}, "unknown_tool"),
        ("read_file", {"path": "/etc/passwd"}, "unknown_tool"),
        ("compare", [APPLE], "invalid_arguments"),  # not an object
        ("compare", {"cik": APPLE, "metric": "revenue", "current_period": FY2026},
         "invalid_arguments"),  # missing prior_period
        ("get_fact", {"cik": APPLE, "metric": "revenue", "period": FY2026,
                      "cache_dir": "/tmp"}, "invalid_arguments"),  # no extra arguments
        ("get_fact", {"cik": APPLE, "metric": "revenue",
                      "period": {"kind": "fiscal_year", "fiscal_year": "2026"}},
         "invalid_arguments"),  # strings are not coerced, also when nested
        ("get_fact", {"cik": APPLE, "metric": "ebitda", "period": FY2026}, "invalid_arguments"),
        ("get_fact", {"cik": "320193", "metric": "revenue", "period": FY2026},
         "invalid_arguments"),
        ("search_commentary", {"comparison_id": "cmp:" + "0" * 64, "query": "  "},
         "invalid_arguments"),
        ("resolve_company", {"identifier": "Microsoft Corp"}, "company_not_found"),
        ("resolve_company", {"identifier": "MSFT"}, "unsupported_company"),  # no pinned data
        ("get_fact", {"cik": "0000789019", "metric": "revenue", "period": FY2026},
         "unsupported_company"),
        ("get_fact", {"cik": APPLE, "metric": "revenue",
                      "period": {"kind": "fiscal_quarter", "fiscal_year": 2026, "quarter": 4}},
         "period_not_supported"),
        ("get_fact", {"cik": APPLE, "metric": "revenue",
                      "period": {"kind": "fiscal_year", "fiscal_year": 2040}},
         "period_not_found"),
        ("compare", {"cik": JPM, "metric": "revenue", "current_period": Q3_2026,
                     "prior_period": {"kind": "fiscal_quarter", "fiscal_year": 2025,
                                      "quarter": 2}}, "period_not_found"),
        ("commentary_filing", {"comparison_id": "cmp:" + "0" * 64}, "unknown_comparison"),
    ],
)  # fmt: skip
def test_rejected_tool_calls_are_reported_to_the_model(name, arguments, code):
    model = one_call_then_answer(name, arguments)
    outcome = run(model)
    assert outcome.status == "drafted"  # the run continues; the model saw the error
    [refused] = outcome.tool_results
    assert not refused.ok and refused.error.code == code and refused.output is None
    assert result(model.received[1], "x") == {"ok": False, "error": refused.error.model_dump()}


@pinned
def test_a_comparison_id_from_another_run_is_unknown():
    previous = run(apple_script()).tool_results[1].output["comparison_id"]
    outcome = run(one_call_then_answer("search_commentary", {"comparison_id": previous}))
    assert outcome.tool_results[0].error.code == "unknown_comparison"


@pinned
def test_a_failing_tool_is_reported_without_internal_details(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk detail /private/secret")

    monkeypatch.setattr(agent, "retrieve_commentary", broken)
    model = ScriptedModel([
        tools(call("c", "compare", cik=APPLE, metric="revenue", current_period=FY2026,
                   prior_period=FY2025)),
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        PLAIN,
    ])  # fmt: skip
    outcome = run(model)
    assert outcome.status == "drafted"
    error = outcome.tool_results[1].error
    assert error.code == "tool_failed" and error.message == "search_commentary failed: OSError"


# --- Model failures, limits and timeouts ------------------------------------------------------


def boom(messages, specs):
    raise RuntimeError("provider unavailable")


@pinned
@pytest.mark.parametrize(
    "steps",
    [
        [boom],
        [{"tool_calls": [], "answer": None}],  # neither tools nor an answer
        [{"tool_calls": [{"id": "a", "name": "compare", "arguments": {}}], "answer": {}}],
        [{"tool_calls": [{"id": "a", "name": "x", "arguments": {}},
                         {"id": "a", "name": "y", "arguments": {}}]}],  # duplicate ids
        [{"tool_calls": [{"id": "../../etc", "name": "x", "arguments": {}}]}],
        ["free text instead of a turn"],
        [],  # the script runs out
    ],
)  # fmt: skip
def test_model_failures_end_the_run(steps):
    outcome = run(ScriptedModel(steps))
    assert outcome.status == "model_error" and outcome.draft is None
    assert outcome.tool_results == [] and outcome.model_turns == 1


@pinned
def test_too_many_calls_in_one_turn_run_none_of_them():
    calls = [call(f"c{i}", "resolve_company", identifier="AAPL") for i in range(7)]
    outcome = run(ScriptedModel([tools(*calls), PLAIN]))
    assert outcome.status == "tool_call_limit" and outcome.tool_results == []


@pinned
def test_the_call_budget_spans_turns():
    four = [call(f"a{i}", "resolve_company", identifier="AAPL") for i in range(4)]
    three = [call(f"b{i}", "resolve_company", identifier="JPM") for i in range(3)]
    outcome = run(ScriptedModel([tools(*four), tools(*three), PLAIN]))
    assert outcome.status == "tool_call_limit" and len(outcome.tool_results) == 4
    assert MAX_TOOL_CALLS == 6


@pinned
def test_the_turn_limit_stops_a_model_that_never_answers():
    steps = [tools(call(f"c{i}", "resolve_company", identifier="AAPL")) for i in range(9)]
    model = ScriptedModel(steps)
    outcome = run(model)
    assert outcome.status == "turn_limit" and outcome.model_turns == MAX_MODEL_TURNS == 6
    assert len(model.received) == 6
    # Five turns ran their call; the sixth turn's request is refused, since no turn would
    # remain to read its result.
    assert [r.call.id for r in outcome.tool_results] == ["c0", "c1", "c2", "c3", "c4"]


@pinned
def test_an_invalid_draft_on_the_last_turn_gets_no_repair():
    steps = [tools(call(f"c{i}", "resolve_company", identifier="AAPL")) for i in range(5)]
    model = ScriptedModel([*steps, BAD, PLAIN])
    outcome = run(model)
    assert outcome.status == "invalid_draft" and not outcome.repair_attempted
    assert len(model.received) == 6


def compare_call(call_id, cik, current=FY2026, prior=FY2025):
    return call(call_id, "compare", cik=cik, metric="revenue", current_period=current,
                prior_period=prior)  # fmt: skip


@pinned
def test_a_five_turn_workflow_completes():
    model = ScriptedModel([
        tools(call("r", "resolve_company", identifier="Apple")),
        lambda m, t: tools(compare_call("c", output(m, "r")["cik"])),
        lambda m, t: tools(call("f", "commentary_filing",
                                comparison_id=output(m, "c")["comparison_id"])),
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        growth_answer("c", "s", "Apple's total net sales"),
    ])  # fmt: skip
    outcome = run(model)
    assert outcome.status == "drafted" and outcome.model_turns == 5
    assert ground(outcome, data=DATA, cache_dir=CACHE).status == "complete"


@pinned
def test_a_corrected_tool_error_fits_in_six_turns():
    q4 = {"kind": "fiscal_quarter", "fiscal_year": 2026, "quarter": 4}
    model = ScriptedModel([
        tools(call("r", "resolve_company", identifier="AAPL")),  # unnecessary, but harmless
        tools(compare_call("bad", APPLE, current=q4)),  # Q4 is not reported separately
        lambda m, t: tools(compare_call("c", APPLE)),  # the model corrects the period
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        lambda m, t: tools(call("f", "commentary_filing",
                                comparison_id=output(m, "c")["comparison_id"])),
        growth_answer("c", "s", "Apple's total net sales"),
    ])  # fmt: skip
    outcome = run(model)
    assert outcome.status == "drafted" and outcome.model_turns == 6
    assert [(r.call.id, r.error.code if r.error else None) for r in outcome.tool_results] == [
        ("r", None), ("bad", "period_not_supported"), ("c", None), ("s", None), ("f", None)
    ]  # fmt: skip
    answer = ground(outcome, data=DATA, cache_dir=CACHE)
    assert answer.status == "complete" and answer.current.formatted == "$478,834,000,000"


@pinned
def test_an_unnecessary_resolution_then_a_direct_comparison():
    model = ScriptedModel([
        tools(call("r", "resolve_company", identifier="AAPL")),
        tools(compare_call("c", APPLE)),  # the pinned CIK used directly
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        growth_answer("c", "s", "Apple's total net sales"),
    ])  # fmt: skip
    answer = ground(run(model), data=DATA, cache_dir=CACHE)
    assert answer.status == "complete" and answer.delta_formatted == "+$62,673,000,000"


@pinned
def test_the_prompt_lists_supported_companies_without_figures():
    model = ScriptedModel([PLAIN])
    run(model)
    prompt = model.received[0][0].content
    assert "Apple Inc.: CIK 0000320193" in prompt and "CIK 0000019617" in prompt
    assert "compare" in prompt and "search_commentary" in prompt and "6 turns" in prompt
    assert not any(figure in prompt for figure in ("478", "416", "15,305", "15.1"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pinned
def test_a_slow_model_turn_times_out_before_its_tools_run():
    clock = FakeClock()

    def slow(messages, specs):
        clock.now = 90.0  # the turn took the whole budget
        return tools(call("c", "resolve_company", identifier="AAPL"))

    outcome = run(ScriptedModel([slow, PLAIN]), clock=clock)
    assert outcome.status == "timeout" and outcome.tool_results == []


@pinned
def test_an_answer_that_arrives_after_the_deadline_is_not_drafted():
    clock = FakeClock()

    def slow_answer(messages, specs):
        clock.now = 91.0
        return PLAIN

    outcome = run(ScriptedModel([slow_answer]), clock=clock)
    assert outcome.status == "timeout" and outcome.draft is None


@pinned
def test_a_slow_tool_times_out_the_rest_of_the_turn(monkeypatch):
    clock = FakeClock()
    real = agent.execute_tool

    def slow_tool(tool_call, state):
        clock.now += 60.0
        return real(tool_call, state)

    monkeypatch.setattr(agent, "execute_tool", slow_tool)
    calls = [call(f"c{i}", "resolve_company", identifier="AAPL") for i in range(3)]
    outcome = run(ScriptedModel([tools(*calls), PLAIN]), clock=clock)
    assert outcome.status == "timeout" and len(outcome.tool_results) == 2


# --- Answer drafts and the single repair ------------------------------------------------------

BAD = ModelTurn(answer={"answer": "Revenue grew.", "confidence": 0.9})  # an unknown field


@pinned
def test_one_repair_can_fix_an_invalid_draft():
    model = ScriptedModel([BAD, PLAIN])
    outcome = run(model)
    assert outcome.status == "drafted" and outcome.repair_attempted
    assert outcome.draft == AnswerDraft(answer="No figures.")
    repair = model.received[1][-1]
    assert repair.role == "user" and "answer draft is invalid" in repair.content


@pinned
@pytest.mark.parametrize(
    "second",
    [BAD, tools(call("c", "resolve_company", identifier="AAPL"))],  # still bad; tools instead
)
def test_only_one_repair_is_attempted(second):
    model = ScriptedModel([BAD, second, PLAIN])
    outcome = run(model)
    assert outcome.status == "invalid_draft" and outcome.draft is None
    assert len(model.received) == 2 and outcome.tool_results == []


@pinned
@pytest.mark.parametrize(
    "answer",
    [
        "just prose",
        {"answer": ""},
        {"answer": "x", "confidence": 0.9},  # unknown field
        {"answer": "{a}", "values": [{"placeholder": "a", "value_id": "478834000000"}]},
        {"answer": "x", "quotes": [{"citation_id": "c", "text": "y" * 301}]},
        {"answer": "x", "citations": [f"c{i}" for i in range(6)]},
        {"answer": "x", "values": [{"value_id": "62673000000"}]},  # a number, not a value_id
    ],
)  # fmt: skip
def test_malformed_drafts_are_rejected_after_one_repair(answer):
    outcome = run(ScriptedModel([ModelTurn(answer=answer), ModelTurn(answer=answer)]))
    assert outcome.status == "invalid_draft" and outcome.repair_attempted


@pinned
def test_a_question_must_be_reasonable():
    for question in ("  ", "x" * 1001):
        with pytest.raises(ValueError):
            run_agent(question, ScriptedModel([PLAIN]), data=DATA, cache_dir=CACHE)


# --- Quotation attribution (M3.6 B2.3) --------------------------------------------------------

MDA_LABELS = {"Item 7", "Part I, Item 2"}


def quoting_answer(compare_id: str, search_id: str, attribution: str | None = None):
    """Quote the first passage. Without an explicit attribution, follow the prompt's rule:
    "management" only for a passage whose returned section is an MD&A label."""

    def step(messages, specs):
        comparison = output(messages, compare_id)
        passage = output(messages, search_id)["evidence"][0]
        chosen = attribution or ("management" if passage["section"] in MDA_LABELS else "filing")
        excerpt = min(passage["text"].split("\n\n"), key=lambda s: (len(s) > 300, -len(s)))
        return ModelTurn(answer={
            "subject": comparison["comparison_id"],
            "answer": "{company} {metric} changed by {delta}.",
            "values": [{"placeholder": "delta", "value_id": comparison["delta"]["value_id"]}],
            "citations": [passage["citation_id"]],
            "quotes": [{"citation_id": passage["citation_id"], "text": excerpt,
                        "attribution": chosen}],
        })  # fmt: skip

    return step


SCOPED_QUESTIONS = {
    APPLE: APPLE_REVENUE_Q,
    JPM: "How did JPMorgan's net income for the third quarter of 2026 compare with Q3 2025?",
}


def quoting_run(cik, metric, current, prior, attribution=None):
    model = ScriptedModel([
        tools(call("c", "compare", cik=cik, metric=metric, current_period=current,
                   prior_period=prior)),
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        quoting_answer("c", "s", attribution),
    ])  # fmt: skip
    return run(model, question=SCOPED_QUESTIONS[cik])


@pinned
def test_the_prompt_states_the_attribution_rule():
    model = ScriptedModel([PLAIN])
    run(model)
    prompt = model.received[0][0].content
    assert '"Item 7" in a 10-K' in prompt and '"Part I, Item 2" in a 10-Q' in prompt
    assert 'Otherwise use "filing"' in prompt
    assert "section is null is\n   never attributed to management" in prompt
    assert "Do not infer management from the company name" in prompt


@pinned
@pytest.mark.parametrize(
    ("cik", "metric", "current", "prior", "section", "attribution", "rendered"),
    [
        (APPLE, "revenue", FY2026, FY2025, "Item 7", "management",
         f"Apple Inc. management, 10-K {APPLE_10K}, Item 7 (MD&A)"),
        (JPM, "net_income", Q3_2026, Q3_2025, None, "filing",
         f"JPMORGAN CHASE & CO 10-Q {JPM_10Q}"),
    ],
)  # fmt: skip
def test_attribution_follows_the_returned_section(cik, metric, current, prior, section,
                                                  attribution, rendered):  # fmt: skip
    outcome = quoting_run(cik, metric, current, prior)
    passage = outcome.tool_results[1].output["evidence"][0]
    assert passage["section"] == section  # Apple: labeled MD&A; JPMorgan: sectionless
    assert outcome.draft.quotes[0].attribution == attribution
    answer = ground(outcome, data=DATA, cache_dir=CACHE)
    assert answer.status == "complete" and answer.issues == []
    assert [q.attribution for q in answer.quotations] == [rendered]


@pinned
def test_management_attribution_of_a_sectionless_passage_is_still_rejected():
    # The live JPMorgan failure (M3.6 B2.2), reproduced: the grounding rule is unchanged.
    outcome = quoting_run(JPM, "net_income", Q3_2026, Q3_2025, attribution="management")
    answer = ground(outcome, data=DATA, cache_dir=CACHE)
    assert answer.status == "partial" and [i.code for i in answer.issues] == [
        "attribution_unsupported"
    ]
    assert answer.quotations == [] and answer.citations  # the passage remains a candidate


# --- Citations the run never returned (M3.7) ---------------------------------------------------


def citing_answer(citation: str | None):
    """Answer citing `citation`, or the first returned passage when None."""

    def step(messages, specs):
        comparison = output(messages, "c")
        cited = citation or output(messages, "s")["evidence"][0]["citation_id"]
        return ModelTurn(answer={
            "subject": comparison["comparison_id"], "answer": "{company} {metric}: {delta}.",
            "values": [{"placeholder": "delta", "value_id": comparison["delta"]["value_id"]}],
            "citations": [cited],
        })  # fmt: skip

    return step


def search_then(*answers):
    return ScriptedModel([
        tools(compare_call("c", APPLE)),
        lambda m, t: tools(call("s", "search_commentary",
                                comparison_id=output(m, "c")["comparison_id"])),
        *answers,
    ])  # fmt: skip


FORGED = "sec-psg:0000320193:0000320193-26-000026:aapl-20260926.htm:forged"


@pinned
def test_an_unreturned_citation_gets_one_repair_listing_the_returned_ids():
    model = search_then(citing_answer(FORGED), citing_answer(None))
    outcome = run(model, question="Why did Apple's revenue grow in fiscal 2026?")
    assert outcome.status == "drafted" and outcome.repair_attempted
    assert outcome.draft_rejections == [["citation_not_returned"]]
    feedback = model.received[3][-1].content
    returned = outcome.tool_results[1].output["evidence"]
    assert all(e["citation_id"] in feedback for e in returned)  # the ids that may be cited
    assert FORGED not in feedback  # the unreturned id is not echoed back
    assert ground(outcome, data=DATA, cache_dir=CACHE).status == "complete"


@pinned
def test_an_unreturned_citation_after_the_repair_is_rejected_by_grounding():
    model = search_then(citing_answer(FORGED), citing_answer(FORGED))
    outcome = run(model, question="Why did Apple's revenue grow in fiscal 2026?")
    assert outcome.status == "drafted"  # no second repair: the draft stands
    answer = ground(outcome, data=DATA, cache_dir=CACHE)
    assert answer.status == "partial" and [i.code for i in answer.issues] == ["citation_not_in_run"]
    assert answer.citations == [] and FORGED not in answer.model_dump_json(exclude={"issues"})


@pinned
def test_the_prompt_asks_for_exact_citation_ids_and_commentary_only_when_asked():
    model = ScriptedModel([PLAIN])
    run(model)
    prompt = model.received[0][0].content
    assert "copied exactly, character for character" in " ".join(prompt.split())
    assert "A question that only asks for\n   the figures needs no search" in prompt

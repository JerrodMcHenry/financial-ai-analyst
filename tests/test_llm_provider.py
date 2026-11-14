"""The OpenAI-compatible adapter and the live CLI, against a mocked provider only.

Every request goes to an httpx.MockTransport; sockets are blocked, so no test can reach a
real provider. The fake API key below must never appear in any output.
"""

import json
import socket
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financial_analyst import live_agent
from financial_analyst.agent import (
    MAX_MODEL_TURNS,
    TOOL_SPECS,
    AnswerDraft,
    Message,
    ToolCall,
    run_agent,
)
from financial_analyst.agent_eval import load_dataset
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.grounding import ground
from financial_analyst.llm_provider import (
    ANSWER_TOOL,
    OpenAICompatibleModel,
    ProviderConfigError,
    ProviderError,
    ProviderSettings,
    load_provider_settings,
    parse_response,
    to_provider_messages,
    to_provider_tools,
)

KEY = "sk-test-SECRET-0123456789"
APPLE = "0000320193"
A10K = "0000320193-26-000026"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "LLM_MODEL", "LLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def settings(**changes) -> ProviderSettings:
    return ProviderSettings(_env_file=None, api_key=KEY, model="test-model", **changes)


def _pinned():
    try:
        data = load_pinned_data(ROOT / "evals" / "sec_snapshots_v1.json", CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if (CACHE / "filings" / APPLE).exists() else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


# --- A scripted fake provider -----------------------------------------------------------------


def tool_call(call_id, name, arguments) -> dict:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": text}}


def completion(tool_calls=None, content=None, finish="stop", usage=(100, 20)) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}  # fmt: skip


class FakeProvider:
    """Answers each request with the next step: a dict body, an httpx.Response, an exception
    to raise, or a function of the request body. Records every request."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps[len(self.requests) - 1]
        if callable(step) and not isinstance(step, type):
            step = step(json.loads(request.content))
        if isinstance(step, Exception):
            raise step
        if isinstance(step, httpx.Response):
            return step
        return httpx.Response(200, json=step)

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def model_for(provider: FakeProvider, clock=None, **changes) -> OpenAICompatibleModel:
    kwargs = {"clock": clock} if clock else {}
    return OpenAICompatibleModel(settings(**changes), transport=httpx.MockTransport(provider),
                                 **kwargs)  # fmt: skip


def tool_output(body: dict, call_id: str) -> dict:
    """What the agent sent back for a call, read from the provider request."""
    content = next(m["content"] for m in body["messages"]
                   if m["role"] == "tool" and m["tool_call_id"] == call_id)  # fmt: skip
    return json.loads(content)


# --- Wire-format translation ---------------------------------------------------------------


def test_tool_specs_become_function_tools_plus_the_answer_tool():
    tools = to_provider_tools(TOOL_SPECS)
    assert [t["function"]["name"] for t in tools] == [s.name for s in TOOL_SPECS] + [ANSWER_TOOL]
    assert all(t["type"] == "function" for t in tools)
    assert tools[2]["function"]["parameters"] == TOOL_SPECS[2].parameters  # compare, unchanged
    assert tools[-1]["function"]["parameters"] == AnswerDraft.model_json_schema()
    with pytest.raises(ValueError, match="reserved"):
        to_provider_tools([*TOOL_SPECS, TOOL_SPECS[0].model_copy(update={"name": ANSWER_TOOL})])


def test_messages_keep_tool_call_ids_and_results_paired():
    call = ToolCall(id="call_1", name="compare", arguments={"cik": APPLE})
    converted = to_provider_messages([
        Message(role="system", content="rules"),
        Message(role="user", content="question"),
        Message(role="assistant", tool_calls=[call]),
        Message(role="tool", tool_call_id="call_1", content='{"ok": true}'),
        Message(role="assistant", content='{"answer": "x"}'),
        Message(role="user", content="repair"),
    ])  # fmt: skip
    assert converted[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "compare", "arguments": '{"cik": "0000320193"}'}}]}  # fmt: skip
    assert converted[3] == {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'}
    assert [m["role"] for m in converted] == ["system", "user", "assistant", "tool", "assistant",
                                              "user"]  # fmt: skip


def test_tool_calls_are_parsed_with_their_ids():
    lookup = tool_call("call_a", "resolve_company", {"identifier": "AAPL"})
    turn = parse_response(completion([lookup, tool_call("call_b", "compare", "{not json")]))
    assert [(c.id, c.name) for c in turn.tool_calls] == [("call_a", "resolve_company"),
                                                         ("call_b", "compare")]  # fmt: skip
    assert turn.tool_calls[0].arguments == {"identifier": "AAPL"}
    assert turn.tool_calls[1].arguments == "{not json"  # left for the tool layer to refuse


@pytest.mark.parametrize(
    ("body", "answer"),
    [
        (completion([tool_call("c", ANSWER_TOOL, {"answer": "Fine."})]), {"answer": "Fine."}),
        (completion(content='{"answer": "From content."}'), {"answer": "From content."}),
        (completion(content="Just prose."), "Just prose."),  # draft validation will refuse it
    ],
)
def test_answers_arrive_as_the_answer_tool_or_as_content(body, answer):
    assert parse_response(body).answer == answer


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": [{"message": "text"}]},
        completion(content=""),
        completion(content="cut", finish="length"),
        completion([{"id": "c", "type": "code", "function": {"name": "x", "arguments": "{}"}}]),
        completion([{"id": "c", "type": "function"}]),
        completion([tool_call("bad id!", "compare", {})]),
        completion([tool_call("c", "compare", {}), tool_call("c", "compare", {})]),
        completion([tool_call("c", ANSWER_TOOL, {"answer": "x"}), tool_call("d", "compare", {})]),
        completion([tool_call("c", ANSWER_TOOL, "null")]),
    ],
)
def test_malformed_provider_output_is_rejected(body):
    with pytest.raises(ProviderError) as error:
        parse_response(body)
    assert error.value.kind == "malformed_response"


# --- Requests, errors, timeouts and secrets ------------------------------------------------------


def test_a_request_carries_the_key_only_in_the_authorization_header():
    provider = FakeProvider([completion(content='{"answer": "x"}')])
    with model_for(provider, temperature=0.0) as model:
        model.complete([Message(role="user", content="q")], TOOL_SPECS)
    [request] = provider.requests
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = provider.bodies[0]
    assert body["model"] == "test-model" and body["temperature"] == 0.0
    assert body["tool_choice"] == "auto" and KEY not in request.content.decode()
    assert len(body["tools"]) == len(TOOL_SPECS) + 1


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        (httpx.Response(500, json={"error": {"message": "overloaded"}}), "http_error"),
        (httpx.Response(429, json={"error": {"message": "rate limited"}}), "http_error"),
        (httpx.Response(302, headers={"location": "https://x.example"}), "malformed_response"),
        (httpx.Response(200, content=b"<html>"), "malformed_response"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "transport"),
    ],
)  # fmt: skip
def test_provider_failures_raise_once_without_retrying(step, kind):
    provider = FakeProvider([step, completion(content="never sent")])
    with model_for(provider) as model, pytest.raises(ProviderError) as error:
        model.complete([Message(role="user", content="q")], TOOL_SPECS)
    assert error.value.kind == kind and len(provider.requests) == 1
    assert model.calls[0].outcome == "error"


def test_an_echoed_key_is_redacted_from_provider_errors():
    echo = httpx.Response(401, json={"error": {"message": f"Incorrect API key: {KEY}"}})
    with model_for(FakeProvider([echo])) as model, pytest.raises(ProviderError) as error:
        model.complete([Message(role="user", content="q")], TOOL_SPECS)
    assert KEY not in str(error.value) and "[redacted]" in str(error.value)


def test_the_request_timeout_never_exceeds_the_run_budget():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    provider = FakeProvider([completion(content='{"answer": "x"}')] * 3)
    with model_for(provider, clock=clock, request_timeout_seconds=30) as model:
        model.complete([], TOOL_SPECS)  # the run budget starts here
        clock.now = 80.0
        model.complete([], TOOL_SPECS)
        clock.now = 90.0
        with pytest.raises(ProviderError) as error:
            model.complete([], TOOL_SPECS)
    timeouts = [r.extensions["timeout"]["read"] for r in provider.requests]
    assert timeouts == [30, 10]  # min(30, what is left of 90 s)
    assert error.value.kind == "timeout" and len(provider.requests) == 2


def test_settings_never_reveal_the_key():
    configured = settings()
    assert KEY not in repr(configured) and KEY not in str(configured.model_dump())
    assert KEY not in repr(model_for(FakeProvider([])))


def test_missing_credentials_are_named_without_values(monkeypatch):
    with pytest.raises(ProviderConfigError) as error:
        load_provider_settings(_env_file=None)
    assert str(error.value) == (
        "LLM provider configuration: LLM_API_KEY (or OPENAI_API_KEY) (missing), LLM_MODEL (missing)"
    )
    monkeypatch.setenv("LLM_API_KEY", KEY)  # a key but no model: the key is still not echoed
    with pytest.raises(ProviderConfigError) as error:
        load_provider_settings(_env_file=None)
    assert KEY not in str(error.value) and "LLM_MODEL (missing)" in str(error.value)


def test_openai_api_key_is_accepted_and_llm_api_key_wins(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "m")
    assert load_provider_settings(_env_file=None).api_key.get_secret_value() == KEY
    monkeypatch.setenv("LLM_API_KEY", "sk-other")
    assert load_provider_settings(_env_file=None).api_key.get_secret_value() == "sk-other"


@pytest.mark.parametrize("url", ["http://api.example.com/v1", "ftp://x", "file:///etc"])
def test_only_https_or_local_endpoints_are_accepted(url):
    with pytest.raises(ProviderConfigError, match="LLM_BASE_URL"):
        load_provider_settings(_env_file=None, api_key=KEY, model="m", base_url=url)
    assert settings(base_url="http://localhost:8000/v1/").base_url == "http://localhost:8000/v1"


def test_usage_and_estimated_cost_are_reported():
    provider = FakeProvider([completion(content='{"answer": "x"}', usage=(1000, 200))] * 2)
    with model_for(provider, input_cost_per_mtok=Decimal("2.50"),
                   output_cost_per_mtok=Decimal("10")) as model:  # fmt: skip
        model.complete([], TOOL_SPECS)
        model.complete([], TOOL_SPECS)
    usage = model.usage()
    assert (usage.requests, usage.prompt_tokens, usage.completion_tokens) == (2, 2000, 400)
    assert usage.estimated_cost_usd == Decimal("0.009")
    assert model_for(FakeProvider([])).usage().estimated_cost_usd is None


# --- Through run_agent and ground --------------------------------------------------------------

STD = ("{company} {metric} was {current} for {current_period}, compared with {prior} for "
       "{prior_period}: a change of {delta} ({growth}).")  # fmt: skip


def apple_provider() -> FakeProvider:
    """A provider that behaves like a good model: it reads tool results from the request."""

    def compare(body):
        cik = tool_output(body, "call_1")["output"]["cik"]
        return completion([tool_call("call_2", "compare", {
            "cik": cik, "metric": "revenue",
            "current_period": {"kind": "fiscal_year", "fiscal_year": 2026},
            "prior_period": {"kind": "fiscal_year", "fiscal_year": 2025}})])  # fmt: skip

    def search(body):
        cid = tool_output(body, "call_2")["output"]["comparison_id"]
        return completion([tool_call("call_3", "search_commentary", {"comparison_id": cid})])

    def answer(body):
        comparison = tool_output(body, "call_2")["output"]
        evidence = tool_output(body, "call_3")["output"]["evidence"]
        cid = comparison["comparison_id"]
        draft = {"subject": cid, "answer": STD,
                 "values": [{"placeholder": p, "value_id": f"{cid}/{part}"} for p, part in
                            [("current", "current"), ("prior", "prior"), ("delta", "delta"),
                             ("growth", "percent")]],
                 "citations": [evidence[0]["citation_id"]]}  # fmt: skip
        return completion([tool_call("call_4", ANSWER_TOOL, draft)])

    first = completion([tool_call("call_1", "resolve_company", {"identifier": "AAPL"})])
    return FakeProvider([first, compare, search, answer])


@pinned
def test_a_mocked_provider_run_is_grounded_and_rendered_by_the_server():
    provider = apple_provider()
    with model_for(provider) as model:
        run = run_agent("How much did Apple's revenue grow in fiscal 2026?", model, data=DATA,
                        cache_dir=CACHE)  # fmt: skip
    answer = ground(run, data=DATA, cache_dir=CACHE)
    assert run.status == "drafted" and len(provider.requests) == 4
    assert [r.call.id for r in run.tool_results] == ["call_1", "call_2", "call_3"]
    assert answer.status == "complete" and answer.issues == []
    assert answer.current.formatted == "$478,834,000,000" and answer.percent.display == "+15.1%"
    assert all(c.accession_number == A10K for c in answer.citations)
    # Each tool result went back under the id of the call it answers.
    last = provider.bodies[-1]["messages"]
    calls = [c["id"] for m in last if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    results = [m["tool_call_id"] for m in last if m["role"] == "tool"]
    assert calls == results == ["call_1", "call_2", "call_3"]


@pinned
def test_invalid_tool_calls_are_returned_to_the_provider_as_errors():
    def check_and_answer(body):
        assert tool_output(body, "c1")["error"]["code"] == "unknown_tool"
        assert tool_output(body, "c2")["error"]["code"] == "invalid_arguments"
        return completion([tool_call("c3", ANSWER_TOOL, {"answer": "Cannot answer."})])

    provider = FakeProvider([
        completion([tool_call("c1", "read_file", {"path": "/etc/passwd"}),
                    tool_call("c2", "compare", "{not json")]),
        check_and_answer,
    ])  # fmt: skip
    with model_for(provider) as model:
        run = run_agent("q", model, data=DATA, cache_dir=CACHE)
    assert run.status == "drafted"
    assert ground(run, data=DATA, cache_dir=CACHE).status == "abstained"  # no subject


@pinned
@pytest.mark.parametrize(
    ("steps", "status", "requests"),
    [
        ([completion([tool_call(f"c{i}", "resolve_company", {"identifier": "AAPL"})])
          for i in range(9)], "turn_limit", MAX_MODEL_TURNS),
        ([completion([tool_call(f"c{i}", "resolve_company", {"identifier": "AAPL"})
                      for i in range(7)])], "tool_call_limit", 1),
        ([completion(content="prose, not a draft"), completion(content="still prose")],
         "invalid_draft", 2),  # one repair, then stop
        ([httpx.Response(503)], "model_error", 1),
        ([httpx.ReadTimeout("slow")], "model_error", 1),
    ],
)  # fmt: skip
def test_the_agents_limits_apply_to_the_live_adapter(steps, status, requests):
    provider = FakeProvider(steps + [completion(content="unused")] * 3)
    with model_for(provider) as model:
        run = run_agent("q", model, data=DATA, cache_dir=CACHE)
    assert run.status == status and len(provider.requests) == requests
    answer = ground(run, data=DATA, cache_dir=CACHE)
    assert answer.status == "abstained" and KEY not in (run.error or "")


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def no_env_file(monkeypatch):
    real = live_agent.load_provider_settings
    monkeypatch.setattr(live_agent, "load_provider_settings", lambda: real(_env_file=None))


def test_the_cli_stops_without_credentials(no_env_file, capsys):
    provider = FakeProvider([])
    assert live_agent.main(["ask", "q"], transport=httpx.MockTransport(provider)) == 2
    err = capsys.readouterr().err
    assert "LLM_API_KEY (or OPENAI_API_KEY) (missing)" in err and "no request was sent" in err
    assert provider.requests == []


@pinned
def test_the_cli_prints_only_the_server_rendered_answer(no_env_file, monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    provider = apple_provider()
    code = live_agent.main(["ask", "How much did Apple's revenue grow in fiscal 2026?"],
                           transport=httpx.MockTransport(provider))  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0
    out = captured.out
    assert "Status: COMPLETE" in out and "Current: FY2026" in out
    assert "an increase of $62,673,000,000 (+15.1%)" in out and f"10-K {A10K}" in out
    assert "Candidate commentary" in out and "sec-psg:" in out and "Usage:   4 requests" in out
    assert "{company}" not in out  # the model's draft is never printed
    assert KEY not in out + captured.err


@pinned
def test_the_live_evaluation_reports_denominators(no_env_file, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_INPUT_COST_PER_MTOK", "1")
    monkeypatch.setenv("LLM_OUTPUT_COST_PER_MTOK", "1")
    provider = apple_provider()  # the JPMorgan smoke case gets a declining answer
    provider.steps.append(completion([tool_call("x", ANSWER_TOOL, {"answer": "Unsure."})]))
    output = tmp_path / "live.json"
    code = live_agent.main(["eval", "--output", str(output)],
                           transport=httpx.MockTransport(provider))  # fmt: skip
    report = json.loads(output.read_text())  # written even though a case failed
    assert code == 1 and report["attempted"] == 2
    metrics = report["metrics"]
    assert metrics["draft_success"] == [2, 2] and metrics["grounding_pass"] == [1, 2]
    assert metrics["financial_correct"] == [1, 2] and metrics["valid_tool_sequence"] == [2, 2]
    assert report["prompt_tokens"] == 500 and Decimal(report["estimated_cost_usd"]) == Decimal(
        "0.0006"
    )
    assert "attempted              2" in capsys.readouterr().out


def test_the_live_evaluation_defaults_to_smoke_cases_and_needs_all_for_more():
    cases = load_dataset(ROOT / "evals" / "agent_v1.json").cases
    assert [c.id for c in live_agent.live_cases(cases, None, False)] == list(live_agent.SMOKE_CASES)
    everything = live_agent.live_cases(cases, None, True)
    assert len(everything) == 19 and "fabricated-quote" not in {c.id for c in everything}
    with pytest.raises(ValueError, match="not live-eligible"):
        live_agent.live_cases(cases, ["fabricated-quote"], False)


def skipping_provider() -> FakeProvider:
    """Apple: compare, then answer without searching. JPMorgan: an unknown tool and a bad
    argument, then a draft citing nothing and an invented value id."""

    def apple_answer(body):
        cid = tool_output(body, "a1")["output"]["comparison_id"]
        values = [{"placeholder": "x", "value_id": f"{cid}/delta"}]
        draft = {"subject": cid, "answer": "Up {x}.", "values": values}
        return completion([tool_call("a2", ANSWER_TOOL, draft)])

    def jpm_answer(body):
        cid = tool_output(body, "j3")["output"]["comparison_id"]
        invented = [{"placeholder": "x", "value_id": "obs:" + "b" * 64}]
        draft = {"subject": cid, "answer": "Was {x}.", "values": invented}
        return completion([tool_call("j4", ANSWER_TOOL, draft)])

    q3 = {"kind": "fiscal_quarter", "quarter": 3}
    jpm_compare = {"cik": "0000019617", "metric": "net_income",
                   "current_period": q3 | {"fiscal_year": 2026},
                   "prior_period": q3 | {"fiscal_year": 2025}}  # fmt: skip
    return FakeProvider([
        completion([tool_call("a1", "compare", {
            "cik": APPLE, "metric": "revenue",
            "current_period": {"kind": "fiscal_year", "fiscal_year": 2026},
            "prior_period": {"kind": "fiscal_year", "fiscal_year": 2025}})]),
        apple_answer,
        completion([tool_call("j1", "read_file", {"path": "/etc/passwd"}),
                    tool_call("j2", "compare", "{not json")]),
        completion([tool_call("j3", "compare", jpm_compare)]),
        jpm_answer,
    ])  # fmt: skip


@pinned
def test_failed_cases_exit_nonzero_with_safe_diagnostics(no_env_file, monkeypatch, capsys,
                                                         tmp_path):  # fmt: skip
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    output = tmp_path / "live.json"
    code = live_agent.main(["eval", "--output", str(output)],
                           transport=httpx.MockTransport(skipping_provider()))  # fmt: skip
    captured = capsys.readouterr()
    assert code == 1
    apple, jpm = json.loads(output.read_text())["results"]

    # Apple skipped commentary although the case expects complete: correct figures, partial.
    d = apple["diagnostics"]
    assert [s["name"] for s in d["tools"]] == ["compare"] and d["model_turns"] == 2
    assert d["reached"] == {"compare": True, "search_commentary": False, "submit_answer": True}
    assert d["terminal_reason"] == "drafted" and apple["status"] == "partial"
    assert apple["checks"]["numeric"] and not apple["checks"]["status"]
    assert not apple["checks"]["citation_provenance"]

    # JPMorgan: tool errors, a correction, then a draft the validator rejects.
    d = jpm["diagnostics"]
    assert [(s["name"], s["ok"], s["error_code"]) for s in d["tools"]] == [
        ("read_file", False, "unknown_tool"), ("compare", False, "invalid_arguments"),
        ("compare", True, None),
    ]  # fmt: skip
    assert d["tool_counts"] == {"read_file": 1, "compare": 2}
    assert d["issue_codes"] == ["value_not_in_run"] and jpm["status"] == "abstained"
    assert jpm["valid_tool_sequence"] is False

    out = captured.out
    assert "FAIL apple-fy2026-revenue" in out and "FAIL jpm-q3-2026-net-income" in out
    assert "search_commentary no" in out
    assert "read_file unknown_tool > compare invalid_arguments > compare ok" in out
    assert "grounding issues: value_not_in_run" in out
    # Codes and names only: no key, header, arguments or payloads.
    everything = out + captured.err + output.read_text()
    for secret in (KEY, "Bearer", "/etc/passwd", "{not json", "Was {x}"):
        assert secret not in everything


@pinned
def test_a_passing_evaluation_exits_zero(no_env_file, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "test-model")
    code = live_agent.main(["eval", "--cases", "apple-fy2026-revenue"],
                           transport=httpx.MockTransport(apple_provider()))  # fmt: skip
    assert code == 0

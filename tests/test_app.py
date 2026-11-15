"""The Streamlit page's adapter and view logic, with a mocked provider. Offline.

Only the pure helpers are tested; Streamlit itself is imported inside main() and never here.
"""

import json
import socket
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from financial_analyst import app
from financial_analyst.analysis import load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.llm_provider import (
    OpenAICompatibleModel,
    ProviderSettings,
    Usage,
)

KEY = "sk-test-SECRET-app"
APPLE = "0000320193"
CACHE = Path(Settings.model_fields["filing_cache_dir"].default)
ROOT = Path(__file__).parent.parent
APPLE_Q = app.EXAMPLES[0]
COMPARE = {"cik": APPLE, "metric": "revenue",
           "current_period": {"kind": "fiscal_year", "fiscal_year": 2026},
           "prior_period": {"kind": "fiscal_year", "fiscal_year": 2025}}  # fmt: skip


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _pinned():
    try:
        data = load_pinned_data(ROOT / "evals" / "sec_snapshots_v1.json", CACHE)
    except Exception:  # noqa: BLE001 - the local cache is optional for the test suite
        return None
    return data if (CACHE / "filings" / APPLE).exists() else None


DATA = _pinned()
pinned = pytest.mark.skipif(DATA is None, reason="pinned snapshots or filings not cached")


def reply(call_id: str, name: str, arguments) -> dict:
    call = {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}  # fmt: skip
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]},
                         "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50}}  # fmt: skip


def factory(*steps):
    """A model factory whose provider answers with `steps` in order (dicts, functions of
    the request body, or httpx responses)."""
    queue = list(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        step = queue.pop(0)
        if callable(step):
            step = step(json.loads(request.content))
        return step if isinstance(step, httpx.Response) else httpx.Response(200, json=step)

    settings = ProviderSettings(_env_file=None, api_key=KEY, model="test-model",
                                input_cost_per_mtok=Decimal("1"),
                                output_cost_per_mtok=Decimal("1"))  # fmt: skip
    return lambda: OpenAICompatibleModel(settings, transport=httpx.MockTransport(handler))


def comparison_id(body: dict) -> str:
    content = next(m["content"] for m in body["messages"] if m["role"] == "tool")
    return json.loads(content)["output"]["comparison_id"]


def answer_with(**draft):
    def step(body):
        cid = comparison_id(body)
        payload = {"subject": cid, "answer": "Note.",
                   "values": [{"value_id": f"{cid}/delta"}]} | draft  # fmt: skip
        return reply("a1", "submit_answer", payload)

    return step


@pinned
def test_a_verified_comparison_is_shown_with_figures_chart_and_source():
    answer, usage = app.analyze(APPLE_Q, DATA, factory(reply("c1", "compare", COMPARE),
                                                       answer_with()))  # fmt: skip
    view = app.answer_view(answer)
    assert view["status"] == "complete" and view["status_text"].startswith("Complete")
    figures = view["figures"]
    assert figures["company"] == "Apple Inc. (CIK 0000320193)"
    assert figures["current"] == ("FY2026 (2025-09-28 to 2026-09-26)", "$478,834,000,000")
    assert figures["prior"] == ("FY2025 (2024-09-29 to 2025-09-27)", "$416,161,000,000")
    assert (figures["change"], figures["percent"]) == ("+$62,673,000,000", "+15.1%")
    assert list(view["chart"].values()) == [416161000000.0, 478834000000.0]
    assert list(view["chart"]) == ["FY2025", "FY2026"]
    assert figures["compact"] == {"current": "$478.83B", "prior": "$416.16B",
                                  "change": "+$62.67B"}  # fmt: skip
    assert "$478,834,000,000" in figures["statement"]  # exact amounts stay in the statement
    assert view["source"]["url"].endswith("/000032019326000026/aapl-20260926.htm")
    assert view["source"]["filing"] == "10-K 0000320193-26-000026 filed 2026-11-03"
    assert not view["provider_failure"] and view["issues"] == []
    assert app.usage_text(usage) == "2 request(s), 2000 input + 100 output tokens, ~$0.0021"


@pinned
def test_model_written_text_is_never_part_of_the_view():
    secret_note = "SECRET NOTE: revenue was $999 billion"
    quote = {"citation_id": "sec-psg:invented", "text": "SECRET QUOTE"}
    draft = answer_with(answer=secret_note, quotes=[quote],
                        explanations=[{"text": "SECRET CAUSE: tariffs vanished."}])  # fmt: skip
    answer, _ = app.analyze(APPLE_Q, DATA, factory(reply("c1", "compare", COMPARE), draft,
                                                   draft))  # fmt: skip
    view = app.answer_view(answer)
    text = json.dumps(view)
    assert "SECRET" not in text and "999" not in text
    assert view["quotations"] == []  # the rejected quotation is not shown
    assert view["figures"]["current"][1] == "$478,834,000,000"  # the verified figures still are


@pinned
def test_an_unsupported_question_shows_no_figures():
    decline = reply("a1", "submit_answer", {"subject": None, "answer": "Not supported."})
    answer, _ = app.analyze("How much did Microsoft's revenue grow in fiscal 2026?", DATA,
                            factory(reply("r1", "resolve_company", {"identifier": "Microsoft"}),
                                    decline))  # fmt: skip
    view = app.answer_view(answer)
    assert view["status"] == "abstained" and view["figures"] is None and view["chart"] is None
    assert view["source"] is None and view["citations"] == []


@pinned
def test_a_provider_failure_is_flagged_without_figures_or_secrets():
    answer, usage = app.analyze(APPLE_Q, DATA, factory(httpx.Response(503)))
    view = app.answer_view(answer)
    assert view["provider_failure"] and view["status"] == "abstained" and view["figures"] is None
    assert any("HTTP 503" in w for w in view["warnings"])
    assert KEY not in json.dumps(view) and usage.requests_without_usage == 1
    assert "(1 request(s) without usage)" in app.usage_text(usage)


@pinned
def test_a_blank_question_is_refused_before_any_request():
    def never(request):
        raise AssertionError("no request expected")

    with pytest.raises(ValueError):
        app.analyze("   ", DATA, factory(never))


def test_usage_text_reports_missing_usage_as_a_lower_bound():
    usage = Usage(requests=3, requests_without_usage=1, prompt_tokens=2000, completion_tokens=10,
                  latency_seconds=1.0, estimated_cost_usd=Decimal("0.00201"))  # fmt: skip
    assert app.usage_text(usage) == (
        "3 request(s), 2000 input + 10 output tokens (1 request(s) without usage), "
        "at least ~$0.0020"
    )


def test_importing_the_app_does_not_import_streamlit():
    assert "streamlit" not in sys.modules or "financial_analyst.app" in sys.modules
    assert callable(app.main) and callable(app.answer_view)


def test_compact_amounts_round_for_display_and_fall_back_to_exact():
    assert app.compact_amount(Decimal("416161000000"), "USD") == "$416.16B"
    assert app.compact_amount(Decimal("-62673000000"), "USD", signed=True) == "-$62.67B"
    assert app.compact_amount(Decimal("4002814000000"), "USD") == "$4.00T"
    assert app.compact_amount(Decimal("14393000000"), "USD") == "$14.39B"
    assert app.compact_amount(Decimal("5005000"), "USD") == "$5.01M"
    assert app.compact_amount(Decimal("999"), "USD") == "$999"
    assert app.compact_amount(Decimal("5000000000"), "shares") == "5,000,000,000 shares"
    assert app.short_period("FY2026 (2025-09-28 to 2026-09-26)") == "FY2026"

"""Minimal local Streamlit interface over the live agent.

    uv run --extra ui streamlit run src/financial_analyst/app.py

The page is a thin view over the same flow as `live_agent ask`: the pinned snapshots and the
filing cache, the configured provider, run_agent, then ground. It shows only the fields
render_answer built from the run's evidence (figures, provenance, verified quotations,
candidate commentary, warnings, issue codes). It never shows the model's own note,
explanations or rejected text, and it never computes or substitutes a figure.

Credentials stay in the server process (environment or .env, read by pydantic-settings).
Errors name a missing setting or a missing cache, never a value. Every question sends
requests to the configured provider.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from financial_analyst.agent import run_agent
from financial_analyst.analysis import PinnedData, load_pinned_data
from financial_analyst.config import Settings
from financial_analyst.grounding import GroundedAnswer, ground
from financial_analyst.llm_provider import (
    OpenAICompatibleModel,
    ProviderConfigError,
    Usage,
    load_provider_settings,
)

MANIFEST = Path("evals/sec_snapshots_v1.json")
CACHE_DIR = Path(Settings.model_fields["filing_cache_dir"].default)
EXAMPLES = (
    "How much did Apple's revenue grow in fiscal 2026 compared with fiscal 2025?",
    "How did JPMorgan's net income for the third quarter of 2026 compare with the third "
    "quarter of 2025?",
)
_STATUS_TEXT = {
    "complete": "Complete: verified figures, plus any commentary the question asked for.",
    "partial": "Partial: the figures are verified, but requested commentary or a quotation is "
    "missing, or some cited material failed verification.",
    "abstained": "Abstained: no reliable answer could be established, so no figures are shown.",
}
_PROVIDER_FAILURES = ("model_error", "timeout")


def analyze(
    question: str,
    data: PinnedData,
    model_factory: Callable[[], OpenAICompatibleModel],
    cache_dir: Path = CACHE_DIR,
) -> tuple[GroundedAnswer, Usage]:
    """Run one question through the agent with a fresh model, then ground it. Raises
    ValueError for a blank or overlong question, exactly as run_agent does."""
    with model_factory() as model:
        run = run_agent(question, model, data=data, cache_dir=cache_dir)
        usage = model.usage()
    return ground(run, data=data, cache_dir=cache_dir), usage


def answer_view(answer: GroundedAnswer) -> dict[str, Any]:
    """Everything the page shows, taken only from server-rendered fields of the answer.
    The withheld model text is not read here, so it cannot be displayed."""
    view: dict[str, Any] = {
        "status": answer.status,
        "status_text": _STATUS_TEXT[answer.status],
        "figures": None,
        "chart": None,
        "source": None,
        "quotations": [{"text": q.text, "attribution": q.attribution} for q in answer.quotations],
        "citations": [
            {
                "label": f"{c.form} {c.accession_number}, {c.section or 'unlabeled section'}",
                "url": c.document_url,
                "citation_id": c.citation_id,
                "preview": c.preview,
            }
            for c in answer.citations
        ],  # fmt: skip
        "warnings": list(answer.warnings),
        "issues": [i.code for i in answer.issues],
        "provider_failure": answer.run_status in _PROVIDER_FAILURES,
        "run": f"{answer.run_status}, {answer.tool_calls} tool call(s), "
        f"{answer.model_turns} model turn(s)",
    }
    if answer.status == "abstained" or answer.current is None or answer.prior is None:
        return view  # no figures are ever shown without a grounded comparison
    view["figures"] = {
        "company": answer.company,
        "metric": answer.statement_line,
        "current": (answer.current.period, answer.current.formatted),
        "prior": (answer.prior.period, answer.prior.formatted),
        "change": answer.delta_formatted,
        "percent": answer.percent.display or answer.percent.note,
        "statement": answer.statement,
    }
    # Chart values are the verified exact values, converted only for plotting.
    view["chart"] = {
        f"Prior: {answer.prior.period}": float(answer.prior.value),
        f"Current: {answer.current.period}": float(answer.current.value),
    }
    provenance = answer.provenance
    view["source"] = {
        "filing": provenance.commentary_filing,
        "url": provenance.commentary_filing_url,
        "concept": f"{provenance.concept} ({provenance.unit})",
        "comparison_id": provenance.comparison_id,
        "snapshot": provenance.company_facts_sha256[:16],
    }
    return view


def usage_text(usage: Usage) -> str:
    tokens = f"{usage.prompt_tokens} input + {usage.completion_tokens} output tokens"
    if not usage.usage_complete:
        tokens += f" ({usage.requests_without_usage} request(s) without usage)"
    if usage.estimated_cost_usd is None:
        return f"{usage.requests} request(s), {tokens}, cost not estimated"
    bound = "" if usage.usage_complete else "at least "
    return f"{usage.requests} request(s), {tokens}, {bound}~${usage.estimated_cost_usd:.4f}"


def main() -> None:  # pragma: no cover - exercised by running Streamlit
    import pandas as pd
    import streamlit as st

    st.set_page_config(page_title="Financial AI Analyst", layout="centered")
    st.title("Financial AI Analyst")
    st.write(
        "Ask about Apple or JPMorgan Chase: revenue, net income or total assets, compared "
        "between two fiscal periods. Figures are computed from pinned SEC XBRL data and "
        "rendered by the server; a language model only chooses which tools to call."
    )

    @st.cache_resource
    def pinned() -> PinnedData:
        return load_pinned_data(MANIFEST, CACHE_DIR)

    try:
        data = pinned()
    except Exception as exc:  # noqa: BLE001 - explain the missing local data and stop
        reason = type(exc).__name__
        st.error(f"The pinned SEC data could not be loaded from {CACHE_DIR} ({reason}). "
                 "The local cache is required; nothing was fetched.")  # fmt: skip
        st.stop()

    question = st.text_input("Question", value=EXAMPLES[0], max_chars=1000,
                             help="Example: " + EXAMPLES[1])  # fmt: skip
    if not st.button("Analyze", type="primary"):
        return
    try:
        settings = load_provider_settings()
    except ProviderConfigError as exc:
        st.error(f"{exc}. Set them in the environment or .env; no request was sent.")
        st.stop()

    with st.spinner("Running the agent and verifying the answer..."):
        try:
            answer, usage = analyze(question, data, lambda: OpenAICompatibleModel(settings))
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
    view = answer_view(answer)

    show = {"complete": st.success, "partial": st.warning, "abstained": st.error}
    show[view["status"]](view["status_text"])
    if view["provider_failure"]:
        st.error("The model provider failed or timed out; see the notes below.")

    if view["figures"]:
        figures = view["figures"]
        st.subheader(f"{figures['company']}: {figures['metric']}")
        left, right, change = st.columns(3)
        left.metric(figures["prior"][0], figures["prior"][1])
        right.metric(figures["current"][0], figures["current"][1])
        change.metric("Change", figures["change"], figures["percent"])
        st.write(figures["statement"])
        chart = pd.DataFrame({"Value (USD)": list(view["chart"].values())},
                             index=list(view["chart"].keys()))  # fmt: skip
        st.bar_chart(chart)
        source = view["source"]
        link = f"[{source['filing']}]({source['url']})" if source["url"] else source["filing"]
        st.markdown(f"**Source:** {link}  \nConcept: `{source['concept']}`")
        st.caption(f"{source['comparison_id']} · Company Facts snapshot {source['snapshot']}")

    for quote in view["quotations"]:
        st.markdown(f"> {quote['text']}\n\n— {quote['attribution']}")
    if view["citations"]:
        st.subheader("Candidate commentary")
        st.caption("Verified source text from the filing; not established as the cause.")
        for citation in view["citations"]:
            label = (
                f"[{citation['label']}]({citation['url']})"
                if citation["url"]
                else (citation["label"])
            )
            st.markdown(f"{label}: {citation['preview']}")
            st.caption(citation["citation_id"])

    for warning in view["warnings"]:
        st.info(warning)
    if view["issues"]:
        st.caption("Validation issues: " + ", ".join(view["issues"]))
    st.caption(f"Run: {view['run']}. Usage: {usage_text(usage)}.")


if __name__ == "__main__":  # Streamlit runs this file as __main__
    main()

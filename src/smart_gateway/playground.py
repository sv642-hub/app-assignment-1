"""Streamlit Playground: send one message to the gateway and show its trace.

Run with `streamlit run src/smart_gateway/playground.py`. It talks to the
gateway over HTTP only, at PLAYGROUND_GATEWAY_URL.
"""

from __future__ import annotations

import httpx
import streamlit as st

from smart_gateway.client import PlaygroundSettings, make_client, send_chat
from smart_gateway.schemas import ChatResponse

EXAMPLES = [
    "My new card still hasn't arrived, when will it come?",
    "I think my card was stolen, what do I do?",
    "I sent money yesterday but the recipient hasn't received it.",
    "What exchange rate do you use for euros?",
    "Why was I charged a fee for my card payment?",
    "Can you recommend a good pizza place?",
]


def _use_example(text: str) -> None:
    st.session_state.message = text
    st.session_state.send = True


def _show(result: ChatResponse) -> None:
    trace = result.trace
    left, right = st.columns(2)
    left.metric("Intent", result.intent)
    right.metric("Jev's choice", trace.route)

    st.subheader("Reply")
    st.info(result.reply)
    if result.policy_card is not None:
        with st.expander(f"Policy card: {result.policy_card.title}"):
            for fact in result.policy_card.facts:
                st.markdown(f"- {fact}")

    st.subheader("Trace")
    rows = {
        "Router": trace.router,
        "Jev's choice": trace.route,
        "Confidence": f"{trace.confidence:.2f}",
        "Model": trace.model or "none (rule router)",
        "Latency": f"{trace.latency_ms:.0f} ms",
        "Format check": trace.format_check,
    }
    if trace.fallback_reason:
        rows["Fallback reason"] = trace.fallback_reason
    rows["Request ID"] = result.request_id
    st.table([{"Field": k, "Value": v} for k, v in rows.items()])


st.set_page_config(page_title="Smart AI Gateway Playground", layout="centered")
st.title("Smart AI Gateway: Playground")
st.caption(
    "Mocked prototype. Jev routing and the Qwen models are simulated from the "
    "files in config/."
)

st.write("Try an example:")
columns = st.columns(2)
for i, example in enumerate(EXAMPLES):
    columns[i % 2].button(
        example, key=f"example-{i}", on_click=_use_example, args=(example,)
    )

st.text_area("Customer message", key="message", height=100)
if st.button("Send", type="primary"):
    st.session_state.send = True

if st.session_state.pop("send", False):
    message = st.session_state.message.strip()
    if not message:
        st.warning("Type a message first.")
    else:
        settings = PlaygroundSettings()
        try:
            with make_client(settings) as http:
                _show(send_chat(http, message))
        except httpx.HTTPError as exc:
            st.error(f"Gateway request to {settings.gateway_url} failed: {exc}")

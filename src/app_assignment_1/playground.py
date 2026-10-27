"""Streamlit UI; all gateway interactions use HTTP."""

import httpx
import streamlit as st

from app_assignment_1.config import get_settings
from app_assignment_1.schemas import ChatResponse

st.set_page_config(page_title="Smart AI Gateway", page_icon="🏦", layout="centered")
st.title("Smart AI Gateway")
st.caption("DSAN 6700 · Deliverable 2 · Mock prototype")
st.info(
    "Jev routing and both Qwen backends are simulated. Replies use fictional "
    "bank policy cards. Use public or fictional messages only."
)


def choose_example(message: str) -> None:
    st.session_state["message"] = message


st.subheader("Try a banking message")
examples = [
    "My card has not arrived",
    "I lost my card",
    "My transfer has not arrived",
    "What is the exchange rate?",
    "Tell me about the weather",
]
for message in examples:
    st.button(message, on_click=choose_example, args=(message,))
with st.form("chat"):
    message = st.text_area(
        "Customer message",
        key="message",
        max_chars=2000,
        placeholder="My card has not arrived",
    )
    submitted = st.form_submit_button("Send message", type="primary")
if submitted:
    st.session_state.pop("result", None)
    if not message.strip():
        st.warning("Enter a banking message first.")
    else:
        settings = get_settings()
        try:
            with st.spinner("Routing your message through the mock gateway…"):
                response = httpx.post(
                    f"{settings.gateway_url.rstrip('/')}/v1/chat",
                    json={"message": message},
                    timeout=settings.request_timeout_seconds,
                )
                response.raise_for_status()
                result = ChatResponse.model_validate(response.json())
                st.session_state["result"] = result.model_dump(mode="json")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                st.error("Both mock backends are unavailable. Please try again later.")
            else:
                st.error(
                    f"The gateway rejected the request ({exc.response.status_code})."
                )
        except httpx.RequestError:
            st.error("Could not reach the gateway. Check that the service is running.")
        except ValueError:
            st.error("The gateway returned an unexpected response format.")
if "result" in st.session_state:
    result = ChatResponse.model_validate(st.session_state["result"])
    st.subheader("Detected intent")
    st.code(result.intent, language=None)
    st.subheader("Reply")
    st.write(result.reply)
    if result.trace.format_check == "failed":
        st.warning(
            "Reply format check failed: " + ", ".join(result.trace.format_errors)
        )
    st.subheader("Request trace")
    trace = result.trace
    st.table(
        {
            "Field": [
                "Routing source",
                "Jev choice (mock)",
                "Final route",
                "Model (mock)",
                "Latency (measured)",
                "Simulated delay",
                "Format check",
                "Fallback reason",
                "Confidence (heuristic)",
            ],
            "Value": [
                trace.router,
                trace.jev_choice or "No match — rule fallback",
                trace.route,
                trace.model,
                f"{trace.latency_ms:.2f} ms",
                f"{trace.simulated_latency_ms} ms",
                trace.format_check,
                trace.fallback_reason or "None",
                str(trace.confidence),
            ],
        }
    )
    st.caption(f"Request ID: {result.id}")
    if result.policy_facts:
        with st.expander("Fictional policy card facts"):
            for fact in result.policy_facts:
                st.write(f"• {fact}")
    st.caption("Format checks validate the response structure, not its correctness.")

import { useEffect, useRef, useState } from "react";

import Logo from "./Logo.jsx";

export const EXAMPLES = [
  "My card has not arrived",
  "I lost my card",
  "My transfer has not arrived",
  "What is the exchange rate?",
  "Tell me about the weather",
];
const MAX_CHARS = 2000;
const limitMessage = (text) => Array.from(text).slice(0, MAX_CHARS).join("");
const DRAFT_KEY = "gateway.chat.draft";
const TURNS_KEY = "gateway.chat.turns";

// The browser tab keeps the draft and the conversation so a page change or a
// refresh does not lose them. Nothing here goes to the server. A tab that
// blocks storage just starts empty.
export function readSaved(key, fallback) {
  try {
    const raw = window.sessionStorage.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function writeSaved(key, value) {
  try {
    if (value === null || (Array.isArray(value) && value.length === 0) || value === "") {
      try {
        window.sessionStorage.removeItem(key);
        return;
      } catch {
        // If deletion is blocked but writing works, overwrite the old chat.
      }
    }
    window.sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage is full or blocked: keep working without it.
  }
}

// Saved data from an older deploy may not fit what the screens expect. Check
// the shape and drop it if it does not, so a bad key cannot crash every refresh.
export function readSavedDraft() {
  const draft = readSaved(DRAFT_KEY, "");
  return typeof draft === "string" ? limitMessage(draft) : "";
}

function validTurn(t) {
  if (!t || typeof t !== "object" || !Number.isSafeInteger(t.id) || t.id < 0 || typeof t.message !== "string") return false;
  if (t.status === "pending") return true;
  if (t.status === "error") return typeof t.error === "string";
  if (t.status !== "done") return false;
  const r = t.result;
  const trace = r?.trace;
  return (
    typeof r?.reply === "string" &&
    typeof r.id === "string" &&
    typeof r.intent === "string" &&
    Array.isArray(r.policy_facts) &&
    r.policy_facts.every((fact) => typeof fact === "string") &&
    (trace?.route === "standard" || trace?.route === "complex") &&
    typeof trace.router === "string" &&
    typeof trace.reply_source === "string" &&
    (trace.format_check === "passed" || trace.format_check === "failed") &&
    Array.isArray(trace.format_errors) &&
    trace.format_errors.every((error) => typeof error === "string") &&
    (trace.fallback_reason == null || typeof trace.fallback_reason === "string") &&
    (trace.controller_action == null || typeof trace.controller_action === "string") &&
    (trace.confidence == null || typeof trace.confidence === "number")
  );
}

export function readSavedTurns() {
  const saved = readSaved(TURNS_KEY, []);
  return Array.isArray(saved) && saved.every(validTurn) ? saved : [];
}

export { DRAFT_KEY, TURNS_KEY };

const TOPICS = [
  "a card that has not arrived",
  "a lost or stolen card",
  "a transfer the recipient has not received",
  "exchange rates",
  "a failed top-up",
  "a pending transfer",
  "a card payment fee",
  "verifying your identity",
  "a refund that has not shown up",
  "the wrong amount of cash received",
];

function detailRows(result) {
  const t = result.trace;
  return [
    ["Intent", result.intent],
    ["Route", t.route],
    ["Routing source", t.router],
    ["Reply source", t.reply_source],
    ["Format check", t.format_errors.length ? `${t.format_check} (${t.format_errors.join(", ")})` : t.format_check],
    ["Fallback reason", t.fallback_reason ?? "none"],
    ["Controller action", t.controller_action ?? "none"],
    ["Confidence", t.confidence ?? "none (rule router)"],
    ["Request ID", result.id],
  ];
}

function Reply({ result }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="reply">
      <p>{result.reply}</p>
      <div className="chips">
        <span className={`chip route-${result.trace.route}`}>{result.trace.route}</span>
        <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide details" : "Show details"}
        </button>
      </div>
      {open && (
        <div className="details">
          {result.policy_facts.length > 0 && (
            <div>
              <h2 className="details-heading">Grounded in policy card</h2>
              <ul className="facts">
                {result.policy_facts.map((fact) => (
                  <li key={fact}>{fact}</li>
                ))}
              </ul>
            </div>
          )}
          <p className="muted note">
            Confidence is how sure the router was about its route choice. It does not say the reply is correct.
          </p>
          <div className="table-scroll">
            <table className="kv">
              <tbody>
                {detailRows(result).map(([key, value]) => (
                  <tr key={key}>
                    <th scope="row">{key}</th>
                    <td className="mono">{String(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Turn({ turn }) {
  return (
    <div className="turn">
      <div className="bubble">{turn.message}</div>
      <div className="assistant">
        <Logo size={30} round />
        <div className="assistant-body">
          {turn.status === "pending" && <span className="muted thinking">Thinking…</span>}
          {turn.status === "error" && (
            <p className="error" role="alert">
              {turn.error}
            </p>
          )}
          {turn.status === "done" && <Reply result={turn.result} />}
        </div>
      </div>
    </div>
  );
}

export default function ChatView({ turns, message, setMessage, onSend }) {
  const endRef = useRef(null);
  const busy = turns.some((t) => t.status === "pending");
  const canSend = !busy && message.trim().length > 0;

  const characterCount = Array.from(message).length;

  useEffect(() => {
    if (turns.length) endRef.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  const submit = (text) => {
    const clean = text.trim();
    if (!clean || busy || Array.from(clean).length > MAX_CHARS) return;
    onSend(clean);
    setMessage("");
  };

  return (
    <div className="chat">
      <div className="thread">
        <div className="column">
          {turns.length === 0 && (
            <div className="empty">
              <h1>How can DemoBank help?</h1>
              <p className="muted">Ask a banking question. Each reply shows how the gateway handled it.</p>
              <details className="topics">
                <summary>What can I ask about?</summary>
                <p className="muted">
                  This demo knows about {TOPICS.join(", ")}. Anything else gets a "could not match" reply.
                </p>
              </details>
              <div className="examples">
                {EXAMPLES.map((text) => (
                  <button key={text} type="button" className="example" onClick={() => submit(text)}>
                    {text}
                  </button>
                ))}
              </div>
              <p className="muted note">The weather example is outside banking on purpose: it shows the fallback reply.</p>
            </div>
          )}
          {turns.map((turn) => (
            <Turn key={turn.id} turn={turn} />
          ))}
          <div ref={endRef} />
        </div>
      </div>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit(message);
        }}
      >
        <div className="column">
          <div className="composer-box">
            <label htmlFor="message" className="visually-hidden">
              Message
            </label>
            <textarea
              id="message"
              rows={1}
              value={message}
              placeholder="Message DemoBank support…"
              onChange={(event) => setMessage(limitMessage(event.target.value))}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submit(message);
                }
              }}
            />
            <button type="submit" className="send" aria-label="Send message" disabled={!canSend}>
              <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
                <path
                  d="M9 14V4M4.5 8.5L9 4l4.5 4.5"
                  fill="none"
                  stroke="#fff"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          </div>
          <p className={`counter${characterCount >= MAX_CHARS ? " counter-full" : ""}`}>
            {characterCount} / {MAX_CHARS} characters
          </p>
          <p className="footnote">
            Fictional bank demo. Each message is routed on its own, so ask one question at a time; the gateway keeps no
            conversation memory yet.
          </p>
        </div>
      </form>
    </div>
  );
}

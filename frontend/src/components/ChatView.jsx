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

export default function ChatView({ turns, onSend }) {
  const [message, setMessage] = useState("");
  const endRef = useRef(null);
  const busy = turns.some((t) => t.status === "pending");
  const canSend = !busy && message.trim().length > 0;

  useEffect(() => {
    if (turns.length) endRef.current?.scrollIntoView?.({ block: "end" });
  }, [turns]);

  const submit = (text) => {
    const clean = text.trim();
    if (!clean || busy) return;
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
              <div className="examples">
                {EXAMPLES.map((text) => (
                  <button key={text} type="button" className="example" onClick={() => submit(text)}>
                    {text}
                  </button>
                ))}
              </div>
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
              maxLength={MAX_CHARS}
              placeholder="Message DemoBank support…"
              onChange={(event) => setMessage(event.target.value)}
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
          <p className="footnote">
            Fictional bank demo. Each message is routed on its own; the gateway keeps no conversation memory yet.
          </p>
        </div>
      </form>
    </div>
  );
}

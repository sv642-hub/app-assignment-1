import { useCallback, useEffect, useRef, useState } from "react";

import { GatewayError, getHealth, sendChat } from "./api.js";
import ChatView, { DRAFT_KEY, TURNS_KEY, readSavedDraft, readSavedTurns, writeSaved } from "./components/ChatView.jsx";
import HealthView from "./components/HealthView.jsx";
import Sidebar from "./components/Sidebar.jsx";

export const HEALTH_POLL_MS = 5000;
const VIEWS = ["chat", "health"];

function viewFromHash() {
  const view = window.location.hash.replace(/^#\/?/, "");
  return VIEWS.includes(view) ? view : "chat";
}

// A reply that was still pending when the page closed will never arrive.
function restoreTurns() {
  return readSavedTurns().map((turn, id) =>
    turn.status === "pending"
      ? { ...turn, id, status: "error", error: "This message was interrupted by a page refresh. Send it again." }
      : { ...turn, id },
  );
}

export default function App() {
  const [view, setView] = useState(viewFromHash);
  const [draft, setDraft] = useState(readSavedDraft);
  const [turns, setTurns] = useState(restoreTurns);
  const nextTurnId = useRef(turns.length);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // Health drives both the Health screen and the sidebar status, so it is
  // polled here whichever screen is open.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getHealth().then(
        (data) => {
          if (!cancelled) {
            setHealth(data);
            setHealthError(null);
          }
        },
        (error) => {
          if (!cancelled) setHealthError(error.message);
        },
      );
    load();
    const timer = setInterval(load, HEALTH_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    writeSaved(TURNS_KEY, turns);
  }, [turns]);

  useEffect(() => {
    writeSaved(DRAFT_KEY, draft);
  }, [draft]);

  const send = useCallback(async (message) => {
    // Turn IDs are local to this app session and also work over plain HTTP.
    const id = nextTurnId.current++;
    setTurns((current) => [...current, { id, message, status: "pending" }]);
    let update;
    try {
      update = { status: "done", result: await sendChat(message) };
    } catch (error) {
      const text = error instanceof GatewayError ? error.message : "Something went wrong.";
      update = { status: "error", error: text };
    }
    setTurns((current) => current.map((t) => (t.id === id ? { ...t, ...update } : t)));
  }, []);

  const go = (next) => {
    window.location.hash = next === "chat" ? "" : `/${next}`;
    setView(next);
  };

  return (
    <div className="app">
      <Sidebar
        view={view}
        onNavigate={go}
        onNewChat={() => {
          setTurns([]);
          setDraft("");
          go("chat");
        }}
        health={health}
        healthError={healthError}
      />
      <main className="main">
        {view === "chat" ? (
          <ChatView turns={turns} message={draft} setMessage={setDraft} onSend={send} />
        ) : (
          <HealthView health={health} error={healthError} />
        )}
      </main>
    </div>
  );
}

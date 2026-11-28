import { useEffect, useRef, useState } from "react";

import Logo from "./Logo.jsx";

const SCREENS = [
  { id: "chat", label: "Chat" },
  { id: "health", label: "Health" },
];

// Behind the Settings button at the bottom of the sidebar.
const SETTINGS_SCREENS = [
  { id: "jev", label: "Jev" },
  { id: "metrics", label: "Metrics" },
];

// Jev and Qwen are each mock or real (health.jev_mode and health.mode).
function modeText(health) {
  const jev = health?.jev_mode === "real";
  const qwen = health?.mode === "real";
  if (jev && qwen) return "Real Jev (TypeSafe) and real Qwen (vLLM)";
  if (jev) return "Real Jev (TypeSafe); Qwen is simulated";
  if (qwen) return "Real Qwen (vLLM); Jev is simulated";
  return "Mock mode: Jev and Qwen are simulated";
}

// A coloured dot beside the app's name. Pressing it shows the status in words;
// Escape or a press anywhere else closes it.
function GatewayStatus({ status, mode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const button = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onPress = (event) => {
      if (!ref.current.contains(event.target)) setOpen(false);
    };
    const onKey = (event) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      button.current.focus();
    };
    document.addEventListener("pointerdown", onPress);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPress);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="gateway-status" ref={ref}>
      <button
        ref={button}
        type="button"
        className="status-button"
        aria-label={status.text}
        aria-expanded={open}
        title={status.text}
        onClick={() => setOpen(!open)}
      >
        <span className={`dot dot-${status.tone}`} aria-hidden="true" />
      </button>
      {open && (
        <div className="status-popover">
          <strong>{status.text}</strong>
          <span className="muted">{mode}</span>
        </div>
      )}
    </div>
  );
}

export default function Sidebar({ view, onNavigate, onNewChat, health, healthError }) {
  const degraded = health?.status === "degraded";
  const status = healthError
    ? { tone: "bad", text: "Gateway unreachable" }
    : !health
      ? { tone: "idle", text: "Checking gateway…" }
      : degraded
        ? { tone: "bad", text: "Gateway degraded" }
        : { tone: "good", text: "Gateway healthy" };

  const inSettings = SETTINGS_SCREENS.some((screen) => screen.id === view);
  const [settingsOpen, setSettingsOpen] = useState(inSettings);
  // Opens on a Settings screen, also from a direct link, and closes on leaving.
  useEffect(() => setSettingsOpen(inSettings), [view, inSettings]);

  const navItem = (screen) => (
    <button
      key={screen.id}
      type="button"
      className="nav-item"
      aria-current={view === screen.id ? "page" : undefined}
      onClick={() => onNavigate(screen.id)}
    >
      {screen.label}
      {screen.id === "health" && (degraded || healthError) && (
        <span className="dot dot-bad" role="img" aria-label="needs attention" />
      )}
    </button>
  );

  return (
    <aside className="sidebar" aria-label="Sidebar">
      <div className="brand">
        <Logo />
        <span>Smart AI Gateway</span>
        <GatewayStatus status={status} mode={modeText(health)} />
      </div>

      <button type="button" className="new-chat" onClick={onNewChat}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        New chat
      </button>

      <nav className="nav" aria-label="Screens">
        {SCREENS.map(navItem)}
      </nav>

      <p className="muted sidebar-note">
        Jev normally picks the route. Open Show details on a reply to see which router handled it.
      </p>

      <div className="settings">
        {settingsOpen && (
          <nav id="settings-panel" className="nav settings-panel" aria-label="Settings screens">
            {SETTINGS_SCREENS.map(navItem)}
          </nav>
        )}
        <button
          type="button"
          className={`nav-item settings-toggle${inSettings ? " settings-active" : ""}`}
          aria-expanded={settingsOpen}
          aria-controls="settings-panel"
          onClick={() => setSettingsOpen(!settingsOpen)}
        >
          <span className="settings-label">
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
              <circle cx="15" cy="7" r="2" />
              <circle cx="9" cy="17" r="2" />
            </svg>
            Settings
          </span>
          <span aria-hidden="true">{settingsOpen ? "▾" : "▸"}</span>
        </button>
      </div>
    </aside>
  );
}

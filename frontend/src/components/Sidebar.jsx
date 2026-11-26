import Logo from "./Logo.jsx";

const SCREENS = [
  { id: "chat", label: "Chat" },
  { id: "health", label: "Health" },
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

export default function Sidebar({ view, onNavigate, onNewChat, health, healthError }) {
  const degraded = health?.status === "degraded";
  const status = healthError
    ? { tone: "bad", text: "Gateway unreachable" }
    : !health
      ? { tone: "idle", text: "Checking gateway…" }
      : degraded
        ? { tone: "bad", text: "Gateway degraded" }
        : { tone: "good", text: "Gateway healthy" };

  return (
    <aside className="sidebar" aria-label="Sidebar">
      <div className="brand">
        <Logo />
        <span>Smart AI Gateway</span>
      </div>

      <button type="button" className="new-chat" onClick={onNewChat}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
        New chat
      </button>

      <nav className="nav" aria-label="Screens">
        {SCREENS.map((screen) => (
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
        ))}
      </nav>

      <p className="muted sidebar-note">
        Jev normally picks the route. Open Show details on a reply to see which router handled it.
      </p>

      <div className="status-card">
        <span className="status-line">
          <span className={`dot dot-${status.tone}`} aria-hidden="true" />
          {status.text}
        </span>
        <span className="muted">
          {modeText(health)}
        </span>
      </div>
    </aside>
  );
}

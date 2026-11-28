import { JevCard, SemanticCard } from "./HealthView.jsx";

export default function JevView({ health, error }) {
  const real = health?.jev_mode === "real";
  const mixed = health && health.jev_mode !== health.mode;
  return (
    <div className="health health-compact">
      <div className="health-column">
        <header className="health-head">
          <h1>Jev</h1>
          <p className="muted small">Updates every 5 s.</p>
        </header>
        {error && (
          <div className="banner banner-bad" role="alert">
            <div>
              <strong>Jev status unavailable</strong>
              <span>{error} Previous readings are hidden until the gateway responds.</span>
            </div>
          </div>
        )}
        {!health && !error && <p role="status">Loading Jev status…</p>}
        {health && !error && (
          <>
            <section className="card summary-line" aria-label="Jev connection">
              <h2>{real ? "Real Jev · TypeSafe" : "Simulated Jev"}</h2>
              <span className="muted small">
                {real
                  ? "Asks TypeSafe for a route and an intent in one call. This setting alone does not prove a successful live call."
                  : "A local mock answers. These readings do not measure the real Jev service."}
              </span>
              {mixed && <span className="muted small">Jev and Qwen use different modes. Their intent answers are not compared by the semantic monitor.</span>}
            </section>
            {health.jev_breaker && <JevCard breaker={health.jev_breaker} usage={health.jev_usage} />}
            {health.semantic && health.semantic_rules && (
              <SemanticCard semantic={health.semantic} rules={health.semantic_rules} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

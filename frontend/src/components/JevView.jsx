import { JevCard, SemanticCard } from "./HealthView.jsx";

export default function JevView({ health, error }) {
  const real = health?.jev_mode === "real";
  const mixed = health && health.jev_mode !== health.mode;
  return (
    <div className="health">
      <div className="health-column">
        <header className="health-head">
          <h1>Jev</h1>
          <p className="muted small">Routing, intent agreement and provider usage. Updates every 5 s.</p>
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
            <section className="card" aria-label="Jev connection">
              <h2>{real ? "Real Jev · TypeSafe" : "Simulated Jev"}</h2>
              <p>
                {real
                  ? "The gateway is configured to ask TypeSafe for a route and an intent in one call. This setting alone does not prove a successful live call."
                  : "Route and intent answers come from the local mock. These readings do not measure the real Jev service."}
              </p>
              <p className="muted small">
                If Jev fails or its circuit breaker opens, keyword rules choose the route.
                Open Show details on a Chat reply to inspect the router used for that request.
              </p>
              {mixed && <p className="muted small">Jev and Qwen use different modes. Their intent answers are not compared by the semantic monitor.</p>}
            </section>
            {health.jev_breaker && <JevCard breaker={health.jev_breaker} usage={health.jev_usage} />}
            {health.semantic && health.semantic_rules && (
              <SemanticCard semantic={health.semantic} rules={health.semantic_rules} />
            )}
            <p className="muted small">
              Agreement with Jev is a monitoring signal, not a measured accuracy score.
              Usage counters restart with the gateway. Cost is an estimate when provider prices are configured.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

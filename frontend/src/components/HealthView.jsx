const ACTION_LABELS = {
  disabled: "disabled",
  "re-enabled": "re-enabled",
  kept_last_backend: "kept last backend",
};

function percent(value) {
  return `${Math.round(value * 100)}%`;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Combine each backend's config with its monitor entry, and work out which
// model is covering for which.
export function describeModels(health) {
  const monitors = Object.fromEntries(health.monitor.map((m) => [m.name, m]));
  const models = health.backends.map((b) => {
    const m = monitors[b.name] ?? { samples: 0, format_failures: 0, failure_rate: null };
    const usable = b.available && !m.disabled_by_controller;
    return { ...b, ...m, usable };
  });
  return models.map((model) => {
    const covering = models.filter((other) => other !== model && !other.usable).map((other) => other.route);
    return { ...model, covering: model.usable ? covering : [] };
  });
}

function bannerFor(health, models) {
  if (health.status === "ok") {
    return { tone: "good", title: "All systems normal", text: "Both models are serving their routes. No controller action is in effect." };
  }
  const down = models.filter((m) => !m.usable);
  if (down.length === models.length) {
    return { tone: "bad", title: "No model available", text: "Chat requests return an error until a model comes back." };
  }
  const up = models.find((m) => m.usable);
  const text = down
    .map((m) =>
      m.disabled_by_controller
        ? `${m.name} was disabled by the controller. ${m.route[0].toUpperCase() + m.route.slice(1)} requests are going to ${up.name} until the cooldown ends.`
        : `${m.name} is marked unavailable in the config. Its requests are going to ${up.name}.`,
    )
    .join(" ");
  return { tone: "bad", title: "Degraded", text };
}

function ModelCard({ model, threshold }) {
  const rate = model.failure_rate ?? 0;
  const status = model.disabled_by_controller ? "Disabled" : model.available ? "Serving" : "Unavailable";
  const extra = model.disabled_by_controller
    ? ["Re-enables in", `${Math.ceil(model.disabled_for_seconds ?? 0)} s`]
    : model.covering.length
      ? ["Also serving", `${model.covering.join(", ")} (rerouted)`]
      : ["Cooldown", "none"];
  return (
    <section className="card model-card" aria-label={model.name}>
      <div className="model-head">
        <div>
          <h2 className="mono">{model.name}</h2>
          <span className="muted small">Serves {model.route} requests</span>
        </div>
        <span className={`pill ${model.usable ? "pill-good" : "pill-bad"}`}>{status}</span>
      </div>
      <div className="meter-block">
        <div className="meter-label">
          <span className="muted">Format failures (last {model.samples})</span>
          <strong>
            {model.failure_rate === null ? "no data yet" : `${percent(rate)} (${model.format_failures} of ${model.samples})`}
          </strong>
        </div>
        <div className="meter" aria-hidden="true">
          <div className={`meter-fill ${rate >= threshold ? "meter-bad" : ""}`} style={{ width: `${Math.max(rate * 100, 2)}%` }} />
          <div className="meter-mark" style={{ left: `${threshold * 100}%` }} />
        </div>
        <span className="muted small">Marker shows the {percent(threshold)} threshold</span>
      </div>
      <div className="stat-grid">
        <div className="stat">
          <div className="muted">Samples</div>
          <strong>{model.samples}</strong>
        </div>
        <div className="stat">
          <div className="muted">{extra[0]}</div>
          <strong>{extra[1]}</strong>
        </div>
      </div>
    </section>
  );
}

export default function HealthView({ health, error }) {
  return (
    <div className="health">
      <div className="health-column">
        <header className="health-head">
          <h1>Health</h1>
          <p className="muted small">Live state of the models and the controller. Updates every 5 s.</p>
        </header>

        {error && (
          <div className="banner banner-bad" role="alert">
            <span className="dot dot-bad" aria-hidden="true" />
            <div>
              <strong>Gateway unreachable</strong>
              <span>{error}</span>
            </div>
          </div>
        )}
        {!health && !error && <p className="muted">Loading health…</p>}
        {health && <HealthBody health={health} />}
      </div>
    </div>
  );
}

function HealthBody({ health }) {
  const models = describeModels(health);
  const banner = bannerFor(health, models);
  const rules = health.controller;
  const log = [...health.interventions].reverse();

  return (
    <>
      <div className={`banner banner-${banner.tone}`} role="status">
        <span className={`dot dot-${banner.tone}`} aria-hidden="true" />
        <div>
          <strong>{banner.title}</strong>
          <span>{banner.text}</span>
        </div>
      </div>

      <div className="model-grid">
        {models.map((model) => (
          <ModelCard key={model.name} model={model} threshold={rules.failure_threshold} />
        ))}
      </div>

      <section className="card">
        <h2>Interventions</h2>
        {log.length === 0 ? (
          <p className="muted small">No interventions since the gateway started.</p>
        ) : (
          <div className="table-scroll">
            <table className="log">
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Model</th>
                  <th scope="col">Action</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {log.map((entry) => (
                  <tr key={`${entry.at}-${entry.backend}-${entry.action}`}>
                    <td className="mono">{formatTime(entry.at)}</td>
                    <td className="mono">{entry.backend}</td>
                    <td>
                      <span className={`pill action-${entry.action}`}>{ACTION_LABELS[entry.action] ?? entry.action}</span>
                    </td>
                    <td>{entry.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Controller rules</h2>
        <p className="muted small">
          A model is taken out of service when its format-failure rate crosses the threshold. The last working model is never disabled.
        </p>
        <div className="rules">
          <div className="stat">
            <div className="muted">Window</div>
            <strong>last {rules.window} replies</strong>
          </div>
          <div className="stat">
            <div className="muted">Minimum samples</div>
            <strong>{rules.min_samples}</strong>
          </div>
          <div className="stat">
            <div className="muted">Failure threshold</div>
            <strong>{percent(rules.failure_threshold)}</strong>
          </div>
          <div className="stat">
            <div className="muted">Cooldown</div>
            <strong>{rules.cooldown_seconds} s</strong>
          </div>
        </div>
      </section>
    </>
  );
}

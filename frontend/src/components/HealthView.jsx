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
    const m = monitors[b.name] ?? { samples: 0, format_failures: 0, call_errors: 0, failure_rate: null };
    const usable = b.available && !m.disabled_by_controller;
    return { ...b, ...m, usable };
  });
  return models.map((model) => {
    const covering = models.filter((other) => other !== model && !other.usable).map((other) => other.route);
    return { ...model, covering: model.usable ? covering : [] };
  });
}

const BREAKER_PILLS = {
  closed: { tone: "good", text: "Calling Jev" },
  open: { tone: "bad", text: "Skipped" },
  // After the cool-off, the breaker waits for the next message, which can be
  // hours on a quiet gateway. Nothing is failing meanwhile.
  half_open: { tone: "neutral", text: "Waiting for next message" },
};

function breakerText(breaker) {
  if (!breaker || breaker.state === "closed") return null;
  if (breaker.state === "open") {
    return `Jev failed ${breaker.consecutive_failures} calls in a row, so the gateway is skipping it. Keyword rules route every message until the ${breaker.cooldown_seconds} s cool-off ends.`;
  }
  return "The Jev cool-off has ended. The next message tries Jev again. Until a call succeeds, keyword rules route messages.";
}

function bannerFor(health, models) {
  // `status` is about the models only. A skipped Jev is a neutral note: the
  // models still answer, and keyword rules pick the route.
  const jev = breakerText(health.jev_breaker);
  if (health.status === "ok") {
    if (jev) return { tone: "note", title: "Keyword rules are routing while Jev is skipped", text: `Both models are serving. ${jev}` };
    return { tone: "good", title: "All systems normal", text: "Both models are serving their routes. No controller action is in effect." };
  }
  const down = models.filter((m) => !m.usable);
  if (down.length === models.length) {
    return { tone: "bad", title: "No model available", text: "Chat requests return an error until a model comes back." };
  }
  const up = models.find((m) => m.usable);
  const parts = down.map((m) =>
    m.disabled_by_controller
      ? `${m.name} was disabled by the controller. ${m.route[0].toUpperCase() + m.route.slice(1)} requests are going to ${up.name} until the cooldown ends.`
      : `${m.name} is marked unavailable in the config. Its requests are going to ${up.name}.`,
  );
  if (jev) parts.push(jev);
  return { tone: "bad", title: "Degraded", text: parts.join(" ") };
}

function JevCard({ breaker }) {
  const pill = BREAKER_PILLS[breaker.state];
  return (
    <section className="card model-card" aria-label="Jev router">
      {/* The subtitle sits under the row, not beside the pill: the half-open
          pill is long enough to squeeze it onto three lines. */}
      <div>
        <div className="model-head">
          <h2>Jev router</h2>
          <span className={`pill pill-${pill.tone}`}>{pill.text}</span>
        </div>
        <span className="muted small">Picks the route for each message</span>
      </div>
      <p className="muted small">
        After {breaker.failure_limit} failed Jev calls in a row, the gateway skips Jev for {breaker.cooldown_seconds} s and routes by keyword rules. Then one trial call
        decides whether Jev comes back. A wrong answer from Jev does not count as a failed call.
      </p>
      <div className="stat-grid">
        <div className="stat">
          <div className="muted">Failed calls in a row</div>
          <strong>
            {breaker.consecutive_failures} of {breaker.failure_limit}
          </strong>
        </div>
        <div className="stat">
          <div className="muted">Cool-off</div>
          <strong>{breaker.cooldown_seconds} s</strong>
        </div>
      </div>
    </section>
  );
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
          <span className="muted">Failures (last {model.samples})</span>
          <strong>
            {model.failure_rate === null ? "no data yet" : `${percent(rate)} (${model.format_failures + model.call_errors} of ${model.samples})`}
          </strong>
        </div>
        <div className="meter" aria-hidden="true">
          <div className={`meter-fill ${rate >= threshold ? "meter-bad" : ""}`} style={{ width: `${Math.max(rate * 100, 2)}%` }} />
          <div className="meter-mark" style={{ left: `${threshold * 100}%` }} />
        </div>
        <span className="muted small">Marker shows the {percent(threshold)} threshold</span>
        {model.call_errors > 0 && (
          <span className="muted small">
            Failed calls: {model.call_errors}. Format failures: {model.format_failures}.
          </span>
        )}
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
        {health && !error && <HealthBody health={health} />}
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
        {health.jev_breaker && <JevCard breaker={health.jev_breaker} />}
      </div>

      <section className="card">
        <h2>Recent completed requests</h2>
        <p className="muted small">Up to 20 retained requests that got a reply, newest first, including replies that failed the format check. Requests that got no reply (HTTP 503) are not listed. Kept across gateway restarts when it uses Postgres, as in Compose.</p>
        {!health.recent_requests?.length ? (
          <p className="muted small">No retained completed requests.</p>
        ) : (
          <div className="table-scroll">
            <table className="log" aria-label="Recent completed requests">
              <thead>
                <tr>
                  <th scope="col">Completed</th>
                  <th scope="col">Route</th>
                  <th scope="col">Actual model</th>
                  <th scope="col">Fallback</th>
                  <th scope="col">Format check</th>
                </tr>
              </thead>
              <tbody>
                {health.recent_requests.map((r, i) => (
                  <tr key={`${r.completed_at}-${i}`}>
                    <td className="mono">{new Date(r.completed_at).toLocaleString()}</td>
                    <td>{r.route}</td>
                    <td className="mono">{r.model}</td>
                    <td>{r.fallback_reason ?? "none"}</td>
                    <td>{r.format_check}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card">
        <h2>Interventions</h2>
        <p className="muted small">With Postgres, as in Compose, this log is kept across gateway restarts. A restart puts every model back in service, so an entry may say disabled for a model that is serving now.</p>
        {log.length === 0 ? (
          <p className="muted small">No interventions recorded.</p>
        ) : (
          <div className="table-scroll">
            <table className="log" aria-label="Interventions">
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
        <h2>Usage since gateway startup</h2>
        <p>Mock mode: token usage and estimated cost are unavailable. No model-provider inference calls are made. Infrastructure costs are not measured; these figures cannot establish routing savings.</p>
        <p className="muted small">Totals include fallback attempts and do not expire with the monitoring window. A completed call may still fail the format check. In-flight or cancelled calls can leave attempts greater than completed plus failed calls. Resets on restart.</p>
        <div className="table-scroll">
          <table className="log" aria-label="Usage since gateway startup">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Attempted calls</th>
                <th scope="col">Completed calls</th>
                <th scope="col">Failed calls</th>
              </tr>
            </thead>
            <tbody>
              {(health.usage ?? []).map((u) => (
                <tr key={u.name}>
                  <td className="mono">{u.name}</td>
                  <td>{u.attempted_calls}</td>
                  <td>{u.completed_calls}</td>
                  <td>{u.failed_calls}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Try a simulated failure</h2>
        <p>Normal questions never inject faults. The team runs this as a demo, on a host testers do not use. The demo starts the gateway with the README's chaos override, which corrupts every small-model reply.</p>
        <ol>
          <li>In Chat, send “My card has not arrived” {rules.min_samples} times.</li>
          <li>Open Show details: each reply fails its format check. After {rules.min_samples} failed replies, the controller disables qwen3-1.7b.</li>
          <li>Send it once more: qwen3-4b answers. Health shows the format failures, the disabled small model and an intervention.</li>
          <li>Finish within {rules.cooldown_seconds} s. After the cooldown, the small model is enabled again and fails again.</li>
        </ol>
        <p>This tests format failures and rerouting only. A wrong answer that passes the format check is not detected.</p>
      </section>

      <section className="card">
        <h2>Controller rules</h2>
        <p className="muted small">
          A model is taken out of service when its failure rate (failed format checks and failed calls) crosses the threshold. The last working model is never disabled.
        </p>
        <div className="rules">
          <div className="stat">
            <div className="muted">Window</div>
            <strong>last {rules.window} calls per model</strong>
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

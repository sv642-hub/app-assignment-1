// Grafana runs behind the same proxy, at /grafana, as an anonymous viewer.
// kiosk hides Grafana's own menus; the page keeps the app's sidebar.
export const DASHBOARD_PATH = "/grafana/d/smart-gateway/smart-ai-gateway";

export default function MetricsView() {
  return (
    <div className="metrics">
      <header className="health-head">
        <h1>Metrics</h1>
        <p className="muted small">
          Latency, traffic, errors, reply quality, self-healing and Jev usage, from Prometheus and
          Postgres. View only.{" "}
          <a href={`${DASHBOARD_PATH}?orgId=1`} target="_blank" rel="noreferrer">
            Open in a new tab
          </a>
        </p>
      </header>
      <iframe
        className="metrics-frame"
        title="Grafana metrics dashboard"
        src={`${DASHBOARD_PATH}?orgId=1&kiosk&refresh=30s`}
      />
    </div>
  );
}

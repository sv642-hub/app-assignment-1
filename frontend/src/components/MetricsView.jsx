// Grafana runs behind the same proxy, at /grafana, as an anonymous viewer.
// The app embeds the short summary dashboard; kiosk hides Grafana's own menus,
// and the light theme matches the app. The full dashboard opens in a new tab.
export const SUMMARY_PATH = "/grafana/d/smart-gateway-summary/smart-ai-gateway-summary";
export const DASHBOARD_PATH = "/grafana/d/smart-gateway/smart-ai-gateway";

export default function MetricsView() {
  return (
    <div className="metrics">
      <header className="health-head">
        <h1>Metrics</h1>
        <p className="muted small">
          A summary of the last 15 minutes, from Prometheus and Postgres. View only.{" "}
          <a href={`${DASHBOARD_PATH}?orgId=1&theme=light`} target="_blank" rel="noreferrer">
            Open the full dashboard
          </a>
        </p>
      </header>
      <iframe
        className="metrics-frame"
        title="Grafana metrics summary"
        src={`${SUMMARY_PATH}?orgId=1&kiosk&theme=light&refresh=30s`}
      />
    </div>
  );
}

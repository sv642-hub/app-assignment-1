import { useState } from "react";

import { changeRelease } from "../api.js";
import { ReleasesCard } from "./HealthView.jsx";

function ReleaseForm({ release }) {
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const versions = release.versions.filter((version) => version !== release.version);
  const [choice, setChoice] = useState("");

  async function submit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const token = data.get("token").trim();
    if (!token || pending) return;
    const rollback = choice.startsWith("rollback:");
    const version = choice.slice(choice.indexOf(":") + 1);
    if (rollback ? version !== release.rollback_target : !versions.includes(version)) return;
    const reason = data.get("reason").trim() || "operator request";
    form.elements.namedItem("token").value = "";
    setPending(true);
    setFeedback(null);
    try {
      const updated = await changeRelease(release.name, rollback ? "rollback" : "release",
        rollback ? { reason } : { version, reason }, token);
      setFeedback({ message: `Gateway confirmed ${updated.name} is serving ${updated.version}.` });
      setChoice("");
    } catch (error) {
      setFeedback({ error: true, message: error.message });
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="card release-form" onSubmit={submit} aria-label={`Change ${release.name} release`}>
      <h2>{release.name}</h2>
      <fieldset disabled={pending}>
        <label>Release change
          <select required name="change" value={choice} onChange={(event) => { setChoice(event.target.value); setFeedback(null); }}>
            <option value="">Choose a change</option>
            {versions.map((version) => <option key={version} value={`deploy:${version}`}>Deploy {version}</option>)}
            {release.rollback_target && <option value={`rollback:${release.rollback_target}`}>Roll back to {release.rollback_target}</option>}
          </select>
        </label>
        <label>Reason (public release history)
          <input name="reason" maxLength={200} placeholder="Optional. Omit customer details." />
        </label>
        <label>Operator token
          <input required type="password" name="token" autoComplete="off" pattern={".*\\S.*"} />
        </label>
        <button type="submit" disabled={!choice}>{pending ? "Submitting…" : "Apply change"}</button>
      </fieldset>
      {feedback && <p role={feedback.error ? "alert" : "status"}>{feedback.message}</p>}
    </form>
  );
}

export default function DeploymentsView({ health, error }) {
  const releases = health?.releases ?? [];
  return (
    <div className="health"><div className="health-column">
      <header className="health-head"><h1>Deployments</h1></header>
      {error ? <p role="alert">Release status unavailable. {error}</p>
        : !health ? <p role="status">Loading deployments…</p>
        : releases.length === 0 ? <p>No release information available.</p>
        : <>
          <ReleasesCard releases={releases} history={health.release_history} />
          {health.mode !== "mock" ? <p>Release controls apply to mock Qwen settings only.</p>
            : !window.isSecureContext ? <p>To change releases, open the gateway over HTTPS or localhost through an SSH tunnel.</p>
            : releases.map((release) => <ReleaseForm key={release.name} release={release} />)}
        </>}
    </div></div>
  );
}

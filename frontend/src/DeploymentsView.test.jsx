import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import App from "./App.jsx";
import DeploymentsView from "./components/DeploymentsView.jsx";
import { fakeGateway, healthyHealth } from "./test/fixtures.js";

const release = { name: "qwen3-1.7b", version: "base", versions: ["base", "v2"], status: "stable", rollback_target: null };
const health = { ...healthyHealth, mode: "mock", releases: [release] };
afterEach(() => vi.unstubAllGlobals());

it("opens Deployments from the sidebar and submits a catalog version", async () => {
  vi.stubGlobal("isSecureContext", true);
  const gateway = fakeGateway({ health });
  const fetch = vi.spyOn(globalThis, "fetch").mockImplementation((url, options) =>
    options?.method === "POST" ? gateway.json({ ...release, version: "v2" }) : gateway.fetch(url, options));
  const user = userEvent.setup();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "Deployments" }));
  expect(window.location.hash).toBe("#/deployments");
  expect(screen.queryByRole("option", { name: /roll back/i })).toBeNull();
  await user.selectOptions(screen.getByLabelText("Release change"), "deploy:v2");
  await user.type(screen.getByLabelText("Operator token"), "demo");
  await user.click(screen.getByRole("button", { name: "Apply change" }));
  expect(fetch).toHaveBeenCalledWith("/v1/operator/models/qwen3-1.7b/release", expect.objectContaining({
    headers: { Authorization: "Bearer demo", "Content-Type": "application/json" },
    body: JSON.stringify({ version: "v2", reason: "operator request" }),
  }));
  expect(await screen.findByText(/Gateway confirmed.*v2/)).toBeInTheDocument();
  expect(screen.getByLabelText("Operator token")).toHaveValue("");
});

it("uses the rollback endpoint and reports the gateway's refusal", async () => {
  vi.stubGlobal("isSecureContext", true);
  const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "Missing or wrong operator token" }), { status: 401 }));
  const user = userEvent.setup();
  render(<DeploymentsView health={{ ...health, releases: [{ ...release, version: "v2", rollback_target: "base" }] }} />);
  await user.selectOptions(screen.getByLabelText("Release change"), "rollback:base");
  await user.type(screen.getByLabelText(/Reason/), "restore baseline");
  await user.type(screen.getByLabelText("Operator token"), "demo");
  await user.click(screen.getByRole("button", { name: "Apply change" }));
  expect(fetch).toHaveBeenCalledWith("/v1/operator/models/qwen3-1.7b/rollback", expect.objectContaining({ body: JSON.stringify({ reason: "restore baseline" }) }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Missing or wrong operator token");
});

it("keeps public HTTP and real Qwen read-only and hides stale releases on error", () => {
  vi.stubGlobal("isSecureContext", false);
  const view = render(<DeploymentsView health={health} />);
  expect(screen.getByRole("table", { name: "Releases" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Operator token")).toBeNull();
  vi.stubGlobal("isSecureContext", true);
  view.rerender(<DeploymentsView health={{ ...health, mode: "real" }} />);
  expect(screen.queryByLabelText("Operator token")).toBeNull();
  view.rerender(<DeploymentsView health={health} error="Connection failed" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Connection failed");
  expect(screen.queryByRole("table")).toBeNull();
});

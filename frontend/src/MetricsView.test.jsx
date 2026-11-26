import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "./App.jsx";
import { fakeGateway } from "./test/fixtures.js";

describe("Metrics screen", () => {
  it("embeds the light Grafana summary from the same origin", async () => {
    window.location.hash = "";
    vi.spyOn(globalThis, "fetch").mockImplementation(fakeGateway().fetch);
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.click(screen.getByRole("button", { name: "Metrics", exact: true }));
    const frame = screen.getByTitle("Grafana metrics summary");
    expect(frame.getAttribute("src")).toMatch(/^\/grafana\/d\/smart-gateway-summary\//);
    expect(frame.getAttribute("src")).toContain("kiosk");
    expect(frame.getAttribute("src")).toContain("theme=light");
    expect(window.location.hash).toBe("#/metrics");
    const full = screen.getByRole("link", { name: "Open the full dashboard" });
    expect(full.getAttribute("href")).toMatch(/^\/grafana\/d\/smart-gateway\/.*theme=light/);
  });
});

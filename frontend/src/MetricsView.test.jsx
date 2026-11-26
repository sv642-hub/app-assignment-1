import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "./App.jsx";
import { fakeGateway } from "./test/fixtures.js";

describe("Metrics screen", () => {
  it("embeds the Grafana dashboard from the same origin", async () => {
    window.location.hash = "";
    vi.spyOn(globalThis, "fetch").mockImplementation(fakeGateway().fetch);
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Metrics", exact: true }));
    const frame = screen.getByTitle("Grafana metrics dashboard");
    expect(frame.getAttribute("src")).toMatch(/^\/grafana\/d\/smart-gateway\//);
    expect(frame.getAttribute("src")).toContain("kiosk");
    expect(window.location.hash).toBe("#/metrics");
    expect(screen.getByRole("link", { name: "Open in a new tab" })).toHaveAttribute("href", expect.stringMatching(/^\/grafana\//));
  });
});

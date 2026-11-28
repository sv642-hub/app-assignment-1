import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App, { HEALTH_POLL_MS } from "./App.jsx";
import { fakeGateway, healthyHealth, jevOutageHealth } from "./test/fixtures.js";

describe("Jev screen", () => {
  it("keeps the chat draft when navigating to Jev and back", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(fakeGateway().fetch);
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText("Message"), "My card has not arrived");
    await user.click(screen.getByRole("button", { name: "Settings" }));
    await user.click(screen.getByRole("button", { name: "Jev", exact: true }));
    expect(await screen.findByRole("heading", { name: "Simulated Jev" })).toBeInTheDocument();
    expect(window.location.hash).toBe("#/jev");
    expect(screen.getByRole("button", { name: "Jev", exact: true })).toHaveAttribute("aria-current", "page");
    await user.click(screen.getByRole("button", { name: "Chat", exact: true }));
    expect(screen.getByLabelText("Message")).toHaveValue("My card has not arrived");
  });

  it("opens a direct link and distinguishes real configuration from live evidence", async () => {
    window.location.hash = "/jev";
    const health = { ...healthyHealth, jev_mode: "real", router: "jev", jev_usage: { ...healthyHealth.jev_usage, mocked: false } };
    vi.spyOn(globalThis, "fetch").mockImplementation(fakeGateway({ health }).fetch);
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Real Jev · TypeSafe" })).toBeInTheDocument();
    expect(screen.getByText(/setting alone does not prove a successful live call/)).toBeInTheDocument();
    expect(screen.getByText(/Jev and Qwen use different modes/)).toBeInTheDocument();
    expect(screen.getByText("price not set")).toBeInTheDocument();
  });

  it("hides stale readings on a poll failure and restores them after recovery", async () => {
    vi.useFakeTimers();
    try {
      window.location.hash = "/jev";
      const gateway = fakeGateway();
      vi.spyOn(globalThis, "fetch")
        .mockImplementationOnce(() => gateway.json(healthyHealth))
        .mockRejectedValueOnce(new Error("offline"))
        .mockImplementation(() => gateway.json(jevOutageHealth));
      render(<App />);
      await act(async () => {});
      expect(screen.getByText("Calling Jev")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS); });
      expect(screen.getByRole("alert")).toHaveTextContent("Previous readings are hidden");
      expect(screen.queryByText("Calling Jev")).not.toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(HEALTH_POLL_MS); });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(screen.getByText("Skipped")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });
});

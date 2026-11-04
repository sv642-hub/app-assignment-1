import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App, { HEALTH_POLL_MS } from "./App.jsx";
import { degradedHealth, fakeGateway, healthyHealth, outageHealth } from "./test/fixtures.js";

function renderWith(gateway) {
  vi.spyOn(globalThis, "fetch").mockImplementation(gateway.fetch);
  const user = userEvent.setup();
  render(<App />);
  return user;
}

describe("Chat", () => {
  it("sends multiple turns when randomUUID is unavailable over HTTP", async () => {
    vi.stubGlobal("crypto", {});
    try {
      const gateway = fakeGateway();
      const user = renderWith(gateway);
      await user.type(screen.getByLabelText("Message"), "I lost my card{Enter}");
      expect(await screen.findByText(/Freeze your card in the fictional app/)).toBeInTheDocument();
      await user.type(screen.getByLabelText("Message"), "Please help with my card{Enter}");
      expect(await screen.findAllByText(/Freeze your card in the fictional app/)).toHaveLength(2);
      expect(gateway.sent).toEqual([
        { message: "I lost my card" },
        { message: "Please help with my card" },
      ]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("sends an example and shows the reply with only its route", async () => {
    const gateway = fakeGateway();
    const user = renderWith(gateway);

    await user.click(screen.getByRole("button", { name: "I lost my card" }));

    expect(await screen.findByText(/Freeze your card in the fictional app/)).toBeInTheDocument();
    expect(gateway.sent).toEqual([{ message: "I lost my card" }]);
    expect(screen.getByText("complex")).toHaveClass("route-complex");
    // The model and timings are internal: not shown to the customer.
    expect(screen.queryByText(/qwen3/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ms\b/)).not.toBeInTheDocument();
  });

  it("opens the details with the policy card and trace", async () => {
    const user = renderWith(fakeGateway());
    await user.type(screen.getByLabelText("Message"), "I lost my card{Enter}");

    await user.click(await screen.findByRole("button", { name: "Show details" }));

    expect(screen.getByText("Grounded in policy card")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("lost_or_stolen_card")).toBeInTheDocument();
    expect(within(table).getByText("262a55b9-3baa-4e0b-8abc-58fb91432cf2")).toBeInTheDocument();
    expect(within(table).queryByText(/qwen3/)).not.toBeInTheDocument();
  });

  it("explains a 503 instead of failing silently", async () => {
    const gateway = fakeGateway({ chat: () => gateway.json({ detail: "No mock backend is available" }, 503) });
    const user = renderWith(gateway);

    await user.type(screen.getByLabelText("Message"), "hello{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("No model is available right now");
  });

  it("does not send a blank message", async () => {
    const gateway = fakeGateway();
    const user = renderWith(gateway);

    await user.type(screen.getByLabelText("Message"), "   ");

    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(gateway.sent).toEqual([]);
  });
});

describe("Health", () => {
  it("hides stale model status after a failed poll and restores it on recovery", async () => {
    const interval = vi.spyOn(globalThis, "setInterval");
    const gateway = fakeGateway();
    const user = renderWith(gateway);
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText("All systems normal")).toBeInTheDocument();
    const poll = interval.mock.calls.find(([, delay]) => delay === HEALTH_POLL_MS)[0];

    globalThis.fetch.mockRejectedValueOnce(new Error("Connection failed"));
    await act(async () => { await poll(); });
    expect(screen.getByRole("alert")).toHaveTextContent("Gateway unreachable");
    expect(screen.queryByText("All systems normal")).not.toBeInTheDocument();
    expect(screen.queryByText("Serving")).not.toBeInTheDocument();
    expect(screen.queryByText("Gateway healthy")).not.toBeInTheDocument();

    await act(async () => { await poll(); });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("All systems normal")).toBeInTheDocument();
    expect(screen.getAllByText("Serving")).toHaveLength(2);
    expect(screen.getByText("Gateway healthy")).toBeInTheDocument();
  });

  it("shows a healthy gateway", async () => {
    const user = renderWith(fakeGateway({ health: healthyHealth }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    expect(await screen.findByText("All systems normal")).toBeInTheDocument();
    expect(screen.getAllByText("Serving")).toHaveLength(2);
    expect(screen.getByText("No interventions since the gateway started.")).toBeInTheDocument();
    expect(screen.getByText("last 20 calls per model")).toBeInTheDocument();
    expect(window.location.hash).toBe("#/health");
  });

  it("explains a model disabled by the controller", async () => {
    const user = renderWith(fakeGateway({ health: degradedHealth }));

    await user.click(screen.getByRole("button", { name: /Health/ }));

    expect(await screen.findByText("Degraded")).toBeInTheDocument();
    expect(screen.getByText(/qwen3-1.7b was disabled by the controller/)).toBeInTheDocument();

    const small = screen.getByRole("region", { name: "qwen3-1.7b" });
    expect(within(small).getByText("Disabled")).toBeInTheDocument();
    expect(within(small).getByText("42 s")).toBeInTheDocument();
    expect(within(small).getByText("100% (5 of 5)")).toBeInTheDocument();

    const large = screen.getByRole("region", { name: "qwen3-4b" });
    expect(within(large).getByText("standard (rerouted)")).toBeInTheDocument();

    // Newest intervention first.
    const rows = within(screen.getByRole("table", { name: "Interventions" })).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("100% over last 5");
    expect(screen.getByText("Gateway degraded")).toBeInTheDocument();
  });

  it("counts failed calls as failures and explains the mix", async () => {
    const user = renderWith(fakeGateway({ health: outageHealth }));

    await user.click(screen.getByRole("button", { name: /Health/ }));

    const small = await screen.findByRole("region", { name: "qwen3-1.7b" });
    expect(within(small).getByText("100% (5 of 5)")).toBeInTheDocument();
    expect(within(small).getByText("Failed calls: 4. Format failures: 1.")).toBeInTheDocument();
    const large = screen.getByRole("region", { name: "qwen3-4b" });
    expect(within(large).queryByText(/Failed calls/)).not.toBeInTheDocument();
  });

  it("opens on the Health screen from its link", async () => {
    window.location.hash = "#/health";
    renderWith(fakeGateway());

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument();
  });
});


describe("Issue 30", () => {
  it("explains recent completed requests on Health", async () => {
    const user = renderWith(fakeGateway());
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByRole("heading", { name: "Recent completed requests" })).toBeInTheDocument();
    expect(screen.getByText(/Actual model/)).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: "Recent completed requests" })).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent(/qwen3-1\.7b.*none.*failed/);
    expect(rows[2]).toHaveTextContent(/qwen3-4b.*selected_backend_error.*passed/);
  });
});

describe("Issue 31", () => {
  it("explains usage since gateway startup on Health", async () => {
    const user = renderWith(fakeGateway());
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByRole("heading", { name: "Usage since gateway startup" })).toBeInTheDocument();
    expect(screen.getByText(/Mock mode: token usage/)).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: "Usage since gateway startup" })).getAllByRole("row");
    expect(rows).toHaveLength(3);
    // Columns: model, attempted, completed, failed.
    expect(within(rows[1]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["qwen3-1.7b", "7", "2", "5"]);
    expect(within(rows[2]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["qwen3-4b", "9", "9", "0"]);
  });
});

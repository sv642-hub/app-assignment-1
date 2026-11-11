import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App, { HEALTH_POLL_MS } from "./App.jsx";
import {
  degradedHealth,
  fakeGateway,
  healthyHealth,
  jevOutageHealth,
  labelSwapHealth,
  lowCoverageHealth,
  outageHealth,
} from "./test/fixtures.js";

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
    expect(screen.getByText("No interventions recorded.")).toBeInTheDocument();
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

  it("shows the Jev breaker calling Jev when it is closed", async () => {
    const user = renderWith(fakeGateway({ health: healthyHealth }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    const jev = await screen.findByRole("region", { name: "Jev router" });
    expect(within(jev).getByText("Calling Jev")).toBeInTheDocument();
    expect(within(jev).getByText("0 of 5")).toBeInTheDocument();
    expect(within(jev).getByText("30 s")).toBeInTheDocument();
  });

  it("shows an open Jev breaker as a neutral note while both models serve", async () => {
    const user = renderWith(fakeGateway({ health: jevOutageHealth }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    const banner = await screen.findByRole("status");
    expect(banner).toHaveClass("banner-note");
    expect(banner).toHaveTextContent("Keyword rules are routing while Jev is skipped");
    expect(banner).toHaveTextContent("Jev failed 5 calls in a row, so the gateway is skipping it.");
    expect(banner).not.toHaveTextContent("Degraded");
    expect(screen.queryByText("All systems normal")).not.toBeInTheDocument();
    expect(screen.getAllByText("Serving")).toHaveLength(2);

    const jev = screen.getByRole("region", { name: "Jev router" });
    expect(within(jev).getByText("Skipped")).toHaveClass("pill-bad");
    expect(within(jev).getByText("5 of 5")).toBeInTheDocument();
    expect(screen.getByText("Gateway healthy")).toBeInTheDocument();
  });

  it("shows a half-open Jev breaker as waiting, not failing", async () => {
    const health = { ...jevOutageHealth, jev_breaker: { ...jevOutageHealth.jev_breaker, state: "half_open" } };
    const user = renderWith(fakeGateway({ health }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    const banner = await screen.findByRole("status");
    expect(banner).toHaveClass("banner-note");
    expect(banner).toHaveTextContent("The next message tries Jev again.");
    const pill = within(screen.getByRole("region", { name: "Jev router" })).getByText("Waiting for next message");
    expect(pill).toHaveClass("pill-neutral");
  });

  it("adds the Jev breaker to a degraded banner when a model is also down", async () => {
    const health = { ...degradedHealth, jev_breaker: jevOutageHealth.jev_breaker };
    const user = renderWith(fakeGateway({ health }));

    await user.click(screen.getByRole("button", { name: /Health/ }));

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Degraded");
    expect(banner).toHaveTextContent("qwen3-1.7b was disabled by the controller.");
    expect(banner).toHaveTextContent("Jev failed 5 calls in a row");
  });

  it("hides the Jev card when the gateway does not report the breaker", async () => {
    const { jev_breaker: _, ...older } = healthyHealth;
    const user = renderWith(fakeGateway({ health: older }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    expect(await screen.findByText("All systems normal")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Jev router" })).not.toBeInTheDocument();
  });

  it("opens on the Health screen from its link", async () => {
    window.location.hash = "#/health";
    renderWith(fakeGateway());

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument();
  });
});


describe("Semantic monitor", () => {
  async function openHealth(health) {
    const user = renderWith(fakeGateway({ health }));
    await user.click(screen.getByRole("button", { name: "Health" }));
    return screen.findByRole("table", { name: "Semantic monitor" });
  }

  function row(table, name) {
    return within(table).getByText(name).closest("tr");
  }

  it("shows rates without flagging them when no baseline is set", async () => {
    const table = await openHealth(healthyHealth);

    const small = row(table, "qwen3-1.7b");
    expect(within(small).getByText("No baseline set")).toHaveClass("pill-neutral");
    expect(small).toHaveTextContent("0% (0 of 12)");
    expect(small).toHaveTextContent("100% (12 of 12)");
    expect(within(row(table, "qwen3-4b")).getByText("Collecting")).toBeInTheDocument();
    expect(row(table, "qwen3-4b")).toHaveTextContent("1 of 3 differ");
    expect(screen.getByText(/No healthy baseline is set yet/)).toBeInTheDocument();
    expect(screen.getByText("All systems normal")).toBeInTheDocument();
  });

  it("flags a model above the baseline in the table and the banner", async () => {
    const table = await openHealth(labelSwapHealth);

    const small = row(table, "qwen3-1.7b");
    expect(within(small).getByText("Above baseline")).toHaveClass("pill-bad");
    expect(small).toHaveTextContent("100% (10 of 10)");
    expect(within(row(table, "qwen3-4b")).getByText("Normal")).toHaveClass("pill-good");

    const banner = screen.getByRole("status");
    expect(banner).toHaveClass("banner-bad");
    expect(banner).toHaveTextContent("Possible wrong intents");
    expect(banner).toHaveTextContent("qwen3-1.7b's intent differs from Jev's on 100% of recent replies (10 of 10), above the 0% baseline.");
    expect(screen.getByText(/Flagged above 15%: the 0% baseline plus a 15% margin/)).toBeInTheDocument();
    // The models still serve, so the gateway's own status is unchanged.
    expect(screen.getByText("Gateway healthy")).toBeInTheDocument();
  });

  it("shows low coverage instead of a healthy rate while Jev is down", async () => {
    const table = await openHealth(lowCoverageHealth);

    const small = row(table, "qwen3-1.7b");
    expect(within(small).getByText("Low coverage")).toHaveClass("pill-neutral");
    expect(small).toHaveTextContent("no data yet");
    expect(small).toHaveTextContent("0% (0 of 12)");
    // An empty window has no rate and no coverage, never 0%.
    expect(within(row(table, "qwen3-4b")).getAllByText("no data yet")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("Keyword rules are routing while Jev is skipped");
  });

  it("adds the semantic alert to a degraded banner", async () => {
    const health = { ...degradedHealth, semantic: labelSwapHealth.semantic, semantic_rules: labelSwapHealth.semantic_rules };
    await openHealth(health);

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Degraded");
    expect(banner).toHaveTextContent("qwen3-1.7b was disabled by the controller.");
    expect(banner).toHaveTextContent("qwen3-1.7b's intent differs from Jev's");
  });

  it("hides the card when the gateway does not report the monitor", async () => {
    const { semantic: _s, semantic_rules: _r, ...older } = healthyHealth;
    const user = renderWith(fakeGateway({ health: older }));

    await user.click(screen.getByRole("button", { name: "Health" }));

    expect(await screen.findByText("All systems normal")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Semantic monitor" })).not.toBeInTheDocument();
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

  it("replaces the recent requests table with a message when there are none", async () => {
    const user = renderWith(fakeGateway({ health: { ...healthyHealth, recent_requests: [] } }));
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText("No retained completed requests.")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Recent completed requests" })).not.toBeInTheDocument();
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

describe("Issue 32", () => {
  it("explains how to try a simulated failure on Health", async () => {
    const user = renderWith(fakeGateway());
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByRole("heading", { name: "Try a simulated failure" })).toBeInTheDocument();
    expect(screen.getByText(/The team runs this as a demo, on a host testers do not use/)).toBeInTheDocument();
    // Numbers come from the controller rules in the fixture.
    expect(screen.getByText(/After 5 failed replies, the controller disables qwen3-1\.7b/)).toBeInTheDocument();
    expect(screen.getByText(/Finish within 60 s/)).toBeInTheDocument();
  });
});

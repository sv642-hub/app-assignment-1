import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App, { HEALTH_POLL_MS } from "./App.jsx";
import {
  degradedHealth,
  fakeGateway,
  healthyHealth,
  operatorHeldHealth,
  jevOutageHealth,
  labelSwapHealth,
  lowCoverageHealth,
  lostCardReply,
  outageHealth,
  semanticActionHealth,
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
    expect(within(jev).getByText("price not set")).toBeInTheDocument();
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

  it("says the controller takes no action while semantic actions are off", async () => {
    await openHealth(labelSwapHealth);

    expect(screen.getByRole("status")).toHaveTextContent("semantic actions are off, so the controller takes no action.");
    expect(screen.getByText(/Semantic actions are off, so it only reports/)).toBeInTheDocument();
  });

  it("shows the controller disabling a model for its disagreement with Jev", async () => {
    await openHealth(semanticActionHealth);

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Degraded");
    expect(banner).toHaveTextContent("qwen3-1.7b was disabled by the controller.");
    expect(banner).toHaveTextContent("If qwen3-4b fails a request, the gateway still sends it to qwen3-1.7b as a last resort.");
    expect(banner).toHaveTextContent("qwen3-1.7b's intent differs from Jev's on 100% of recent replies (10 of 10), above the 0% baseline.");
    expect(banner).not.toHaveTextContent("takes no action");
    expect(banner).not.toHaveTextContent("keeps it in service");
    expect(screen.getByText(/Semantic actions are on: when a model is above the baseline/)).toBeInTheDocument();
    const small = screen.getByRole("region", { name: "qwen3-1.7b" });
    expect(within(small).getByText("Disabled")).toBeInTheDocument();
    expect(within(small).getByText("0% (0 of 10)")).toBeInTheDocument();
    const rows = within(screen.getByRole("table", { name: "Interventions" })).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("intent differs from Jev's on 100% of last 10 scored replies");
  });

  it("says when the controller keeps the last working model in service", async () => {
    const health = {
      ...labelSwapHealth,
      semantic_rules: semanticActionHealth.semantic_rules,
      backends: labelSwapHealth.backends.map((b) => (b.name === "qwen3-4b" ? { ...b, available: false } : b)),
      status: "degraded",
    };
    await openHealth(health);

    expect(screen.getByRole("status")).toHaveTextContent("The controller keeps it in service because the other model is not available.");
  });

  it("says the controller acts on the next reply when the other model is answering", async () => {
    // Both serve, as right after a cooldown: the flagged model is not disabled yet.
    await openHealth({ ...labelSwapHealth, semantic_rules: semanticActionHealth.semantic_rules });

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("The controller takes it out of service on its next reply.");
    expect(banner).not.toHaveTextContent("not available");
  });

  it("says the controller waits while the other model has not answered yet", async () => {
    const monitor = labelSwapHealth.monitor.map((m) => (m.name === "qwen3-4b" ? { ...m, samples: 0, failure_rate: null, answering: false } : m));
    await openHealth({ ...labelSwapHealth, monitor, semantic_rules: semanticActionHealth.semantic_rules });

    expect(screen.getByRole("status")).toHaveTextContent("keeps it in service until qwen3-4b has answered recently without failing.");
  });

  it("follows the gateway's answering flag, not the rounded failure rate", async () => {
    // Threshold 0.67 and 2 failures in 3: the rate rounds to 0.67, but the
    // exact 0.6667 is below the threshold, so the controller acts.
    const monitor = labelSwapHealth.monitor.map((m) => (m.name === "qwen3-4b" ? { ...m, samples: 3, call_errors: 2, failure_rate: 0.67, answering: true } : m));
    const controller = { ...labelSwapHealth.controller, failure_threshold: 0.67 };
    await openHealth({ ...labelSwapHealth, monitor, controller, semantic_rules: semanticActionHealth.semantic_rules });

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("The controller takes it out of service on its next reply.");
    expect(banner).not.toHaveTextContent("keeps it in service");
  });

  it("does not offer a last resort for a model disabled for failures", async () => {
    await openHealth(degradedHealth);

    expect(screen.getByRole("status")).not.toHaveTextContent("last resort");
  });

  it("shows Jev tokens and the estimated cost", async () => {
    const jev_usage = { mocked: false, answered_calls: 3, input_tokens: 6000, output_tokens: 21, estimated_cost_usd: 0.000252 };
    await openHealth({ ...healthyHealth, jev_usage });

    const jev = screen.getByRole("region", { name: "Jev router" });
    expect(within(jev).getByText("6,000 in, 21 out (3 calls)")).toBeInTheDocument();
    expect(within(jev).getByText("$0.0003")).toBeInTheDocument();
    expect(within(jev).queryByText(/Mock Jev reports no tokens/)).not.toBeInTheDocument();
  });

  it.each([
    [0.0000168, "$0.0000168"], // 400 input tokens at $0.042 per million
    [0.00001, "$0.0000100"],
    [0, "$0"],
  ])("never shows a nonzero cost as $0.0000 (%s)", async (cost, text) => {
    const jev_usage = { mocked: false, answered_calls: 1, input_tokens: 400, output_tokens: 7, estimated_cost_usd: cost };
    await openHealth({ ...healthyHealth, jev_usage });
    expect(within(screen.getByRole("region", { name: "Jev router" })).getByText(text)).toBeInTheDocument();
  });

  it("says how many replies were not compared with Jev", async () => {
    const semantic = labelSwapHealth.semantic.map((m) => (m.name === "qwen3-1.7b" ? { ...m, not_compared: 7 } : m));
    await openHealth({ ...labelSwapHealth, semantic });

    expect(screen.getByText(/Not compared since the gateway started: 7 from qwen3-1.7b/)).toBeInTheDocument();
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

  it("lists rerouted replies that had no Jev answer", async () => {
    const semantic = healthyHealth.semantic.map((m) =>
      m.name === "qwen3-4b" ? { ...m, rerouted_agreements: 0, rerouted_disagreements: 0, rerouted_no_signal: 5 } : m,
    );
    const table = await openHealth({ ...healthyHealth, semantic });

    expect(row(table, "qwen3-4b")).toHaveTextContent("5 without a Jev answer");
    expect(row(table, "qwen3-1.7b")).toHaveTextContent("none");
  });

  it("shows a rate just above the threshold with one decimal", async () => {
    // 5 of 33 is 15.2%, above the 15% limit; whole percents would show 15% twice.
    const semantic = labelSwapHealth.semantic.map((m) =>
      m.name === "qwen3-1.7b" ? { ...m, samples: 33, agreements: 28, disagreements: 5, disagreement_rate: 0.152 } : m,
    );
    const table = await openHealth({ ...labelSwapHealth, semantic });

    expect(row(table, "qwen3-1.7b")).toHaveTextContent("15.2% (5 of 33)");
    expect(screen.getByText(/Flagged above 15%:/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("on 15.2% of recent replies (5 of 33)");
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

  it.each([
    ["mock", "real", "Real Qwen (vLLM); Jev is simulated", /Jev is a mock\. The Qwen models run on vLLM/],
    ["real", "real", "Real Jev (TypeSafe) and real Qwen (vLLM)", /Jev calls go to TypeSafe.*The Qwen models run on vLLM/],
  ])("names real Qwen when jev_mode is %s and mode is %s", async (jevMode, mode, sidebar, note) => {
    const health = { ...healthyHealth, jev_mode: jevMode, mode };
    const user = renderWith(fakeGateway({ health }));
    expect(await screen.findByText(sidebar)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText(note)).toBeInTheDocument();
    expect(screen.queryByText(/Mock mode: token usage/)).not.toBeInTheDocument();
  });

  it("says Jev calls go to TypeSafe when Jev is real", async () => {
    const health = { ...healthyHealth, jev_mode: "real", router: "jev" };
    const user = renderWith(fakeGateway({ health }));
    expect(await screen.findByText("Real Jev (TypeSafe); Qwen is simulated")).toBeInTheDocument();
    expect(screen.queryByText(/Mock mode: Jev and Qwen/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText(/Jev calls go to TypeSafe/)).toBeInTheDocument();
    expect(screen.queryByText(/Mock mode: token usage/)).not.toBeInTheDocument();
  });
});

describe("Operator routes", () => {
  it("shows a model held by an operator as disabled", async () => {
    const user = renderWith(fakeGateway({ health: operatorHeldHealth }));
    await user.click(screen.getByRole("button", { name: "Health" }));
    expect(await screen.findByText(/qwen3-1\.7b was disabled by an operator/)).toBeInTheDocument();
    const card = screen.getByRole("region", { name: "qwen3-1.7b" });
    expect(within(card).getByText("Disabled")).toBeInTheDocument();
    expect(within(card).getByText("when an operator enables it")).toBeInTheDocument();
    expect(screen.getByText("disabled by operator")).toBeInTheDocument();
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

describe("Chat keeps its state in the tab", () => {
  it("shows the character counter and the supported topics", async () => {
    const user = renderWith(fakeGateway());
    expect(screen.getByText("0 / 2000 characters")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Message"), "hello");
    expect(screen.getByText("5 / 2000 characters")).toBeInTheDocument();
    expect(screen.getByText(/This demo knows about a card that has not arrived/)).toBeInTheDocument();
  });

  it("counts and caps messages using the same Unicode characters as the gateway", () => {
    renderWith(fakeGateway());
    const composer = screen.getByLabelText("Message");
    fireEvent.change(composer, { target: { value: "😀" } });
    expect(screen.getByText("1 / 2000 characters")).toBeInTheDocument();
    fireEvent.change(composer, { target: { value: "😀".repeat(2001) } });
    expect(composer).toHaveValue("😀".repeat(2000));
    expect(screen.getByText("2000 / 2000 characters")).toBeInTheDocument();
  });

  it("explains the confidence score in the details", async () => {
    const user = renderWith(fakeGateway());
    await user.type(screen.getByLabelText("Message"), "I lost my card{Enter}");
    await screen.findByText(/Freeze your card/);
    await user.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText(/how sure the router was about its route choice/)).toBeInTheDocument();
  });

  it("keeps an unsent message when switching to Health and back", async () => {
    const user = renderWith(fakeGateway());
    await user.type(screen.getByLabelText("Message"), "my draft");
    await user.click(screen.getByRole("button", { name: /^Health/ }));
    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByLabelText("Message")).toHaveValue("my draft");
  });

  it("restores the conversation after a refresh and clears it on New chat", async () => {
    const first = renderWith(fakeGateway());
    await first.type(screen.getByLabelText("Message"), "I lost my card{Enter}");
    await screen.findByText(/Freeze your card/);
    cleanup();
    vi.restoreAllMocks();

    const second = renderWith(fakeGateway());
    expect(await screen.findByText(/Freeze your card/)).toBeInTheDocument();
    await second.click(screen.getByRole("button", { name: /New chat/ }));
    expect(screen.queryByText(/Freeze your card/)).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("gateway.chat.turns")).toBeNull();
  });

  it("works when the tab blocks storage", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const user = renderWith(fakeGateway());
    await user.type(screen.getByLabelText("Message"), "I lost my card{Enter}");
    expect(await screen.findByText(/Freeze your card/)).toBeInTheDocument();
  });

  it("no longer shows disabled Jev and Dashboard items", async () => {
    renderWith(fakeGateway());
    expect(screen.queryByText("soon")).not.toBeInTheDocument();
    expect(screen.getByText(/Jev normally picks the route/)).toBeInTheDocument();
  });

  it("clears the unsent draft on New chat, also from Health", async () => {
    const user = renderWith(fakeGateway());
    await user.type(screen.getByLabelText("Message"), "private draft");
    await user.click(screen.getByRole("button", { name: /New chat/ }));
    expect(screen.getByLabelText("Message")).toHaveValue("");
    expect(window.sessionStorage.getItem("gateway.chat.draft")).toBeNull();
    await user.type(screen.getByLabelText("Message"), "again");
    await user.click(screen.getByRole("button", { name: /^Health/ }));
    await user.click(screen.getByRole("button", { name: /New chat/ }));
    expect(screen.getByLabelText("Message")).toHaveValue("");
    expect(window.sessionStorage.getItem("gateway.chat.draft")).toBeNull();
  });

  it("clears the draft on New chat even when storage deletion is blocked", async () => {
    window.sessionStorage.setItem("gateway.chat.draft", JSON.stringify("saved draft"));
    const user = renderWith(fakeGateway());
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    await user.click(screen.getByRole("button", { name: /New chat/ }));
    expect(screen.getByLabelText("Message")).toHaveValue("");
    expect(JSON.parse(window.sessionStorage.getItem("gateway.chat.draft"))).toBe("");
    await user.click(screen.getByRole("button", { name: /^Health/ }));
    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByLabelText("Message")).toHaveValue("");
  });

  it.each([
    ["turns", "[null]"],
    ["turns", JSON.stringify([{ id: 0, message: "hi", status: "done" }])],
    ["turns", JSON.stringify([{ message: "hi", status: "pending" }])],
    ["turns", JSON.stringify([{
      id: 0, message: "hi", status: "done",
      result: { ...lostCardReply, trace: { ...lostCardReply.trace, route: {} } },
    }])],
    ["turns", JSON.stringify([{
      id: 0, message: "hi", status: "done",
      result: { ...lostCardReply, policy_facts: [{}] },
    }])],
    ["draft", "123"],
  ])("starts empty and sends when saved %s is malformed (%s)", async (key, raw) => {
    window.sessionStorage.setItem(`gateway.chat.${key}`, raw);
    const user = renderWith(fakeGateway());
    expect(screen.getByLabelText("Message")).toHaveValue("");
    await user.type(screen.getByLabelText("Message"), "I lost my card{Enter}");
    expect(await screen.findByText(/Freeze your card/)).toBeInTheDocument();
  });
});

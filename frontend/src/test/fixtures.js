// Responses shaped like the gateway's Pydantic models.

const BACKENDS = [
  { name: "qwen3-1.7b", route: "standard", latency_ms: 120, available: true, fault_rate: 0 },
  { name: "qwen3-4b", route: "complex", latency_ms: 350, available: true, fault_rate: 0 },
];
const RULES = { window: 20, min_samples: 5, failure_threshold: 0.5, cooldown_seconds: 60 };

export const healthyHealth = {
  status: "ok",
  mode: "mock",
  router: "mock_jev",
  backends: BACKENDS,
  monitor: [
    { name: "qwen3-1.7b", samples: 20, format_failures: 0, failure_rate: 0, disabled_by_controller: false, disabled_for_seconds: null },
    { name: "qwen3-4b", samples: 14, format_failures: 1, failure_rate: 0.07, disabled_by_controller: false, disabled_for_seconds: null },
  ],
  interventions: [],
  controller: RULES,
};

export const degradedHealth = {
  ...healthyHealth,
  status: "degraded",
  monitor: [
    { name: "qwen3-1.7b", samples: 5, format_failures: 5, failure_rate: 1, disabled_by_controller: true, disabled_for_seconds: 41.2 },
    { name: "qwen3-4b", samples: 12, format_failures: 0, failure_rate: 0, disabled_by_controller: false, disabled_for_seconds: null },
  ],
  interventions: [
    { at: "2026-10-30T14:19:51Z", backend: "qwen3-1.7b", action: "disabled", reason: "format failure rate 60% over last 5 responses" },
    { at: "2026-10-30T14:32:08Z", backend: "qwen3-1.7b", action: "disabled", reason: "format failure rate 100% over last 5 responses" },
  ],
};

export const lostCardReply = {
  id: "262a55b9-3baa-4e0b-8abc-58fb91432cf2",
  intent: "lost_or_stolen_card",
  reply: "For this fictional DemoBank scenario: Freeze your card in the fictional app. Request a replacement through the Cards section. Contact DemoBank support for unrecognized transactions.",
  policy_facts: [
    "Freeze your card in the fictional app.",
    "Request a replacement through the Cards section.",
    "Contact DemoBank support for unrecognized transactions.",
  ],
  trace: {
    router: "mock_jev",
    jev_choice: "complex",
    route: "complex",
    model: "qwen3-4b",
    reply_source: "policy_template",
    confidence: 0.79,
    latency_ms: 351.34,
    simulated_latency_ms: 350,
    format_check: "passed",
    format_errors: [],
    fallback_reason: null,
    controller_action: null,
    mocked: true,
  },
};

function json(body, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

// A fetch stub that answers health and chat calls and records chat bodies.
export function fakeGateway({ health = healthyHealth, chat = () => json(lostCardReply) } = {}) {
  const sent = [];
  const fetch = (url, options = {}) => {
    if (url === "/v1/health") return json(health);
    if (url === "/v1/chat") {
      const body = JSON.parse(options.body);
      sent.push(body);
      return chat(body);
    }
    return json({ detail: "Not Found" }, 404);
  };
  return { fetch, sent, json };
}

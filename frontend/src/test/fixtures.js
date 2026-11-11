// Responses shaped like the gateway's Pydantic models.

const BACKENDS = [
  { name: "qwen3-1.7b", route: "standard", latency_ms: 120, available: true, fault_rate: 0, error_rate: 0 },
  { name: "qwen3-4b", route: "complex", latency_ms: 350, available: true, fault_rate: 0, error_rate: 0 },
];
const RULES = { window: 20, min_samples: 5, failure_threshold: 0.5, cooldown_seconds: 60 };

export const healthyHealth = {
  status: "ok",
  mode: "mock",
  router: "mock_jev",
  backends: BACKENDS,
  monitor: [
    { name: "qwen3-1.7b", samples: 20, format_failures: 0, call_errors: 0, failure_rate: 0, disabled_by_controller: false, disabled_for_seconds: null },
    { name: "qwen3-4b", samples: 14, format_failures: 1, call_errors: 0, failure_rate: 0.07, disabled_by_controller: false, disabled_for_seconds: null },
  ],
  interventions: [],
  controller: RULES,
  jev_breaker: { state: "closed", consecutive_failures: 0, failure_limit: 5, cooldown_seconds: 30 },
  // The default: no baseline set, so rates are shown but never flagged.
  semantic: [
    { name: "qwen3-1.7b", samples: 12, agreements: 12, disagreements: 0, no_signal: 0, disagreement_rate: 0, coverage: 1, status: "no_baseline", rerouted_agreements: 0, rerouted_disagreements: 0 },
    { name: "qwen3-4b", samples: 3, agreements: 3, disagreements: 0, no_signal: 0, disagreement_rate: 0, coverage: 1, status: "collecting", rerouted_agreements: 2, rerouted_disagreements: 1 },
  ],
  semantic_rules: { window: 50, min_samples: 10, min_coverage: 0.5, baseline_disagreement_rate: null, alert_margin: 0.15 },
  recent_requests: [
    { completed_at: "2026-10-30T14:33:02Z", route: "standard", model: "qwen3-1.7b", fallback_reason: null, format_check: "failed" },
    { completed_at: "2026-10-30T14:32:51Z", route: "complex", model: "qwen3-4b", fallback_reason: "selected_backend_error", format_check: "passed" },
  ],
  usage: [
    { name: "qwen3-1.7b", attempted_calls: 7, completed_calls: 2, failed_calls: 5, input_tokens: null, output_tokens: null, estimated_cost_usd: null },
    { name: "qwen3-4b", attempted_calls: 9, completed_calls: 9, failed_calls: 0, input_tokens: null, output_tokens: null, estimated_cost_usd: null },
  ],
};

export const degradedHealth = {
  ...healthyHealth,
  status: "degraded",
  monitor: [
    { name: "qwen3-1.7b", samples: 5, format_failures: 5, call_errors: 0, failure_rate: 1, disabled_by_controller: true, disabled_for_seconds: 41.2 },
    { name: "qwen3-4b", samples: 12, format_failures: 0, call_errors: 0, failure_rate: 0, disabled_by_controller: false, disabled_for_seconds: null },
  ],
  interventions: [
    { at: "2026-10-30T14:19:51Z", backend: "qwen3-1.7b", action: "disabled", reason: "format failure rate 60% over last 5 calls" },
    { at: "2026-10-30T14:32:08Z", backend: "qwen3-1.7b", action: "disabled", reason: "format failure rate 100% over last 5 calls" },
  ],
};

export const outageHealth = {
  ...healthyHealth,
  status: "degraded",
  monitor: [
    { name: "qwen3-1.7b", samples: 5, format_failures: 1, call_errors: 4, failure_rate: 1, disabled_by_controller: true, disabled_for_seconds: 41.2 },
    { name: "qwen3-4b", samples: 12, format_failures: 0, call_errors: 0, failure_rate: 0, disabled_by_controller: false, disabled_for_seconds: null },
  ],
  interventions: [
    { at: "2026-10-30T14:32:08Z", backend: "qwen3-1.7b", action: "disabled", reason: "failure rate 100% over last 5 calls" },
  ],
};

// Both models serve, but Jev failed 5 calls in a row, so the breaker is open.
// `status` is about the models only, so it stays ok.
export const jevOutageHealth = {
  ...healthyHealth,
  jev_breaker: { state: "open", consecutive_failures: 5, failure_limit: 5, cooldown_seconds: 30 },
};

// The label-swap override: the small model answers with the wrong intent,
// every reply passes the format check, and the mock baseline is 0.
export const labelSwapHealth = {
  ...healthyHealth,
  semantic: [
    { name: "qwen3-1.7b", samples: 10, agreements: 0, disagreements: 10, no_signal: 0, disagreement_rate: 1, coverage: 1, status: "above_baseline", rerouted_agreements: 0, rerouted_disagreements: 0 },
    { name: "qwen3-4b", samples: 12, agreements: 12, disagreements: 0, no_signal: 0, disagreement_rate: 0, coverage: 1, status: "normal", rerouted_agreements: 0, rerouted_disagreements: 0 },
  ],
  semantic_rules: { ...healthyHealth.semantic_rules, baseline_disagreement_rate: 0 },
};

// Jev is down, so no reply has a Jev answer to compare with.
export const lowCoverageHealth = {
  ...jevOutageHealth,
  semantic: [
    { name: "qwen3-1.7b", samples: 12, agreements: 0, disagreements: 0, no_signal: 12, disagreement_rate: null, coverage: 0, status: "low_coverage", rerouted_agreements: 0, rerouted_disagreements: 0 },
    { name: "qwen3-4b", samples: 0, agreements: 0, disagreements: 0, no_signal: 0, disagreement_rate: null, coverage: null, status: "collecting", rerouted_agreements: 0, rerouted_disagreements: 0 },
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

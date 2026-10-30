// The gateway is always reached on the page's own origin: Caddy in Docker,
// the Vite dev proxy locally.

export class GatewayError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new GatewayError("Couldn't reach the gateway. Check that it is running.", 0);
  }
  if (!response.ok) {
    const message =
      response.status === 503
        ? "No model is available right now. Try again in a minute."
        : response.status === 422
          ? "That message can't be sent. Messages need 1 to 2,000 characters."
          : `The gateway answered with an error (${response.status}).`;
    throw new GatewayError(message, response.status);
  }
  return response.json();
}

export function sendChat(message) {
  return request("/v1/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}

export function getHealth() {
  return request("/v1/health");
}

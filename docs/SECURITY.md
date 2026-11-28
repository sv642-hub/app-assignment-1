# Prompt injection, data exfiltration and who owns the data

What a customer message can and cannot do in this system, what leaves it, and why there are no logins. This is a statement of the design as built, with its limits. It is not a security audit, and nobody has attacked the system.

## Where a customer's message goes

1. The browser sends it to `POST /v1/chat` through Caddy.
2. **Jev** (TypeSafe, a third party) reads it and answers two questions: which route, and which intent. In mock mode this is a local keyword match and the message goes nowhere.
3. **Qwen** (our own vLLM servers on the GPU instance, private network) reads it with a system prompt and must answer in a JSON schema.
4. The gateway checks the answer, builds the reply from an approved policy card and returns it.

The message is **not stored**: `ChatResponse` has no message field, and the store keeps the reply, the intent, the route and the trace. The browser keeps the conversation in `sessionStorage` for the open tab only. Logs record error types, not messages.

## Prompt injection

A customer can type "ignore your instructions and ..." into the chat. This is what that can do:

| Attack | Why it fails or is limited |
| --- | --- |
| Make the model say something off-policy | The reply is built from the policy card for the chosen intent. The model's own reply text is discarded and only the intent is used. The model can influence *which* approved card is shown, not what a card says |
| Make the model choose a different intent | Possible. The schema limits the intent to the 78 labels, so the worst result is a wrong but approved card, or `unknown`. The semantic monitor compares the model's intent with Jev's, and a rise in disagreement can disable the model |
| Make the model run an action or call a tool | There are none. The model has no tools, no network and no database access. It returns text |
| Make the model reveal its system prompt | The prompt holds the label list and the policy card facts for a fictional bank. All of it is already public in the repository. It holds no key, no password and no customer data |
| Make Jev route to the wrong model | Possible, and harmless to correctness: the route chooses a model, and either model builds the reply from the same cards. The rule router replaces Jev when it fails |
| Overflow the input | The request model limits a message to 2,000 characters at the edge (Pydantic); an over-long or blank message gets a 422 |

What we have **not** done: no adversarial test set, no red-team run, and no measurement of how often an injected message changes the intent. The claim above comes from how the reply is built, not from an attack result.

## Data exfiltration

What could leak, and how:

| Path | Status |
| --- | --- |
| The model's reply carries private data out | The reply comes from the card, not the model. Even so, the model has nothing private to leak: its prompt is public and it has no access to other customers' messages (each call stands alone) |
| One customer reads another's reply | `GET /v1/requests/{id}` has no login, so **anyone who has a request's ID can read that reply**. IDs are random UUIDs (`uuid4`), so they cannot be guessed, but they are not secret once shared. The stored reply holds a policy answer, no message text and no personal data. See the ownership section |
| Secrets in logs, images, the repository | Keys live only in the `env/` files on the instance (mode 600, ignored by Git). The Jev and vLLM keys are `SecretStr`, never logged. The database URL is kept out of logs and error messages. The Dockerfile copies only `src/`, `config/` and the README, so no `env/` file is in the image. A secret scan in CI is **not in place yet** (planned: gitleaks) |
| Jev receives the message | When `APP_JEV_MODE=real`, each message goes to TypeSafe, a third party, over HTTPS. This is the one place customer text leaves our machines. The tester brief says to use made-up details only. The optional reply-check call sends the reply as well |
| Traffic in transit | Until the HTTPS Caddyfile merges the app is served on plain HTTP at port 8080: a network observer could read messages. This is stated to testers in `USER_BRIEF.md`. **PENDING:** change this line when HTTPS is live |
| Operator routes | Behind `APP_OPERATOR_TOKEN`: 403 when unset, 401 for a wrong token. The gateway refuses the public demo token in production |
| Ports | Only Caddy publishes a port. The database, gateway, Prometheus and vLLM ports are not published. vLLM binds to the GPU instance's private IP and requires a bearer key |
| Grafana | Anonymous viewer, read-only, at `/grafana`. It reads the database through a read-only role with a statement timeout. It shows request metadata, never message text |

## Who owns the data: why there are no logins

A tester asked for logins so each user's messages stay private and separate ([#36](https://github.com/sv642-hub/app-assignment-1/issues/36)). We closed it as a known limit, not planned. The reasons:

1. **There is no user data to separate.** The server never stores a customer's message, so there is nothing per-user to protect on the server. What it stores is the reply and the trace, which hold a policy card's text, an intent and timings.
2. **Each message stands alone.** The gateway has no conversation memory: a message is routed and answered by itself. History is held only in the browser tab.
3. **The point of the project is the gateway, not an account system.** Accounts would add passwords or an identity provider, sessions, a user table, per-user rate limits and a privacy policy, all outside what we are measuring (routing, monitoring and recovery).
4. **The demo is fictional.** The bank is made up, and testers are told to use made-up details.

What this costs, and what a real product would need:

| Gap | What a real product would add |
| --- | --- |
| Anyone with a request ID can read that reply | Tie each request to an owner (a user ID column and a check on `GET /v1/requests/{id}`), or an unguessable per-session token |
| No way to delete one user's data | A user ID on every row, and a delete route. Today the only delete is of the whole volume |
| No rate limit per user | A per-user limit, and a 429 with `Retry-After`. Not in place yet |
| Plain HTTP until HTTPS merges | HTTPS and a cookie with `Secure` and `HttpOnly` |
| Real Jev sees the text | A data-processing agreement with the provider, or a self-hosted router |

If we added accounts, the decision would be: keep `sessionStorage` history as is, and add an owner to `requests` through a new numbered migration (expand: a nullable column; contract: required after backfilling).

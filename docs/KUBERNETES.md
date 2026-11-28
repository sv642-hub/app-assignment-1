# What breaks with two copies running, and how to fix it

The gateway runs as **one process** (`uvicorn` with one worker) behind Caddy. That is a design choice, stated in the README: "Run one gateway worker: the monitor's windows are still per process." This page lists what would go wrong if we ran two copies, for example two replicas in Kubernetes or two Compose containers, and how we would move each piece of state out of the process. It is a design note. Nothing here is built, and nobody has run two copies.

## State that lives in the process

| State | Where | Two copies: what happens | Fix |
| --- | --- | --- | --- |
| **Monitor windows** (format failures and failed calls, per model) | `monitor.py`, in memory. Saved as `monitor_samples` rows and reloaded at startup only | Each copy sees half the traffic and fills its window half as fast. The 50% threshold needs twice as long to cross, and copies disagree | Compute the failure rate from Postgres (`monitor_samples`), or keep the windows in Redis. The samples are already stored; reading them on each decision is the change |
| **Which models are disabled, and cooldowns** | `Controller`, in memory. Interventions are stored; cooldowns are rebuilt at startup | Copy A disables the model; copy B keeps sending traffic to it until its own window fills. Customers get different behaviour per request | One controller for the whole system (below), and every copy reads the disabled flag from shared state before routing |
| **Operator actions** (`/v1/operator/models/{name}/disable`, `/enable`, `/release`, `/rollback`) | Act on one process | The load balancer sends the call to one copy. The other keeps the old state | Operator routes write to shared state, not to memory. Copies read it |
| **Releases** (which version each model serves, rollback target) | `releases.py`, in memory; events saved in `model_releases`, loaded at startup | A release deployed through one copy is invisible to the other until it restarts | Read the highest revision from `model_releases` on a short interval, or on a notification (`LISTEN`/`NOTIFY`) |
| **Jev circuit breaker** | `breaker.py`, in memory, not saved anywhere | Each copy trips on its own failures. A copy that has not seen failures keeps calling a broken Jev, each call waiting up to the 2 s timeout | This one can stay per copy: each copy protecting itself is acceptable. A shared breaker (Redis) is better if Jev calls are expensive |
| **Semantic monitor window and baseline** | `semantic.py`, in memory | Same as the monitor windows | Same fix: compute from the saved Jev assessments |
| **Injected faults** (`/v1/faults`) | In memory, on the backend objects | A fault set on one copy affects half the traffic. This is a test tool, so the simplest fix is to refuse `/v1/faults` unless there is one copy | Keep it out of production, or store it with the other shared state |
| **Usage counters** and the numbers in `/v1/health` | In memory | `/v1/health` shows the copy that answered, so refreshing the page changes the numbers | Compute from Postgres, or sum the per-copy Prometheus series |
| **Pending intervention writes** | A queue per process, retried on the next request | Each copy retries its own. If a copy dies, its queue is lost | Write synchronously, or use a database outbox table |
| **Memory store** (tests, `APP_STORE=memory`) | In memory | Not shared at all | Refuse `memory` when more than one copy is configured; Postgres is already the Compose default |

Prometheus metrics are fine: each copy exposes its own `/metrics`, and Prometheus scrapes every copy. The alert queries sum across copies, so only the labels need an instance dimension.

## What is already safe

- **Requests and assessments** are written to Postgres, so `GET /v1/requests/{id}` works whichever copy handles it.
- **Migrations** run under a Postgres lock, so two copies starting together will not apply the same file twice.
- **The reply is built from the policy card**, with no per-process cache to go stale.
- **Config files** are read-only in the image, the same for every copy.

## The one controller

The monitor and controller make a decision (disable a model) that every copy must obey. The clean split is:

1. **Gateway copies, stateless**: route, call the models, write samples to Postgres, read the disabled flags.
2. **One controller**, a separate small process with one replica (a Deployment with `replicas: 1`, or a lease held in Postgres with `pg_advisory_lock`): reads the samples, decides, writes interventions.

A controller that dies stops decisions. The gateway copies keep running with the last flags, as today when the controller is in the same process. A restart rebuilds from Postgres, which is what the startup code does.

## What Kubernetes would change

| Compose today | Kubernetes |
| --- | --- |
| Caddy at 80/443 | Ingress controller, with cert-manager for TLS |
| `gateway` container, `restart: unless-stopped` | Deployment of copies, with a `readinessProbe` on `/v1/health` and a `livenessProbe` that does not call the database |
| `postgres` + named volume | StatefulSet with a PersistentVolumeClaim, or a managed database |
| `env/.env`, `env/postgres.env` | ConfigMap and Secrets |
| Start at boot (systemd units) | The cluster's scheduler |
| `docker compose up --wait`, healthchecks | Rolling update, with `readinessProbe` gating traffic |
| Migrations at gateway start, under a lock | A Job that runs before the new Deployment, then copies start (expand and contract still apply: old and new copies run together during a rollout) |
| GPU instance with vLLM, private network | A node pool with GPUs and a Service, reached by the gateway through cluster DNS. The API key stays a Secret |

A rolling update also means two *versions* run at once, so each migration must work with the old code and the new code. That is the same expand-and-contract rule the deploy already needs for rollback.

## Order of work, if we ever did it

1. Read the disabled flags and release state from Postgres on each request (or every few seconds). This alone makes two copies behave the same.
2. Move the failure rate calculation to Postgres or Redis.
3. Move the controller out as a single process.
4. Only then add a second gateway copy.

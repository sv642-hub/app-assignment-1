-- Completed requests and controller interventions.
-- No customer message text is stored: ChatResponse has no message field.

CREATE TABLE requests (
    seq bigserial PRIMARY KEY,           -- insertion order, newest = highest
    id uuid NOT NULL UNIQUE,
    completed_at timestamptz NOT NULL,
    route text NOT NULL,
    model text NOT NULL,
    intent text NOT NULL,
    format_check text NOT NULL,
    response jsonb NOT NULL              -- the full ChatResponse, as returned
);

CREATE TABLE interventions (
    seq bigserial PRIMARY KEY,
    at timestamptz NOT NULL,
    backend text NOT NULL,
    action text NOT NULL,
    reason text NOT NULL
);

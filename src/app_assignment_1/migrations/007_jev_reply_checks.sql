-- The second Jev call (APP_JEV_REPLY_CHECK_ENABLED): one row per checked
-- request, saved after the request itself, so a check can be missing for a
-- saved request but never the other way round in practice.
-- Neither the customer's message nor the reply is stored.
--
-- No foreign key to requests: requests are pruned to the retention limit.
-- The *_agrees columns are null when either call gave no valid answer.

CREATE TABLE jev_reply_checks (
    seq bigserial PRIMARY KEY,
    request_id uuid NOT NULL,
    call_id uuid NOT NULL UNIQUE,
    status text NOT NULL,                -- ok, timeout or error
    finished_at timestamptz NOT NULL,
    mocked boolean NOT NULL,
    option_set_version text NOT NULL,
    latency_ms double precision NOT NULL,
    error_type text,
    route_first text,
    route_second text,
    intent_first text,
    intent_second text,
    reply_fit text,                      -- fits or does_not_fit
    route_agrees boolean,
    intent_agrees boolean,
    record jsonb NOT NULL                -- the full ReplyCheck
);

CREATE INDEX jev_reply_checks_request_id ON jev_reply_checks (request_id);
CREATE INDEX jev_reply_checks_finished_at ON jev_reply_checks (finished_at);

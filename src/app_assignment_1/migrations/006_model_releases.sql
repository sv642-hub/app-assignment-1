-- Model release history: every deploy, confirmation and rollback.
-- revision counts each model's changes in the order the gateway made them;
-- a restarted gateway loads the row with the highest revision per model,
-- whatever order the rows were written in. Numbered 006 so it does not clash with the open PRs' 003 to 005.

CREATE TABLE model_releases (
    seq bigserial PRIMARY KEY,
    at timestamptz NOT NULL,
    backend text NOT NULL,
    action text NOT NULL,                -- deployed, confirmed or rolled_back
    version text NOT NULL,               -- what the model serves after it
    from_version text,                   -- what it served before
    rollback_target text,                -- where a rollback would go after it
    actor text NOT NULL,                 -- operator or controller
    reason text NOT NULL,
    revision bigint NOT NULL,
    -- A retried write of the same change is a no-op (ON CONFLICT DO NOTHING).
    UNIQUE (backend, revision)
);

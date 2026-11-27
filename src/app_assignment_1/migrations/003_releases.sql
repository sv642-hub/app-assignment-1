-- Canary release history: one row per change. The row with the highest seq
-- is the release in effect, which a restarted gateway loads.
-- No customer data is stored here.

CREATE TABLE releases (
    seq bigserial PRIMARY KEY,
    changed_at timestamptz NOT NULL,
    stable text NOT NULL,
    canary text,
    canary_percent integer NOT NULL CHECK (canary_percent BETWEEN 0 AND 100),
    reason text NOT NULL,
    CHECK (canary IS NOT NULL OR canary_percent = 0),
    CHECK (canary IS NULL OR canary <> stable)
);

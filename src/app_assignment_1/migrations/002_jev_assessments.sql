-- Jev assessments: one row per question per Jev call, two per request.
-- Saved in the same transaction as the request they belong to.
-- No customer message text is stored: JevAssessment has no message field.
--
-- No foreign key to requests: requests are pruned to the retention limit, and
-- the monitor keeps scoring older assessments.

CREATE TABLE jev_assessments (
    seq bigserial PRIMARY KEY,
    request_id uuid NOT NULL,
    call_id uuid NOT NULL,
    question text NOT NULL,              -- route or intent
    status text NOT NULL,                -- ok, timeout, error or skipped
    finished_at timestamptz NOT NULL,
    mocked boolean NOT NULL,
    option_set_version text NOT NULL,
    choice text,                         -- only when status is ok
    error_type text,                     -- only when status is timeout or error
    record jsonb NOT NULL,               -- the full JevAssessment
    UNIQUE (call_id, question)
);

CREATE INDEX jev_assessments_request_id ON jev_assessments (request_id);
CREATE INDEX jev_assessments_finished_at ON jev_assessments (finished_at);

-- Escalation fields (confidence.py), copied out of response->'trace' so
-- dashboards and analysis can filter on them. Numbered 008 because the open
-- PRs use 003, 005 and 007.
--
-- escalation_score: the small model's intent confidence, when it answered and
--   gave one. Null otherwise and for older rows.
-- escalated: the large model answered because that score was below the
--   threshold. False for older rows.

ALTER TABLE requests ADD COLUMN escalation_score double precision;
ALTER TABLE requests ADD COLUMN escalated boolean NOT NULL DEFAULT false;

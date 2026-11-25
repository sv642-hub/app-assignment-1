-- Fields for analysing requests later, for example the position-bias study
-- in docs/JEV_POSITION_BIAS_PROPOSAL.md (#86). Numbered 004 because #84 adds
-- 003_releases.sql; the runner applies whichever files have not run.
--
-- settings_version: which settings and config files answered the request
--   (the same value is in response->'trace'). Null for older rows.
-- option_order: the order Jev was shown the route options in, on route rows.
--   Null when unknown, for older rows and on intent rows.

ALTER TABLE requests ADD COLUMN settings_version text;
ALTER TABLE jev_assessments ADD COLUMN option_order text[];

CREATE INDEX requests_settings_version ON requests (settings_version);

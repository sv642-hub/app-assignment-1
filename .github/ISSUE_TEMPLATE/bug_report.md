---
name: Bug report
about: Report a reproducible problem in Forseti's gateway, model integration, or deployment.
title: ""
labels: ""
assignees: ""
---

## Problem
Describe what went wrong and how it affects a caller or developer.

## Expected and actual behavior
**Expected:**

**Actual:**

## Steps to reproduce
1.
2.
3.

Include a minimal synthetic request, command, or failing test when useful.

## Environment
- Commit or branch:
- Runtime: local Python / local container / Jetstream2
- Python version (if relevant):
- Affected endpoint or component:
- Backend/model and routing mode (if relevant):

## Evidence
Include relevant sanitized logs, response status, traceback, or screenshots.
For slow or blocking requests, include concurrent request count, total elapsed time,
slowest request, and whether `/health` responded while a request was in flight.

Do not include API keys, real `.env` files, credentials, or real customer information.

## Completion criteria
- [ ] The reproduction produces the expected behavior after the fix.
- [ ] A regression test is added when appropriate, and existing quality checks pass.
- [ ] Relevant documentation is updated if behavior or setup changes.

## Related work
Link related issues. Link the implementation pull request when available.

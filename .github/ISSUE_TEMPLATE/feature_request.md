---
name: Feature request
about: Propose a focused Forseti capability or assignment implementation task.
title: ""
labels: ""
assignees: ""
---

## Goal
Describe the user or developer need and the outcome this task should deliver.
Keep the issue focused on one reviewable change.

## Project or assignment context
Identify the relevant assignment requirement or project milestone.
State whether this is required assignment work, core Forseti work, or optional stretch work.

Core Forseti work includes the FastAPI gateway, Jev Standard/Complex routing,
Qwen model integration, rule-based routing fallback, availability-based backend
fallback, fictional policy replies, request logging, operational monitoring,
and routing evaluation. Semantic monitoring, activation probes, automatic semantic
rollback, and canary releases are optional stretch work.

## Proposed behavior
Describe the request or trigger, expected result, and relevant failure behavior.
Include API or configuration changes if needed. For routing or backend work,
consider timeouts, unavailable services, and how fallback is recorded.

## Scope
**Included:**

**Out of scope:**

## Dependencies and alternatives
List related issues, required access, or services. Explain any simpler alternative
considered. State whether development can proceed with a stub or fake.

## Acceptance criteria
Replace these examples with observable criteria specific to this task.
- [ ] The intended behavior is demonstrated with a concrete request or command.
- [ ] Relevant failure cases are handled and verified.
- [ ] Tests and documentation are updated as appropriate; quality checks pass.

For the Assignment 2 blocking-call audit, include the tagged blocking version,
the fixed version, both ten-request measurements (total time and slowest request),
and the request-step table and explanation in `discussion.md`.

## Validation plan
Describe how the team will demonstrate completion. Distinguish stubbed tests from
real-service checks; do not claim measured performance from simulated results.

## Related work
Link related issues. Link the implementation pull request when available.

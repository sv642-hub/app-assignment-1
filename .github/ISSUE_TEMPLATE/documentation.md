---
name: Documentation
about: Request a correction or addition to Forseti's setup, API, architecture, or assignment documentation.
title: ""
labels: ""
assignees: ""
---

## Documentation task
Describe the missing, incorrect, or unclear information and who needs it.

## Location
Link the file, section, or page. For new documentation, suggest where it belongs.

## Proposed content
Describe the correction or addition. Include an example if helpful.

Possible areas include installation, environment-variable examples, endpoint usage,
model backend setup, routing and fallback behavior, Docker/Compose, Jetstream2,
and the blocking-call measurements in `discussion.md`.

## Verification
Explain how someone can check the documentation. For setup instructions, specify
the starting environment and expected result. Clearly distinguish simulated
examples from real-model results and planned features from implemented behavior.

Use placeholders for secrets and synthetic banking examples.

## Completion criteria
- [ ] The intended reader can find and understand the updated information.
- [ ] Commands and examples are checked where applicable; unverified steps are identified.
- [ ] Descriptions match the implemented behavior or clearly label planned work.

## Related work
Link related issues, assignment requirements, or implementation pull requests.

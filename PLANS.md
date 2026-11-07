# Project plan

## Current state: Phase 1 complete

The initial deterministic lexical submission is in `solution/`. The last recorded public run answered 40 questions with QA score **0.5044**, p95 answer latency **0.09 s**, and consistency **1.0**. It reported zero runtime errors and zero stale-trap hits. These are public-kit measurements, not hidden-set or production results. The public run did not include a conflict gold file.

## Phase 2: failure analysis (planned)

Goal: inspect misses by category and identify retrieval, extraction, abstention, and conflict-detection causes without encoding practice answers. Acceptance: a documented failure inventory with source evidence, proposed general fixes, and a baseline comparison. Gate: approve the specific implementation plan before editing solution code or tests.

## Accuracy and conflict work (planned)

Goal: improve grounded answers to broad and branch questions and reduce false conflict reports. Acceptance: focused regression tests, contract-compliant answers and sources, and a public evaluation that reports QA, category scores, stale-trap hits, consistency, and latency against Phase 1. Any score regression must be explained. Gate: approve each scoped code change and any new dependency or external service before use.

## Final validation (planned)

Goal: review the submission, provenance, data exclusions, and reproducibility. Acceptance: relevant tests pass, public evaluation and `git diff --check` are recorded, changes are reviewed, and remaining risks are reported. Gate: obtain explicit approval before any commit or push.

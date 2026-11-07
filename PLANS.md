# Project plan

## Current state: Phase 3A implemented, awaiting review

The initial deterministic lexical submission is in `solution/`. The Phase 1 public baseline was **0.5044 QA** on 40 questions, with **0.03 s p50**, **0.09 s p95**, **1.0 consistency**, zero runtime errors, and zero stale-trap hits. The official rerun before Phase 3A reproduced that baseline. These are public-kit measurements, not hidden-set or production results. The public run did not include a conflict gold file.

## Phase 2: failure analysis (complete)

The question-by-question diagnosis found that passage body-only postings omitted relevant facts whose matching terms appeared in headings, page titles, or link targets. Branch schedules lost their section labels, short links were often dropped, and repeated locator/navigation text competed with factual passages. Other misses need source authority, multi-passage answers, or conflict resolution, which are outside Phase 3A.

## Phase 3A: retrieval context (implemented, awaiting review)

The approved change adds bounded heading and branch context, weighted title/path/link metadata, literal Markdown link targets, and structural downweighting of repeated or short boilerplate. Answer checks still require explicit value, time, address, action, or link evidence. Snapshot selection and the public contract remain unchanged.

The final official public rerun after implementation scored **0.5915 QA** (**+0.0871**), with **0.07 s p50**, **0.16 s p95**, **1.0 consistency**, zero errors, and zero stale-trap hits. Four questions gained score: `dup-034` 0.2917→1.0, `dup-066` 0→0.85, `dup-068` 0→1.0, and `dup-082` 0→1.0. No question lost score; all 18 baseline full-credit cases remain full-credit. Ingest time rose from **3.1 s** to **9.1 s**, within the 30-minute limit. Branch information rose from **0.1667** to **0.475**, list completeness from **0.4479** to **0.625**, and stale-trap questions from **0.4679** to **0.6107**. Other category scores were unchanged.

Focused synthetic-snapshot tests cover heading-only retrieval, mortgage rows, table labels, branch and ATM boundaries, schedules, literal link destinations with query strings and fragments, procedural evidence, boilerplate ranking, and historical selection. The ten test functions passed via a standard-library runner; `pytest` is unavailable in the current environment. No commit or push has been made.

## Later accuracy and conflict work (planned)

Improve source authority, answer completeness across passages, and cross-page conflict resolution under separate approval. Current retrieval can still favor a special mortgage program over general rates or a dated release over the requested time period. Public conflict accuracy remains unmeasured without a gold file. Acceptance: focused regression tests, contract-compliant answers and sources, and public evaluation against Phase 3A. Explain any score regression.

## Final validation (planned)

Goal: review the submission, provenance, data exclusions, and reproducibility. Acceptance: relevant tests pass, public evaluation and `git diff --check` are recorded, changes are reviewed, and remaining risks are reported. Gate: obtain explicit approval before any commit or push.

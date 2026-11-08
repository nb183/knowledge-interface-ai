# Project plan

## Current state: Phase 4F implemented, awaiting review

Phase 4F conservatively activates a verified structured mortgage-rate proposal only after the Phase 4E validator classifies it as an improvement. The final answer preserves the verified rate/term/tier schedule, summarizes source-supported eligibility and rate-lock conditions, and cites each selected-snapshot row. Other fact types, incomplete evidence, ambiguous requests, and already-correct lexical answers keep the existing answer. `conflicts()` and resolver rules are unchanged.

The official public run immediately before activation scored **0.7124 QA** (40 questions, 0.07 s p50 / 0.18 s p95, 13.6 s ingest). After activation it scored **0.7388 QA** (**+0.0264**), with **0.08 s p50 / 0.19 s p95**, **13.2 s ingest**, zero errors and stale-value trap hits, and 1.0 consistency. Only `dup-009` changed (0→0.85); the other 39 answer objects and scores remained identical. Stale-trap category score rose from 0.5286 to 0.6500; all other category scores were unchanged. One of 40 public questions was promoted. Of the 39 fallbacks, three were verified no-ops; the rest were unsupported or insufficient requests. A direct comparison found the same 84 `conflicts()` entries before and after. Fifty-five focused test functions passed. The public kit has no conflict gold set, and hidden-set impact remains unmeasured. No Phase 4F commit or push has been made.

Phase 4A is committed as `c7955d6`. Phase 4B adds a standalone resolver in `solution/resolver.py`; the existing answer and conflict methods remain unchanged. The resolver filters by selected snapshot first, matches typed qualifiers, distinguishes schedule succession from same-period disagreement, and retains competing claims with applied rules and provenance. It does not connect to member-facing answers or the contract conflict output.

The Phase 4B public evaluation remains **0.7124 QA**, with all 40 answer objects and question scores identical to Phase 4A, **1.0 consistency**, zero errors and stale-value trap hits, **0.07 s p50**, **0.17 s p95**, and **15.9 s ingest**. Synthetic focused tests pass. On 2,565 extracted claims, resolver construction took approximately **0.000008 s** and retained approximately **0.3 KiB**; a 100-call latest-snapshot mortgage sample measured **0.651 ms p50 / 0.657 ms p95** per lookup under `tracemalloc`, and discovery took **0.15 s**. These are local measurements, not production guarantees. The current lexical `conflicts()` output is unchanged and remains unscored without a public conflict gold set.

The initial deterministic lexical submission is in `solution/`. The Phase 1 public baseline was **0.5044 QA** on 40 questions, with **0.03 s p50**, **0.09 s p95**, **1.0 consistency**, zero runtime errors, and zero stale-trap hits. The official rerun before Phase 3A reproduced that baseline. These are public-kit measurements, not hidden-set or production results. The public run did not include a conflict gold file.

## Phase 2: failure analysis (complete)

The question-by-question diagnosis found that passage body-only postings omitted relevant facts whose matching terms appeared in headings, page titles, or link targets. Branch schedules lost their section labels, short links were often dropped, and repeated locator/navigation text competed with factual passages. Other misses need source authority, multi-passage answers, or conflict resolution, which are outside Phase 3A.

## Phase 3A: retrieval context (complete)

The approved change adds bounded heading and branch context, weighted title/path/link metadata, literal Markdown link targets, and structural downweighting of repeated or short boilerplate. Answer checks still require explicit value, time, address, action, or link evidence. Snapshot selection and the public contract remain unchanged.

The final official public rerun after implementation scored **0.5915 QA** (**+0.0871**), with **0.07 s p50**, **0.16 s p95**, **1.0 consistency**, zero errors, and zero stale-trap hits. Four questions gained score: `dup-034` 0.2917→1.0, `dup-066` 0→0.85, `dup-068` 0→1.0, and `dup-082` 0→1.0. No question lost score; all 18 baseline full-credit cases remain full-credit. Ingest time rose from **3.1 s** to **9.1 s**, within the 30-minute limit. Branch information rose from **0.1667** to **0.475**, list completeness from **0.4479** to **0.625**, and stale-trap questions from **0.4679** to **0.6107**. Other category scores were unchanged.

Focused synthetic-snapshot tests cover heading-only retrieval, mortgage rows, table labels, branch and ATM boundaries, schedules, literal link destinations with query strings and fragments, procedural evidence, boilerplate ranking, and historical selection. The ten Phase 3A test functions passed via a standard-library runner; `pytest` is unavailable in the current environment. Phase 3A was committed as `55999bf`; it was not pushed.

## Phase 3B: question-aware evidence selection (implemented, awaiting review)

The approved change separates request parsing, retrieval, passage-level sufficiency checks, compatible-fact selection, and answer composition. Required qualifiers must match; unspecified qualifiers remain available for broad answers; incompatible candidates are excluded. Broad card, certificate, mortgage, and branch-hour answers aggregate labelled facts with citations. When equally applicable evidence gives different values for the same fact, the system keeps the competing passages internally and abstains. The public contract, snapshot selection, and `conflicts()` remain unchanged; no LLM or API dependency was added.

The official Phase 3A rerun immediately before editing scored **0.5915 QA** on 40 questions, with **0.07 s p50**, **0.17 s p95**, **9.1 s ingest**, zero errors, and zero stale-value trap hits. The final Phase 3B run scored **0.7124 QA** (**+0.1209**), with **0.07 s p50**, **0.16 s p95**, **9.2 s ingest**, zero errors, and zero stale-value trap hits. Consistency remained **1.0**. Question gains were `dup-064` 0→0.85, `dup-145` 0→1.0, `dup-065` 0→1.0, `dup-040` 0.5→1.0, `dup-031` 0→1.0, and `dup-104` 0→1.0. The regression was `dup-009` 0.575→0: it now abstains because different pages in the selected snapshot give incompatible 30-year mortgage rates for the same amount tiers. No baseline full-credit question regressed, and no newly confident zero-credit answer appeared.

Category scores changed as follows: branch information **0.475→0.95**, current values **0.3333→0.6667**, list completeness **0.625→0.75**, should-refuse **0.6667→1.0**, and stale-trap **0.6107→0.5286**. Conflict resolution stayed **0**, entity attribution **1.0**, temporal as-of **0.75**, and tier disambiguation **0.8333**. Seventeen focused test functions passed via a standard-library runner; `pytest` remains unavailable. No Phase 3B commit or push has been made.

## Later source-authority and conflict work (planned)

Resolve contradictory dated rate pages and cross-page factual conflicts under a separate approval. A general mortgage question now safely abstains when its selected snapshot contains incompatible rate tables, and the public conflict pillar remains unmeasured without a gold file. Acceptance: focused regression tests, contract-compliant answers and sources, and a public evaluation against Phase 3B with every score regression explained.

## Final validation (planned)

Goal: review the submission, provenance, data exclusions, and reproducibility. Acceptance: relevant tests pass, public evaluation and `git diff --check` are recorded, changes are reviewed, and remaining risks are reported. Gate: obtain explicit approval before any commit or push.

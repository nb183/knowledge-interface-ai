# Agent instructions

## Mission and sources of truth

Build a snapshot-aware, source-grounded knowledge system for the supplied credit-union website. Use `harness/contract.py` for the submission interface, `harness/run_eval.py` and `harness/scoring.py` for evaluation behavior, `solution/solution.py` for the current implementation, and `PLANS.md` and `DECISIONS.md` for the work record. The participant kit is private; do not commit or redistribute its snapshots, ZIPs, evaluation cases, or assignment PDF.

## Working rules

- For nontrivial changes, follow **inspect → plan → approval → implement → verify → review → report**. State the exact files and expected behavior before requesting approval.
- Get explicit approval before repository edits, dependency installation, destructive operations, commits, pushes, or external API use. Approval for one scoped change does not authorize unrelated changes.
- Keep Python changes small, deterministic, and compatible with the standard-library harness contract. Do not change supplied harness, evaluation cases, starter, or snapshot data without explicit approval. Avoid unnecessary frameworks.
- Do not hardcode public-evaluation answers, fabricate sources, or claim accuracy that has not been measured.
- Add focused tests for behavior changes. Verify relevant tests and the public evaluation when solution behavior changes. For documentation-only work, review the diff and run `git diff --check` when Git is available.
- Report changed files, test and evaluation evidence, regressions against the recorded baseline, limitations, and any unverified requirement. Do not commit or push without explicit approval.

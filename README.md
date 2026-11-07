# Temporal knowledge interface

This project answers member questions from dated website snapshots. An answer should use the appropriate snapshot, preserve a supporting URL, respect rates and balance tiers, and abstain when the available pages do not support a value. The current implementation is an initial submission, not a production service.

## Private participant kit

Running the evaluation requires the privately supplied `harness/`, `eval/public_eval.jsonl`, and five archives in `snapshots_zip/`. The kit also contains `starter/` as a reference. The archives unpack into `snapshots/` on the first evaluation run. Snapshot pages, ZIPs, evaluation data, and the assignment PDF are excluded from Git and must not be redistributed.

`harness/contract.py` defines `build()`, `ingest(snapshot)`, `answer(question, as_of=None)`, and `conflicts()`. The current submission is `solution/solution.py`. It uses separate in-memory lexical indexes for each snapshot, labels Markdown table rows with their column headers, and returns answers with source URLs and snapshot IDs. It needs Python 3.9 or newer and no external runtime packages, API keys, or network access.

## Local checks

From the project root, with the private kit present:

```bash
python3 harness/run_eval.py --submission solution --eval eval/public_eval.jsonl --out results
python3 -m pytest -q solution/test_solution.py
```

Pytest is needed only to run the tests; it is not a solution dependency. The latest recorded public run scored **0.5044 QA** across 40 questions, with **0.09 s p95** answer latency and **1.0** consistency. It reported zero runtime errors and zero stale-trap hits. See `PLANS.md` for the next phase and `DECISIONS.md` for the initial design record.

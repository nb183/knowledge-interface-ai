# Decision record

## Initial submission: deterministic lexical retrieval

The initial submission uses one in-memory inverted index per full snapshot, BM25-style lexical ranking, Markdown prose and labelled table-row passages, and `as_of` selection of the latest snapshot no later than the requested date. It uses only the Python standard library and supplied snapshot objects, with no network or LLM dependency.

This choice kept the harness integration simple and answers fast while preserving URL and snapshot provenance. The recorded public run scored **0.5044 QA** on 40 questions, with **0.09 s p95** latency and **1.0** consistency. The small harness selftest detected its one labelled certificate-rate conflict, but public conflict accuracy was not measured.

Observed limitations include broad questions needing facts from several passages, branch details, and conflict resolution across differently worded pages. Phase 2 will analyze these failures before proposing code changes. No decision on a replacement retrieval method or new dependency has been made.

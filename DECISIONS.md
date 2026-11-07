# Decision record

## Initial submission: deterministic lexical retrieval

The initial submission uses one in-memory inverted index per full snapshot, BM25-style lexical ranking, Markdown prose and labelled table-row passages, and `as_of` selection of the latest snapshot no later than the requested date. It uses only the Python standard library and supplied snapshot objects, with no network or LLM dependency.

This choice kept the harness integration simple and answers fast while preserving URL and snapshot provenance. The recorded public run scored **0.5044 QA** on 40 questions, with **0.09 s p95** latency and **1.0** consistency. The small harness selftest detected its one labelled certificate-rate conflict, but public conflict accuracy was not measured.

Observed limitations include broad questions needing facts from several passages, branch details, and conflict resolution across differently worded pages. Phase 2 analyzed these failures before the scoped Phase 3A retrieval change. No replacement retrieval framework or new dependency has been adopted.

## Phase 3A: bounded context in lexical retrieval

The Phase 2 diagnosis showed that heading, title, and link terms could boost a passage but could not retrieve it when its body had no query term. Phase 3A keeps one index per full snapshot and adds lower-weight postings for bounded heading context, page title and path, and literal Markdown link destinations. Table rows retain column labels. Branch labels, addresses, and lobby/drive-up headings stay within their own section and reset at the next branch boundary. Repeated or short generic text receives less weight. A small home-loan/mortgage vocabulary bridge is applied to page metadata so rate rows can be retrieved by either term.

Metadata establishes relevance only. The answer path still checks for a stated rate, time, address, named branch, action, or retained link when that evidence is requested. Direct link answers use a destination found in the selected Markdown passage, preserving its original fragment and query parameters. No source-authority ranking, multi-document synthesis, conflict-resolution change, external service, or new dependency was introduced.

The official public evaluation reproduced **0.5044 QA** immediately before the change and measured **0.5915 QA** afterward. Four question scores increased and none decreased. P95 answer latency increased from **0.09 s** to **0.16 s**; ingest time increased from **3.1 s** to **9.1 s**. Both runs had zero runtime errors and zero stale-trap hits. All baseline full-credit cases stayed full-credit. The result demonstrates retrieval improvement on the public set, not hidden-set accuracy. Current limitations include special-program rates outranking general rates, incomplete lists, dated claims within a snapshot, and unmeasured conflict accuracy.

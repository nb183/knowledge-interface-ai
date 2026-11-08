# Decision record

## Initial submission: deterministic lexical retrieval

The initial submission uses one in-memory inverted index per full snapshot, BM25-style lexical ranking, Markdown prose and labelled table-row passages, and `as_of` selection of the latest snapshot no later than the requested date. It uses only the Python standard library and supplied snapshot objects, with no network or LLM dependency.

This choice kept the harness integration simple and answers fast while preserving URL and snapshot provenance. The recorded public run scored **0.5044 QA** on 40 questions, with **0.09 s p95** latency and **1.0** consistency. The small harness selftest detected its one labelled certificate-rate conflict, but public conflict accuracy was not measured.

Observed limitations include broad questions needing facts from several passages, branch details, and conflict resolution across differently worded pages. Phase 2 analyzed these failures before the scoped Phase 3A retrieval change. No replacement retrieval framework or new dependency has been adopted.

## Phase 3A: bounded context in lexical retrieval

The Phase 2 diagnosis showed that heading, title, and link terms could boost a passage but could not retrieve it when its body had no query term. Phase 3A keeps one index per full snapshot and adds lower-weight postings for bounded heading context, page title and path, and literal Markdown link destinations. Table rows retain column labels. Branch labels, addresses, and lobby/drive-up headings stay within their own section and reset at the next branch boundary. Repeated or short generic text receives less weight. A small home-loan/mortgage vocabulary bridge is applied to page metadata so rate rows can be retrieved by either term.

Metadata establishes relevance only. The answer path still checks for a stated rate, time, address, named branch, action, or retained link when that evidence is requested. Direct link answers use a destination found in the selected Markdown passage, preserving its original fragment and query parameters. No source-authority ranking, multi-document synthesis, conflict-resolution change, external service, or new dependency was introduced.

The official public evaluation reproduced **0.5044 QA** immediately before the change and measured **0.5915 QA** afterward. Four question scores increased and none decreased. P95 answer latency increased from **0.09 s** to **0.16 s**; ingest time increased from **3.1 s** to **9.1 s**. Both runs had zero runtime errors and zero stale-trap hits. All baseline full-credit cases stayed full-credit. The result demonstrates retrieval improvement on the public set, not hidden-set accuracy. Current limitations include special-program rates outranking general rates, incomplete lists, dated claims within a snapshot, and unmeasured conflict accuracy.

## Phase 3B: separate retrieval, sufficiency, and composition

Phase 3B adds deterministic request parsing and product-specific evidence checks after retrieval. The request specification marks named entities, terms, services, and other qualifiers as required or unspecified; a candidate with a mismatched required qualifier is incompatible. A special mortgage program cannot answer an unqualified standard-rate request, and a percentage describing an ARM adjustment cannot stand in for a published rate on an exact ARM product. Substantive program benefits can outrank application links on the same page.

Compatible card, certificate, mortgage, and branch-hour facts are grouped by product and relevant qualifiers before composition. Equivalent duplicate values are collapsed; materially different values for the same fact are retained in `unresolved_evidence` and cause an abstention. This deliberately avoids choosing a winner by URL, recency, or source authority. The existing snapshot indexes, `as_of` selection, public contract, and `conflicts()` are unchanged. No LLM, external API, dependency, or new framework was introduced.

The official Phase 3A rerun scored **0.5915 QA**, and the final Phase 3B run scored **0.7124 QA** on the 40 public questions. Six questions gained score; one fell from **0.575 to 0** because cross-page 30-year mortgage rates conflict and the system now abstains. No previously full-credit question regressed. Both runs had zero runtime errors and stale-value trap hits; p95 latency was **0.17 s** before and **0.16 s** after, with ingest times of **9.1 s** and **9.2 s**. This is public-set evidence, not a hidden-set accuracy estimate. Source-authority resolution and the unmeasured conflict pillar remain later work.

## Phase 4B: standalone temporal resolution

The resolver uses the selected snapshot before any qualifier, date, or authority comparison. Explicitly different products, terms, tiers, services, and reporting years cannot conflict. Missing qualifiers remain compatible but unproven. Distinct effective dates establish schedule succession only for the same non-special labelled rate fact with sufficient structural authority and no explicit overlapping validity; equal-period disagreements remain visible. Publication dates are kept separate from effective dates. Structural evidence roles, rather than individual URLs or public answers, determine domain-specific authority. An equal-authority disagreement remains unresolved. The resolver exposes decisions and conflict candidates for later integration; `answer()` and `conflicts()` are unchanged.

The official 40-question public run after Phase 4B reproduced **0.7124 QA** with identical answer objects and scores, zero errors and stale-value hits, **0.07 s p50**, **0.17 s p95**, and **15.9 s ingest**. Resolver-specific synthetic tests cover succession, overlap, missing dates, tiers, promotions, branch counts, reporting years, and snapshot isolation. No public conflict gold file exists; resolver discovery precision and recall on real claims remain unmeasured.

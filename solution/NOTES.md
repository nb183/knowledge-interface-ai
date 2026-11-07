# Initial submission

`solution.py` uses only the Python standard library and the snapshots supplied through the harness. It makes no network calls and needs no API key.

Each full snapshot gets a separate in-memory inverted index. `as_of` selects the latest snapshot dated no later than the requested date. The Markdown parser indexes prose blocks and table rows; every table cell is labelled with its column header. Answers cite the page URL and the selected snapshot ID.

Retrieval uses lexical BM25-style scoring with small boosts for matching page context, exact terms, and rate tables. It abstains when evidence is missing or a requested term is absent. Conflict detection compares labelled facts and table rows on the same URL across snapshots, using the newest observed value as the resolution.

The system is deterministic. Its main limitation is broad questions that require collecting several facts from distant pages or interpreting complex prose; it does not synthesize facts beyond retrieved passages.

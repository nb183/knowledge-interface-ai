"""Synthetic resolver tests independent of the submission answer path."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from solution.facts import FactIdentity, FactIndex
from solution.resolver import (ConflictCandidate, FactPattern, TemporalResolver,
                               classify_pair, group_review_records, qualifier_match, relationship)


def snapshot(sid, taken, *pages):
    sources = [SimpleNamespace(url=url, title=title, markdown=body,
                               fetched_at=taken + "T00:00:00Z") for url, title, body in pages]
    return FactIndex(SimpleNamespace(snapshot_id=sid, taken_on=taken, pages=sources))


def mortgage(value, effective=None, expiry=None, tier="$100,000 or less", program="Conventional"):
    body = ("# Home Rates\n| Program | Rate |\n| --- | --- |\n"
            f"| Purchase: 30-Year Fixed {program} {tier} | {value} |\n")
    if effective:
        body += f"Rates are effective as of {effective}.\n"
    if expiry:
        body += f"Offer expires {expiry}.\n"
    return body


def pattern(claim):
    return FactPattern(claim.identity)


def test_april_and_september_versions_are_successive_with_explanation():
    index = snapshot("latest", "2026-09-28",
                     ("https://example.test/home-guidance", "Home Guidance",
                      mortgage("5.750%", "04/22/2026")),
                     ("https://example.test/home-loans", "Home Loans",
                      mortgage("7.13%", "09/25/2026")))
    old, new = index.claims
    resolver = TemporalResolver([index])
    assert relationship(old, new) == "confirmed_succession"
    decision = resolver.resolve(pattern(new), "2026-09-28", "latest")
    assert decision.status == "resolved" and decision.winner == new
    assert (old, "successive_schedule_version") in decision.excluded
    assert not decision.conflicts
    assert "successive_schedule_version" in decision.rules
    assert resolver.discover_conflicts("latest", "2026-09-28") == ()


def test_same_period_disagreement_is_retained_and_unresolved():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates", mortgage("7.13%", "09/25/2026")),
                     ("https://example.test/b", "Home Rates", mortgage("7.25%", "09/25/2026")))
    first, second = index.claims
    resolver = TemporalResolver([index])
    decision = resolver.resolve(pattern(first), "2026-09-28", "one")
    assert decision.status == "unresolved" and decision.winner is None
    assert decision.conflicts[0].classification == "confirmed_overlap"
    assert {c.id for c in decision.conflicts[0].claims} == {first.id, second.id}
    assert {c.value.raw for c in decision.conflicts[0].claims} == {"7.13%", "7.25%"}
    assert resolver.discover_conflicts("one", "2026-09-28") == decision.conflicts


def test_missing_dates_are_unknown_and_not_silently_superseded():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates", mortgage("6.00%")),
                     ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026")))
    first, second = index.claims
    assert relationship(first, second) == "unknown"
    decision = TemporalResolver([index]).resolve(pattern(first), "2026-09-28", "one")
    assert decision.status == "resolved"  # dated table has stronger documented authority
    assert decision.conflicts[0].classification == "unknown"
    assert decision.conflicts[0].resolution == "7.00%"


def test_explicit_overlap_blocks_schedule_succession():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates",
                      mortgage("6.00%", "04/22/2026", "10/01/2026")),
                     ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026")))
    a, b = index.claims
    assert relationship(a, b) == "confirmed_overlap"
    decision = TemporalResolver([index]).resolve(pattern(a), "2026-09-28", "one")
    assert decision.status == "unresolved"
    assert not any(reason == "successive_schedule_version" for _, reason in decision.excluded)


def test_different_tiers_and_promotions_do_not_share_identity():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates", mortgage("7.13%", "09/25/2026")),
                     ("https://example.test/b", "Home Rates", mortgage("7.25%", "09/25/2026", tier="$200,000 or less")),
                     ("https://example.test/c", "Home Rates", mortgage("4.50%", "09/01/2026", program="Relief Program")))
    a, b, promo = index.claims
    assert qualifier_match(a.identity, b.identity) == "incompatible"
    assert qualifier_match(a.identity, promo.identity) == "incompatible"
    decision = TemporalResolver([index]).resolve(pattern(a), "2026-09-28", "one")
    assert decision.winner == a and not decision.conflicts
    assert TemporalResolver([index]).discover_conflicts("one", "2026-09-28") == ()


def test_expired_promotion_is_inapplicable():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates",
                      mortgage("4.50%", "08/01/2026", "09/01/2026", program="Relief Program")))
    resolver = TemporalResolver([index])
    decision = resolver.resolve(pattern(index.claims[0]), "2026-09-28", "one")
    assert decision.status == "insufficient"
    assert decision.excluded[0][1] == "expired"


def test_branch_count_uses_dated_institutional_claims_without_voting():
    old = "November 17, 2025\n# About\nDupaco has 23 branch office locations.\n"
    new = "April 06, 2026\n# About\nDupaco has 22 branch office locations.\n"
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/old", "About Dupaco", old),
                     ("https://example.test/new", "About Dupaco", new))
    decision = TemporalResolver([index]).resolve(pattern(index.claims[0]), "2026-09-28", "one")
    assert decision.status == "resolved" and decision.winner.value.raw == "22"
    assert decision.winner.dates.published_on == "2026-04-06"
    assert decision.conflicts[0].classification == "unknown"


def test_reporting_years_and_snapshot_isolation():
    older = snapshot("old", "2026-01-01",
                     ("https://example.test/vto", "Impact",
                      "April 19, 2024\n# Impact\nIn 2023 alone:\n- **294 Dupaco employees** used Volunteer Time Off in 2023.\n"))
    newer = snapshot("new", "2026-09-28",
                     ("https://example.test/vto", "Impact",
                      "April 21, 2025\n# Impact\nIn 2024 alone:\n- **256 Dupaco employees** used Volunteer Time Off in 2024.\n"))
    resolver = TemporalResolver([older, newer])
    old_claim, new_claim = older.claims[0], newer.claims[0]
    assert old_claim.identity != new_claim.identity
    assert resolver.resolve(pattern(old_claim), "2026-01-01", "old").winner == old_claim
    assert resolver.resolve(pattern(new_claim), "2026-01-01", "old").status == "insufficient"
    assert resolver.resolve(pattern(new_claim), "2026-09-28", "new").winner == new_claim


def test_ira_sizes_terms_minimums_and_measures_are_not_conflicts():
    body = ("# IRA Account Rates\nSpecial IRAs\n"
            "| Term | Dividend Rate | APY | Min. to Earn |\n| --- | --- | --- | --- |\n"
            "| Roth: 10 Month Fixed Jumbo Special | 3.80% | 3.90% | $100,000 |\n"
            "| Roth: 10 Month Fixed Special | 3.70% | 3.80% | $1,000 |\n"
            "| Traditional: 10 Month Fixed Special | 3.60% | 3.70% | $1,000 |\n"
            "| Roth: 25 Month Fixed Special | 3.50% | 3.60% | $1,000 |\n")
    index = snapshot("one", "2026-09-28", ("https://example.test/retirement", "Retirement", body))
    resolver = TemporalResolver([index])
    assert resolver.discover_conflicts("one", "2026-09-28") == ()


def test_promotion_periods_are_not_competing_rates():
    body = ("# Savings Rates\n| Account Type | Balance Tier | Dividend Rate for 1st 6 Months | "
            "Annual Percentage Yield (APY) for 1st 6 Months | Dividend Rate After Promotional Period | "
            "Annual Percentage Yield (APY) After Promotional Period |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| Achieve Money Market | $25,000-$99,999.99 | 4.00% | 3.66% | 3.20% | 3.25% |\n")
    index = snapshot("one", "2026-09-28", ("https://example.test/earn", "Savings", body))
    assert len(index.claims) == 4
    assert TemporalResolver([index]).discover_conflicts("one", "2026-09-28") == ()


def test_missing_qualifiers_remain_review_candidates_not_confirmed_conflicts():
    index = snapshot("one", "2026-09-28",
                     ("https://example.test/a", "Home Rates", mortgage("6.00%", "09/25/2026")),
                     ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026", tier="$200,000 or less")))
    first, second = index.claims
    # A source with an unspecified tier is compatible but cannot prove the same loan amount.
    second = second.__class__(second.id, FactIdentity.make("mortgage", "mortgage", "interest_rate",
                            term="30 year", transaction="purchase"), second.value, second.dates,
                            second.snapshot_id, second.url, second.title, second.section, second.evidence, second.line)
    index.by_family = {"mortgage": (first, second)}
    index.by_identity = {first.identity: (first,), second.identity: (second,)}
    index.claims = (first, second)
    resolver = TemporalResolver([index])
    assert qualifier_match(first.identity, second.identity) == "compatible_unknown"
    assert resolver.discover_conflicts("one", "2026-09-28") == ()
    review = resolver.discover_review_candidates("one", "2026-09-28")
    assert len(review) == 1 and review[0].reason == "missing_qualifier"
    assert review[0].classification == "unknown" and review[0].resolution is None


def test_real_snapshot_ira_and_promotion_false_positives_are_not_confirmed():
    root = Path(__file__).resolve().parents[1] / "snapshots"
    folder = next(d for d in sorted(root.iterdir(), reverse=True) if (d / "manifest.json").exists())
    meta = json.loads((folder / "manifest.json").read_text())
    selected = []
    for item in meta["pages"]:
        body = (folder / item["file"]).read_text()
        if (("Roth: 10 Month Fixed Jumbo Special" in body and "Roth: 10 Month Fixed Special" in body)
                or ("Dividend Rate for 1st 6 Months" in body
                    and "Dividend Rate After Promotional Period" in body)):
            selected.append((item["url"], item.get("title", ""), body))
    assert any("Roth: 10 Month" in body for _, _, body in selected)
    assert any("Dividend Rate for 1st 6 Months" in body for _, _, body in selected)
    index = snapshot(meta["snapshot_id"], meta["taken_on"], *selected)
    candidates = TemporalResolver([index]).discover_conflicts(index.snapshot_id, meta["taken_on"])
    assert not any(c.classification == "confirmed_overlap" and
                   c.claims[0].identity.subject in {"ira certificate", "achieve money market"}
                   for c in candidates)


def test_labeled_conflict_quality_fixture_keeps_four_outcomes_distinct():
    """Manually labeled synthetic gold: contradiction, non-conflict, stale, unresolved."""
    confirmed = snapshot("one", "2026-09-28",
                         ("https://example.test/a", "Home Rates", mortgage("6.00%", "09/25/2026")),
                         ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026")))
    tiered = snapshot("two", "2026-09-28",
                      ("https://example.test/a", "Home Rates", mortgage("6.00%", "09/25/2026")),
                      ("https://example.test/b", "Home Rates",
                       mortgage("7.00%", "09/25/2026", tier="$200,000 or less")))
    stale = snapshot("three", "2026-09-28",
                     ("https://example.test/a", "Home Rates",
                      mortgage("6.00%", "04/01/2026", "06/30/2026")),
                     ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026")))
    undated = snapshot("four", "2026-09-28",
                       ("https://example.test/a", "Home Rates", mortgage("6.00%")),
                       ("https://example.test/b", "Home Rates", mortgage("7.00%", "09/25/2026")))
    periods = snapshot("five", "2026-09-28",
                       ("https://example.test/a", "Savings Rates",
                        "# Savings Rates\n| Account Type | Dividend Rate for 1st 6 Months |\n"
                        "| --- | --- |\n| Achieve Money Market | 4.00% |\n"),
                       ("https://example.test/b", "Savings Rates",
                        "# Savings Rates\n| Account Type | Dividend Rate |\n"
                        "| --- | --- |\n| Achieve Money Market | 3.20% |\n"))
    products = snapshot("six", "2026-09-28",
                        ("https://example.test/a", "Savings Rates",
                         "# Savings Rates\n| Account Type | APY |\n| --- | --- |\n"
                         "| Share Savings | 0.20% |\n| Bright Savings | 2.20% |\n"))
    measures = snapshot("seven", "2026-09-28",
                        ("https://example.test/a", "Savings Rates",
                         "# Savings Rates\n| Account Type | Dividend Rate | APY |\n"
                         "| --- | --- | --- |\n| Share Savings | 0.15% | 0.20% |\n"))
    missing = replace(confirmed.claims[1], identity=FactIdentity.make(
        "mortgage", "mortgage", "interest_rate", term="30 year", transaction="purchase"))
    fixture = (
        (confirmed.claims, "confirmed_simultaneous"),
        (tiered.claims, "non_conflict"),
        (products.claims, "non_conflict"),
        (measures.claims, "non_conflict"),
        (stale.claims, "stale_or_superseded"),
        (undated.claims, "unresolved"),
        (periods.claims, "unresolved"),
        ((confirmed.claims[0], missing), "unresolved"),
    )
    observed = [classify_pair(*pair) for pair, _ in fixture]
    gold = [label for _, label in fixture]
    assert observed == gold
    positives = sum(x == "confirmed_simultaneous" for x in observed)
    assert positives == sum(x == "confirmed_simultaneous" for x in gold) == 1
    assert TemporalResolver([confirmed]).resolve(pattern(confirmed.claims[0]),
                                                  "2026-09-28", "one").winner is None
    assert TemporalResolver([stale]).discover_stale_candidates("three")[0].classification == "stale_or_superseded"


def test_review_grouping_keeps_every_comparison_claim_and_snapshot():
    def paired(sid):
        index = snapshot(sid, "2026-09-28",
                         ("https://example.test/a", "Home Rates",
                          "# Home Rates\n| Program | Rate | APR |\n| --- | --- | --- |\n"
                          "| Purchase: 30-Year Fixed Conventional $100,000 or less | 6.00% | 6.10% |\n"),
                         ("https://example.test/b", "Home Rates",
                          "# Home Rates\n| Program | Rate | APR |\n| --- | --- | --- |\n"
                          "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.00% | 7.10% |\n"))
        return index
    records = []
    for index in (paired("old"), paired("new")):
        by_measure = {}
        for claim in index.claims:
            by_measure.setdefault(claim.identity.measure, []).append(claim)
        for measure, claims in by_measure.items():
            records.append(ConflictCandidate(measure, tuple(c.value.raw for c in claims),
                                             tuple(claims), "unknown", None, "review"))
    grouped = group_review_records(records)
    assert len(records) == 4 and len(grouped) == 1
    assert grouped[0].comparisons == tuple(records)
    assert len(grouped[0].claims) == 8  # No claim is silently discarded.
    assert set(grouped[0].snapshot_ids) == {"old", "new"}
    assert {c.url for c in grouped[0].claims} == {"https://example.test/a", "https://example.test/b"}

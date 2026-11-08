"""Synthetic resolver tests independent of the submission answer path."""
from types import SimpleNamespace

from solution.facts import FactIdentity, FactIndex
from solution.resolver import FactPattern, TemporalResolver, qualifier_match, relationship


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

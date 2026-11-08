"""Synthetic tests for the Phase 4A inventory; no supplied snapshots required."""
from types import SimpleNamespace

from solution.facts import (FactIdentity, FactIndex, extract_claims, parse_date,
                            temporal_relationship)


def page(text, url="https://example.test/rates", title="Rates", fetched="2026-09-28T02:00:00Z"):
    return SimpleNamespace(markdown=text, url=url, title=title, fetched_at=fetched)


def claims(text, **kwargs):
    return extract_claims(page(text, **kwargs), "snapshot-five", "2026-09-28")


def test_explicit_dates_are_validated_and_distinct_from_crawl_date():
    assert parse_date("Rates are effective as of 09/25/2026.") == "2026-09-25"
    assert parse_date("April 06, 2026") == "2026-04-06"
    assert parse_date("2026-04-06") == "2026-04-06"
    assert parse_date("09/31/2026") is None
    assert parse_date("No date supplied") is None
    text = ("April 06, 2026\n# Home Rates\n| Program | Rate | APR |\n"
            "| --- | --- | --- |\n| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.27% |\n"
            "Rates are effective as of 09/25/2026.\nOffer expires 10/01/2026.\n")
    rate = next(c for c in claims(text) if c.identity.measure == "interest_rate")
    assert rate.dates.snapshot_on == "2026-09-28"
    assert rate.dates.fetched_at == "2026-09-28T02:00:00Z"
    assert rate.dates.published_on == "2026-04-06"
    assert rate.dates.effective_on == "2026-09-25"
    assert rate.dates.expires_on == "2026-10-01"
    assert rate.dates.reporting_year is None


def test_effective_dates_are_scoped_to_their_own_rate_table():
    text = ("# Rates\nShare Savings\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n| Share Savings | $25,000+ | 0.20% | 0.20% |\n"
            "Rates are effective as of 06/15/2026.\n\nBright Savings\n"
            "| Account Type | Balance Tier | Dividend Rate | APY |\n| --- | --- | --- | --- |\n"
            "| Bright Savings | $0.01 – $1,000,000 | 2.23% | 2.25% |\n"
            "Rates are effective as of 06/02/2026.\n")
    items = claims(text)
    share = next(c for c in items if c.identity.subject == "share savings" and c.identity.measure == "apy")
    bright = next(c for c in items if c.identity.subject == "bright savings" and c.identity.measure == "apy")
    assert share.dates.effective_on == "2026-06-15"
    assert bright.dates.effective_on == "2026-06-02"
    assert share.identity != bright.identity
    assert share.value.value != bright.value.value


def test_missing_dates_stay_unknown_and_do_not_imply_overlap():
    table = ("# Mortgage Rates\n| Program | Rate |\n| --- | --- |\n"
             "| Purchase: 30-Year Fixed Conventional $100,000 or less | {rate} |\n")
    older = claims(table.format(rate="6.00%"))[0]
    newer = claims(table.format(rate="7.00%") + "Rates are effective as of 09/25/2026.\n")[0]
    assert older.dates.effective_on is None
    assert temporal_relationship(older, newer) == "unknown"


def test_product_tier_term_transaction_and_rate_type_have_distinct_keys():
    text = ("# Home Rates\n| Program | Rate | APR |\n| --- | --- | --- |\n"
            "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.27% |\n"
            "| Purchase: 30-Year Fixed Conventional $200,000 or less | 7.25% | 7.34% |\n"
            "| Refinance: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.28% |\n"
            "| Purchase: 15-Year Fixed Conventional $100,000 or less | 6.75% | 6.88% |\n"
            "Rates are effective as of 09/25/2026.\n")
    items = claims(text)
    assert len(items) == 8
    assert len({c.identity for c in items}) == 8
    assert all(c.identity.family == "mortgage" for c in items)
    assert any(c.identity.measure == "apr" for c in items)
    assert any(("tier", "$100000orless") in c.identity.qualifiers for c in items)
    subtype = claims("# Home Rates\n| Program | Rate |\n| --- | --- |\n"
                     "| Purchase: 30-Year Fixed FHA $100,000 or less | 6.50% |\n")
    assert subtype[0].identity != items[0].identity
    certificates = claims("# Certificate Rates\n| Certificate | APY |\n| --- | --- |\n"
                          "| 10 Month Fixed Certificate | 3.50% |\n"
                          "| 10 Month Fixed Jumbo Certificate | 3.75% |\n")
    assert len(certificates) == 2
    assert certificates[0].identity != certificates[1].identity


def test_confirmed_succession_overlap_and_unknown_are_explicit():
    def dated(rate, start, end=""):
        text = ("# Home Rates\n| Program | Rate |\n| --- | --- |\n"
                f"| Purchase: 30-Year Fixed Conventional $100,000 or less | {rate} |\n"
                f"Rates are effective as of {start}.\n" +
                (f"Offer expires {end}.\n" if end else ""))
        return claims(text)[0]
    first = dated("6.00%", "01/01/2026", "03/31/2026")
    later = dated("7.00%", "04/01/2026", "06/30/2026")
    overlap = dated("6.50%", "03/15/2026", "05/01/2026")
    open_end = dated("7.50%", "09/25/2026")
    assert temporal_relationship(first, later) == "confirmed_succession"
    assert temporal_relationship(later, overlap) == "confirmed_overlap"
    assert temporal_relationship(later, open_end) == "confirmed_succession"
    assert temporal_relationship(first, open_end) == "confirmed_succession"


def test_provenance_branch_schedule_and_reporting_year_are_preserved():
    text = ("April 19, 2024\n# Community\nIn 2023 alone:\n"
            "- **294 Dupaco employees** used a new paid benefit in 2023 called Volunteer Time Off.\n"
            "# Cedar Branch\n**Lobby** Monday: 8:30 a.m.-5 p.m.\n"
            "# About Dupaco\nDupaco has 22 branch office locations.\n")
    items = claims(text, url="https://example.test/newsroom/community", title="Community Update")
    vto = next(c for c in items if c.identity.measure == "vto_employee_count")
    hours = next(c for c in items if c.identity.measure == "hours")
    branches = next(c for c in items if c.identity.measure == "branch_office_count")
    assert vto.dates.reporting_year == 2023
    assert ("reporting_year", "2023") in vto.identity.qualifiers
    assert vto.dates.published_on == "2024-04-19"
    assert "**294 Dupaco employees**" in vto.evidence
    assert vto.url == "https://example.test/newsroom/community"
    assert vto.section == "community" and vto.line == 4
    assert hours.identity.subject == "cedar branch"
    assert ("service", "lobby") in hours.identity.qualifiers
    assert ("day", "monday") in hours.identity.qualifiers
    assert branches.value.value == 22


def test_named_branch_in_article_keeps_lobby_and_drive_up_separate():
    text = ("# Branch reopening\nThe Platteville Dupaco branch is located downtown. Branch hours are:\n"
            "**Lobby** Monday-Thursday: 8:30 a.m.–5 p.m.\n"
            "**Drive-up** Monday-Friday: 8 a.m.–6 p.m.\n")
    items = [c for c in claims(text) if c.identity.family == "branch"]
    assert len(items) == 2
    assert all(c.identity.subject == "platteville branch" for c in items)
    assert {dict(c.identity.qualifiers)["service"] for c in items} == {"lobby", "drive-up"}
    assert {dict(c.identity.qualifiers)["day"] for c in items} == {"monday-thursday", "monday-friday"}
    assert len({c.identity for c in items}) == 2


def test_fact_index_is_deterministic_and_snapshot_isolated():
    source = page("# Rates\n| Account Type | Balance Tier | APY |\n| --- | --- | --- |\n"
                  "| Share Savings | $25,000+ | 0.20% |\n", title="Savings")
    one = SimpleNamespace(snapshot_id="one", taken_on="2026-01-01", pages=[source])
    two = SimpleNamespace(snapshot_id="two", taken_on="2026-03-01", pages=[source])
    a, again, b = FactIndex(one), FactIndex(one), FactIndex(two)
    assert [c.id for c in a.claims] == [c.id for c in again.claims]
    assert [c.id for c in a.claims] != [c.id for c in b.claims]
    assert a.get(a.claims[0].identity) == a.claims
    assert a.by_family["deposit"] == a.claims
    assert a.claims[0].dates.snapshot_on == "2026-01-01"
    assert b.claims[0].dates.snapshot_on == "2026-03-01"
    assert a.get(FactIdentity.make("card", "visa", "apr")) == ()

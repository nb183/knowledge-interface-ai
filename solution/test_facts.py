"""Synthetic tests for the Phase 4A inventory; no supplied snapshots required."""
import json
from pathlib import Path
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


def test_ira_identity_keeps_account_term_size_and_minimum_distinct():
    body = ("# IRA Account Rates\nSpecial IRAs\n"
            "| Term | Dividend Rate | APY | Min. to Earn |\n| --- | --- | --- | --- |\n"
            "| Roth: 10 Month Fixed Jumbo Special | 3.80% | 3.90% | $100,000 |\n"
            "| Roth: 10 Month Fixed Special | 3.70% | 3.80% | $1,000.00 |\n"
            "| Traditional: 10 Month Fixed Special | 3.60% | 3.70% | $1,000 |\n"
            "| Roth: 25 Month Fixed Special | 3.50% | 3.60% | $1,000 |\n")
    rates = [c for c in claims(body) if c.identity.measure == "dividend_rate"]
    assert len(rates) == len({c.identity for c in rates}) == 4
    assert {dict(c.identity.qualifiers)["min_deposit"] for c in rates} == {"100000", "1000"}
    assert {dict(c.identity.qualifiers)["account_type"] for c in rates} == {"roth", "traditional"}
    assert {dict(c.identity.qualifiers)["term"] for c in rates} == {"10 month", "25 month"}
    assert {dict(c.identity.qualifiers)["size"] for c in rates} == {"jumbo", "ordinary"}


def test_rate_columns_keep_measure_and_promotion_period_distinct():
    body = ("# Savings Rates\n| Account Type | Balance Tier | Dividend Rate for 1st 6 Months | "
            "Annual Percentage Yield (APY) for 1st 6 Months | Dividend Rate After Promotional Period | "
            "Annual Percentage Yield (APY) After Promotional Period |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| Achieve Money Market | $25,000-$99,999.99 | 4.00% | 3.66% | 3.20% | 3.25% |\n")
    items = claims(body)
    assert len(items) == len({c.identity for c in items}) == 4
    assert {(c.identity.measure, dict(c.identity.qualifiers)["rate_period"]) for c in items} == {
        ("dividend_rate", "1st 6 months"), ("apy", "1st 6 months"),
        ("dividend_rate", "after promotional period"), ("apy", "after promotional period")}
    assert all(c.evidence.startswith("| Achieve Money Market") for c in items)


def test_reused_balance_tier_keeps_savings_and_checking_products_separate():
    body = ("# Rates\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n"
            "| Share Savings | $25,000+ | 0.20% | 0.20% |\n"
            "| 1st Rate Checking | $25,000+ | 0.10% | 0.10% |\n"
            "Investor's Choice\n| Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- |\n| $25,000+ | 0.35% | 0.35% |\n")
    items = claims(body)
    assert len(items) == 6
    assert {c.identity.subject for c in items} == {
        "share savings", "1st rate checking", "investor's choice"}
    assert len({c.identity for c in items}) == 6


def test_certificate_same_term_but_different_minimum_is_distinct():
    body = ("# Certificate Rates\n| Certificate | APY | Min. to Earn |\n"
            "| --- | --- | --- |\n"
            "| 10 Month Fixed Certificate | 3.50% | $1,000 |\n"
            "| 10 Month Fixed Certificate | 3.75% | $5,000.00 |\n")
    items = claims(body)
    assert len(items) == 2 and items[0].identity != items[1].identity
    assert {dict(c.identity.qualifiers)["min_deposit"] for c in items} == {"1000", "5000"}


def test_labelled_card_apr_range_is_one_typed_value():
    body = ("# Credit Card Rates\n| Card Type | APR |\n| --- | --- |\n"
            "| Platinum Visa | 12.50% – 19.50%2 |\n")
    items = claims(body)
    assert len(items) == 1
    assert items[0].identity.measure == "apr"
    assert items[0].value.kind == "percent_range"
    assert items[0].value.value == "12.50-19.50"
    assert items[0].value.raw == "12.50% – 19.50%2"


def test_irregular_empty_and_ambiguous_rate_cells_keep_diagnostics():
    body = ("# Home Rates\n| Program | Rate / APR | APR |\n| --- | --- | --- |\n"
            "| Purchase: 30-Year Fixed Conventional | 2.00% | |\n"
            "| Purchase: 20-Year Fixed Conventional | 3.00% | 3.10% | extra |\n"
            "| Purchase: 15-Year Fixed Conventional | 2.00% | |\n")
    notes = []
    assert extract_claims(page(body), "one", "2026-09-28", notes) == ()
    assert {note.reason for note in notes} == {
        "ambiguous_rate_column", "empty_rate_cell", "irregular_table_row"}
    assert all(note.url == "https://example.test/rates" and note.line > 0 for note in notes)


def _snapshot_page_with(*fragments):
    root = Path(__file__).resolve().parents[1] / "snapshots"
    for folder in sorted(root.iterdir(), reverse=True):
        manifest = folder / "manifest.json"
        if not manifest.exists():
            continue
        meta = json.loads(manifest.read_text())
        for item in meta["pages"]:
            body = (folder / item["file"]).read_text()
            if all(fragment in body for fragment in fragments):
                return page(body, url=item["url"], title=item.get("title", "")), meta
    raise AssertionError(f"No supplied snapshot page contains {fragments}")


def test_real_ira_rows_do_not_collapse_jumbo_and_ordinary():
    source, meta = _snapshot_page_with("Roth: 10 Month Fixed Jumbo Special", "Roth: 10 Month Fixed Special")
    items = extract_claims(source, meta["snapshot_id"], meta["taken_on"])
    matched = [c for c in items if "roth: 10 month fixed" in c.evidence.lower()
               and c.identity.measure == "dividend_rate"]
    assert len(matched) >= 2
    assert len({c.identity for c in matched}) == len(matched)


def test_real_promotional_columns_have_distinct_periods():
    source, meta = _snapshot_page_with("Dividend Rate for 1st 6 Months", "Dividend Rate After Promotional Period")
    items = extract_claims(source, meta["snapshot_id"], meta["taken_on"])
    promo = [c for c in items if "achieve money market" in c.evidence.lower()
             and "$250,000-$1,000,000" in c.evidence]
    periods = {dict(c.identity.qualifiers).get("rate_period") for c in promo}
    assert "1st 6 months" in periods and "after promotional period" in periods
    assert len({c.identity for c in promo}) >= 4

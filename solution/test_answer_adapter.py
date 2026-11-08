"""Shadow behavior and snapshot-backed request checks."""
import json
from pathlib import Path

from solution.solution import build
from harness.contract import Page, Snapshot


def page(url, body, title="Rates"):
    return Page(url, title, body, "2026-09-28T00:00:00Z")


def rate(value, date, cap="$100,000 or less"):
    return ("# Home Rates\n| Program | Rate |\n| --- | --- |\n"
            f"| Purchase: 30-Year Fixed Conventional {cap} | {value} |\n"
            f"Rates are effective as of {date}.\n")


def test_shadow_preserves_answer_and_historical_snapshot():
    ks = build()
    for sid, date, value in (("old", "2026-05-01", "6.00%"), ("new", "2026-09-28", "7.13%")):
        ks.ingest(Snapshot(sid, date, [page("https://example.test/home", rate(value, "04/22/2026"), "Home Rates")]))
    question = "What is the purchase 30-year mortgage rate for a $90,000 loan?"
    old = ks.answer(question, as_of="2026-05-01")
    record = ks.shadow_diagnostics[-1]
    assert record.snapshot_id == "old" and record.target_date == "2026-05-01"
    assert all(c.snapshot_id == "old" for c in record.supporting_claims)
    assert "6.00%" in old.text and "7.13%" not in old.text
    new = ks.answer(question)
    assert "7.13%" in new.text and ks.shadow_diagnostics[-1].snapshot_id == "new"
    assert not ks.shadow_errors


def test_multi_tier_shadow_requires_coherent_complete_schedule():
    ks = build()
    body = ("# Home Rates\n| Program | Rate |\n| --- | --- |\n"
            "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% |\n"
            "| Purchase: 30-Year Fixed Conventional $200,000 or less | 7.25% |\n"
            "Rates are effective as of 09/25/2026.\n")
    ks.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/home", body, "Home Rates")]))
    question = "What is the purchase 30-year mortgage interest rate?"
    existing = ks.answer(question)
    record = ks.shadow_diagnostics[-1]
    assert record.eligible and len(record.supporting_claims) == 2
    assert "7.13%" in record.proposed_answer and "7.25%" in record.proposed_answer
    assert record.existing_answer == existing.text

    single = build()
    single.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/home", rate("7.13%", "09/25/2026"), "Home Rates")]))
    single.answer(question)
    assert not single.shadow_diagnostics[-1].eligible
    assert single.shadow_diagnostics[-1].reason == "incomplete_multi_tier_schedule"


def test_conflicting_rates_and_unpublished_arm_are_rejected():
    ks = build()
    ks.ingest(Snapshot("one", "2026-09-28", [
        page("https://example.test/a", rate("7.13%", "09/25/2026"), "Home Rates"),
        page("https://example.test/b", rate("7.25%", "09/25/2026"), "Home Rates")]))
    ks.answer("What is the purchase 30-year mortgage rate for a $90,000 loan?")
    assert not ks.shadow_diagnostics[-1].eligible
    ks.answer("What is the rate for a 7/1 adjustable-rate mortgage?")
    assert not ks.shadow_diagnostics[-1].eligible
    assert ks.shadow_diagnostics[-1].reason == "unsupported_or_missing_term"
    ks.answer("What is the special purchase 30-year mortgage rate for a $90,000 loan?")
    assert ks.shadow_diagnostics[-1].reason == "incompatible_mortgage_product"
    ks.answer("What is the adjustable purchase 30-year mortgage rate for a $90,000 loan?")
    assert ks.shadow_diagnostics[-1].reason == "incompatible_mortgage_product"


def test_branch_count_requires_corrobation():
    ks = build()
    body = "April 06, 2026\n# About\nDupaco has 22 branch office locations.\n"
    ks.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/about", body, "About")]))
    ks.answer("How many branches does Dupaco have?")
    assert not ks.shadow_diagnostics[-1].eligible
    assert ks.shadow_diagnostics[-1].reason == "uncorroborated_branch_count"


def test_named_savings_tier_and_reporting_year_are_checked():
    ks = build()
    savings = ("# Savings Rates\n| Account Type | Balance Tier | APY |\n| --- | --- | --- |\n"
               "| Share Savings | $25,000+ | 0.20% |\n"
               "| Health Savings Account | $25,000+ | 0.35% |\n"
               "Rates are effective as of 06/15/2026.\n")
    vto = ("April 19, 2024\n# Impact\nIn 2023 alone:\n"
           "- **294 Dupaco employees** used Volunteer Time Off in 2023.\n")
    ks.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/savings", savings, "Savings Rates"),
                                                 page("https://example.test/impact", vto, "Impact")]))
    ks.answer("What is the Share Savings APY for $25,000?")
    assert ks.shadow_diagnostics[-1].eligible
    assert ks.shadow_diagnostics[-1].supporting_claims[0].identity.subject == "share savings"
    ks.answer("What is the Share Savings APY for balances between $20,000 and $30,000?")
    assert ks.shadow_diagnostics[-1].reason == "savings_product_or_tier_not_verified"
    ks.answer("Do any savings tiers pay 0.35% APY?")
    assert not ks.shadow_diagnostics[-1].eligible
    ks.answer("How many employees used Volunteer Time Off in 2023?")
    assert ks.shadow_diagnostics[-1].eligible
    assert ks.shadow_diagnostics[-1].supporting_claims[0].dates.reporting_year == 2023
    ks.answer("How many employees used Volunteer Time Off last year?")
    assert ks.shadow_diagnostics[-1].reason == "ambiguous_reporting_year"


def test_real_snapshot_shadow_never_crosses_selected_snapshot():
    root = Path(__file__).resolve().parents[1] / "snapshots"
    dirs = sorted(p for p in root.iterdir() if (p / "manifest.json").exists())
    if len(dirs) < 2:
        return
    ks = build()
    for folder in (dirs[0], dirs[-1]):
        manifest = json.loads((folder / "manifest.json").read_text())
        pages = [Page(p["url"], p.get("title", ""), (folder / p["file"]).read_text(),
                      p.get("fetched_at", "")) for p in manifest["pages"]]
        ks.ingest(Snapshot(manifest["snapshot_id"], manifest["taken_on"], pages))
    for question, as_of in (("What is the purchase 30-year mortgage rate?", dirs[0].name[3:13]),
                            ("How many branches does Dupaco have?", None),
                            ("Do any savings tiers pay 0.35% APY?", None),
                            ("How many employees used Volunteer Time Off in 2023?", None),
                            ("What is the 7/1 adjustable-rate mortgage rate?", None)):
        ks.answer(question, as_of=as_of)
        record = ks.shadow_diagnostics[-1]
        assert all(c.snapshot_id == record.snapshot_id for c in record.supporting_claims)
        assert not ks.shadow_errors


def test_unspecified_mortgage_transaction_requires_matching_explicit_schedules():
    def schedule(purchase, refinance):
        return ("# Rates\n## Home Loans - Purchase\n| Program | Rate |\n| --- | --- |\n"
                f"| Purchase: 30-Year Fixed Conventional $100,000 or less | {purchase[0]} |\n"
                f"| Purchase: 30-Year Fixed Conventional $200,000 or less | {purchase[1]} |\n"
                "Rates are effective as of 09/25/2026.\n"
                "## Home Loans - Refinance\n| Program | Rate |\n| --- | --- |\n"
                f"| Refinance: 30-Year Fixed Conventional $100,000 or less | {refinance[0]} |\n"
                f"| Refinance: 30-Year Fixed Conventional $200,000 or less | {refinance[1]} |\n"
                "Rates are effective as of 09/25/2026.\n")
    question = "What is the interest rate for a 30-year mortgage?"
    ks = build()
    ks.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/rates", schedule(
        ("7.13%", "7.25%"), ("7.13%", "7.25%")))]))
    ks.answer(question)
    record = ks.shadow_diagnostics[-1]
    assert record.eligible and len(record.supporting_claims) == 4
    assert "purchase and refinance" in record.proposed_answer
    assert {dict(c.identity.qualifiers)["transaction"] for c in record.supporting_claims} == {"purchase", "refinance"}
    different = build()
    different.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/rates", schedule(
        ("7.13%", "7.25%"), ("7.13%", "7.50%")))]))
    different.answer(question)
    assert different.shadow_diagnostics[-1].reason == "different_transaction_schedules"
    one = build()
    one.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/rates", schedule(
        ("7.13%", "7.25%"), ("7.13%", "7.25%")).split("## Home Loans - Refinance")[0])]))
    one.answer(question)
    assert one.shadow_diagnostics[-1].reason == "ambiguous_mortgage_transaction"


def test_untiered_savings_needs_named_complete_schedule():
    body = ("# Savings Rates\n## Share Savings\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n"
            "| Share Savings | $0 – $99.99 | 0.00% | 0.00% |\n"
            "| Share Savings | $100 – $24,999.99 | 0.15% | 0.15% |\n"
            "| Share Savings | $25,000+ | 0.20% | 0.20% |\n"
            "Rates are effective as of 06/15/2026.\n"
            "## Bright Savings\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n"
            "| Bright Savings | $0.01 – $1,000,000 | 2.23% | 2.25% |\n"
            "Rates are effective as of 06/02/2026.\n")
    ks = build()
    ks.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/rates", body)]))
    ks.answer("What is the highest Share Savings APY?")
    assert ks.shadow_diagnostics[-1].eligible
    assert "0.20%" in ks.shadow_diagnostics[-1].proposed_answer
    ks.answer("What is the rate for Bright Savings?")
    assert ks.shadow_diagnostics[-1].eligible
    assert "2.23%" in ks.shadow_diagnostics[-1].proposed_answer
    assert "2.25%" in ks.shadow_diagnostics[-1].proposed_answer
    ks.answer("What is the Share Savings rate?")
    assert ks.shadow_diagnostics[-1].reason == "savings_tier_unspecified"
    ks.answer("Do any savings tiers pay 0.35% APY?")
    assert ks.shadow_diagnostics[-1].reason == "savings_product_unspecified_or_ambiguous"
    incomplete = build()
    incomplete.ingest(Snapshot("one", "2026-09-28", [page("https://example.test/rates", body.replace(
        "| Share Savings | $25,000+ | 0.20% | 0.20% |\n", ""))]))
    incomplete.answer("What is the highest Share Savings APY?")
    assert incomplete.shadow_diagnostics[-1].reason == "incomplete_savings_schedule"

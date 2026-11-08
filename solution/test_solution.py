import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from contract import Answer, Page, Snapshot
from solution.solution import (build, links, parse_request, passages,
                               qualifier_state, words)


def page(url, text, title="Rates"):
    return Page(url, title, text, "2026-01-01T00:00:00Z")


def test_historical_full_snapshot_selection():
    ks = build()
    ks.ingest(Snapshot("old", "2026-01-01", [page("https://example.test/rates", "# Certificates\n\n| Term | APY |\n| --- | --- |\n| 10 month jumbo | 3.90% |")]))
    ks.ingest(Snapshot("new", "2026-03-01", [page("https://example.test/rates", "# Certificates\n\n| Term | APY |\n| --- | --- |\n| 10 month jumbo | 4.00% |")]))
    old = ks.answer("10 month jumbo certificate APY", as_of="2026-02-01")
    new = ks.answer("10 month jumbo certificate APY")
    assert isinstance(old, Answer)
    assert "3.90%" in old.text and "4.00%" not in old.text
    assert "4.00%" in new.text and "3.90%" not in new.text
    assert old.sources[0].snapshot_id == "old"
    assert new.sources[0].snapshot_id == "new"


def test_table_rows_keep_column_headers():
    p = page("https://example.test/table", "# Savings\n\n| Account | Balance Tier | Dividend Rate | APY |\n| --- | --- | --- | --- |\n| Share Savings | $10,000-$24,999 | 0.15% | 0.15% |")
    rows = [x for x in passages(p, "one") if x.kind == "table"]
    assert len(rows) == 1
    assert "Balance Tier: $10,000-$24,999" in rows[0].text
    assert "Dividend Rate: 0.15%" in rows[0].text
    assert "APY: 0.15%" in rows[0].text


def test_provenance_and_repeat_determinism():
    ks = build()
    ks.ingest(Snapshot("crawl", "2026-01-01", [page("https://example.test/fees", "# Fees\n\nLost debit card replacement fee is $5.", "Fees")]))
    first = ks.answer("lost debit card replacement fee")
    second = ks.answer("lost debit card replacement fee")
    assert first == second
    assert first.sources[0].url == "https://example.test/fees"
    assert first.sources[0].snapshot_id == "crawl"
    assert "Lost debit card" in first.sources[0].quote


def test_unknown_abstains_and_pre_snapshot_date_abstains():
    ks = build()
    ks.ingest(Snapshot("crawl", "2026-01-01", [page("https://example.test/fees", "# Fees\n\nLost debit card replacement fee is $5.")]))
    assert not ks.answer("What is the rate for a submarine loan?").confident
    assert not ks.answer("lost debit card replacement fee", as_of="2025-12-31").confident


def test_heading_context_retrieves_mortgage_row_without_inventing_a_rate():
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/home-loans",
        "# Home Rates\n\n| Program | Rate | APR |\n| --- | --- | --- |\n"
        "| 30-Year Fixed | 7.13% | 7.27% |", "Home Loans")]))
    ranked = ks.snapshots[0].search("mortgage 30-year rate")
    assert ranked[0][1].kind == "table"
    assert "Home Rates" in ranked[0][1].context
    assert "Program: 30-Year Fixed" in ranked[0][1].text
    assert "Rate: 7.13%" in ks.answer("mortgage 30-year rate").text

    empty = build()
    empty.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/home-loans", "# Mortgage Rates\n\nGeneral guidance about borrowing.")]))
    assert not empty.answer("mortgage rate").confident


def test_branch_labels_and_addresses_do_not_cross_section_boundaries():
    content = ("# Locations\n\n### Credit Union\n\nBranch\n\n"
               "11375 Oldenburg Ln., Galena, IL, 61036\n\n**Lobby**\n\n"
               "Monday: 8:30 a.m.-5 p.m.\n\n### Credit Union\n\nBranch\n\n"
               "999 Second St., Dubuque, IA, 52001\n\n**Lobby**\n\n"
               "Monday: 9 a.m.-4 p.m.\n\n### Credit Union ATM\n\n"
               "Surcharge-Free ATM\n\n937 Galena Square Dr., Galena, IL, 61036\n")
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/contact", content, "Find a Branch")]))
    ranked = ks.snapshots[0].search("Galena branch lobby hours")
    assert "8:30 a.m." in ranked[0][1].text
    assert "11375 Oldenburg" in ranked[0][1].context
    assert "999 Second" not in ranked[0][1].context
    answer = ks.answer("Galena branch lobby hours")
    assert "8:30 a.m." in answer.text and "9 a.m." not in answer.text
    assert "11375 Oldenburg" in ks.answer("Galena branch address").text
    later = [p for p in ks.snapshots[0].passages if "9 a.m.-4 p.m." in p.text][0]
    assert "999 Second" in later.context
    assert "11375 Oldenburg" not in later.context
    atm = [p for p in ks.snapshots[0].passages if "937 Galena Square" in p.text][0]
    assert "ATM" in atm.context and "11375 Oldenburg" not in atm.context
    assert not ks.answer("Mullan Avenue branch lobby hours").confident


def test_markdown_link_target_is_literal_and_cited_from_its_page():
    target = "https://join.example.test/start?product=share&step=2#confirm"
    assert links(f"[Join online]({target})") == (("Join online", target),)
    assert words("Dupaco's") == ["dupaco"]
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/about/join", f"# Membership\n\n[Join online]({target})",
        "Become a Member")]))
    answer = ks.answer("Where can I join online?")
    assert answer.text == target
    assert answer.sources[0].url == "https://example.test/about/join"
    assert answer.sources[0].snapshot_id == "one"
    assert answer.sources[0].quote == target


def test_link_targets_remain_isolated_by_snapshot():
    ks = build()
    for sid, date, destination in (
            ("old", "2026-01-01", "https://join.example.test/start?version=old#apply"),
            ("new", "2026-03-01", "https://join.example.test/start?version=new#apply")):
        ks.ingest(Snapshot(sid, date, [page(
            "https://example.test/about/join", f"# Membership\n\n[Join online]({destination})",
            "Become a Member")]))
    old = ks.answer("Where can I join online?", as_of="2026-02-01")
    new = ks.answer("Where can I join online?")
    assert old.text == "https://join.example.test/start?version=old#apply"
    assert new.text == "https://join.example.test/start?version=new#apply"
    assert old.sources[0].snapshot_id == "old"
    assert new.sources[0].snapshot_id == "new"


def test_repeated_navigation_does_not_outrank_supported_fact():
    content = ("# Branch Hours\n\n" + "Find a branch near you.\n\n" * 5
               + "Lobby hours: Monday 8:30 a.m. to 5 p.m.\n")
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/contact", content, "Contact")]))
    ranked = ks.snapshots[0].search("branch lobby hours")
    assert "8:30 a.m." in ranked[0][1].text
    assert "8:30 a.m." in ks.answer("branch lobby hours").text


def test_procedure_answer_needs_action_evidence_in_passage():
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/contact", "# Update Contact Information\n\nWeb: Contact Us")]))
    assert not ks.answer("How can I update my contact information?").confident


def test_request_qualifiers_have_required_unspecified_and_incompatible_states():
    broad = parse_request("What are the mortgage rates?")
    assert broad.intent == "mortgage_overview"
    assert broad.qualifiers["term"] is None
    assert qualifier_state(None, "30 year") == "unspecified"
    assert qualifier_state("30 year", "30 year") == "required"
    assert qualifier_state("30 year", "15 year") == "incompatible"
    specific = parse_request("What is the rate for a 7/1 adjustable-rate mortgage?")
    assert specific.qualifiers["term"] == "7/1"
    branch = parse_request("What are Cedar Branch drive-up hours?")
    assert branch.qualifiers["branch"] == "cedar"
    assert branch.qualifiers["service"] == "drive-up"


def test_mortgage_selection_uses_matching_standard_term_and_refuses_missing_arm_rate():
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/home-loans",
        "# Home Rates\n\n| Program | Rate | APR |\n| --- | --- | --- |\n"
        "| Mortgage Relief Program for qualified borrowers | 4.50% | 4.70% |\n"
        "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 7.27% |\n"
        "| Purchase: 30-Year Fixed Conventional $200,000 or less | 7.25% | 7.34% |\n"
        "| Purchase: 15-Year Fixed Conventional | 6.75% | 6.88% |\n\n"
        "An adjustable mortgage can change by up to 2% each year.", "Home Loans"),
        page("https://example.test/rates", "# Home Rates\n\n| Program | Rate |\n| --- | --- |\n"
             "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.130% |", "Home Loans")]))
    answer = ks.answer("What is the rate on a 30-year mortgage?")
    assert "7.13%" in answer.text and "7.25%" in answer.text and "4.50%" not in answer.text
    small = ks.answer("What is the rate on a 30-year mortgage for a $90,000 loan?")
    large = ks.answer("What is the rate on a 30-year mortgage for a $150,000 loan?")
    assert "7.13%" in small.text and "7.25%" not in small.text
    assert "7.25%" in large.text and "7.13%" not in large.text
    assert not ks.answer("What is the rate on a 7/1 adjustable-rate mortgage?").confident

    exact = build()
    exact.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/home-loans", "# Home Rates\n\n| Program | Rate |\n| --- | --- |\n"
        "| 7/1 Adjustable Mortgage | 6.25% |", "Home Loans")]))
    assert "6.25%" in exact.answer("What is the rate on a 7/1 adjustable-rate mortgage?").text


def test_program_definition_prefers_substantive_benefit_over_application_link():
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/pathway", "# Pathway Program\n\n"
        "For more information about the Pathway program and to apply, visit example.test/apply.\n\n"
        "The Pathway program provides up to $2,000 in assistance toward closing costs for eligible buyers.",
        "Pathway Program")]))
    answer = ks.answer("What is the Pathway program?")
    assert "$2,000" in answer.text
    assert "For more information" not in answer.text
    assert answer.sources[0].url == "https://example.test/pathway"


def test_hours_keep_branch_and_service_and_compare_distinct_schedules():
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [
        page("https://example.test/cedar", "# Cedar Branch\n\n**Lobby** Monday: 8:30 a.m.-5 p.m.\n\n"
             "Saturday: 8:30 a.m.-12:30 p.m.\n\n**Drive-up** Monday: 8 a.m.-6 p.m.", "Cedar Branch"),
        page("https://example.test/maple", "# Maple Branch\n\n**Lobby** Monday: 9 a.m.-5 p.m.\n\n"
             "Saturday: 9 a.m.-noon", "Maple Branch")]))
    cedar = ks.answer("What are Cedar Branch lobby hours?")
    assert "8:30 a.m." in cedar.text and "8 a.m.-6 p.m." not in cedar.text
    assert all(s.url == "https://example.test/cedar" for s in cedar.sources)
    comparison = ks.answer("Do all branches keep the same lobby hours?")
    assert "8:30" in comparison.text and "9 a.m." in comparison.text
    assert "Cedar Branch" in comparison.text and "Maple Branch" in comparison.text
    assert len(comparison.sources) == 2


def test_list_deduplicates_compatible_card_rates_and_abstains_on_conflict():
    personal = "# Cards\n\n### Travel Visa\n\n- Ongoing rate: 12.50%–19.50% APR.\n\n12.50% APR is the starting rate."
    business = "# Cards\n\n| Card Type | APR |\n| --- | --- |\n| Business Cashback Visa | 20.50% |"
    ks = build()
    ks.ingest(Snapshot("one", "2026-01-01", [
        page("https://example.test/personal/cards", personal, "Personal Credit Cards"),
        page("https://example.test/business/cards", business, "Business Credit Cards")]))
    answer = ks.answer("What credit cards do you offer and what are their APRs?")
    assert answer.text.count("Travel Visa") == 1
    assert "Business Cashback Visa" in answer.text
    assert len(answer.sources) == 2
    assert any("12.50%" in source.quote for source in answer.sources)
    assert not ks.unresolved_evidence[("one", "What credit cards do you offer and what are their APRs?")]

    contradictory = build()
    contradictory.ingest(Snapshot("one", "2026-01-01", [
        page("https://example.test/cards-a", "# Cards\n\n| Card Type | APR |\n| --- | --- |\n| Travel Visa | 12.50% |", "Credit Cards"),
        page("https://example.test/cards-b", "# Cards\n\n| Card Type | APR |\n| --- | --- |\n| Travel Visa | 17.00% |", "Credit Cards")]))
    question = "What credit cards do you offer and what are their APRs?"
    assert not contradictory.answer(question).confident
    assert contradictory.unresolved_evidence[("one", question)]


def test_certificate_list_collects_terms_without_duplicate_rows():
    ks = build()
    table = ("# Certificate Rates\n\n| Certificate | Annual Percentage Yield (APY) |\n"
             "| --- | --- |\n| 6 Month | 3.00% |\n| 12 Month | 3.50% |")
    ks.ingest(Snapshot("one", "2026-01-01", [page(
        "https://example.test/personal/savings", table, "Certificates")]))
    answer = ks.answer("What are the certificate rates?")
    assert "6 Month: 3.00%" in answer.text
    assert "12 Month: 3.50%" in answer.text


def test_selected_list_never_uses_a_later_snapshot_for_historical_answer():
    ks = build()
    for sid, date, rate in (("old", "2026-01-01", "11.00%"),
                            ("new", "2026-03-01", "13.00%")):
        ks.ingest(Snapshot(sid, date, [page(
            "https://example.test/cards", "# Credit Cards\n\n| Card Type | APR |\n"
            f"| --- | --- |\n| Travel Visa | {rate} |", "Credit Cards")]))
    question = "What credit cards do you offer and what are their APRs?"
    old = ks.answer(question, as_of="2026-02-01")
    new = ks.answer(question)
    assert "11.00%" in old.text and "13.00%" not in old.text
    assert "13.00%" in new.text and "11.00%" not in new.text
    assert old.sources[0].snapshot_id == "old"
    assert new.sources[0].snapshot_id == "new"

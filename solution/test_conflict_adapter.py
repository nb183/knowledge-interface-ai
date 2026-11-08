"""Focused shadow conflict eligibility tests."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from harness.contract import Page, Snapshot
from harness.run_eval import load_snapshots
from solution.conflict_adapter import (ShadowCase, _deduplicate_proposals, _key,
                                       _proposal, analyze, classify_snapshot_pair)
from solution.facts import Claim, ClaimDates, FactIdentity, TypedValue
from solution.resolver import TemporalResolver
from solution.solution import build


def _site():
    site = build()
    for snapshot in load_snapshots(Path(__file__).parents[1] / "snapshots"):
        site.ingest(snapshot)
    return site


def test_same_period_real_rate_conflict_has_both_exact_sources():
    site = _site()
    latest = site.snapshots[-2]  # August snapshot
    report = analyze([latest], TemporalResolver([latest.fact_index]))
    confirmed = [case for case in report.cases if case.classification == "simultaneous"]
    assert confirmed
    case = confirmed[0]
    assert len({claim.url for claim in case.claims}) == 2
    assert len({claim.value.raw for claim in case.claims}) == 2
    assert {source.url for source in case.proposed.sources} == {claim.url for claim in case.claims}
    assert case.proposed.resolution is None


def test_stale_published_requires_visible_rows_and_qualifier_identity():
    site = _site()
    latest = site.snapshots[-1]
    report = analyze([latest], TemporalResolver([latest.fact_index]))
    stale = next(case for case in report.cases if case.classification == "stale_published")
    assert stale.claims[0].dates.effective_on < stale.claims[1].dates.effective_on
    assert stale.proposed.resolution == stale.claims[1].value.raw
    assert all(claim.url in latest.pages for claim in stale.claims)
    assert all(claim.evidence in latest.pages[claim.url].markdown for claim in stale.claims)
    altered = replace(stale.claims[0], identity=replace(
        stale.claims[0].identity, qualifiers=tuple((key, "other" if key == "term" else value)
                                                  for key, value in stale.claims[0].identity.qualifiers)))
    assert classify_snapshot_pair(altered, stale.claims[1], latest).classification == "unresolved"


def test_wrong_snapshot_and_unverified_row_never_propose():
    site = _site()
    latest = site.snapshots[-1]
    stale = next(case for case in analyze([latest], TemporalResolver([latest.fact_index])).cases
                 if case.classification == "stale_published")
    first, second = stale.claims
    assert classify_snapshot_pair(replace(first, snapshot_id="earlier"), second, latest).proposed is None
    assert classify_snapshot_pair(replace(first, evidence="| unsupported |"), second, latest).proposed is None


def test_shadow_is_deterministic_and_does_not_change_public_conflicts():
    site = _site()
    first = site.conflicts()
    report = site.conflict_shadow_diagnostics[-1]
    assert site.conflicts() == first
    assert site.conflict_shadow_diagnostics[-1] == report
    assert not site.conflict_shadow_errors


def test_refinance_subtypes_from_real_sections_remain_unresolved():
    site = _site()
    for index, distinct in ((site.snapshots[-2], "cash-out refinance"),
                            (site.snapshots[-1], "home equity refinance")):
        report = analyze([index], TemporalResolver([index.fact_index]))
        comparisons = [case for case in report.cases
                       if len(case.claims) == 2
                       and "home loan refinance" in case.claims[0].section
                       and distinct in case.claims[1].section]
        scoped = [case for case in comparisons
                  if case.rule == "different_or_unknown_section_scope"]
        assert scoped
        assert all(case.classification == "unresolved" and case.proposed is None
                   for case in scoped)
        assert not any(case.proposed is not None for case in comparisons)


def test_missing_or_ambiguous_refinance_scope_is_not_confirmed():
    site = _site()
    index = site.snapshots[-2]
    report = analyze([index], TemporalResolver([index.fact_index]))
    matching = next(case for case in report.cases
                    if case.classification == "stale_published"
                    and dict(case.claims[0].identity.qualifiers).get("transaction") == "refinance")
    old, new = matching.claims
    for section in ("rates", "rates > refinance", "rates > home equity refinance"):
        result = classify_snapshot_pair(old, replace(new, section=section), index)
        assert result.classification == "unresolved"
        assert result.rule == "different_or_unknown_section_scope"
        assert result.proposed is None
    assert classify_snapshot_pair(old, new, index).classification == "stale_published"


def _synthetic_claim(sid, url, value, effective, *, measure="interest_rate", transaction="purchase"):
    identity = FactIdentity.make("mortgage", "mortgage", measure, loan_type="conventional",
                                 program="standard", rate_structure="fixed", term="30 year",
                                 transaction=transaction, tier="$100,000 or less")
    return Claim(f"{sid}:{url}:{value}:{effective}", identity,
                 TypedValue("percentage", Decimal(value.rstrip("%")), "%", value),
                 ClaimDates("2026-09-28", "2026-09-28T00:00:00Z", effective_on=effective),
                 sid, url, "Home Rates", "home loans", f"| {value} |", 4)


def _synthetic_case(old, new):
    return ShadowCase("stale_published", new.snapshot_id, (old, new), "verified_generation",
                      _proposal(old, new, "stale_published", "Later effective source"))


def test_dedup_retains_distinct_effective_generations_and_matching_evidence():
    old = _synthetic_claim("june", "https://example.test/guide", "5.00%", "2026-04-22")
    june = _synthetic_claim("june", "https://example.test/rates", "6.00%", "2026-06-08")
    august = replace(june, id="august", snapshot_id="august",
                     dates=replace(june.dates, effective_on="2026-08-26"),
                     evidence="| revised 6.00% |")
    first = _synthetic_case(old, june)
    second = _synthetic_case(replace(old, snapshot_id="august"), august)
    assert _key(first) != _key(second)
    proposals = _deduplicate_proposals([first, second])
    assert len(proposals) == 2
    for case, proposal in zip((first, second), proposals):
        assert proposal.values == [claim.value.raw for claim in case.claims]
        assert [(source.url, source.snapshot_id, source.quote) for source in proposal.sources] == [
            (claim.url, claim.snapshot_id, claim.evidence) for claim in case.claims]
        assert proposal.resolution == case.claims[1].value.raw


def test_dedup_repeated_generation_is_deterministic_and_keeps_fact_dimensions():
    old = _synthetic_claim("one", "https://example.test/guide", "5.00%", "2026-04-22")
    new = _synthetic_claim("one", "https://example.test/rates", "6.00%", "2026-06-08")
    first = _synthetic_case(old, new)
    repeated = _synthetic_case(replace(old, snapshot_id="two"),
                               replace(new, snapshot_id="two"))
    apr = _synthetic_case(replace(old, identity=replace(old.identity, measure="apr")),
                          replace(new, identity=replace(new.identity, measure="apr")))
    refinance = _synthetic_case(replace(old, identity=FactIdentity.make(
        "mortgage", "mortgage", "interest_rate", loan_type="conventional", program="standard",
        rate_structure="fixed", term="30 year", transaction="refinance", tier="$100,000 or less")),
        replace(new, identity=FactIdentity.make(
            "mortgage", "mortgage", "interest_rate", loan_type="conventional", program="standard",
            rate_structure="fixed", term="30 year", transaction="refinance", tier="$100,000 or less")))
    cases = [first, repeated, apr, refinance]
    assert len(_deduplicate_proposals(cases)) == 3
    assert _deduplicate_proposals(cases) == _deduplicate_proposals(cases)
    assert _deduplicate_proposals(cases)[0].sources[1].snapshot_id == "two"


def test_real_proposals_keep_each_claims_dates_values_and_quotes():
    site = _site()
    report = analyze(site.snapshots, TemporalResolver([s.fact_index for s in site.snapshots]))
    proposed = [case for case in report.cases if case.proposed]
    keys = {_key(case) for case in proposed}
    assert len(report.proposed) == len(keys)
    assert any(case.classification == "simultaneous" for case in proposed)
    assert any(case.classification == "stale_published" for case in proposed)
    for case in proposed:
        assert case.proposed.values == [claim.value.raw for claim in case.claims]
        assert [(source.url, source.snapshot_id, source.quote) for source in case.proposed.sources] == [
            (claim.url, claim.snapshot_id, claim.evidence) for claim in case.claims]


def test_overlapping_auto_ranges_are_review_only_and_no_cross_measure_match():
    site = build()
    product = ("# Auto Loans\nAuto Loan Rates\n"
               "| Loan Program | Term | Annual Percentage Rate (As Low As) |\n"
               "| --- | --- | --- |\n| 2024-27 Model Year | 60 Months | 5.39% APR |\n"
               "Rates are effective as of 08/31/2026.\n")
    calculator = ("# Auto Loan Calculator\nAuto Loan Rates\n"
                  "| Account Type | Balance Tier | Annual Percentage Yield |\n"
                  "| --- | --- | --- |\n| 2023–26 Model Year | 60 Months | 5.34% APR |\n"
                  "Rates are effective as of 01/01/2026.\n")
    site.ingest(Snapshot("one", "2026-09-28", [
        Page("https://example.test/product", "Auto Loans", product, "2026-09-28T00:00:00Z"),
        Page("https://example.test/calculator", "Auto Loan Calculator", calculator,
             "2026-09-28T00:00:00Z")]))
    index = site.snapshots[0]
    report = analyze([index], TemporalResolver([index.fact_index]))
    overlap = [case for case in report.cases if "overlapping_auto" in case.rule]
    assert len(overlap) == 1
    assert overlap[0].classification == "unresolved" and overlap[0].proposed is None
    assert {c.identity.measure for c in overlap[0].claims} == {"apr"}
    assert {dict(c.identity.qualifiers)["model_year_start"] for c in overlap[0].claims} == {"2023", "2024"}
    assert all(c.evidence in index.pages[c.url].markdown for c in overlap[0].claims)
    assert report.proposed == ()


def test_real_new_categories_remain_internal_and_snapshot_isolated():
    site = _site()
    for index in (site.snapshots[0], site.snapshots[-1]):
        report = analyze([index], TemporalResolver([index.fact_index]))
        new = [case for case in report.cases if "auto" in case.rule or case.rule == "new_category_review_only"]
        assert new
        assert all(case.snapshot_id == index.snapshot_id and case.proposed is None for case in new)
        assert all(claim.snapshot_id == index.snapshot_id for case in new for claim in case.claims)
    latest = site.snapshots[-1]
    assert latest.fact_index.by_family["fee"] and latest.fact_index.by_family["branch"]
    assert all(c.url in latest.pages for c in latest.fact_index.by_family["fee"])
    copied = {}
    for claim in latest.fact_index.by_family["fee"]:
        copied.setdefault((claim.identity, claim.value.value), set()).add(claim.url)
    assert any(len(urls) > 1 for urls in copied.values())


def test_fee_and_branch_differences_stay_review_only_with_all_sources():
    site = build()
    fee = ("## Rate and Fee Schedule\nEffective February 17, 2025\n"
           "| Deposit Services Consumer | Col2 |\n| --- | --- |\n"
           "| Stop payment (check) | ${amount} per item |\n")
    branch = "# Cedar Branch\n**Lobby** Monday: {hours}\n"
    pages = [Page("https://example.test/fees/a", "Fee Schedule", fee.format(amount="30.00"), "2026-09-28T00:00:00Z"),
             Page("https://example.test/fees/b", "Fee Schedule", fee.format(amount="35.00"), "2026-09-28T00:00:00Z"),
             Page("https://example.test/branch/a", "Branch", branch.format(hours="8 a.m.–5 p.m."), "2026-09-28T00:00:00Z"),
             Page("https://example.test/branch/b", "Branch", branch.format(hours="9 a.m.–5 p.m."), "2026-09-28T00:00:00Z")]
    site.ingest(Snapshot("one", "2026-09-28", pages))
    index = site.snapshots[0]
    report = analyze([index], TemporalResolver([index.fact_index]))
    for family in ("fee", "branch"):
        cases = [case for case in report.cases if case.claims[0].identity.family == family
                 and case.rule == "new_category_review_only"]
        assert len(cases) == 1
        assert cases[0].proposed is None
        assert len({claim.url for claim in cases[0].claims}) == 2
        assert all(claim.evidence in index.pages[claim.url].markdown for claim in cases[0].claims)
    assert report.proposed == ()

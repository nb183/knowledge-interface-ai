"""Focused shadow conflict eligibility tests."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from harness.contract import Page, Snapshot
from harness.run_eval import load_snapshots
from solution.conflict_adapter import (ShadowCase, ShadowConflictReport, _deduplicate_proposals, _key,
                                       _proposal, _disclosure_scope, _section_scope, _verified, analyze,
                                       classify_snapshot_pair, public_conflicts)
from solution.facts import Claim, ClaimDates, FactIdentity, TypedValue
from solution.resolver import TemporalResolver
from solution.solution import build
from contract import Conflict as SubmissionConflict, Source as SubmissionSource


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
    validated = public_conflicts(report, site.snapshots)
    assert first == list(validated.conflicts)
    assert all(isinstance(entry, SubmissionConflict) and
               all(isinstance(source, SubmissionSource) for source in entry.sources)
               for entry in first)
    assert all(len(entry.values) == len(entry.sources) and entry.reason for entry in first)
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


def test_explicit_rate_term_disclosures_equate_refinance_heading_variants():
    site = _site()
    index = site.snapshots[-1]
    claims = index.fact_index.claims
    old = next(c for c in claims if c.identity.family == "mortgage" and c.identity.measure == "apr"
               and c.section.endswith("home loan refinance")
               and dict(c.identity.qualifiers).get("tier"))
    new = next(c for c in claims if c.identity == old.identity
               and c.section.endswith("home loans - refinance")
               and c.dates.effective_on > old.dates.effective_on)
    result = classify_snapshot_pair(old, new, index)
    assert result.classification == "stale_published"
    assert result.proposed.resolution == new.value.raw
    assert result.proposed.sources[0].quote == old.evidence
    assert result.proposed.sources[1].quote == new.evidence

    row, _ = _verified(new, index)
    assert _section_scope(new, row, SimpleNamespace(markdown="")) is None
    for heading in ("home loans - cash-out refinance", "home loans - home equity refinance",
                    "home loans - refinance options", "refinance"):
        altered = replace(new, section="rates > " + heading)
        rejected = classify_snapshot_pair(old, altered, index)
        assert rejected.classification == "unresolved"
        assert rejected.rule == "different_or_unknown_section_scope"


def test_purchase_rows_with_refinance_disclosure_are_unresolved_in_copied_tables():
    site = _site()
    latest = site.snapshots[-1]
    claims = latest.fact_index.claims
    older = next(c for c in claims if c.identity.family == "mortgage"
                 and c.identity.measure == "apr" and c.section.endswith("home loans")
                 and dict(c.identity.qualifiers).get("transaction") == "purchase")
    current = [c for c in claims if c.identity == older.identity
               and c.dates.effective_on > older.dates.effective_on]
    assert len({c.url for c in current}) == 2
    for newer in current:
        assert _disclosure_scope(newer, latest.pages[newer.url]) == "refinance"
        case = classify_snapshot_pair(older, newer, latest)
        assert case.classification == "unresolved"
        assert case.rule == "different_or_unknown_section_scope"
        assert case.proposed is None

    earlier = site.snapshots[-2]
    aligned = [c for c in earlier.fact_index.claims if c.identity == older.identity]
    old = next(c for c in aligned if c.dates.effective_on == older.dates.effective_on)
    new = next(c for c in aligned if c.dates.effective_on
               and c.dates.effective_on > old.dates.effective_on)
    assert _disclosure_scope(new, earlier.pages[new.url]) is None
    assert classify_snapshot_pair(old, new, earlier).classification == "stale_published"


def test_disclosure_scope_stops_at_next_section_and_rejects_mixed_scope():
    site = _site()
    index = site.snapshots[-1]
    claim = next(c for c in index.fact_index.claims if c.identity.family == "mortgage"
                 and c.section.endswith("home loans - purchase")
                 and c.identity.measure == "apr")
    row, _ = _verified(claim, index)
    def page_with_footer(footer):
        lines = [""] * (claim.line - 1) + [claim.evidence, "", footer, "", "Home Loans - Refinance",
                                              "Rate/Term Refinance Transaction"]
        return SimpleNamespace(markdown="\n".join(lines))
    assert _disclosure_scope(claim, page_with_footer("Purchase transaction.")) == "purchase"
    assert _section_scope(claim, row, page_with_footer("Purchase transaction.")) == "purchase"
    assert _disclosure_scope(claim, page_with_footer("Purchase or rate/term refinance.")) == "ambiguous"
    assert _section_scope(claim, row, page_with_footer("Purchase or rate/term refinance.")) is None
    assert _disclosure_scope(claim, page_with_footer("Purchase or refinance.")) == "ambiguous"
    assert _section_scope(claim, row, page_with_footer("Purchase or refinance.")) is None
    assert _disclosure_scope(claim, page_with_footer("")) is None
    assert _section_scope(claim, row, page_with_footer("Cash-out refinance transaction.")) is None
    assert _section_scope(claim, row, page_with_footer("Home-equity refinance transaction.")) is None


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


def test_public_adapter_retains_copied_sources_and_value_alignment():
    site = _site()
    report = analyze(site.snapshots, TemporalResolver([s.fact_index for s in site.snapshots]))
    result = public_conflicts(report, site.snapshots)
    assert result.error is None and not result.rejected
    assert len(result.conflicts) < len(report.proposed)
    for entry in result.conflicts:
        assert entry.values and len(entry.values) == len(entry.sources)
        assert entry.reason
        for value, source in zip(entry.values, entry.sources):
            index = next(index for index in site.snapshots if index.snapshot_id == source.snapshot_id)
            assert source.url in index.pages
            assert source.quote in index.pages[source.url].markdown
            assert value in source.quote
        if entry.resolution is None:
            assert len(set(entry.values)) > 1
        else:
            assert entry.resolution in entry.values
    assert result == public_conflicts(report, site.snapshots)


def test_public_adapter_rejects_tampered_dates_quotes_and_wrong_snapshot():
    site = _site()
    index = site.snapshots[-1]
    case = next(c for c in analyze([index], TemporalResolver([index.fact_index])).cases
                if c.classification == "stale_published")
    altered = (
        replace(case, claims=(replace(case.claims[0], dates=replace(
            case.claims[0].dates, effective_on="2020-01-01")), case.claims[1])),
        replace(case, claims=(replace(case.claims[0], evidence="| invented |"), case.claims[1])),
        replace(case, snapshot_id="unknown"),
    )
    for bad in altered:
        report = ShadowConflictReport((bad,), (case.proposed,), ())
        result = public_conflicts(report, [index])
        assert result.error is None and result.conflicts == () and len(result.rejected) == 1


def test_public_adapter_distinguishes_empty_from_failure():
    empty = ShadowConflictReport((), (), ())
    assert public_conflicts(empty, []).error is None
    assert public_conflicts(empty, []).conflicts == ()
    failed = public_conflicts(empty, None)
    assert failed.conflicts == () and failed.error

"""Shadow promotion decisions, including adversarial evidence checks."""
from dataclasses import replace

from harness.contract import Answer, Page, Snapshot
from solution.promotion import classify
from solution.solution import _verified_promoted_answer, build


def _page(body):
    return Page("https://example.test/rates", "Rates", body, "2026-09-28T00:00:00Z")


def _mortgage_system():
    body = ("# Rates\n## Home Loans - Purchase\n| Program | Rate | Points | APR |\n| --- | --- | --- | --- |\n"
            "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.27% |\n"
            "| Purchase: 30-Year Fixed Conventional $200,000 or less | 7.25% | 0 | 7.34% |\n"
            "Rates are effective as of 09/25/2026. Rates may be as low as quoted rates. "
            "All loans are subject to approval. Rates, terms, and conditions are subject to change.\n"
            "Rates assume owner-occupied property and a minimum credit score of 760 "
            "and 75% loan-to-value.\n"
            "Rate quotes reflect a 30-day rate lock and expire at 3 pm Central Time daily.\n"
            "## Home Loans - Refinance\n| Program | Rate | Points | APR |\n| --- | --- | --- | --- |\n"
            "| Refinance: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.28% |\n"
            "| Refinance: 30-Year Fixed Conventional $200,000 or less | 7.25% | 0 | 7.35% |\n"
            "Rates are effective as of 09/25/2026. Rates may be as low as quoted rates. "
            "All loans are subject to approval. Rates, terms, and conditions are subject to change.\n"
            "Rates assume owner-occupied property and a minimum credit score of 760 "
            "and 75% loan-to-value.\n"
            "Rate quotes reflect a 30-day rate lock and expire at 3 pm Central Time daily.\n")
    ks = build()
    ks.ingest(Snapshot("one", "2026-09-28", [_page(body)]))
    ks.answer("What is the interest rate for a 30-year mortgage?")
    return ks, ks.shadow_diagnostics[-1]


def _savings_system():
    body = ("# Rates\n## Bright Savings\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n"
            "| Bright Savings | $0.01 – $1,000,000 | 2.23% | 2.25% |\n"
            "Rates are effective as of 06/02/2026.\n"
            "## Share Savings\n| Account Type | Balance Tier | Dividend Rate | APY |\n"
            "| --- | --- | --- | --- |\n"
            "| Share Savings | $25,000+ | 0.20% | 0.20% |\n"
            "Rates are effective as of 06/15/2026.\n")
    ks = build()
    ks.ingest(Snapshot("one", "2026-09-28", [_page(body)]))
    ks.answer("What is the rate for Bright Savings?")
    return ks, ks.shadow_diagnostics[-1]


def test_verified_mortgage_schedule_replaces_abstention_only_in_shadow():
    ks, record = _mortgage_system()
    status, reason = classify(record, Answer("I don't know.", confident=False), ks.snapshots[-1])
    assert (status, reason) == ("IMPROVEMENT", "verified_proposal_replaces_abstention")
    assert {dict(c.identity.qualifiers)["transaction"] for c in record.supporting_claims} == {
        "purchase", "refinance"}
    assert all(c.dates.effective_on == "2026-09-25" for c in record.supporting_claims)


def test_equivalent_answer_is_no_op_despite_different_wording():
    ks, record = _savings_system()
    answer = Answer("Bright Savings pays 2.25% APY and 2.23% dividend rate on $0.01–$1,000,000.")
    assert classify(record, answer, ks.snapshots[-1])[0] == "NO_OP"
    with_extra_rate = Answer(answer.text + " Another tier pays 9.99% APY.")
    assert classify(record, with_extra_rate, ks.snapshots[-1])[0] == "UNSAFE"


def test_incomplete_or_unsupported_mortgage_proposal_is_unsafe():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    assert classify(replace(record, proposed_answer=record.proposed_answer.replace(
        "up to $200,000: 7.25%", "")), abstention, index)[0] == "UNSAFE"
    assert classify(replace(record, supporting_claims=record.supporting_claims[:-1]),
                    abstention, index)[0] == "UNSAFE"
    broken = replace(record.supporting_claims[0], evidence="unrelated text")
    assert classify(replace(record, supporting_claims=(broken,) + record.supporting_claims[1:]),
                    abstention, index)[0] == "UNSAFE"
    assert classify(record, Answer("A 30-year mortgage rate is 6.00%."), index)[0] == "UNSAFE"


def test_ambiguous_product_and_missing_rate_condition_are_unsafe():
    ks, record = _savings_system()
    index = ks.snapshots[-1]
    other = next(c for c in index.fact_index.by_family["deposit"]
                 if c.identity.subject == "share savings" and c.identity.measure == "apy")
    mixed = replace(record, question="What is the savings rate?",
                    supporting_claims=record.supporting_claims + (other,),
                    proposed_answer=(record.proposed_answer +
                                     " Share Savings $25,000+ is 0.20% APY effective 2026-06-15."))
    assert classify(mixed, Answer("I don't know.", confident=False), index)[0] == "UNSAFE"
    missing_date = replace(record, proposed_answer=record.proposed_answer.replace("effective 2026-06-02", ""))
    assert classify(missing_date, Answer("I don't know.", confident=False), index) == (
        "UNSAFE", "effective_date_omitted")
    missing_label = replace(record, proposed_answer=record.proposed_answer.replace("2.25% apy", "2.25%"))
    assert classify(missing_label, Answer("I don't know.", confident=False), index) == (
        "UNSAFE", "deposit_measure_label_omitted")
    wrong_measure = replace(record, proposed_answer=record.proposed_answer.replace(
        "2.23% dividend rate, 2.25% apy", "2.25% dividend rate, 2.23% apy"))
    assert classify(wrong_measure, Answer("I don't know.", confident=False), index) == (
        "UNSAFE", "deposit_measure_value_mismatch")


def test_swapped_values_wrong_boundaries_and_rate_apr_confusion_are_unsafe():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    swapped = record.proposed_answer.replace("up to $100,000: 7.13%", "MARKER")
    swapped = swapped.replace("up to $200,000: 7.25%", "up to $200,000: 7.13%")
    swapped = swapped.replace("MARKER", "up to $100,000: 7.25%")
    assert classify(replace(record, proposed_answer=swapped), abstention, index)[0] == "UNSAFE"
    wrong_cap = record.proposed_answer.replace("$200,000", "$250,000")
    assert classify(replace(record, proposed_answer=wrong_cap), abstention, index)[0] == "UNSAFE"
    wrong_column = record.proposed_answer.replace("7.13%", "7.27%").replace("7.25%", "7.34%")
    assert classify(replace(record, proposed_answer=wrong_column), abstention, index)[0] == "UNSAFE"
    both = replace(record, question="What are the interest rate and APR for a 30-year mortgage?")
    assert classify(both, abstention, index) == ("UNSAFE", "requested_measure_incomplete")
    ks.answer("What are the interest rate and APR for a purchase 30-year mortgage?")
    assert ks.shadow_diagnostics[-1].reason == "multiple_requested_measures"


def test_partial_schedule_mixed_transactions_and_dates_are_unsafe():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    truncated = replace(record, supporting_claims=record.supporting_claims[:-1],
                        proposed_citations=record.proposed_citations[:-1],
                        proposed_answer=record.proposed_answer.replace(
                            "over $100,000 and up to $200,000: 7.25%", ""))
    assert classify(truncated, abstention, index)[0] == "UNSAFE"
    mixed = replace(record, supporting_claims=record.supporting_claims[:-1] +
                    (record.supporting_claims[0],),
                    proposed_citations=record.proposed_citations[:-1] +
                    (record.proposed_citations[0],))
    assert classify(mixed, abstention, index)[0] == "UNSAFE"
    later = _page(index.pages["https://example.test/rates"].markdown.replace(
        "## Home Loans - Refinance", "## Home Loans - Refinance").replace(
        "Rates are effective as of 09/25/2026. Rates may be as low as quoted rates.",
        "Rates are effective as of 09/26/2026. Rates may be as low as quoted rates.", 1))
    changed = build()
    changed.ingest(Snapshot("one", "2026-09-28", [later]))
    changed.answer("What is the interest rate for a 30-year mortgage?")
    assert not changed.shadow_diagnostics[-1].eligible


def test_conflicts_conditions_citations_and_historical_snapshot_are_checked():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    omitted = replace(record, proposed_answer=record.proposed_answer.replace(
        "Rates assume owner-occupied property and a minimum credit score of 760 "
        "and 75% loan-to-value.", ""))
    assert classify(omitted, abstention, index)[0] == "UNSAFE"
    omitted_lock = replace(record, proposed_answer=record.proposed_answer.replace(
        "Rate quotes reflect a 30-day rate lock and expire at 3 pm Central Time daily.", ""))
    assert classify(omitted_lock, abstention, index)[0] == "UNSAFE"
    bad_url = replace(record, proposed_citations=(("https://example.test/wrong", record.proposed_citations[0][1]),)
                      + record.proposed_citations[1:])
    assert classify(bad_url, abstention, index)[0] == "UNSAFE"
    body = index.pages["https://example.test/rates"].markdown.replace(
        "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.27% |",
        "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.50% | 0 | 7.27% |")
    conflicting = build()
    conflicting.ingest(Snapshot("one", "2026-09-28", [index.pages["https://example.test/rates"],
                                                    Page("https://example.test/other", "Rates", body,
                                                         "2026-09-28T00:00:00Z")]))
    conflicting.answer("What is the interest rate for a 30-year mortgage?")
    assert not conflicting.shadow_diagnostics[-1].eligible
    old = build()
    old.ingest(Snapshot("old", "2026-05-01", [Page("https://example.test/old", "Rates",
        index.pages["https://example.test/rates"].markdown.replace("09/25/2026", "04/22/2026"),
        "2026-05-01T00:00:00Z")]))
    old.ingest(Snapshot("new", "2026-09-28", [index.pages["https://example.test/rates"]]))
    old.answer("What is the interest rate for a 30-year mortgage?", as_of="2026-05-01")
    historical = old.shadow_diagnostics[-1]
    assert historical.snapshot_id == "old"
    assert all(c.snapshot_id == "old" and c.url == "https://example.test/old"
               for c in historical.supporting_claims)


def test_unsupported_loan_conditions_are_unsafe():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    for question in ("What is the interest rate for a 30-year investment property mortgage?",
                     "What is the interest rate for a 30-year mortgage with credit score 700?",
                     "What is the interest rate for a 30-year mortgage with a 40-day rate lock?"):
        changed = replace(record, question=question)
        assert classify(changed, abstention, index) == ("UNSAFE", "unsupported_loan_conditions")


def test_specific_loan_amount_requires_correct_interval():
    ks, _ = _mortgage_system()
    ks.answer("What is the purchase 30-year mortgage interest rate for a $150,000 loan?")
    record = ks.shadow_diagnostics[-1]
    index = ks.snapshots[-1]
    assert record.eligible and "over $100,000 and up to $200,000" in record.proposed_answer
    assert classify(record, Answer("I don't know.", confident=False), index)[0] == "IMPROVEMENT"
    wrong = replace(record, requested=tuple((key, "50000") if key == "tier_amount" else
                                            (key, value) for key, value in record.requested))
    assert classify(wrong, Answer("I don't know.", confident=False), index) == (
        "UNSAFE", "wrong_loan_amount_tier")


def test_verified_mortgage_promotion_renders_only_supported_facts_and_citations():
    ks, record = _mortgage_system()
    ks._answer_existing = lambda question, as_of=None: Answer("I don't know.", confident=False)
    answer = ks.answer(record.question)
    assert answer.confident and ks.promotion_diagnostics[-1][2] == "PROMOTED"
    assert "7.13%" in answer.text and "7.25%" in answer.text
    assert "7.27%" not in answer.text and "7.34%" not in answer.text
    assert "over $100,000 and up to $200,000" in answer.text
    assert "0 points" in answer.text and "minimum credit score of 760" in answer.text
    assert "75% loan-to-value" in answer.text and "30-day rate lock" in answer.text
    assert "subject to approval" in answer.text
    assert len(answer.sources) == len(ks.shadow_diagnostics[-1].supporting_claims) == 4
    assert all(s.url == "https://example.test/rates" and s.snapshot_id == "one" and
               s.quote in ks.snapshots[-1].pages[s.url].markdown for s in answer.sources)
    assert ks.answer(record.question) == answer


def test_promotion_gate_revalidates_final_text_and_citations():
    ks, record = _mortgage_system()
    index = ks.snapshots[-1]
    abstention = Answer("I don't know.", confident=False)
    verified = replace(record, classification="IMPROVEMENT")
    bad_rate = replace(verified, proposed_answer=verified.proposed_answer.replace("7.13%", "7.99%"))
    assert _verified_promoted_answer(bad_rate, abstention, index)[0] is None
    bad_source = replace(verified, proposed_citations=(("https://example.test/wrong",
                    verified.proposed_citations[0][1]),) + verified.proposed_citations[1:])
    assert _verified_promoted_answer(bad_source, abstention, index)[0] is None
    assert _verified_promoted_answer(replace(record, classification="NO_OP"), abstention, index)[0] is None


def test_promotion_falls_back_for_dual_measures_partial_schedules_and_conflicts():
    ks, record = _mortgage_system()
    ks._answer_existing = lambda question, as_of=None: Answer("I don't know.", confident=False)
    question = "What are the interest rate and APR for a purchase 30-year mortgage?"
    assert ks.answer(question) == Answer("I don't know.", confident=False)
    assert ks.promotion_diagnostics[-1][2] == "FALLBACK"
    index = ks.snapshots[-1]
    incomplete = index.pages["https://example.test/rates"].markdown.replace(
        "| Refinance: 30-Year Fixed Conventional $200,000 or less | 7.25% | 0 | 7.35% |\n", "")
    partial = build()
    partial.ingest(Snapshot("one", "2026-09-28", [_page(incomplete)]))
    partial._answer_existing = ks._answer_existing
    assert partial.answer(record.question) == Answer("I don't know.", confident=False)
    assert partial.promotion_diagnostics[-1][2] == "FALLBACK"
    conflicting = build()
    other = index.pages["https://example.test/rates"].markdown.replace(
        "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.27% |",
        "| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.50% | 0 | 7.27% |")
    conflicting.ingest(Snapshot("one", "2026-09-28", [index.pages["https://example.test/rates"],
                    Page("https://example.test/other", "Rates", other, "2026-09-28T00:00:00Z")]))
    conflicting._answer_existing = ks._answer_existing
    assert conflicting.answer(record.question) == Answer("I don't know.", confident=False)
    assert conflicting.promotion_diagnostics[-1][2] == "FALLBACK"


def test_promoted_historical_answer_uses_only_selected_snapshot():
    ks, _ = _mortgage_system()
    body = ks.snapshots[-1].pages["https://example.test/rates"].markdown
    old = build()
    old_body = body.replace("09/25/2026", "04/22/2026").replace("7.13%", "6.13%")
    old.ingest(Snapshot("old", "2026-05-01", [Page("https://example.test/old", "Rates", old_body,
                                                   "2026-05-01T00:00:00Z")]))
    old.ingest(Snapshot("new", "2026-09-28", [Page("https://example.test/new", "Rates", body,
                                                    "2026-09-28T00:00:00Z")]))
    old._answer_existing = lambda question, as_of=None: Answer("I don't know.", confident=False)
    question = "What is the interest rate for a 30-year mortgage?"
    historical = old.answer(question, as_of="2026-05-01")
    current = old.answer(question)
    assert historical.confident and current.confident
    assert "6.13%" in historical.text and "7.13%" not in historical.text
    assert "7.13%" in current.text and "6.13%" not in current.text
    assert all(s.snapshot_id == "old" and s.url == "https://example.test/old" for s in historical.sources)
    assert all(s.snapshot_id == "new" and s.url == "https://example.test/new" for s in current.sources)

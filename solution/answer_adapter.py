"""Shadow-only bridge from requests to resolved, cited facts.

Records stay in memory; this module never changes the submitted Answer.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from decimal import Decimal

try:
    from .facts import Claim, FactIdentity, normalize
    from .resolver import FactPattern, TemporalResolver
except ImportError:
    from facts import Claim, FactIdentity, normalize
    from resolver import FactPattern, TemporalResolver


@dataclass(frozen=True, slots=True)
class ShadowRecord:
    question: str
    snapshot_id: str
    target_date: str
    requested: tuple[tuple[str, str], ...]
    eligible: bool
    reason: str
    proposed_answer: str | None
    existing_answer: str
    supporting_claims: tuple[Claim, ...]
    rules: tuple[str, ...]
    resolver_ms: float
    adapter_ms: float

    @property
    def changes_answer(self):
        return self.eligible and self.proposed_answer != self.existing_answer


def _qualifiers(claim: Claim):
    return dict(claim.identity.qualifiers)


class _TimedResolver:
    def __init__(self, resolver):
        self.resolver = resolver
        self.elapsed = 0.0

    def resolve(self, *args):
        started = time.perf_counter()
        try:
            return self.resolver.resolve(*args)
        finally:
            self.elapsed += time.perf_counter() - started


def _rate_supported(claim: Claim, term: str, transaction: str, measure: str):
    q = _qualifiers(claim)
    evidence = normalize(claim.evidence)
    return (claim.identity.family == "mortgage" and claim.identity.measure == measure
            and q.get("term") == term and q.get("transaction") == transaction
            and q.get("program") == "standard" and q.get("rate_structure") == "fixed"
            and q.get("loan_type") == "conventional" and claim.dates.effective_on
            and term.replace(" ", "-") in evidence
            and claim.value.raw in claim.evidence)


def _tier_cap(claim: Claim):
    tier = _qualifiers(claim).get("tier", "")
    match = re.fullmatch(r"\$(\d+)orless", tier)
    return Decimal(match.group(1)) if match else None


def _balance_in_tier(tier: str, amount: Decimal):
    plus = re.fullmatch(r"\$(\d+(?:\.\d+)?)\+", tier)
    if plus:
        return amount >= Decimal(plus.group(1))
    span = re.fullmatch(r"\$(\d+(?:\.\d+)?)-\$(\d+(?:\.\d+)?)", tier)
    return bool(span and Decimal(span.group(1)) <= amount <= Decimal(span.group(2)))


def _resolve_one(resolver, index, claim, target):
    decision = resolver.resolve(FactPattern(claim.identity), target, index.snapshot_id)
    winner = decision.winner
    if (decision.status != "resolved" or winner is None or winner.identity != claim.identity
            or winner.snapshot_id != index.snapshot_id):
        return None, "unresolved_or_unknown_qualifier", decision.rules
    if any(c.classification == "confirmed_overlap" and c.resolution is None for c in decision.conflicts):
        return None, "same_period_conflict", decision.rules
    return winner, "accepted", decision.rules


def _mortgage(question, spec, index, resolver, target):
    q = spec.qualifiers
    term = q.get("term")
    if not term or "/" in term:
        return None, (), (), "unsupported_or_missing_term"
    if (q.get("program") != "standard" or
            re.search(r"\b(?:adjustable|variable|arm|jumbo)\b", question, re.I)):
        return None, (), (), "incompatible_mortgage_product"
    term = term.replace("-", " ")
    measure = "apr" if re.search(r"\bapr\b", question, re.I) else "interest_rate"
    transaction = q.get("transaction")
    if transaction is None:
        transactions = {_qualifiers(c).get("transaction") for c in index.fact_index.by_family.get("mortgage", ())
                        if c.identity.measure == measure and _qualifiers(c).get("term") == term
                        and _qualifiers(c).get("program") == "standard"
                        and _qualifiers(c).get("loan_type") == "conventional"
                        and _qualifiers(c).get("rate_structure") == "fixed"}
        if len(transactions) < 2 or None in transactions:
            return None, (), (), "ambiguous_mortgage_transaction"
        proposals = []
        for named in sorted(transactions):
            explicit = spec._replace(qualifiers={**q, "transaction": named})
            text, claims, rules, reason = _mortgage(question, explicit, index, resolver, target)
            if reason != "accepted":
                return None, (), rules, "unresolved_mortgage_transaction"
            proposals.append((named, claims, rules))
        signatures = [tuple((_tier_cap(c), c.value.raw, c.dates.effective_on) for c in claims)
                      for _, claims, _ in proposals]
        if len(set(signatures)) != 1:
            return None, (), (), "different_transaction_schedules"
        claims = tuple(c for _, group, _ in proposals for c in group)
        rules = tuple(dict.fromkeys(rule for _, _, group_rules in proposals for rule in group_rules))
        tiers = "; ".join(f"up to ${cap:,.0f}: {value}" for cap, value, _ in signatures[0])
        names = " and ".join(name for name, _, _ in proposals)
        return (f"Published {names} {term.replace(' ', '-')} fixed conventional mortgage "
                f"{measure.replace('_', ' ')}s effective {signatures[0][0][2]}: {tiers}."), claims, rules, "accepted"
    candidates = [c for c in index.fact_index.by_family.get("mortgage", ())
                  if _rate_supported(c, term, transaction, measure) and _tier_cap(c) is not None
                  and (q.get("scope") is None or _qualifiers(c).get("scope") == q["scope"])]
    if not candidates:
        return None, (), (), "no_exact_product_rate"
    amount = q.get("tier_amount")
    if amount is not None:
        applicable = [c for c in candidates if _tier_cap(c) >= Decimal(str(amount))]
        if not applicable:
            return None, (), (), "tier_not_published"
        cap = min(_tier_cap(c) for c in applicable)
        candidates = [c for c in applicable if _tier_cap(c) == cap]
        candidates.sort(key=lambda c: (c.dates.effective_on or "", c.url), reverse=True)
        winner, reason, rules = _resolve_one(resolver, index, candidates[0], target)
        if winner is None or not _rate_supported(winner, term, transaction, measure):
            return None, (), rules, reason
        return (f"{transaction.title()} {term.replace(' ', '-')} fixed conventional "
                f"mortgage, loans up to ${cap:,.0f}: {winner.value.raw} {measure.replace('_', ' ')}."), (winner,), rules, "accepted"

    # Broad questions require every tier in one source schedule and a resolved
    # claim for every row. A lone top hit never establishes completeness.
    schedules = {}
    for c in candidates:
        key = (c.url, c.section, c.dates.effective_on, c.identity.measure,
               _qualifiers(c).get("transaction"), _qualifiers(c).get("scope"))
        schedules.setdefault(key, []).append(c)
    schedules = [(key, rows) for key, rows in schedules.items() if key[2] and key[2] <= target]
    if not schedules:
        return None, (), (), "no_applicable_schedule"
    schedules.sort(key=lambda item: (item[0][2], len({_tier_cap(c) for c in item[1]}), item[0][0]), reverse=True)
    key, rows = schedules[0]
    unique = {cap: next(c for c in rows if _tier_cap(c) == cap) for cap in {_tier_cap(c) for c in rows}}
    if len(unique) < 2:
        return None, (), (), "incomplete_multi_tier_schedule"
    winners, all_rules = [], []
    for cap in sorted(unique):
        winner, reason, rules = _resolve_one(resolver, index, unique[cap], target)
        all_rules.extend(rules)
        if winner is None or not _rate_supported(winner, term, transaction, measure):
            return None, (), tuple(all_rules), "unresolved_schedule_tier"
        if (winner.url, winner.section, winner.dates.effective_on) != key[:3]:
            return None, (), tuple(all_rules), "mixed_schedule_sources"
        winners.append(winner)
    text = (f"{transaction.title()} {term.replace(' ', '-')} fixed conventional mortgage "
            f"rates effective {key[2]}: " + "; ".join(
                f"up to ${_tier_cap(c):,.0f}: {c.value.raw}" for c in winners) + ".")
    return text, tuple(winners), tuple(dict.fromkeys(all_rules)), "accepted"


def _branch_count(question, index, resolver, target):
    if not re.search(r"\bhow many\b.*\bbranches\b|\bnumber of\b.*\bbranches\b", question.lower()):
        return None, (), (), "unsupported_request"
    claims = index.fact_index.by_family.get("institution", ())
    if not claims:
        return None, (), (), "no_branch_count"
    decision = resolver.resolve(FactPattern(FactIdentity.make("institution", "dupaco", "branch_office_count")),
                                target, index.snapshot_id)
    winner = decision.winner
    if decision.status != "resolved" or winner is None or not winner.dates.published_on:
        return None, (), decision.rules, "unresolved_branch_count"
    # A single article's boilerplate is not sufficient for a current total.
    same = [c for c in decision.considered if c.value.value == winner.value.value
            and c.dates.published_on and c.dates.published_on >= winner.dates.published_on
            and c.url != winner.url]
    if not same:
        return None, (), decision.rules, "uncorroborated_branch_count"
    return f"Dupaco reports {winner.value.raw} branch office locations.", (winner, same[0]), decision.rules, "accepted"


def _reporting_year(question, index, resolver, target):
    if "volunteer time off" not in question.lower():
        return None, (), (), "unsupported_request"
    year = re.search(r"\b(20\d\d)\b", question)
    if not year:
        return None, (), (), "ambiguous_reporting_year"
    identity = FactIdentity.make("workforce", "dupaco", "vto_employee_count", reporting_year=year.group(1))
    decision = resolver.resolve(FactPattern(identity), target, index.snapshot_id)
    if decision.status != "resolved" or not decision.winner or decision.winner.identity != identity:
        return None, (), decision.rules, "unresolved_reporting_year"
    winner = decision.winner
    if str(winner.value.raw) not in winner.evidence or winner.dates.reporting_year != int(year.group(1)):
        return None, (), decision.rules, "unsupported_reporting_year"
    return f"{winner.value.raw} employees used Volunteer Time Off in {year.group(1)}.", (winner,), decision.rules, "accepted"


def _savings(question, spec, index, resolver, target):
    q = spec.qualifiers
    amount = q.get("tier_amount")
    if amount is None:
        return _untiered_savings(question, index, resolver, target)
    mentioned = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)", question)
    upper = Decimal(mentioned[1].replace(",", "")) if len(mentioned) > 1 else None
    measure = "apy" if "apy" in question.lower() else "dividend_rate" if "dividend rate" in question.lower() else None
    if measure is None:
        return None, (), (), "savings_measure_unspecified"
    candidates = [c for c in index.fact_index.by_family.get("deposit", ())
                  if c.identity.measure == measure and c.identity.subject in normalize(question)
                  and _balance_in_tier(_qualifiers(c).get("tier", ""), Decimal(str(amount)))
                  and (upper is None or _balance_in_tier(_qualifiers(c).get("tier", ""), upper))]
    if not candidates:
        return None, (), (), "savings_product_or_tier_not_verified"
    subjects = {c.identity.subject for c in candidates}
    tiers = {_qualifiers(c).get("tier") for c in candidates}
    if len(subjects) != 1 or len(tiers) != 1:
        return None, (), (), "ambiguous_savings_product_or_tier"
    candidates.sort(key=lambda c: (c.dates.effective_on or "", c.url), reverse=True)
    winner, reason, rules = _resolve_one(resolver, index, candidates[0], target)
    if winner is None or winner.identity.subject not in normalize(winner.evidence):
        return None, (), rules, reason
    return (f"{winner.identity.subject.title()} at the {winner.value.raw} {measure.replace('_', ' ')} "
            f"for balance tier {_qualifiers(winner).get('tier')}."), (winner,), rules, "accepted"


def _untiered_savings(question, index, resolver, target):
    """Use a whole named schedule, or a single explicitly bounded product row."""
    named = {c.identity.subject for c in index.fact_index.by_family.get("deposit", ())
             if c.identity.subject in normalize(question)}
    if len(named) != 1:
        return None, (), (), "savings_product_unspecified_or_ambiguous"
    subject = named.pop()
    lower = question.lower()
    measure = "apy" if "apy" in lower else "dividend_rate" if "dividend rate" in lower else None
    if measure is None and not re.search(r"\brate\b", lower):
        return None, (), (), "savings_measure_unspecified"
    measures = (measure,) if measure else ("dividend_rate", "apy")
    rows = [c for c in index.fact_index.by_family.get("deposit", ())
            if c.identity.subject == subject and c.identity.measure in measures
            and c.dates.effective_on and c.dates.effective_on <= target
            and subject in normalize(c.evidence) and _qualifiers(c).get("tier")]
    if not rows:
        return None, (), (), "no_named_savings_schedule"
    schedules = {}
    for c in rows:
        schedules.setdefault((c.url, c.section, c.dates.effective_on), []).append(c)
    key, schedule = max(schedules.items(), key=lambda item: (item[0][2], len(item[1]), item[0][0]))
    tiers = {_qualifiers(c)["tier"] for c in schedule if c.identity.measure == measures[0]}
    if "highest" in lower:
        if measure is None or len(tiers) < 2 or not any(t.endswith("+") for t in tiers):
            return None, (), (), "incomplete_savings_schedule"
    elif len(tiers) != 1:
        return None, (), (), "savings_tier_unspecified"
    winners, rules = [], []
    for tier in sorted(tiers):
        for kind in measures:
            candidates = [c for c in schedule if _qualifiers(c).get("tier") == tier
                          and c.identity.measure == kind]
            if len(candidates) != 1:
                return None, (), (), "incomplete_savings_schedule"
            winner, reason, applied = _resolve_one(resolver, index, candidates[0], target)
            rules.extend(applied)
            if winner is None or (winner.url, winner.section, winner.dates.effective_on) != key:
                return None, (), tuple(rules), reason if winner is None else "mixed_savings_schedule"
            winners.append(winner)
    if "highest" in lower:
        best = max(winners, key=lambda c: c.value.value)
        tier = _qualifiers(best)["tier"]
        if not tier.endswith("+"):
            return None, (), tuple(rules), "highest_tier_not_open_ended"
        return (f"The highest published {subject.title()} {measure.upper()} is {best.value.raw} "
                f"for balances of {tier}."), (best,), tuple(dict.fromkeys(rules)), "accepted"
    tier = next(iter(tiers))
    facts = ", ".join(f"{c.value.raw} {c.identity.measure.replace('_', ' ')}" for c in winners)
    return (f"Published {subject.title()} rates for balance tier {tier}: {facts}.",
            tuple(winners), tuple(dict.fromkeys(rules)), "accepted")


def propose(question, spec, index, resolver: TemporalResolver, existing_answer, as_of=None):
    """Return a bounded in-memory diagnostic; never mutate the submitted answer."""
    started = time.perf_counter()
    timed = _TimedResolver(resolver)
    target = as_of or index.taken_on
    requested = tuple(sorted((k, str(v)) for k, v in spec.qualifiers.items() if v is not None))
    if spec.product == "mortgage":
        result = _mortgage(question, spec, index, timed, target)
    elif re.search(r"\bhow many\b.*\bbranches\b|\bnumber of\b.*\bbranches\b", question.lower()):
        result = _branch_count(question, index, timed, target)
    elif "volunteer time off" in question.lower():
        result = _reporting_year(question, index, timed, target)
    elif "savings" in question.lower():
        result = _savings(question, spec, index, timed, target)
    elif re.search(r"\brate|\bapr|\bapy", question.lower()):
        result = (None, (), (), "unsupported_rate_product")
    else:
        result = (None, (), (), "unsupported_request")
    text, claims, rules, reason = result
    return ShadowRecord(question[:240], index.snapshot_id, target, requested,
                        reason == "accepted", reason, text, existing_answer.text[:1000],
                        claims[:12], tuple(rules)[:12],
                        round(timed.elapsed * 1000, 3),
                        round((time.perf_counter() - started) * 1000, 3))

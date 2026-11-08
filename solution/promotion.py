"""Conservative, shadow-only comparison of an existing answer and a fact proposal."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

try:
    from .facts import normalize
    from .resolver import FactPattern, TemporalResolver
except ImportError:
    from facts import normalize
    from resolver import FactPattern, TemporalResolver


PERCENT = re.compile(r"(?<![\d.])\d+(?:\.\d+)?%")
DOLLAR = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
EFFECTIVE = re.compile(r"effective\s+as\s+of\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
TIER_CAP = re.compile(r"\$\s*([\d,]+)\s+or\s+less", re.I)
PROPOSED_RATE = re.compile(r"up to\s+\$([\d,]+):\s*(\d+(?:\.\d+)?%)", re.I)


def _qualifiers(claim):
    return dict(claim.identity.qualifiers)


def _amounts(text):
    return {Decimal(value.replace(",", "")) for value in DOLLAR.findall(text)}


def _rates(text):
    return {Decimal(value[:-1]) for value in PERCENT.findall(text)}


def _tier_amounts(claim):
    return _amounts(_qualifiers(claim).get("tier", ""))


def _plain(text):
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _requested_measures(question, family):
    lower = question.lower()
    if family == "mortgage":
        apr = bool(re.search(r"\bapr\b", lower))
        rate = bool(re.search(r"\binterest\s+rates?\b", lower) or
                    re.search(r"\brates?\s*(?:and|&|/)\s*aprs?\b", lower) or
                    re.search(r"\baprs?\s*(?:and|&|/)\s*rates?\b", lower))
        return {"apr", "interest_rate"} if apr and rate else {"apr"} if apr else {"interest_rate"}
    if family == "deposit":
        apy = bool(re.search(r"\bapy\b", lower))
        dividend = "dividend rate" in lower
        return ({"apy"} if apy else set()) | ({"dividend_rate"} if dividend else set()) or {
            "apy", "dividend_rate"}
    return set()


def _cells(line):
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _source_table(index, claim):
    """Read the selected raw page independently of extracted claim fields."""
    page = index.pages.get(claim.url)
    if page is None:
        return None
    lines = page.markdown.splitlines()
    pos = claim.line - 1
    if pos < 0 or pos >= len(lines) or lines[pos].strip() != claim.evidence:
        return None
    start = pos
    while start and lines[start - 1].strip().startswith("|"):
        start -= 1
    end = pos + 1
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    if pos < start + 2 or end - start < 3:
        return None
    headers = [normalize(part) for part in _cells(lines[start])]
    rows = [_cells(lines[j]) for j in range(start + 2, end)]
    if any(len(row) != len(headers) for row in rows):
        return None
    notes = []
    for line in lines[end:end + 12]:
        value = line.strip()
        if value.startswith(("#", "|")):
            break
        if value.startswith("["):
            break
        if value:
            notes.append(value)
        if sum(map(len, notes)) > 2000:
            return None
    match = EFFECTIVE.search(" ".join(notes))
    if match is None:
        return None
    try:
        effective = datetime.strptime(match.group(1), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None
    return headers, rows, _cells(lines[pos]), tuple(notes), effective


def _measure_column(headers, measure):
    aliases = {"interest_rate": ("rate",), "apr": ("apr",),
               "apy": ("apy", "annual percentage yield"),
               "dividend_rate": ("dividend rate",)}
    indexes = [i for i, header in enumerate(headers)
               if any(header == word or (word == "annual percentage yield" and word in header)
                      for word in aliases.get(measure, ()))]
    return indexes[0] if len(indexes) == 1 else None


def _row_rate(cell):
    values = PERCENT.findall(cell)
    return Decimal(values[0][:-1]) if len(values) == 1 else None


def _row_cap(label):
    match = TIER_CAP.search(label)
    return Decimal(match.group(1).replace(",", "")) if match else None


def _mortgage_conditions_supported(question, notes):
    lower = question.lower()
    source = " ".join(notes).lower()
    if re.search(r"\b(?:investment|rental|non[- ]owner[- ]occupied)\b", lower):
        return False
    score = re.search(r"\b(?:credit score|fico)(?:\s+of)?\s*(\d{3})\b", lower)
    if score:
        minimum = re.search(r"minimum credit score(?:\s+of)?\s*(\d{3})", source)
        if minimum is None or int(score.group(1)) < int(minimum.group(1)):
            return False
    ltv = re.search(r"\b(\d{1,3})\s*%\s*(?:ltv|loan[- ]to[- ]value)", lower)
    if ltv:
        limit = re.search(r"(\d{1,3})\s*%\s*(?:ltv|loan[- ]to[- ]value)", source)
        if limit is None or int(ltv.group(1)) > int(limit.group(1)):
            return False
    lock = re.search(r"\b(\d{1,3})[- ]day\s+rate\s+lock\b", lower)
    if lock:
        published = re.search(r"\b(\d{1,3})[- ]day\s+rate\s+lock\b", source)
        if published is None or int(lock.group(1)) > int(published.group(1)):
            return False
    return True


def _normalized_tier(value):
    return re.sub(r"\s+", "", normalize(value).replace(",", "").replace("–", "-"))


def _tier_contains(tier, amount):
    plus = re.fullmatch(r"\$(\d+(?:\.\d+)?)\+", tier)
    if plus:
        return amount >= Decimal(plus.group(1))
    span = re.fullmatch(r"\$(\d+(?:\.\d+)?)-\$(\d+(?:\.\d+)?)", tier)
    return bool(span and Decimal(span.group(1)) <= amount <= Decimal(span.group(2)))


def _mortgage_supported(record, index):
    claims = record.supporting_claims
    requested = dict(record.requested)
    term = requested.get("term")
    if not term or "/" in term:
        return False, "missing_fixed_term"
    measures = _requested_measures(record.question, "mortgage")
    if {c.identity.measure for c in claims} != measures:
        return False, "requested_measure_incomplete"
    if len(measures) != 1:
        return False, "multiple_measures_not_composed"
    if not _mortgage_conditions_supported(record.question, record.source_notes):
        return False, "unsupported_loan_conditions"
    expected_measure = next(iter(measures))
    core = (record.proposed_answer or "").split(" Conditions: ", 1)[0]
    if expected_measure.replace("_", " ") not in core.lower():
        return False, "mortgage_measure_label_omitted"
    proposed = [(Decimal(amount.replace(",", "")), Decimal(rate[:-1]))
                for amount, rate in PROPOSED_RATE.findall(core)]
    if len(proposed) != len(set(proposed)) or not proposed:
        return False, "proposed_rate_rows_missing"
    groups = defaultdict(list)
    for claim in claims:
        q = _qualifiers(claim)
        if (claim.identity.family != "mortgage" or claim.identity.measure != expected_measure
                or q.get("term") != term or q.get("program") != "standard"
                or q.get("rate_structure") != "fixed" or q.get("loan_type") != "conventional"
                or not q.get("transaction") or not q.get("tier")):
            return False, "mortgage_qualifier_mismatch"
        if requested.get("scope") and q.get("scope") != requested["scope"]:
            return False, "mortgage_scope_mismatch"
        if not all(part in normalize(claim.evidence) for part in
                   (q["transaction"], "fixed", "conventional", term.replace(" ", "-"))):
            return False, "mortgage_row_unverified"
        groups[q["transaction"]].append(claim)
    wanted = requested.get("transaction")
    if wanted and set(groups) != {wanted}:
        return False, "wrong_transaction"
    if not wanted and set(groups) != {"purchase", "refinance"}:
        return False, "incomplete_transaction_coverage"
    signatures = []
    dates = set()
    for transaction, group in groups.items():
        source = {(c.url, c.section, c.dates.effective_on) for c in group}
        if len(source) != 1:
            return False, "mixed_mortgage_schedule"
        url, section, date = next(iter(source))
        dates.add(date)
        table = _source_table(index, group[0])
        if table is None:
            return False, "source_table_unverified"
        headers, rows, _, _, source_date = table
        if date != source_date or not headers or headers[0] not in ("program", "term"):
            return False, "source_date_or_header_mismatch"
        column = _measure_column(headers, expected_measure)
        if column is None:
            return False, "measure_column_unverified"
        raw = {}
        for row in rows:
            label = normalize(row[0])
            if all(part in label for part in (transaction, "fixed", "conventional", term.replace(" ", "-"))):
                cap, value = _row_cap(label), _row_rate(row[column])
                if cap is None or value is None or cap in raw:
                    return False, "source_tier_or_value_unverified"
                raw[cap] = value
        if not raw:
            return False, "source_schedule_missing"
        claimed = {}
        for c in group:
            q = _qualifiers(c)
            table_c = _source_table(index, c)
            cap = _row_cap(table_c[2][0]) if table_c else None
            if (table_c is None or table_c[0] != headers or table_c[4] != date
                    or cap is None or cap in claimed or c.value.value != _row_rate(table_c[2][column])
                    or cap != _row_cap(c.evidence)):
                return False, "claim_column_or_tier_mismatch"
            claimed[cap] = c.value.value
        if requested.get("tier_amount") is None:
            if len(raw) < 2 or claimed != raw:
                return False, "incomplete_mortgage_schedule"
        else:
            amount = Decimal(requested["tier_amount"])
            applicable = [cap for cap in raw if cap >= amount]
            if not applicable or claimed != {min(applicable): raw[min(applicable)]}:
                return False, "wrong_loan_amount_tier"
            selected = min(applicable)
            earlier = [cap for cap in raw if cap < selected]
            if earlier and f"over ${max(earlier):,.0f} and up to ${selected:,.0f}" not in core:
                return False, "tier_boundary_omitted"
        for other in index.fact_index.by_family.get("mortgage", ()):
            oq = _qualifiers(other)
            if (other.dates.effective_on == date and other.identity.measure == expected_measure
                    and oq.get("term") == term and oq.get("transaction") == transaction
                    and oq.get("program") == "standard" and oq.get("loan_type") == "conventional"
                    and oq.get("rate_structure") == "fixed"):
                other_cap = _row_cap(other.evidence)
                if other_cap is not None and other_cap not in raw and requested.get("tier_amount") is None:
                    return False, "partial_source_schedule"
        signature = frozenset(raw.items()) if requested.get("tier_amount") is None else frozenset(claimed.items())
        signatures.append(signature)
        if transaction not in record.proposed_answer.lower():
            return False, "transaction_label_omitted"
    if len(dates) != 1 or (not wanted and len(set(map(frozenset, signatures))) != 1):
        return False, "transaction_values_disagree"
    if dict(proposed) != dict(signatures[0]):
        return False, "rate_tier_association_mismatch"
    if len(proposed) > 1:
        for previous, current in zip(proposed, proposed[1:]):
            if previous[0] >= current[0] or f"over ${previous[0]:,.0f} and up to ${current[0]:,.0f}" not in core:
                return False, "tier_boundary_omitted"
    expected_amounts = {cap for signature in signatures for cap, _ in signature}
    if requested.get("tier_amount") is not None:
        for transaction, group in groups.items():
            table = _source_table(index, group[0])
            column = _measure_column(table[0], expected_measure)
            raw_caps = sorted(_row_cap(row[0]) for row in table[1]
                              if all(part in normalize(row[0]) for part in
                                     (transaction, "fixed", "conventional", term.replace(" ", "-")))
                              and _row_cap(row[0]) is not None and _row_rate(row[column]) is not None)
            earlier = [cap for cap in raw_caps if cap < proposed[0][0]]
            if earlier:
                expected_amounts.add(max(earlier))
    if _amounts(core) != expected_amounts:
        return False, "tier_boundary_omitted"
    if not all(part in _plain(record.proposed_answer) for part in
               (_plain(term), "fixed", "conventional")):
        return False, "mortgage_qualifier_omitted"
    return True, "verified_mortgage_schedule"


def _deposit_supported(record, index):
    claims = record.supporting_claims
    subject = {c.identity.subject for c in claims}
    if len(subject) != 1 or next(iter(subject)) not in normalize(record.question):
        return False, "deposit_product_ambiguous"
    if next(iter(subject)) not in normalize(record.proposed_answer or ""):
        return False, "deposit_product_omitted"
    if any(c.identity.family != "deposit" or not _qualifiers(c).get("tier") for c in claims):
        return False, "deposit_qualifier_mismatch"
    if any(c.identity.subject not in normalize(c.evidence) for c in claims):
        return False, "deposit_row_unverified"
    source = {(c.url, c.section, c.dates.effective_on) for c in claims}
    if len(source) != 1:
        return False, "mixed_deposit_schedule"
    url, section, date = next(iter(source))
    table = _source_table(index, claims[0])
    if table is None:
        return False, "deposit_source_table_unverified"
    headers, rows, _, _, _ = table
    if "account type" not in headers or "balance tier" not in headers:
        return False, "deposit_table_labels_missing"
    product_col, tier_col = headers.index("account type"), headers.index("balance tier")
    raw_rows = [row for row in rows if normalize(row[product_col]) == claims[0].identity.subject]
    raw_tiers = {_normalized_tier(row[tier_col]) for row in raw_rows}
    for c in claims:
        current = _source_table(index, c)
        if (current is None or current[0] != headers or
                normalize(current[2][product_col]) != c.identity.subject or
                _normalized_tier(current[2][tier_col]) != _qualifiers(c)["tier"]):
            return False, "deposit_row_tier_mismatch"
    if {c.identity.measure for c in claims} != _requested_measures(record.question, "deposit"):
        return False, "requested_deposit_measure_incomplete"
    if "highest" in record.question.lower():
        if len(claims) != 1 or not _qualifiers(claims[0])["tier"].endswith("+"):
            return False, "highest_tier_unverified"
        column = _measure_column(headers, claims[0].identity.measure)
        values = [_row_rate(row[column]) for row in raw_rows] if column is not None else []
        if len(raw_tiers) < 2 or any(v is None or v > claims[0].value.value for v in values):
            return False, "highest_rate_unverified"
    elif len({_qualifiers(c)["tier"] for c in claims}) != 1:
        return False, "deposit_tier_ambiguous"
    elif len(raw_tiers) != 1 and dict(record.requested).get("tier_amount") is None:
        return False, "incomplete_deposit_schedule"
    requested_amount = dict(record.requested).get("tier_amount")
    if requested_amount is not None and not all(
            _tier_contains(_qualifiers(c)["tier"], Decimal(requested_amount)) for c in claims):
        return False, "wrong_deposit_tier"
    if "apy" in record.question.lower() and any(c.identity.measure != "apy" for c in claims):
        return False, "wrong_deposit_measure"
    if "dividend rate" in record.question.lower() and any(
            c.identity.measure != "dividend_rate" for c in claims):
        return False, "wrong_deposit_measure"
    if "apy" not in record.question.lower() and "dividend rate" not in record.question.lower():
        if {c.identity.measure for c in claims} != {"apy", "dividend_rate"}:
            return False, "incomplete_deposit_measures"
    if any(c.identity.measure.replace("_", " ") not in normalize(record.proposed_answer or "")
           and not (c.identity.measure == "apy" and "apy" in record.proposed_answer.lower())
           for c in claims):
        return False, "deposit_measure_label_omitted"
    core = (record.proposed_answer or "").split(" Conditions: ", 1)[0].lower()
    for c in claims:
        label = "apy" if c.identity.measure == "apy" else "dividend rate"
        value = re.escape(c.value.raw.lower())
        if not (re.search(rf"{value}.{{0,18}}\b{label}\b", core) or
                re.search(rf"\b{label}\b.{{0,18}}{value}", core)):
            return False, "deposit_measure_value_mismatch"
    return True, "verified_deposit_schedule"


def _proposal_supported(record, index):
    claims = record.supporting_claims
    text = record.proposed_answer or ""
    if not record.eligible or not claims or not text:
        return False, "missing_proposal_or_claims"
    if record.proposed_citations != tuple((c.url, c.evidence) for c in claims):
        return False, "citation_claim_mismatch"
    if not all(url in index.pages and quote in index.pages[url].markdown
               for url, quote in record.proposed_citations):
        return False, "citation_not_in_selected_snapshot"
    if any(c.snapshot_id != record.snapshot_id or c.snapshot_id != index.snapshot_id
           or c not in index.fact_index.by_identity.get(c.identity, ())
           or not c.dates.effective_on or c.dates.effective_on > record.target_date
           or (c.dates.expires_on and c.dates.expires_on < record.target_date)
           or (c.dates.published_on and c.dates.published_on > record.target_date)
           or c.value.raw not in c.evidence for c in claims):
        return False, "claim_provenance_or_date_unverified"
    tables = [_source_table(index, c) for c in claims]
    if any(table is None or table[4] != c.dates.effective_on
           or (column := _measure_column(table[0], c.identity.measure)) is None
           or _row_rate(table[2][column]) != c.value.value
           for c, table in zip(claims, tables)):
        return False, "source_column_or_effective_date_unverified"
    if not record.source_notes or any(table[3] != record.source_notes for table in tables):
        return False, "source_conditions_inconsistent"
    if " Conditions: " not in text or not all(note in text for note in record.source_notes):
        return False, "source_conditions_omitted"
    resolver = TemporalResolver([index.fact_index])
    for c in claims:
        decision = resolver.resolve(FactPattern(c.identity), record.target_date, index.snapshot_id)
        if decision.status != "resolved" or decision.winner is None or decision.winner.id != c.id:
            return False, "unresolved_applicable_claim"
        if any(conflict.resolution is None or conflict.classification == "confirmed_overlap"
               for conflict in decision.conflicts):
            return False, "unresolved_or_overlapping_conflict"
    if any(c.dates.effective_on not in text for c in claims):
        return False, "effective_date_omitted"
    core = text.split(" Conditions: ", 1)[0]
    claim_rates = [_rates(c.value.raw) for c in claims]
    if any(len(values) != 1 for values in claim_rates):
        return False, "unsupported_value_type"
    if {next(iter(values)) for values in claim_rates} != _rates(core):
        return False, "claim_value_omitted"
    families = {c.identity.family for c in claims}
    if families != {"mortgage"} and set().union(*(_tier_amounts(c) for c in claims)) != _amounts(core):
        return False, "tier_boundary_omitted"
    if families == {"mortgage"}:
        return _mortgage_supported(record, index)
    if families == {"deposit"}:
        return _deposit_supported(record, index)
    return False, "unsupported_fact_family"


def classify(record, existing_answer, index):
    """Return IMPROVEMENT, NO_OP, or UNSAFE with an auditable reason."""
    supported, reason = _proposal_supported(record, index)
    if not supported:
        return "UNSAFE", reason
    claims = record.supporting_claims
    existing = existing_answer.text
    required_rates = {_rates(c.value.raw).pop() for c in claims}
    required_amounts = set().union(*(_tier_amounts(c) for c in claims))
    subject = claims[0].identity.subject
    equivalent = (existing_answer.confident and subject in normalize(existing)
                  and required_rates == _rates(existing)
                  and required_amounts.issubset(_amounts(existing)))
    if equivalent and claims[0].identity.family == "mortgage":
        equivalent = all(_qualifiers(c)["transaction"] in existing.lower() for c in claims)
        equivalent = equivalent and _plain(_qualifiers(claims[0])["term"]) in _plain(existing)
    if equivalent and claims[0].identity.family == "deposit":
        equivalent = all(c.identity.measure.replace("_", " ") in normalize(existing)
                         or (c.identity.measure == "apy" and "apy" in existing.lower()) for c in claims)
    if equivalent:
        return "NO_OP", "existing_answer_covers_verified_facts"
    if not existing_answer.confident or re.search(r"\bi don't know\b|\bunable to confirm\b", existing, re.I):
        return "IMPROVEMENT", "verified_proposal_replaces_abstention"
    return "UNSAFE", "confident_existing_answer_requires_review"

"""Source-verified conflict discovery and validated public conflict entries."""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Match the top-level contract module loaded by the official harness.
sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                             if (d / "harness" / "contract.py").exists())))
from contract import Conflict, Source

try:
    from .facts import Claim, FactIdentity, MODEL_YEAR_RE, OLDER_MODEL_RE, _table_dates, normalize, temporal_relationship
    from .resolver import TemporalResolver, qualifier_match
except ImportError:
    from facts import Claim, FactIdentity, MODEL_YEAR_RE, OLDER_MODEL_RE, _table_dates, normalize, temporal_relationship
    from resolver import TemporalResolver, qualifier_match


SUPPORTED = frozenset(("mortgage", "deposit", "certificate"))
REVIEW_ONLY = frozenset(("auto", "fee", "branch"))


@dataclass(frozen=True, slots=True)
class ShadowCase:
    classification: str  # simultaneous, stale_published, historical, unresolved
    snapshot_id: str
    claims: tuple[Claim, ...]
    rule: str
    proposed: Conflict | None = None


@dataclass(frozen=True, slots=True)
class ShadowConflictReport:
    cases: tuple[ShadowCase, ...]
    proposed: tuple[Conflict, ...]
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class PublicConflictResult:
    """An empty validated result is distinct from a failed validation."""
    conflicts: tuple[Conflict, ...]
    rejected: tuple[tuple[str, str], ...]
    error: str | None = None


def _number(text: str) -> str:
    try:
        return format(Decimal(re.sub(r"[^\d.]", "", text)).normalize(), "f")
    except InvalidOperation:
        return re.sub(r"\s+", " ", text.strip().lower())


def _row_fields(claim: Claim, page) -> dict[str, str] | None:
    lines = page.markdown.splitlines()
    pos = claim.line - 1
    if (pos < 2 or pos >= len(lines) or lines[pos].strip() != claim.evidence.strip()
            or not claim.evidence.lstrip().startswith("|")):
        return None
    top = pos
    while top > 0 and lines[top - 1].strip().startswith("|"):
        top -= 1
    if top + 1 >= pos:
        return None
    headers = [x.strip().lower() for x in lines[top].strip().strip("|").split("|")]
    cells = [x.strip() for x in lines[pos].strip().strip("|").split("|")]
    separator = [x.strip() for x in lines[top + 1].strip().strip("|").split("|")]
    if (len(headers) != len(cells) or len(headers) != len(separator)
            or not all(re.fullmatch(r":?-{3,}:?", x) for x in separator)
            or len(set(headers)) != len(headers)):
        return None
    return dict(zip(headers, cells))


def _verified(claim: Claim, index) -> tuple[dict[str, str], object] | None:
    if claim.snapshot_id != index.snapshot_id:
        return None
    page = index.pages.get(claim.url)
    if page is None:
        return None
    row = _row_fields(claim, page)
    if row is None:
        return None
    measure = claim.identity.measure
    labels = {"interest_rate": ("rate",), "apr": ("apr",),
              "apy": ("apy", "annual percentage yield"),
              "dividend_rate": ("dividend rate",)}.get(measure, ())
    if not any(label in header for header in row for label in labels):
        return None
    matching = [cell for header, cell in row.items() if any(label in header for label in labels)]
    if len(matching) != 1 or claim.value.raw not in matching[0]:
        return None
    return row, page


def _row_conditions(row: dict[str, str]) -> tuple[tuple[str, str], ...]:
    relevant = ("min. to open", "min. to earn", "minimum to open", "minimum to earn", "points")
    return tuple(sorted((key, _number(value)) for key, value in row.items()
                        if key in relevant and value.strip()))


def _disclosure_text(claim: Claim, page) -> str:
    lines = page.markdown.splitlines()
    pos = claim.line
    while pos < len(lines) and lines[pos].strip().startswith("|"):
        pos += 1
    following = []
    for line in lines[pos:pos + 12]:
        plain = line.strip().lower()
        if (plain.startswith(("#", "|")) or
                re.fullmatch(r"(?:home loans?(?:\s*[-–:]\s*(?:purchase|refinance|cash[- ]out refinance|home equity refinance))?"
                             r"|home equity refinance|cash[- ]out refinance)", plain)):
            break
        following.append(line)
    return " ".join(following).lower()


def _disclosure_scope(claim: Claim, page) -> str | None:
    text = _disclosure_text(claim, page)
    scopes = set()
    if re.search(r"\bpurchase(?: transaction)?\b", text):
        scopes.add("purchase")
    if re.search(r"\bcash[- ]out refinance\b", text):
        scopes.add("cash_out_refinance")
    if re.search(r"\bhome[- ]equity refinance\b", text):
        scopes.add("home_equity_refinance")
    if re.search(r"\brate[/ -]?term refinance\b", text):
        scopes.add("refinance")
    elif re.search(r"\brefinance\b", text) and not scopes.intersection(
            {"refinance", "cash_out_refinance", "home_equity_refinance"}):
        scopes.add("refinance_unspecified")
    return next(iter(scopes)) if len(scopes) == 1 else "ambiguous" if scopes else None


def _disclosures(claim: Claim, page) -> dict[str, str]:
    text = _disclosure_text(claim, page)
    patterns = {
        "credit_score": r"minimum credit score of\s*(\d+)",
        "ltv": r"(\d+(?:\.\d+)?)%\s*(?:loan[- ]to[- ]value|\(ltv\))",
        "rate_lock_days": r"(\d+)\s*[- ]day rate lock",
        "daily_deadline": r"expire at\s*(\d+)\s*pm",
    }
    found = {key: match.group(1) for key, pattern in patterns.items()
             if (match := re.search(pattern, text))}
    for key, pattern in (("owner_occupied", r"owner[- ]occupied"),
                         ("loan_approval", r"all loans are subject to approval"),
                         ("as_low_as", r"rates may be as low as")):
        if re.search(pattern, text):
            found[key] = "yes"
    if dict(claim.identity.qualifiers).get("transaction") == "refinance" and re.search(
            r"rate[/ ]term refinance", text):
        found["rate_term_refinance"] = "yes"
    return found


def _conditions_match(left: Claim, right: Claim, left_row, right_row, left_page, right_page) -> bool:
    if _row_conditions(left_row) != _row_conditions(right_row):
        return False
    if left.identity.family != "mortgage":
        return True
    a, b = _disclosures(left, left_page), _disclosures(right, right_page)
    required = {"credit_score", "ltv", "rate_lock_days", "owner_occupied", "loan_approval"}
    return required <= a.keys() and required <= b.keys() and a == b


def _section_scope(claim: Claim, row: dict[str, str], page) -> str | None:
    """Read transaction scope from the actual section and its verified row."""
    if claim.identity.family != "mortgage":
        return None
    heading = claim.section.rsplit(" > ", 1)[-1].lower()
    row_label = next(iter(row.values()), "").lower()
    transaction = dict(claim.identity.qualifiers).get("transaction")
    disclosed = _disclosure_scope(claim, page)
    if disclosed == "ambiguous":
        return None
    if transaction == "purchase":
        if (not row_label.startswith("purchase:") or
                not re.fullmatch(r"home loans?(?:\s*[-–:]\s*purchase)?|purchase", heading) or
                disclosed not in (None, "purchase")):
            return None
        return "purchase"
    if transaction != "refinance" or not row_label.startswith("refinance:"):
        return None
    if "cash-out refinance" in heading or "cash out refinance" in heading:
        return "cash_out_refinance" if disclosed in (None, "cash_out_refinance") else None
    if "home equity refinance" in heading:
        return "home_equity_refinance" if disclosed in (None, "home_equity_refinance") else None
    if "home loan refinance" in heading:
        return "refinance" if disclosed in (None, "refinance") else None
    if (re.fullmatch(r"home loans?\s*[-–:]\s*refinance", heading)
            and disclosed == "refinance"):
        return "refinance"
    return None


def _source_role(claim: Claim) -> str:
    title = claim.title.lower()
    if re.search(r"\b(?:rates?|rate sheet)\b", title) and "rate" in claim.section.lower():
        return "dedicated_rate_sheet"
    if re.search(r"\b(?:calculator|guide|guidance|tips)\b|\?", title):
        return "guidance"
    if (claim.identity.family == "mortgage" and re.search(r"\b(?:mortgage|home loans?)\b", title)):
        return "product_page"
    return "other"


def _complete(identity: FactIdentity) -> bool:
    q = dict(identity.qualifiers)
    if identity.family == "mortgage":
        return {"term", "transaction", "loan_type", "program", "rate_structure"} <= q.keys()
    if identity.family == "certificate":
        return {"term", "size", "min_deposit", "program"} <= q.keys()
    if identity.family == "deposit":
        return bool(identity.subject and q.get("tier") and q.get("program"))
    return False


def _topic(identity: FactIdentity) -> str:
    q = dict(identity.qualifiers)
    details = [q[k] for k in ("scope", "account_type", "variant", "program", "transaction",
                              "loan_type", "term", "tier", "rate_period") if q.get(k)]
    return " ".join((identity.subject, *details, identity.measure.replace("_", " ")))


def _proposal(left: Claim, right: Claim, classification: str, reason: str) -> Conflict:
    claims = (left, right)
    winner = right if classification == "stale_published" else None
    return Conflict(_topic(left.identity), [c.value.raw for c in claims],
                    [Source(c.url, c.snapshot_id, c.evidence) for c in claims],
                    winner.value.raw if winner else None, reason)


def classify_snapshot_pair(left: Claim, right: Claim, index) -> ShadowCase:
    """Require direct rows, exact identity, conditions, and one selected snapshot."""
    claims = (left, right)
    if left.snapshot_id != index.snapshot_id or right.snapshot_id != index.snapshot_id:
        return ShadowCase("unresolved", index.snapshot_id, claims, "different_snapshot")
    if left.identity.family not in SUPPORTED or qualifier_match(left.identity, right.identity) != "exact":
        return ShadowCase("unresolved", index.snapshot_id, claims, "unproven_identity")
    if left.value.kind != right.value.kind or left.value.value == right.value.value or not _complete(left.identity):
        return ShadowCase("unresolved", index.snapshot_id, claims, "unverified_value_or_qualifier")
    evidence = (_verified(left, index), _verified(right, index))
    if any(item is None for item in evidence):
        return ShadowCase("unresolved", index.snapshot_id, claims, "unverified_source_row")
    (left_row, left_page), (right_row, right_page) = evidence
    if left.identity.family == "mortgage":
        left_scope = _section_scope(left, left_row, left_page)
        right_scope = _section_scope(right, right_row, right_page)
        if left_scope is None or right_scope is None or left_scope != right_scope:
            return ShadowCase("unresolved", index.snapshot_id, claims,
                              "different_or_unknown_section_scope")
    if not _conditions_match(left, right, left_row, right_row, left_page, right_page):
        return ShadowCase("unresolved", index.snapshot_id, claims, "different_or_unknown_conditions")
    if temporal_relationship(left, right) == "confirmed_overlap":
        reason = "Same qualified fact and effective period; no justified winner."
        return ShadowCase("simultaneous", index.snapshot_id, claims, "confirmed_overlap",
                          _proposal(left, right, "simultaneous", reason))
    older, newer = sorted(claims, key=lambda c: c.dates.effective_on or "")
    if not older.dates.effective_on or not newer.dates.effective_on or older.dates.effective_on == newer.dates.effective_on:
        return ShadowCase("unresolved", index.snapshot_id, claims, "unknown_effective_order")
    if older.dates.expires_on and older.dates.expires_on >= newer.dates.effective_on:
        return ShadowCase("unresolved", index.snapshot_id, claims, "overlapping_offer")
    if dict(older.identity.qualifiers).get("program") != "standard":
        return ShadowCase("unresolved", index.snapshot_id, claims, "promotion_applicability_unknown")
    if _source_role(older) != "guidance" or _source_role(newer) not in ("dedicated_rate_sheet", "product_page"):
        return ShadowCase("unresolved", index.snapshot_id, claims, "source_role_not_decisive")
    reason = ("Older guidance remains published beside a later effective dedicated rate source; "
              "the later value is current guidance, without an inferred expiration date.")
    return ShadowCase("stale_published", index.snapshot_id, (older, newer),
                      "verified_source_role_and_schedule", _proposal(older, newer, "stale_published", reason))


def _key(case: ShadowCase):
    return (case.classification, case.claims[0].identity,
            tuple(sorted((c.url, c.value.kind, str(c.value.value),
                          c.dates.effective_on, c.dates.expires_on) for c in case.claims)))


def _deduplicate_proposals(cases: list[ShadowCase]) -> tuple[Conflict, ...]:
    """Keep one cited pair per fact and effective-date generation."""
    chosen = {}
    for case in cases:
        if case.proposed is not None:
            # Repeated snapshots of the same generation retain its last visible citation.
            chosen[_key(case)] = case.proposed
    return tuple(chosen.values())


def _source_line_verified(claim: Claim, index) -> bool:
    if claim.snapshot_id != index.snapshot_id:
        return False
    page = index.pages.get(claim.url)
    if page is None:
        return False
    lines = page.markdown.splitlines()
    if not 1 <= claim.line <= len(lines) or lines[claim.line - 1].strip() != claim.evidence.strip():
        return False
    if claim.identity.family == "auto":
        row = _row_fields(claim, page)
        if row is None or len(row) != 3 or claim.identity.measure != "apr":
            return False
        headers, cells = list(row), list(row.values())
        q = dict(claim.identity.qualifiers)
        years = MODEL_YEAR_RE.fullmatch(normalize(cells[0]).replace("–", "-"))
        older = OLDER_MODEL_RE.fullmatch(normalize(cells[0]))
        if not (years or older) or not re.fullmatch(r"\d{1,3}\s+months?", normalize(cells[1])):
            return False
        start = int(years.group(1)) if years else None
        end = (int(years.group(2)) if len(years.group(2)) == 4 else
               int(years.group(1)[:2] + years.group(2))) if years else int(older.group(1))
        if (q.get("model_year_start") != (str(start) if start else None)
                or q.get("model_year_end") != str(end)
                or q.get("term") != normalize(cells[1]).removesuffix("s")):
            return False
        header_apr = "annual percentage rate" in headers[2] or re.search(r"\bapr\b", headers[2])
        cell_apr = re.search(r"\bapr\b", cells[2], re.I)
        return bool((header_apr or cell_apr) and ("annual percentage yield" not in headers[2] or cell_apr)
                    and claim.value.raw in cells[2])
    return claim.value.raw in claim.evidence or claim.identity.family == "branch"


def _auto_range(claim: Claim) -> tuple[int, int] | None:
    q = dict(claim.identity.qualifiers)
    if not q.get("model_year_end"):
        return None
    start = int(q.get("model_year_start") or 0)
    end = int(q["model_year_end"])
    return (start, end) if start <= end else None


def _review_new_pair(left: Claim, right: Claim, index, reason: str) -> ShadowCase:
    rule = reason if _source_line_verified(left, index) and _source_line_verified(right, index) else "unverified_new_source_row"
    return ShadowCase("unresolved", index.snapshot_id, (left, right), rule)


def analyze(indexes, resolver: TemporalResolver) -> ShadowConflictReport:
    """Compare one snapshot at a time; keep all records and dedupe only proposals."""
    cases = []
    previous_by_url = {}
    for index in indexes:
        by_identity = defaultdict(list)
        for claim in index.fact_index.claims:
            if claim.identity.family in SUPPORTED | REVIEW_ONLY:
                by_identity[claim.identity].append(claim)
        for claims in by_identity.values():
            for pos, left in enumerate(claims):
                for right in claims[pos + 1:]:
                    if left.value.kind != right.value.kind or left.value.value == right.value.value:
                        continue
                    if left.identity.family in REVIEW_ONLY:
                        cases.append(_review_new_pair(left, right, index, "new_category_review_only"))
                    else:
                        cases.append(classify_snapshot_pair(left, right, index))
        auto = [claim for claim in index.fact_index.claims if claim.identity.family == "auto"]
        for pos, left in enumerate(auto):
            for right in auto[pos + 1:]:
                if (left.identity == right.identity or left.value.value == right.value.value
                        or left.url == right.url):
                    continue
                a, b = dict(left.identity.qualifiers), dict(right.identity.qualifiers)
                if (left.identity.measure != right.identity.measure
                        or any(a.get(key) != b.get(key) for key in ("program", "term"))):
                    continue
                first, second = _auto_range(left), _auto_range(right)
                if first and second and max(first[0], second[0]) <= min(first[1], second[1]):
                    rule = ("overlapping_auto_model_year_ranges" if a.get("rate_type") == b.get("rate_type")
                            else "overlapping_auto_ranges_different_rate_type")
                    cases.append(_review_new_pair(left, right, index, rule))
        for candidate in resolver.discover_review_candidates(index.snapshot_id, index.taken_on):
            cases.append(ShadowCase("unresolved", index.snapshot_id, candidate.claims,
                                    candidate.reason))
        current_by_url = {(c.url, c.identity): c for c in index.fact_index.claims
                          if c.identity.family in SUPPORTED}
        for key, current in current_by_url.items():
            earlier = previous_by_url.get(key)
            if (earlier and earlier.value.kind == current.value.kind
                    and earlier.value.value != current.value.value
                    and earlier.dates.effective_on and current.dates.effective_on
                    and earlier.dates.effective_on < current.dates.effective_on):
                cases.append(ShadowCase("historical", index.snapshot_id, (earlier, current),
                                        "same_source_updated_between_snapshots"))
        previous_by_url = current_by_url
    counts = Counter(case.classification for case in cases)
    return ShadowConflictReport(tuple(cases), _deduplicate_proposals(cases),
                                tuple(sorted(counts.items())))


def _raw_table_dates(claim: Claim, page) -> tuple[str | None, str | None]:
    lines = page.markdown.splitlines()
    pos = claim.line - 1
    if pos < 0 or pos >= len(lines) or lines[pos].strip() != claim.evidence.strip():
        return None, None
    end = pos + 1
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    return _table_dates(lines, end)


def _validated_case(case: ShadowCase, index) -> ShadowCase | None:
    if case.classification not in ("simultaneous", "stale_published") or len(case.claims) != 2:
        return None
    if case.snapshot_id != index.snapshot_id or any(c.snapshot_id != index.snapshot_id for c in case.claims):
        return None
    for claim in case.claims:
        verified = _verified(claim, index)
        if verified is None or not _complete(claim.identity):
            return None
        _, page = verified
        if _raw_table_dates(claim, page) != (claim.dates.effective_on, claim.dates.expires_on):
            return None
    checked = classify_snapshot_pair(*case.claims, index)
    if checked.classification != case.classification or checked.proposed is None:
        return None
    if case.proposed != checked.proposed:
        return None
    return checked


def _public_group_key(case: ShadowCase):
    claims = case.claims
    return (case.snapshot_id, claims[0].identity, case.classification,
            tuple((c.value.kind, str(c.value.value), c.dates.effective_on,
                   c.dates.expires_on) for c in claims))


def public_conflicts(report: ShadowConflictReport, indexes) -> PublicConflictResult:
    """Build validated output; distinguish empty, rejected, and failed results."""
    try:
        by_snapshot = {index.snapshot_id: index for index in indexes}
        groups = defaultdict(list)
        rejected = []
        for case in report.cases:
            if case.proposed is None:
                continue
            index = by_snapshot.get(case.snapshot_id)
            checked = _validated_case(case, index) if index is not None else None
            if checked is None:
                rejected.append((case.snapshot_id, "public_evidence_validation_failed"))
                continue
            groups[_public_group_key(checked)].append(checked)
        output = []
        for key in sorted(groups, key=repr):
            cases = groups[key]
            representative = cases[0]
            citations = []
            seen = set()
            for case in cases:
                for claim in case.claims:
                    citation = (claim.value.raw, claim.url, claim.snapshot_id, claim.evidence)
                    if citation not in seen:
                        seen.add(citation)
                        citations.append(citation)
            citations.sort()
            reason = representative.proposed.reason
            if representative.classification == "stale_published":
                older, newer = representative.claims
                reason = (f"Older {_source_role(older)} effective {older.dates.effective_on} remains published "
                          f"beside {_source_role(newer)} effective {newer.dates.effective_on}; "
                          "the later source supplies current guidance, with no inferred expiration date.")
            output.append(Conflict(representative.proposed.topic,
                                   [value for value, _, _, _ in citations],
                                   [Source(url, sid, quote) for _, url, sid, quote in citations],
                                   representative.proposed.resolution, reason))
        return PublicConflictResult(tuple(output), tuple(rejected))
    except Exception as exc:
        return PublicConflictResult((), (), f"{type(exc).__name__}: {exc}")

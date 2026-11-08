"""Deterministic, standalone resolution of extracted website claims.

The submission's answer() and conflicts() paths do not call this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

try:
    from .facts import Claim, FactIdentity, FactIndex, temporal_relationship
except ImportError:  # The harness loads solution.py as a top-level module.
    from facts import Claim, FactIdentity, FactIndex, temporal_relationship


@dataclass(frozen=True, slots=True)
class FactPattern:
    identity: FactIdentity


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    topic: str
    values: tuple[str, ...]
    claims: tuple[Claim, ...]
    classification: str  # confirmed_overlap or unknown
    resolution: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    status: str  # resolved, unresolved, insufficient
    winner: Claim | None
    considered: tuple[Claim, ...]
    excluded: tuple[tuple[Claim, str], ...]
    rules: tuple[str, ...]
    conflicts: tuple[ConflictCandidate, ...]


def qualifier_match(request: FactIdentity, candidate: FactIdentity) -> str:
    """Exact, compatible_unknown, or incompatible; never merge different tiers."""
    if (request.family, request.subject, request.measure) != (candidate.family, candidate.subject, candidate.measure):
        return "incompatible"
    wanted, found = dict(request.qualifiers), dict(candidate.qualifiers)
    unknown = False
    for key in wanted.keys() | found.keys():
        a, b = wanted.get(key), found.get(key)
        if a is None or b is None:
            unknown = True
        elif a != b:
            return "incompatible"
    return "compatible_unknown" if unknown else "exact"


def authority(claim: Claim) -> tuple[int, str]:
    """Rank evidence roles from structure, not individual URLs or values."""
    family = claim.identity.family
    table = claim.evidence.lstrip().startswith("|")
    section = claim.section.lower()
    if family in ("mortgage", "deposit", "certificate", "card"):
        if table and "rate" in section and claim.dates.effective_on:
            return 3, "dated_rate_table"
        if table:
            return 2, "labelled_product_table"
        return 1, "product_prose"
    if family == "institution":
        if "branch" in section and "directory" in section:
            return 3, "branch_directory"
        if claim.dates.published_on:
            return 2, "dated_institutional_statement"
        return 1, "undated_institutional_statement"
    if family == "workforce":
        return (2, "dated_annual_statistic") if claim.dates.reporting_year is not None else (1, "undated_statistic")
    return 1, "labelled_claim"


def relationship(left: Claim, right: Claim) -> str:
    """Infer schedule succession only when version evidence supports replacement."""
    if qualifier_match(left.identity, right.identity) != "exact":
        return "unknown"
    basic = temporal_relationship(left, right)
    if basic != "unknown":
        return basic
    a, b = left.dates, right.dates
    if a.effective_on and b.effective_on:
        if a.effective_on < b.effective_on and a.expires_on and a.expires_on >= b.effective_on:
            return "confirmed_overlap"
        if b.effective_on < a.effective_on and b.expires_on and b.expires_on >= a.effective_on:
            return "confirmed_overlap"
    if (left.identity.family in ("mortgage", "deposit", "certificate", "card")
            and a.effective_on and b.effective_on and a.effective_on != b.effective_on
            and authority(left)[0] >= 2 and authority(right)[0] >= 2
            and "special" not in dict(left.identity.qualifiers).get("program", "")
            and left.identity == right.identity):
        # Two versions of the same labelled rate schedule. An explicit
        # overlapping expiration would have been classified above.
        return "confirmed_succession"
    return "unknown"


def _value_key(claim: Claim):
    return claim.value.kind, claim.value.unit, claim.value.value


def _deduplicate(claims):
    seen = set()
    result = []
    for claim in claims:
        signature = (claim.url, claim.identity, _value_key(claim),
                     claim.dates.published_on, claim.dates.effective_on,
                     claim.dates.expires_on, claim.dates.reporting_year)
        if signature not in seen:
            seen.add(signature)
            result.append(claim)
    return tuple(result)


class TemporalResolver:
    def __init__(self, indexes: tuple[FactIndex, ...] | list[FactIndex]):
        self.indexes = {index.snapshot_id: index for index in indexes}

    def resolve(self, pattern: FactPattern, target_date: str,
                selected_snapshot: str | FactIndex) -> ResolutionDecision:
        sid = selected_snapshot if isinstance(selected_snapshot, str) else selected_snapshot.snapshot_id
        index = self.indexes[sid]
        excluded = []
        eligible = []
        # Snapshot isolation is the first filter; no other index is consulted.
        for claim in index.by_family.get(pattern.identity.family, ()):
            match = qualifier_match(pattern.identity, claim.identity)
            if match == "incompatible":
                continue
            if claim.dates.effective_on and claim.dates.effective_on > target_date:
                excluded.append((claim, "not_yet_effective"))
            elif claim.dates.expires_on and claim.dates.expires_on < target_date:
                excluded.append((claim, "expired"))
            elif claim.dates.published_on and claim.dates.published_on > target_date:
                excluded.append((claim, "not_yet_published"))
            else:
                eligible.append(claim)
        eligible = list(_deduplicate(eligible))
        if not eligible:
            return ResolutionDecision("insufficient", None, (), tuple(excluded), ("no_applicable_claim",), ())

        # Remove a proven earlier schedule version before comparing values.
        superseded = set()
        for older in eligible:
            for newer in eligible:
                if (older.id != newer.id and older.dates.effective_on and newer.dates.effective_on
                        and older.dates.effective_on < newer.dates.effective_on
                        and relationship(older, newer) == "confirmed_succession"
                        and authority(newer)[0] >= authority(older)[0]):
                    superseded.add(older.id)
        active = [claim for claim in eligible if claim.id not in superseded]
        excluded.extend((claim, "successive_schedule_version") for claim in eligible if claim.id in superseded)
        if not active:
            return ResolutionDecision("insufficient", None, tuple(eligible), tuple(excluded),
                                      ("no_applicable_claim",), ())

        # Preserve all competing values and their provenance. Unknown temporal
        # relationships are possible conflicts, never confirmed contradictions.
        competing = []
        for i, left in enumerate(active):
            for right in active[i + 1:]:
                if _value_key(left) != _value_key(right) and qualifier_match(left.identity, right.identity) == "exact":
                    competing.append((left, right, relationship(left, right)))
        conflicts = []
        if competing:
            involved = _deduplicate(c for left, right, _ in competing for c in (left, right))
            classifications = {kind for _, _, kind in competing}
            classification = "confirmed_overlap" if "confirmed_overlap" in classifications else "unknown"
            conflicts.append(ConflictCandidate(
                f"{pattern.identity.family}: {pattern.identity.subject} {pattern.identity.measure}",
                tuple(dict.fromkeys(c.value.raw for c in involved)), involved,
                classification, None, "unresolved_competing_values"))

        best_authority = max(authority(c)[0] for c in active)
        leaders = [c for c in active if authority(c)[0] == best_authority]
        if pattern.identity.family == "institution":
            dated = [c for c in leaders if c.dates.published_on]
            if dated:
                latest = max(c.dates.published_on for c in dated)
                leaders = [c for c in dated if c.dates.published_on == latest]
        values = {_value_key(c) for c in leaders}
        if len(values) != 1:
            return ResolutionDecision("unresolved", None, tuple(active), tuple(excluded),
                                      ("equal_authority_disagreement",), tuple(conflicts))
        leaders.sort(key=lambda c: (c.dates.effective_on or "", c.dates.published_on or "",
                                    c.url, c.line, c.id), reverse=True)
        winner = leaders[0]
        rules = ("successive_schedule_version",) if superseded else ()
        rules += (authority(winner)[1], "unique_top_authority_value")
        if conflicts:
            conflicts = [ConflictCandidate(c.topic, c.values, c.claims, c.classification,
                                           winner.value.raw, authority(winner)[1]) for c in conflicts]
        return ResolutionDecision("resolved", winner, tuple(active), tuple(excluded), rules, tuple(conflicts))

    def discover_conflicts(self, selected_snapshot: str | FactIndex, target_date: str):
        sid = selected_snapshot if isinstance(selected_snapshot, str) else selected_snapshot.snapshot_id
        index = self.indexes[sid]
        result = []
        for identity in index.by_identity:
            decision = self.resolve(FactPattern(identity), target_date, sid)
            result.extend(decision.conflicts)
        # Several exact identities may produce the same candidate; retain one.
        return tuple({tuple(sorted(c.id for c in item.claims)): item for item in result}.values())

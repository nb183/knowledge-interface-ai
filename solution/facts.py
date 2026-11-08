"""Small, deterministic fact inventory for supplied website snapshots.

This module records evidence; it does not choose authoritative values or answer
questions. Unknown validity bounds remain unknown.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from types import MappingProxyType


MONTHS = {name.lower(): number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"), 1)}
DATE_RE = re.compile(
    r"\b(20\d{2}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/20\d{2}|"
    r"(?:" + "|".join(MONTHS) + r")\s+\d{1,2},?\s+20\d{2})\b", re.I)
PERCENT_RE = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*%")
COUNT_RE = re.compile(r"\b(\d[\d,]*)\s+branch office locations\b", re.I)
VTO_RE = re.compile(r"\b(\d[\d,]*)\s+(?:Dupaco\s+)?employees\b.*\bVolunteer Time Off\b", re.I)
HOUR_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)", re.I)


def parse_date(text: str) -> str | None:
    """Return an explicit valid ISO date; never infer one from the crawl."""
    match = DATE_RE.search(text)
    if not match:
        return None
    raw = match.group(0)
    try:
        if "-" in raw:
            year, month, day = (int(x) for x in raw.split("-"))
        elif "/" in raw:
            month, day, year = (int(x) for x in raw.split("/"))
        else:
            parts = raw.replace(",", "").split()
            month, day, year = MONTHS[parts[0].lower()], int(parts[1]), int(parts[2])
        return date(year, month, day).isoformat()
    except (ValueError, KeyError):
        return None


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[*_`\\]", "", text).lower().split())


def normalized_tier(text: str) -> str:
    return re.sub(r"\s+", "", normalize(text).replace(",", "").replace("–", "-"))


@dataclass(frozen=True, slots=True)
class FactIdentity:
    family: str
    subject: str
    measure: str
    qualifiers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def make(cls, family: str, subject: str, measure: str, **qualifiers: str):
        return cls(family, normalize(subject), measure,
                   tuple(sorted((k, normalize(v)) for k, v in qualifiers.items() if v)))


@dataclass(frozen=True, slots=True)
class TypedValue:
    kind: str
    value: Decimal | str
    unit: str
    raw: str


@dataclass(frozen=True, slots=True)
class ClaimDates:
    snapshot_on: str
    fetched_at: str
    published_on: str | None = None
    effective_on: str | None = None
    expires_on: str | None = None
    reporting_year: int | None = None


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    identity: FactIdentity
    value: TypedValue
    dates: ClaimDates
    snapshot_id: str
    url: str
    title: str
    section: str
    evidence: str
    line: int


def temporal_relationship(left: Claim, right: Claim) -> str:
    """Return confirmed_overlap, confirmed_succession, or unknown.

    An effective date alone is an open lower bound, not proof that an older
    claim ceased to apply. Different reporting years denote successive periods.
    """
    def without_year(identity):
        return (identity.family, identity.subject, identity.measure,
                tuple((key, value) for key, value in identity.qualifiers if key != "reporting_year"))
    if without_year(left.identity) != without_year(right.identity):
        return "unknown"
    a, b = left.dates, right.dates
    if a.reporting_year is not None and b.reporting_year is not None:
        return "confirmed_overlap" if a.reporting_year == b.reporting_year else "confirmed_succession"
    if a.reporting_year is not None or b.reporting_year is not None:
        return "unknown"
    if a.effective_on and b.effective_on:
        if a.expires_on and a.expires_on < b.effective_on or b.expires_on and b.expires_on < a.effective_on:
            return "confirmed_succession"
        if a.effective_on == b.effective_on:
            return "confirmed_overlap"
        if a.expires_on and b.expires_on:
            return "confirmed_overlap"
    return "unknown"


def _number(raw: str, kind: str) -> TypedValue | None:
    try:
        return TypedValue(kind, Decimal(raw.replace(",", "").replace("%", "").strip()),
                          "%" if kind == "percent" else "count", raw)
    except InvalidOperation:
        return None


def _publication(lines: list[str]) -> str | None:
    # Newsroom pages put a standalone publication date before the article title.
    for line in lines[:12]:
        if DATE_RE.fullmatch(line.strip()):
            return parse_date(line)
    return None


def _table_dates(lines: list[str], end: int) -> tuple[str | None, str | None]:
    effective = expires = None
    for line in lines[end:min(end + 10, len(lines))]:
        plain = normalize(line)
        if line.lstrip().startswith("|") or line.lstrip().startswith("#"):
            break
        if plain and not DATE_RE.search(line) and len(plain) < 75 and not re.search(r"[.!?]$", plain):
            break
        if not effective and re.search(r"\b(?:rates? are |effective )?effective as of\b", plain):
            effective = parse_date(line)
        if not expires and re.search(r"\b(?:offer|promotion|rate|rates)\b.*\bexpir(?:es|ation)\b", plain):
            expires = parse_date(line)
        if effective and expires:
            break
    return effective, expires


def _claim(identity: FactIdentity, value: TypedValue, dates: ClaimDates,
           snapshot_id: str, page, section: str, evidence: str, line: int, field: str) -> Claim:
    raw_id = "\x1f".join((snapshot_id, page.url, str(line), field,
                          repr(identity), value.raw, evidence))
    return Claim(hashlib.sha256(raw_id.encode()).hexdigest()[:24], identity, value,
                 dates, snapshot_id, page.url, page.title, section, evidence, line)


def extract_claims(page, snapshot_id: str, snapshot_on: str) -> tuple[Claim, ...]:
    """Extract structured rate rows, branch hours/counts, and dated VTO counts."""
    lines = page.markdown.splitlines()
    published = _publication(lines)
    base = ClaimDates(snapshot_on, page.fetched_at, published)
    claims = []
    headings: list[tuple[int, str]] = []
    section_label = ""
    branch = service = ""
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, normalize(line.lstrip("# "))))
            section_label = ""
            service = ""
            if level <= 3:
                branch = ""
        section = " > ".join(h for _, h in headings)
        if line.startswith("|") and line.endswith("|"):
            start = i
            table = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table.append((i + 1, lines[i].strip(), [x.strip() for x in lines[i].strip().strip("|").split("|")]))
                i += 1
            effective, expires = _table_dates(lines, i)
            if len(table) >= 3:
                headers = [normalize(x) for x in table[0][2]]
                for number, evidence, cells in table[2:]:
                    if len(cells) != len(headers) or all(not x for x in cells):
                        continue
                    row = dict(zip(headers, cells))
                    subject = row.get("account type") or row.get("certificate") or row.get("card type") or row.get("program") or row.get("term") or ""
                    if not subject:
                        continue
                    lower = normalize(subject + " " + section + " " + section_label + " " + page.title)
                    if "mortgage" in lower or "home loan" in lower or re.search(r"\b\d{1,3}-year fixed\b", lower):
                        family = "mortgage"
                        loan_type = next((kind for kind in ("conventional", "fha", "va", "usda", "jumbo")
                                          if re.search(r"\b" + kind + r"\b", normalize(subject))), "unspecified")
                        product = "mortgage"
                    elif "certificate" in lower or re.search(r"\bcd\b", lower):
                        family = "certificate"
                        product = "certificate"
                    elif "visa" in lower or "credit card" in lower:
                        family = "card"
                        product = subject
                    elif "savings" in lower or "money market" in lower or "checking" in lower:
                        family = "deposit"
                        product = subject if row.get("account type") else section_label
                    else:
                        continue
                    term = re.search(r"\b\d{1,3}[- ](?:month|year)\b|\b\d{1,2}/\d{1,2}\b", lower)
                    qualifiers = {"tier": normalized_tier(row.get("balance tier", "") or
                                                             (re.search(r"\$[\d,]+\s+or\s+less", subject, re.I) or [""])[0]),
                                  "term": term.group(0).replace("-", " ") if term else "",
                                  "transaction": "refinance" if "refinance" in normalize(subject) else
                                                 ("purchase" if "purchase" in normalize(subject) else ""),
                                  "program": "special" if re.search(r"\b(relief|special|promotion)\b", lower) else "standard",
                                  "variant": normalize(subject) if family == "certificate" else "",
                                  "loan_type": loan_type if family == "mortgage" else "",
                                  "rate_structure": ("arm" if re.search(r"\barm\b|adjustable", lower) else
                                                     "fixed" if "fixed" in normalize(subject) else "")
                                                    if family == "mortgage" else "",
                                  "scope": "business" if "/business" in page.url else
                                           ("personal" if "/personal" in page.url else "")}
                    # The product name carries card/deposit identity; qualifiers
                    # keep tiers and loan terms distinct within that product.
                    for header, cell in row.items():
                        measure = ("apy" if "annual percentage yield" in header or header == "apy" else
                                   "apr" if header == "apr" else
                                   "dividend_rate" if "dividend rate" in header else
                                   "interest_rate" if header == "rate" else "")
                        if not measure:
                            continue
                        match = PERCENT_RE.search(cell)
                        if not match:
                            continue
                        value = _number(match.group(0), "percent")
                        if value:
                            dates = ClaimDates(base.snapshot_on, base.fetched_at, base.published_on,
                                               effective, expires)
                            identity = FactIdentity.make(family, product, measure, **qualifiers)
                            claims.append(_claim(identity, value, dates, snapshot_id, page,
                                                 section + (" > " + section_label if section_label else ""),
                                                 evidence, number, header))
            continue
        if line and not line.startswith(("[", "!", "* *")) and len(line) < 75 and not re.search(r"[.!?]$", line):
            # Plain labels often delimit product tables on the supplied rate pages.
            if not re.search(r"\d", line) and not line.startswith("-"):
                section_label = normalize(line)
        if line.lower().endswith("branch") and len(line) < 60:
            branch = normalize(line.lstrip("# "))
            service = ""
        named_branch = re.search(
            r"\b(?:The\s+)?([A-Z][A-Za-z-]+(?:\s+(?!Dupaco\b)[A-Z][A-Za-z-]+){0,2})\s+"
            r"(?:Dupaco\s+)?branch\s+(?:is located|hours|is open)\b", raw)
        if named_branch:
            branch = normalize(named_branch.group(1) + " branch")
            service = ""
        address = re.search(r"\b\d{2,6}\s+[^,]+,\s*[^,]+,\s*[A-Z]{2},?\s*\d{5}\b", line)
        if branch in ("branch", "dupaco branch") and address:
            branch = normalize(address.group(0))
        label = re.match(r"^(?:\*\*|__)?(Lobby|Drive-up|Drive up)(?:\*\*|__)?\b", line, re.I)
        if label:
            service = normalize(label.group(1)).replace("drive up", "drive-up")
        if branch and service and HOUR_RE.search(line):
            day = re.search(
                r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
                r"(?:\s*[-–]\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?\b",
                line, re.I)
            if day:
                day_label = day.group(1) + ("-" + day.group(2) if day.group(2) else "")
                identity = FactIdentity.make("branch", branch, "hours", service=service, day=day_label)
                claims.append(_claim(identity, TypedValue("schedule", normalize(line), "hours", line),
                                     base, snapshot_id, page, section, raw, i + 1, "hours"))
        count = COUNT_RE.search(line)
        if count:
            value = _number(count.group(1), "count")
            if value:
                identity = FactIdentity.make("institution", "dupaco", "branch_office_count")
                claims.append(_claim(identity, value, base, snapshot_id, page, section,
                                     raw, i + 1, "branch_office_count"))
        vto = VTO_RE.search(re.sub(r"[*_]", "", line))
        if vto:
            nearby = " ".join(lines[max(0, i - 5):i + 1])
            year = re.search(r"\b(?:in\s+)?(20\d{2})\s+(?:alone|called\s+Volunteer Time Off)\b", nearby, re.I)
            if not year:
                year = re.search(r"\b(?:in\s+)?(20\d{2})\b", line)
            value = _number(vto.group(1), "count")
            if value:
                report = int(year.group(1)) if year else None
                identity = FactIdentity.make("workforce", "dupaco", "vto_employee_count",
                                             reporting_year=str(report) if report else "")
                dates = ClaimDates(base.snapshot_on, base.fetched_at, base.published_on,
                                   reporting_year=report)
                claims.append(_claim(identity, value, dates, snapshot_id, page, section,
                                     raw, i + 1, "vto_employee_count"))
        i += 1
    return tuple(claims)


class FactIndex:
    """Immutable views of one snapshot's claims, in deterministic page order."""

    def __init__(self, snapshot):
        self.snapshot_id = snapshot.snapshot_id
        self.claims = tuple(claim for page in snapshot.pages
                            for claim in extract_claims(page, snapshot.snapshot_id, snapshot.taken_on))
        identities = {}
        families = {}
        for claim in self.claims:
            identities.setdefault(claim.identity, []).append(claim)
            families.setdefault(claim.identity.family, []).append(claim)
        self.by_identity = MappingProxyType({key: tuple(value) for key, value in identities.items()})
        self.by_family = MappingProxyType({key: tuple(value) for key, value in families.items()})

    def get(self, identity: FactIdentity) -> tuple[Claim, ...]:
        return self.by_identity.get(identity, ())

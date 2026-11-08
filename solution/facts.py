"""Small, deterministic fact inventory for supplied website snapshots.

This module records evidence; it does not choose authoritative values or answer
questions. Unknown validity bounds remain unknown.
"""
from __future__ import annotations

import hashlib
import html
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
MODEL_YEAR_RE = re.compile(r"^(20\d{2})\s*[-–]\s*(\d{2}|20\d{2})\s+model years?$", re.I)
OLDER_MODEL_RE = re.compile(r"^(20\d{2})\s+model years?\s*\(and older\)$", re.I)
MONEY_RE = re.compile(r"\$\s*(\d[\d,]*(?:\.\d{1,2})?)")


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


def normalized_amount(text: str) -> str:
    """Use one identity for amounts printed as $1,000 and $1,000.00."""
    match = re.fullmatch(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text.strip())
    return format(Decimal(match.group(1).replace(",", "")).normalize(), "f") if match else ""


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    url: str
    line: int
    reason: str
    evidence: str


def _rate_column(header: str) -> tuple[str, str] | None:
    """Identify one measure and its explicit time condition, or decline the cell."""
    label = normalize(header)
    measure_words = (("annual percentage yield", "apy"), ("apy", "apy"),
                     ("annual percentage rate", "apr"), ("apr", "apr"),
                     ("dividend rate", "dividend_rate"), ("interest rate", "interest_rate"))
    kinds = {kind for word, kind in measure_words
             if re.search(r"\b" + re.escape(word) + r"\b", label)}
    if re.search(r"\brate\s*[/&+]", label) and kinds:
        return None
    if not kinds and re.fullmatch(r"rate(?:\s*\(?as low as\)?)?", label):
        kinds = {"interest_rate"}
    if len(kinds) != 1:
        return None
    conditions = []
    period = re.search(r"\b(?:first|initial|1st)\s+\d+\s+months?\b", label)
    if period:
        conditions.append(period.group())
    if "after promotional period" in label:
        conditions.append("after promotional period")
    if "as low as" in label:
        conditions.append("as low as")
    remainder = label
    for word, _ in measure_words:
        remainder = re.sub(r"\b" + re.escape(word) + r"\b", " ", remainder)
    if kinds == {"interest_rate"}:
        remainder = re.sub(r"\brate\b", " ", remainder)
    for condition in conditions:
        remainder = remainder.replace(condition, " ")
    remainder = re.sub(r"\bfor\b|[\d\s()*:.-]", "", remainder)
    if remainder:
        return None
    return kinds.pop(), "; ".join(conditions)


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
    qualifier_provenance: tuple[tuple[str, str, str], ...] = ()


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
           snapshot_id: str, page, section: str, evidence: str, line: int, field: str,
           qualifier_provenance: tuple[tuple[str, str, str], ...] = ()) -> Claim:
    raw_id = "\x1f".join((snapshot_id, page.url, str(line), field,
                          repr(identity), value.raw, evidence))
    return Claim(hashlib.sha256(raw_id.encode()).hexdigest()[:24], identity, value,
                 dates, snapshot_id, page.url, page.title, section, evidence, line,
                 qualifier_provenance)


def _auto_claims(page, snapshot_id, base, section, label, headers, table, effective,
                 expires, diagnostics):
    """Accept only an explicitly labelled auto model-year APR table."""
    nearby = " ".join(page.markdown.splitlines()[max(0, table[0][0] - 6):table[0][0] - 1]).lower()
    auto_context = "auto loan" in label or ("auto & motorcycle" in label and "auto loan rates" in nearby)
    if (len(headers) != 3 or headers[1] not in ("term", "balance tier")
            or not auto_context
            or not any("model year" in cells[0].lower() for _, _, cells in table[2:] if cells)):
        return None
    header = headers[2]
    header_apr = "annual percentage rate" in header or re.search(r"\bapr\b", header)
    header_apy = "annual percentage yield" in header or re.search(r"\bapy\b", header)
    if not header_apr and not header_apy:
        return ()
    role = ("calculator_rate_table" if "calculator" in page.title.lower() else
            "product_rate_table" if re.search(r"auto|motorcycle", page.title, re.I) else
            "dedicated_rate_sheet" if re.search(r"\brates?\b", page.title, re.I) else
            "campaign_rate_table" if re.search(r"\b(?:offer|promotion|switch|cash back)\b", page.title, re.I) else
            "other_rate_table")
    found = []
    for number, evidence, cells in table[2:]:
        if len(cells) != 3:
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "irregular_auto_row", evidence))
            continue
        model, term, raw = cells
        years = MODEL_YEAR_RE.fullmatch(normalize(model).replace("–", "-"))
        older = OLDER_MODEL_RE.fullmatch(normalize(model))
        months = re.fullmatch(r"(\d{1,3})\s+months?", normalize(term).replace("\xa0", " "))
        rate = PERCENT_RE.search(raw)
        cell_apr = bool(re.search(r"\bapr\b", raw, re.I))
        if not (years or older) or not months or not rate or len(PERCENT_RE.findall(raw)) != 1:
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "ambiguous_auto_row", evidence))
            continue
        if header_apy:
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "auto_header_cell_measure_mismatch", evidence))
            if not cell_apr:
                continue
        elif not header_apr or not cell_apr and role == "calculator_rate_table":
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "auto_apr_not_explicit", evidence))
            continue
        start = int(years.group(1)) if years else None
        end = (int(years.group(2)) if len(years.group(2)) == 4 else
               int(years.group(1)[:2] + years.group(2))) if years else int(older.group(1))
        if start is not None and (end < start or end - start > 15):
            continue
        identity = FactIdentity.make("auto", "auto loan", "apr", program="auto loan",
                                    term=f"{months.group(1)} month", model_year_start=str(start) if start else "",
                                    model_year_end=str(end), model_year_relation="and older" if older else "range",
                                    rate_type="as low as" if "as low as" in header else "quoted")
        dates = ClaimDates(base.snapshot_on, base.fetched_at, base.published_on, effective, expires)
        provenance = (("model_year", "row", model), ("term", "row", term),
                      ("measure", "cell" if header_apy else "header", raw if header_apy else header),
                      ("source_role", "page_title", role))
        value = _number(rate.group(0), "percent")
        found.append(_claim(identity, value, dates, snapshot_id, page,
                            section + " > " + label if label else section,
                            evidence, number, "auto_apr", provenance))
    return tuple(found)


def _fee_claims(page, snapshot_id, base, section, headers, table, diagnostics):
    """Extract one fee amount only when its scope and conditions travel with it."""
    if not headers or not headers[0].startswith("deposit services"):
        return None
    effective = next((parse_date(line) for line in page.markdown.splitlines()[:12]
                      if re.search(r"\beffective\b", line, re.I) and parse_date(line)), None)
    tax_note = next((line for line in page.markdown.splitlines()
                     if re.search(r"\*\s*sales tax may apply", line, re.I)), "")
    scope = "consumer" if "consumer" in headers[0] else "business" if "business" in headers[0] else ""
    found = []
    for number, evidence, cells in table[2:]:
        cells = ([cell.strip() for cell in evidence[1:-1].split("|")]
                 if evidence.startswith("|") else cells)
        if len(cells) != len(headers) or not cells[0]:
            continue
        label = normalize(html.unescape(html.unescape(cells[0])))
        if not any(cells[1:]):
            if "deposit services business" in label:
                scope = "business"
            elif "deposit services consumer" in label:
                scope = "consumer"
            continue
        if not scope or any(cells[2:]):
            continue
        amounts = MONEY_RE.findall(cells[1])
        if not amounts or len(amounts) > 2:
            continue
        detail = normalize(html.unescape(html.unescape(cells[1])))
        if "*" in cells[1] and not tax_note:
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "unresolved_fee_footnote", evidence))
            continue
        threshold = re.search(r"less than\s*\$\s*(\d+(?:\.\d{1,2})?)", detail)
        daily = re.search(r"maximum charge of\s+(one|\d+)\s+fee per day", detail)
        free = re.search(r"after\s+(\d+)\s+free per month", label)
        exemption = re.search(r"free if\s+([^)]*)", detail)
        if "overdraft" in label and (not threshold or not daily):
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "incomplete_fee_conditions", evidence))
            continue
        if len(amounts) > 1 and not (threshold and amounts[1] == threshold.group(1)):
            if diagnostics is not None:
                diagnostics.append(ExtractionDiagnostic(page.url, number, "ambiguous_fee_amounts", evidence))
            continue
        unit = next((name for name in ("per item", "per month", "per hour", "per roll",
                                       "per transaction", "per inquiry", "each") if name in detail), "flat")
        identity = FactIdentity.make("fee", label, "amount", account_scope=scope, unit=unit,
                                    transaction_threshold=threshold.group(1) if threshold else "",
                                    daily_cap=daily.group(1) if daily else "",
                                    free_transactions=free.group(1) if free else "",
                                    sales_tax="may apply" if "*" in cells[1] else "",
                                    exemption=("transactions below threshold" if threshold else
                                               exemption.group(1) if exemption else ""))
        dates = ClaimDates(base.snapshot_on, base.fetched_at, base.published_on, effective)
        amount = Decimal(amounts[0].replace(",", ""))
        value = TypedValue("money", amount, "USD", "$" + amounts[0])
        provenance = (("amount", "row", cells[1]), ("account_scope", "table", scope),
                      ("conditions", "row", cells[0] + " " + cells[1]))
        if "*" in cells[1]:
            provenance += (("sales_tax", "footnote", tax_note),)
        found.append(_claim(identity, value, dates, snapshot_id, page,
                            section + " > " + scope + " fees", evidence, number, "fee_amount", provenance))
    return tuple(found)


def _fee_prose_claims(page, snapshot_id, base, diagnostics):
    """Read the same labelled deposit-fee schedule when the PDF is plain text."""
    lines = page.markdown.splitlines()
    if (not any("rate and fee schedule" in normalize(line) for line in lines[:8])
            or any(line.lstrip().startswith("|") for line in lines[:15])):
        return ()
    rows = [(0, "", []), (0, "", [])]
    active = False
    for pos, line in enumerate(lines):
        label = normalize(line)
        if label == "deposit services consumer":
            active = True
            continue
        if not active:
            continue
        if label == "teller and cash handling service charges":
            break
        if label == "deposit services business":
            rows.append((pos + 1, line, [line, ""]))
            continue
        amount = MONEY_RE.search(line)
        if not amount:
            continue
        detail = line[amount.start():]
        if pos + 1 < len(lines) and re.match(r"\s*\*\s*you will not be charged", lines[pos + 1], re.I):
            detail += " " + lines[pos + 1]
        rows.append((pos + 1, line, [line[:amount.start()].strip(), detail]))
    return _fee_claims(page, snapshot_id, base, "rate and fee schedule",
                       ["deposit services consumer", "value"], rows, diagnostics) or ()


def _clock(text: str) -> str | None:
    if normalize(text) == "noon":
        return "12:00"
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", text.strip(), re.I)
    if not match:
        return None
    hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "p" else 0)
    return f"{hour:02d}:{int(match.group(2) or '0'):02d}"


def _schedule_value(line: str) -> tuple[str, tuple[tuple[str, str, str], ...]] | None:
    text = normalize(re.sub(r"^.*?\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b\s*"
                            r"(?:[-–]\s*(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))?\s*:",
                            "", line, flags=re.I))
    if text in ("closed", "atm only"):
        return text, (("status", "row", text),)
    clock = r"(?:\d{1,2}(?::\d{2})?\s*[ap]\.?(?:m\.?)?|noon)"
    match = re.fullmatch(rf"\s*({clock})\s*[-–]\s*({clock})\s*", text, re.I)
    if not match:
        return None
    opening, closing = _clock(match.group(1)), _clock(match.group(2))
    if opening is None or closing is None or opening >= closing:
        return None
    return f"{opening}-{closing}", (("opens", "row", match.group(1)),
                                     ("closes", "row", match.group(2)))


def _explicit_scope(page, section: str, subject: str) -> tuple[str, str, str] | None:
    """Return a scope only when the row, section, or page title says it."""
    for origin, evidence in (("row", subject), ("section", section), ("title", page.title)):
        business = bool(re.search(r"\bbusiness\b", evidence, re.I))
        personal = bool(re.search(r"\bpersonal\b", evidence, re.I))
        if business != personal:
            return ("business" if business else "personal", origin, evidence)
    return None


def extract_claims(page, snapshot_id: str, snapshot_on: str,
                   diagnostics: list[ExtractionDiagnostic] | None = None) -> tuple[Claim, ...]:
    """Extract structured rate rows, branch hours/counts, and dated VTO counts."""
    lines = page.markdown.splitlines()
    published = _publication(lines)
    base = ClaimDates(snapshot_on, page.fetched_at, published)
    claims = list(_fee_prose_claims(page, snapshot_id, base, diagnostics))
    headings: list[tuple[int, str]] = []
    section_label = ""
    branch = service = ""
    branch_schedule = False
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
                branch_schedule = False
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
                separator = table[1][2]
                if (len(separator) != len(headers) or not all(re.fullmatch(r":?-{3,}:?", x.strip()) for x in separator)
                        or any(not x for x in headers)):
                    if diagnostics is not None:
                        diagnostics.append(ExtractionDiagnostic(page.url, table[0][0], "invalid_table_header", table[0][1]))
                    continue
                rate_columns = [_rate_column(x) for x in headers]
                if len(set(headers)) != len(headers):
                    if diagnostics is not None:
                        diagnostics.append(ExtractionDiagnostic(page.url, table[0][0], "duplicate_table_header", table[0][1]))
                    continue
                auto = _auto_claims(page, snapshot_id, base, section, section_label,
                                    headers, table, effective, expires, diagnostics)
                if auto is not None:
                    claims.extend(auto)
                    continue
                fees = _fee_claims(page, snapshot_id, base, section, headers, table, diagnostics)
                if fees is not None:
                    claims.extend(fees)
                    continue
                if not any(rate_columns):
                    continue
                for number, evidence, cells in table[2:]:
                    if len(cells) != len(headers) or all(not x for x in cells):
                        if diagnostics is not None:
                            diagnostics.append(ExtractionDiagnostic(page.url, number, "irregular_table_row", evidence))
                        continue
                    row = dict(zip(headers, cells))
                    inferred_deposit = (section_label if (row.get("balance tier") or row.get("balance"))
                                        and re.search(r"\b(?:savings?|checking|money market|choice|club|account)\b",
                                                      section_label)
                                        and not re.search(r"\b(?:rates?|select an option)\b", section_label) else "")
                    subject = (row.get("account type") or row.get("ira type") or row.get("certificate")
                               or row.get("card type") or row.get("program") or row.get("term")
                               or inferred_deposit or "")
                    if not subject:
                        continue
                    lower = normalize(subject + " " + section + " " + section_label + " " + page.title)
                    ira_type = next((kind for kind in ("roth", "traditional", "sep", "simple")
                                     if re.match(r"^" + kind + r"\s*:", normalize(subject))), "")
                    is_ira = bool(ira_type or "ira account rates" in section or "iras" in section_label)
                    if "mortgage" in lower or "home loan" in lower or re.search(r"\b\d{1,3}-year fixed\b", lower):
                        family = "mortgage"
                        loan_type = next((kind for kind in ("conventional", "fha", "va", "usda", "jumbo")
                                          if re.search(r"\b" + kind + r"\b", normalize(subject))), "unspecified")
                        product = "mortgage"
                    elif "certificate" in lower or re.search(r"\bcd\b", lower):
                        family = "certificate"
                        product = "ira certificate" if is_ira else "certificate"
                    elif is_ira:
                        family = "certificate"
                        product = "ira certificate"
                    elif "visa" in lower or "credit card" in lower:
                        family = "card"
                        product = subject
                    elif ("savings" in lower or "money market" in lower or "checking" in lower
                          or (row.get("balance tier") or row.get("balance")) and
                          any(kind and kind[0] in ("dividend_rate", "apy") for kind in rate_columns)):
                        family = "deposit"
                        product = subject if row.get("account type") else section_label
                        if not product or product in ("select an option below", "regular", "special"):
                            if diagnostics is not None:
                                diagnostics.append(ExtractionDiagnostic(page.url, number, "unknown_deposit_product", evidence))
                            continue
                    else:
                        continue
                    term = re.search(r"\b\d{1,3}[- ](?:months?|years?)\b|\b\d{1,2}/\d{1,2}\b", normalize(subject))
                    term_value = re.sub(r"\b(months|years)\b", lambda m: m.group()[:-1], term.group().replace("-", " ")) if term else ""
                    minimum = normalized_amount(row.get("min. to earn", "") or row.get("minimum to earn", ""))
                    explicit_scope = _explicit_scope(page, section, subject)
                    url_scope = ("business" if "/business" in page.url else
                                 "personal" if "/personal" in page.url else "")
                    scope = explicit_scope[0] if explicit_scope else url_scope
                    qualifiers = {"tier": normalized_tier(row.get("balance tier", "") or row.get("balance", "") or
                                                             (re.search(r"\$[\d,]+\s+or\s+less", subject, re.I) or [""])[0]),
                                  "term": term_value,
                                  "transaction": "refinance" if "refinance" in normalize(subject) else
                                                 ("purchase" if "purchase" in normalize(subject) else ""),
                                  "program": "special" if re.search(r"\b(relief|special|promotion)\b", lower) else "standard",
                                  "variant": normalize(subject) if family == "certificate" else "",
                                  "account_type": ira_type if is_ira else "",
                                  "size": "jumbo" if family == "certificate" and re.search(r"\bjumbo\b", normalize(subject)) else
                                          "ordinary" if family == "certificate" else "",
                                  "min_deposit": minimum if family == "certificate" else "",
                                  "loan_type": loan_type if family == "mortgage" else "",
                                  "rate_structure": ("arm" if re.search(r"\barm\b|adjustable", lower) else
                                                     "fixed" if "fixed" in normalize(subject) else "")
                                                    if family == "mortgage" else "",
                                  "scope": scope}
                    # The product name carries card/deposit identity; qualifiers
                    # keep tiers and loan terms distinct within that product.
                    for col, (header, cell) in enumerate(zip(headers, cells)):
                        column = rate_columns[col]
                        if column is None:
                            if PERCENT_RE.search(cell) and diagnostics is not None:
                                diagnostics.append(ExtractionDiagnostic(page.url, number, "ambiguous_rate_column", evidence))
                            continue
                        measure, condition = column
                        if family in ("deposit", "certificate") and measure == "apr":
                            if diagnostics is not None:
                                diagnostics.append(ExtractionDiagnostic(page.url, number, "unsupported_measure_for_product", evidence))
                            continue
                        matches = list(PERCENT_RE.finditer(cell))
                        if not cell.strip():
                            if diagnostics is not None:
                                diagnostics.append(ExtractionDiagnostic(page.url, number, "empty_rate_cell", evidence))
                            continue
                        range_cell = (len(matches) == 2 and bool(re.fullmatch(
                            r"\s*\d+(?:\.\d+)?\s*%\s*[-–]\s*\d+(?:\.\d+)?\s*%\d*\s*", cell)))
                        if len(matches) != 1 and not range_cell:
                            if diagnostics is not None:
                                diagnostics.append(ExtractionDiagnostic(page.url, number, "ambiguous_rate_cell", evidence))
                            continue
                        value = (TypedValue("percent_range",
                                            "-".join(m.group(1) for m in matches), "%", cell.strip())
                                 if range_cell else _number(matches[0].group(0), "percent"))
                        if value:
                            dates = ClaimDates(base.snapshot_on, base.fetched_at, base.published_on,
                                               effective, expires)
                            identity = FactIdentity.make(family, product, measure, **qualifiers,
                                                         rate_period=condition)
                            provenance = ()
                            if explicit_scope and explicit_scope[0] == scope:
                                provenance += (("scope", explicit_scope[1], explicit_scope[2]),)
                            if condition:
                                provenance += (("rate_period", "header", table[0][2][col]),)
                            claims.append(_claim(identity, value, dates, snapshot_id, page,
                                                 section + (" > " + section_label if section_label else ""),
                                                 evidence, number, f"{col}:{header}", provenance))
            continue
        if line and not line.startswith(("#", "[", "!", "* *")) and len(line) < 75 and not re.search(r"[.!?]$", line):
            # Plain labels often delimit product tables on the supplied rate pages.
            if not re.search(r"\d", line) and not line.startswith("-"):
                section_label = normalize(line)
        if line.lower().endswith("branch") and len(line) < 60:
            branch = normalize(line.lstrip("# "))
            service = ""
            branch_schedule = True
        named_branch = re.search(
            r"\b(?:The\s+)?([A-Z][A-Za-z-]+(?:\s+(?!Dupaco\b)[A-Z][A-Za-z-]+){0,2})\s+"
            r"(?:Dupaco\s+)?branch\s+(?:is located|hours|is open)\b", raw)
        if not named_branch:
            named_branch = re.search(
                r"\bphone number for the\s+([A-Z][A-Za-z-]+(?:\s+[A-Z][A-Za-z-]+){0,2})\s+branch\b",
                raw)
        if named_branch:
            branch = normalize(named_branch.group(1) + " branch")
            service = ""
            branch_schedule = "branch hours" in line.lower()
        if re.search(r"\bbranch hours are\s*:", line, re.I) and branch:
            branch_schedule = True
        if re.search(r"\babout dupaco community credit union\b", line, re.I):
            branch_schedule = False
        address = re.search(r"\b\d{2,6}\s+[^,]+,\s*[^,]+,\s*[A-Z]{2},?\s*\d{5}\b", line)
        if branch in ("branch", "dupaco branch") and address:
            branch = normalize(address.group(0))
        label = re.match(r"^(?:\*\*|__)?(Lobby|Drive-up|Drive up)(?:\*\*|__)?\b", line, re.I)
        if label:
            service = normalize(label.group(1)).replace("drive up", "drive-up")
        if branch and service and branch_schedule:
            day = re.search(
                r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
                r"(?:\s*[-–]\s*(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))?\b",
                line, re.I)
            if day:
                day_label = day.group(1) + ("-" + day.group(2) if day.group(2) else "")
                schedule = _schedule_value(line)
                if schedule:
                    identity = FactIdentity.make("branch", branch, "hours", service=service, day=day_label)
                    claims.append(_claim(identity, TypedValue("schedule", schedule[0], "hours", line),
                                         base, snapshot_id, page, section, raw, i + 1, "hours",
                                         (("branch", "page", branch), ("service", "label", service),
                                          ("day", "row", day_label)) + schedule[1]))
        if (branch_schedule and line and not line.startswith("#") and not label
                and not re.match(r"^(?:\*\*)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                                 line, re.I)
                and "branch hours" not in line.lower()):
            branch_schedule = False
            service = ""
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
        diagnostics: list[ExtractionDiagnostic] = []
        self.claims = tuple(claim for page in snapshot.pages
                            for claim in extract_claims(page, snapshot.snapshot_id, snapshot.taken_on, diagnostics))
        self.diagnostics = tuple(diagnostics)
        identities = {}
        families = {}
        for claim in self.claims:
            identities.setdefault(claim.identity, []).append(claim)
            families.setdefault(claim.identity.family, []).append(claim)
        self.by_identity = MappingProxyType({key: tuple(value) for key, value in identities.items()})
        self.by_family = MappingProxyType({key: tuple(value) for key, value in families.items()})

    def get(self, identity: FactIdentity) -> tuple[Claim, ...]:
        return self.by_identity.get(identity, ())

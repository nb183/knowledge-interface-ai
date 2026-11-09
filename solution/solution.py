"""Local, snapshot-aware lexical knowledge system.

Only the supplied Snapshot objects are used.  Each full crawl gets its own
inverted index, so a historical query never sees a later page.
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict, deque
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                             if (d / "harness" / "contract.py").exists())))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import Answer, Source  # noqa: E402
from facts import FactIndex  # noqa: E402
from answer_adapter import propose  # noqa: E402
from resolver import TemporalResolver  # noqa: E402
from promotion import classify  # noqa: E402

TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:[.,]\d+)*", re.I)
NUMBER = re.compile(r"(?:\$\s*)?\d[\d,]*(?:\.\d+)?\s*%?")
TIME = re.compile(r"\b\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)", re.I)
ADDRESS = re.compile(r"\b\d{2,6}\s+[^,]+,\s*[^,]+,\s*[A-Z]{2},?\s*\d{5}\b")
STOP = set("a an and are as at be by can do does for from have how i in is it me my of on or our please tell the to us was what when where which who with would you your dupaco rate rates interest apr apy current today".split())


def words(s):
    return [w.lower().removesuffix("'s") for w in TOKEN.findall(s)]


def useful(s):
    out = []
    for w in words(s):
        w = {"six": "6", "twelve": "12", "thirty": "30"}.get(w, w)
        if w not in STOP and (len(w) > 1 or w.isdigit()):
            out.append(w)
    return out


def clean(s):
    s = re.sub(r"!\[[^]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", s)
    return re.sub(r"[*_`\\]", "", s).strip()


def links(s):
    """Keep literal Markdown destinations, including fragments and query strings."""
    out = []
    for match in re.finditer(r"(?<!!)\[([^]]+)\]\(", s):
        start, depth, pos = match.end(), 1, match.end()
        while pos < len(s) and depth:
            if s[pos] == "\\":
                pos += 2
                continue
            if s[pos] == "(":
                depth += 1
            elif s[pos] == ")":
                depth -= 1
            pos += 1
        if depth:
            continue
        raw = s[start:pos - 1].strip()
        if raw.startswith("<") and ">" in raw:
            target = raw[1:raw.index(">")]
        else:
            target = re.split(r'\s+"', raw, maxsplit=1)[0]
        if target:
            out.append((clean(match.group(1)), target))
    return tuple(out)


class Passage(NamedTuple):
    text: str
    url: str
    title: str
    snapshot_id: str
    heading: str
    kind: str
    row_key: str = ""
    context: str = ""
    links: tuple = ()


def passages(page, sid):
    """Yield blocks with bounded section and location context."""
    lines = page.markdown.splitlines()
    headings = []
    label = ""
    location_kind = ""
    location_identity = ""
    paragraph = []
    headers = None

    def context():
        return " ".join(x for x in [*(h for _, h in headings), label,
                                    location_kind, location_identity] if x)

    def flush():
        nonlocal location_identity
        if paragraph:
            raw = " ".join(paragraph)
            body = clean(raw)
            found_links = links(raw)
            paragraph.clear()
            if location_kind == "Branch":
                address = ADDRESS.search(body)
                if address:
                    location_identity = address.group(0)
            if (len(body) >= 18 or found_links and len(body) >= 8) and not body.startswith("http"):
                heading = headings[-1][1] if headings else ""
                return Passage(body[:1100], page.url, page.title, sid, heading,
                               "text", context=context(), links=found_links)
        return None

    for line in lines + [""]:
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            p = flush()
            if p:
                yield p
            cells = [clean(x.strip()) for x in line.strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]+", x or "-") for x in cells):
                continue
            if headers is None or len(headers) != len(cells):
                headers = cells
                continue
            row = "; ".join(f"{h}: {v}" for h, v in zip(headers, cells) if v)
            if row:
                heading = headings[-1][1] if headings else ""
                yield Passage(row, page.url, page.title, sid, heading,
                              "table", cells[0], context())
            continue
        headers = None
        if line.startswith("#"):
            p = flush()
            if p:
                yield p
            level = len(line) - len(line.lstrip("#"))
            headings = [(n, h) for n, h in headings if n < level]
            headings.append((level, clean(line.lstrip("# "))))
            label = ""
            if level <= 3:
                location_kind = location_identity = ""
            continue
        if not line or line == "* * *":
            p = flush()
            if p:
                yield p
            continue
        if line.startswith("![") or line.startswith("[") and len(line) > 250:
            continue
        plain = clean(line).strip()
        if line.startswith(("**", "__")) and plain.lower() in ("lobby", "drive-up", "drive up"):
            p = flush()
            if p:
                yield p
            label = plain
            continue
        inline_label = re.match(r"^(?:\*\*|__)(Lobby|Drive-up|Drive up)(?:\*\*|__)\s+", line, re.I)
        if inline_label:
            p = flush()
            if p:
                yield p
            label = inline_label.group(1)
        if plain.lower().endswith("branch") and len(plain) < 35 and not any(c.isdigit() for c in plain):
            location_kind = "Branch"
        elif plain.lower().endswith("atm") and len(plain) < 35 and not any(c.isdigit() for c in plain):
            location_kind = "ATM"
        paragraph.append(line)


class SnapshotIndex:
    def __init__(self, snap):
        self.snapshot_id = snap.snapshot_id
        self.taken_on = snap.taken_on
        self.passages = []
        self.postings = defaultdict(list)
        self.df = Counter()
        self.lengths = []
        self.pages = {p.url: p for p in snap.pages}
        for page in snap.pages:
            self.passages.extend(passages(page, snap.snapshot_id))
        repeated = Counter((p.url, p.text) for p in self.passages)
        for i, p in enumerate(self.passages):
            tf = Counter()
            body_weight = .25 if (repeated[(p.url, p.text)] >= 4 and p.kind == "text") else 1.0
            if len(p.text) < 35 and p.kind == "text" and not p.links and not re.search(r"\d", p.text):
                body_weight *= .35
            for token in useful(p.text):
                tf[token] += body_weight
            for value, weight in ((p.context, .55), (p.title, .22),
                                  (urlparse(p.url).path.replace("/", " "), .12)):
                for token in useful(value):
                    tf[token] += weight
            # Product vocabulary in page metadata can connect a common synonym
            # to a rate row whose body only names its term and value.
            if re.search(r"\bhome[- ]loans?\b", p.title + " " + p.context, re.I):
                tf["mortgage"] += .35
            for anchor, target in p.links:
                parsed = urlparse(target)
                for token in useful(parsed.netloc + " " + parsed.path.replace("/", " ")):
                    tf[token] += .20
            self.lengths.append(sum(tf.values()))
            for token, count in tf.items():
                self.postings[token].append((i, count))
                self.df[token] += 1
        self.avg_length = sum(self.lengths) / max(1, len(self.lengths))
        # Phase 4A inventory only: answer() and conflicts() still use their
        # existing passage paths until a separately approved resolution phase.
        self.fact_index = FactIndex(snap)

    def search(self, question, limit=12):
        q = useful(question)
        if not q:
            return []
        scores = defaultdict(float)
        n = len(self.passages)
        for token, qcount in Counter(q).items():
            df = self.df[token]
            if not df:
                continue
            idf = math.log1p((n - df + .5) / (df + .5))
            for i, tf in self.postings[token]:
                norm = tf + 1.2 * (.25 + .75 * self.lengths[i] / max(self.avg_length, 1))
                scores[i] += idf * tf * 2.2 / norm * min(qcount, 2)
        qset = set(q)
        lower_question = question.lower()
        wants_value = bool(re.search(r"\b(?:rates?|aprs?|apys?|fees?|cost|price|yield)\b", lower_question))
        wants_hours = bool(re.search(r"\bhours?\b", lower_question))
        wants_address = bool(re.search(r"\baddress\b", lower_question))
        topic_phrases = ("share savings", "bright savings", "personal loan", "auto loan",
                         "mortgage", "credit card", "certificate", "routing number",
                         "branch", "checking", "construction loan")
        ranked = []
        for i, score in scores.items():
            p = self.passages[i]
            text = p.text.lower()
            context = set(useful(p.title + " " + p.heading + " " + p.url))
            score *= 1 + .11 * len(qset & context)
            for phrase in topic_phrases:
                if phrase in lower_question:
                    if phrase in text:
                        score *= 1.45
                    elif phrase in (p.title + " " + p.url).lower():
                        score *= 1.2
            if p.kind == "table" and wants_value:
                score *= 1.4
            if "/personal/" in p.url or "/business/" in p.url or p.url.rstrip("/").endswith("/rates"):
                score *= 1.18
            if "auto loan" in lower_question and "/auto" in p.url:
                score *= 1.4
            if "mortgage" in lower_question and "/home-loans" in p.url:
                score *= 1.4
            if "platinum visa" in lower_question and "business" not in lower_question and "business" in text:
                score *= .5
            if wants_value:
                if re.search(r"\d[\d,.]*\s*%", text):
                    score *= 1.5
                else:
                    score *= .55
            if wants_hours:
                score *= 2.2 if TIME.search(p.text) else .65
            if wants_address and "branch" in lower_question and "branch" in p.context.lower() and ADDRESS.search(p.text):
                score *= 1.6
            # Match a requested term to its row, rather than a different tier
            # on a relevant rate sheet.
            for amount in re.findall(r"\b\d{1,3}(?:,\d{3})?\s*[- ]\s*(?:month|year)\b", lower_question):
                term = re.sub(r"\s*[- ]\s*", " ", amount)
                if term in re.sub(r"\s*[- ]\s*", " ", text):
                    score *= 1.5
                elif p.kind == "table":
                    score *= .6
            model_year = re.search(r"\b(20\d\d)\b", lower_question)
            if model_year and p.kind == "table" and "model year" in text:
                year = int(model_year.group(1))
                ranges = re.findall(r"(20\d\d)\s*[-–]\s*(\d\d|20\d\d)", text)
                if ranges:
                    within = any(int(a) <= year <= (int(b) if len(b) == 4 else int(a[:2] + b)) for a, b in ranges)
                    score *= 1.8 if within else .55
            if any(x in p.url for x in ("/newsroom/", "/learn/blog/", "/20")):
                score *= .7
            if "privacy-policy?" in p.url:
                score *= .2
            ranked.append((score, i))
        ranked.sort(key=lambda x: (-x[0], self.passages[x[1]].url, x[1]))
        return [(score, self.passages[i]) for score, i in ranked[:limit]]


class RequestSpec(NamedTuple):
    intent: str
    product: str
    qualifiers: dict


class Evidence(NamedTuple):
    passage: Passage
    rank: int
    score: float
    key: tuple
    value: str
    text: str


class Selection(NamedTuple):
    evidence: tuple
    conflicts: tuple


def qualifier_state(required, observed):
    """Report a required match, an unspecified constraint, or incompatibility."""
    if required is None:
        return "unspecified"
    return "required" if observed == required else "incompatible"


def parse_request(question):
    lower = question.lower()
    term = re.search(r"\b(\d{1,3})\s*[- ]\s*(month|year)\b", lower)
    arm = re.search(r"\b\d{1,2}/\d{1,2}\b", lower)
    branch = re.search(r"\b([A-Z][A-Za-z-]+(?:\s+[A-Z][A-Za-z-]+){0,2})\s+[Bb]ranch\b", question)
    amount = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", question)
    qualifiers = {
        "term": arm.group(0) if arm else (f"{term.group(1)} {term.group(2)}" if term else None),
        "tier_amount": float(amount.group(1).replace(",", "")) if amount else None,
        "branch": branch.group(1).lower() if branch else None,
        "service": "drive-up" if re.search(r"\bdrive[- ]up\b", lower) else
                   ("lobby" if "lobby" in lower else None),
        "transaction": "refinance" if "refinanc" in lower else
                       ("purchase" if "purchase" in lower else None),
        "scope": "business" if "business" in lower else
                 ("personal" if "personal" in lower else None),
        "day": next((d for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                                 "saturday", "sunday") if d in lower), None),
    }
    if re.search(r"\b(?:url|link|website)\b", lower):
        return RequestSpec("legacy", "", qualifiers)
    if re.search(r"\bmortgage\b|\bhome[- ]loans?\b", lower) and re.search(r"\brate|\bapr|\binterest", lower):
        qualifiers["program"] = "special" if re.search(r"\b(relief|special|promotion|intro)\b", lower) else "standard"
        return RequestSpec("mortgage_rate" if qualifiers["term"] else "mortgage_overview",
                           "mortgage", qualifiers)
    if re.search(r"\bcredit cards?\b", lower) and re.search(r"\brat|\bapr|\boffer", lower):
        return RequestSpec("list", "card", qualifiers)
    if re.search(r"\b(certificates?|cds?)\b", lower) and re.search(r"\brat|\bapy", lower) and not qualifiers["term"]:
        return RequestSpec("list", "certificate", qualifiers)
    if re.search(r"\bhours?\b", lower) and ("branch" in lower or qualifiers["service"]):
        return RequestSpec("hours_compare" if re.search(r"\b(?:all|same|vary|different)\b", lower)
                           else "hours", "branch", qualifiers)
    program = re.search(r"\bwhat is (?:\w+(?:'s)?\s+)?(.+?)\s+program\b", question, re.I)
    if program:
        qualifiers["program_name"] = program.group(1).strip().lower()
        return RequestSpec("definition", "program", qualifiers)
    return RequestSpec("legacy", "", qualifiers)


def _membership_eligibility_answer(index, question):
    """Assemble an explicit eligibility section from one selected-snapshot page."""
    lower = question.lower()
    if (not re.search(r"\b(?:eligible|eligibility|qualify|who can join)\b", lower)
            or not re.search(r"\b(?:member|membership|join)\b", lower)):
        return None
    seen = set()
    for _, passage in index.search(question, limit=80):
        if passage.url in seen:
            continue
        seen.add(passage.url)
        page = index.pages.get(passage.url)
        if page is None:
            continue
        lines = page.markdown.splitlines()
        for heading, line in enumerate(lines):
            if not re.match(r"^#{1,6}\s+.*\b(?:eligible to join|who can join)\b", line, re.I):
                continue
            bullets = []
            for item in lines[heading + 1:heading + 16]:
                if item.startswith("#"):
                    break
                if re.match(r"^\s*[-*]\s+", item):
                    bullets.append(item.strip())
                elif bullets and item.strip():
                    break
            if not bullets or not any(re.search(r"\blive or work\b.*\bcharter area\b", item, re.I)
                                      for item in bullets):
                continue
            clean = lambda item: re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", item).lstrip("-* ").rstrip(". ")
            text = "You may be eligible to join if " + "; or ".join(clean(item).removeprefix("You ")
                                                             for item in bullets) + "."
            sources = [Source(page.url, index.snapshot_id, item) for item in bullets]
            for item in lines[max(0, heading - 24):heading]:
                match = re.search(r"\bYou can find us in ([^.\n]{8,100})\.", item, re.I)
                if match:
                    text += " The same page describes local service in " + match.group(1).strip() + "."
                    sources.append(Source(page.url, index.snapshot_id, item.strip()))
                    break
            return Answer(text, sources)
    return None


def retrieve_candidates(index, question, spec):
    """Retrieve only from the already selected full snapshot."""
    limit = 160 if spec.intent != "legacy" else 12
    return [(rank, score, passage) for rank, (score, passage) in
            enumerate(index.search(question, limit=limit), 1)]


def _field(text, label):
    match = re.search(r"(?:^|; )" + re.escape(label) + r":\s*([^;]+)", text, re.I)
    return match.group(1).strip() if match else ""


def _rate(text, label="Rate"):
    match = re.search(r"(?:^|; )" + re.escape(label) + r"(?:\s*\([^)]*\))?:\s*(\d+(?:\.\d+)?%)", text, re.I)
    return match.group(1) if match else ""


def _card_rate(text):
    labelled = _rate(text, "APR")
    if labelled:
        return labelled
    spans = re.findall(r"\d+(?:\.\d+)?%\s*[–-]\s*\d+(?:\.\d+)?%\s*APR|\d+(?:\.\d+)?%\s*APR", text, re.I)
    return re.sub(r"\s*APR$", "", spans[0], flags=re.I).strip() if spans else ""


def _assess_mortgage(spec, rank, retrieval_score, passage):
    if passage.kind != "table":
        return None
    q, text = spec.qualifiers, passage.text
    program = _field(text, "Program") or _field(text, "Term")
    product_context = (passage.title + " " + passage.context + " " + passage.url).lower()
    if not re.search(r"mortgage|home[- ]loans?|home rates", product_context):
        return None
    if q["scope"] and q["scope"] not in product_context:
        return None
    kind = "special" if re.search(r"\b(relief|special|promotion|intro|qualified)\b", program, re.I) else "standard"
    if qualifier_state(q.get("program"), kind) == "incompatible":
        return None
    term = q["term"]
    if term and not re.search(r"(?<!\d)" + re.escape(term).replace(r"\ ", r"[- ]") + r"\b", program.lower()):
        return None
    if not term and not re.search(r"\b\d{1,3}[- ]year\b", program, re.I):
        return None
    transaction = "refinance" if "refinance" in program.lower() else "purchase" if "purchase" in program.lower() else ""
    if qualifier_state(q["transaction"], transaction) == "incompatible":
        return None
    rate = _rate(text)
    if not rate:
        return None
    term_found = re.search(r"\b\d{1,3}[- ]year\b|\b\d{1,2}/\d{1,2}\b", program, re.I)
    tier = re.search(r"\$[\d,]+\s+or\s+less", program, re.I)
    if q["tier_amount"] is not None and (not tier or
            float(re.search(r"\d[\d,]*", tier.group(0)).group(0).replace(",", "")) < q["tier_amount"]):
        return None
    key = ("mortgage", term_found.group(0).lower() if term_found else term,
           tier.group(0).replace(",", "") if tier else "", transaction)
    return Evidence(passage, rank, retrieval_score + 8, key, rate, f"{program.strip()}; Rate: {rate}")


def _assess_card(spec, rank, retrieval_score, passage):
    text = passage.text
    name_match = re.search(r"\b(?:Business\s+)?[A-Z][A-Za-z-]+\s+Visa\b", text + " " + passage.context)
    rate = _card_rate(text)
    if not name_match or not rate:
        return None
    name = " ".join(name_match.group(0).split())
    scope = "business" if name.lower().startswith("business") else "personal"
    if qualifier_state(spec.qualifiers["scope"], scope) == "incompatible":
        return None
    return Evidence(passage, rank, retrieval_score + (8 if passage.kind == "table" else 4),
                    ("card", name.lower()), rate, f"{name}: {rate} APR")


def _assess_certificate(spec, rank, retrieval_score, passage):
    text = passage.text
    if passage.kind != "table" or not text.startswith("Certificate:"):
        return None
    scope = "business" if "/business/" in passage.url else "personal" if "/personal/" in passage.url else ""
    if spec.qualifiers["scope"] and qualifier_state(spec.qualifiers["scope"], scope) == "incompatible":
        return None
    certificate = _field(text, "Certificate")
    apy = re.search(r"Annual Percentage Yield \(APY\):\s*(\d+(?:\.\d+)?%)", text, re.I)
    if not certificate or not apy:
        return None
    return Evidence(passage, rank, retrieval_score + 5,
                    ("certificate", scope, certificate.lower()), apy.group(1),
                    f"{certificate}: {apy.group(1)} APY" + (f" ({scope})" if scope else ""))


def _assess_branch(spec, rank, retrieval_score, passage):
    text = passage.text
    if not TIME.search(text):
        return None
    q = spec.qualifiers
    combined = (text + " " + passage.context + " " + passage.title).lower()
    service = "drive-up" if "drive-up" in combined or "drive up" in combined else "lobby" if "lobby" in combined else None
    if qualifier_state(q["service"], service) == "incompatible":
        return None
    if q["branch"] and q["branch"] not in combined:
        return None
    day = next((d for d in ("monday", "tuesday", "wednesday", "thursday", "friday",
                            "saturday", "sunday") if d in text.lower()), "")
    if q["day"] and q["day"] not in text.lower():
        return None
    return Evidence(passage, rank, retrieval_score + 4,
                    ("hours", passage.url, service, day), text, text)


def _assess_program(spec, rank, retrieval_score, passage):
    name, text = spec.qualifiers["program_name"], passage.text
    combined = (text + " " + passage.context + " " + passage.title).lower()
    if name not in combined or not re.search(r"\b(provides?|offers?|assistance|grants?|credit toward|benefit)\b", text, re.I):
        return None
    if re.search(r"\b(?:for more information|to apply|keep reading)\b", text, re.I) and not re.search(r"\$\s*\d", text):
        return None
    amount = re.search(r"\$\s*\d[\d,]*(?:\.\d+)?", text)
    return Evidence(passage, rank, retrieval_score + (10 if amount else 2),
                    ("program", name), amount.group(0) if amount else "", text[:500])


def assess_evidence(spec, rank, retrieval_score, passage):
    """Check qualifiers and sufficiency independently of retrieval rank."""
    assessors = {"mortgage": _assess_mortgage, "card": _assess_card,
                 "certificate": _assess_certificate, "branch": _assess_branch,
                 "program": _assess_program}
    return assessors[spec.product](spec, rank, retrieval_score, passage)


def select_evidence(spec, candidates):
    """Keep incompatible facts out and retain unresolved equal-key conflicts."""
    ranked = sorted(candidates, key=lambda e: (-e.score, e.rank, e.passage.url))
    by_key = defaultdict(list)
    for evidence in ranked:
        by_key[evidence.key].append(evidence)
    def compatible(group):
        values = {item.value for item in group if item.value}
        if len(values) == 1:
            return True
        if not values:
            return True
        if group[0].key[0] != "card":
            numbers = [re.findall(r"\d[\d,]*(?:\.\d+)?", value) for value in values]
            return all(len(n) == 1 for n in numbers) and len({float(n[0].replace(",", "")) for n in numbers}) == 1
        ranges = [tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", value)) for value in values]
        full = [r for r in ranges if len(r) == 2]
        singles = [r[0] for r in ranges if len(r) == 1]
        return bool(full) and len(set(full)) == 1 and all(full[0][0] <= n <= full[0][1] for n in singles)

    conflicts = tuple(tuple(group) for group in by_key.values() if not compatible(group))
    safe = [next((item for item in group if "–" in item.value or "-" in item.value), group[0])
            for group in by_key.values() if compatible(group)]
    if spec.product == "mortgage" and spec.qualifiers["transaction"] is None:
        purchase = [e for e in safe if e.key[-1] == "purchase"]
        if purchase:
            safe = purchase
    if spec.product == "mortgage" and spec.qualifiers["tier_amount"] is not None:
        caps = [float(re.search(r"\d+", e.key[2]).group(0)) for e in safe if e.key[2]]
        if caps:
            smallest = min(caps)
            safe = [e for e in safe if e.key[2] and float(re.search(r"\d+", e.key[2]).group(0)) == smallest]
    if spec.product == "certificate":
        unique = []
        seen = set()
        for evidence in safe:
            identity = (evidence.key[-1], evidence.value)
            if identity not in seen:
                unique.append(evidence)
                seen.add(identity)
        safe = unique
    safe.sort(key=lambda e: (-e.score, e.rank, e.passage.url))
    if spec.intent == "definition":
        safe = safe[:1]
    if spec.intent == "hours":
        if safe:
            first_url = safe[0].passage.url
            safe = [e for e in safe if e.passage.url == first_url][:5]
    elif spec.intent == "hours_compare":
        urls = []
        for evidence in safe:
            if evidence.passage.url not in urls:
                urls.append(evidence.passage.url)
            if len(urls) >= 1:
                break
        first_saturday = {e.value for e in safe if e.passage.url == urls[0]
                          and "saturday" in e.text.lower()} if urls else set()
        alternatives = [e for e in safe if e.passage.url not in urls and
                        "saturday" in e.text.lower() and e.value not in first_saturday]
        if alternatives:
            urls.append(alternatives[0].passage.url)
        else:
            second = next((e.passage.url for e in safe if e.passage.url not in urls), None)
            if second:
                urls.append(second)
        safe = [e for e in safe if e.passage.url in urls][:10]
    elif spec.intent == "mortgage_overview":
        safe = safe[:12]
    elif spec.intent == "mortgage_rate":
        safe = safe[:8]
    elif spec.intent == "list":
        safe = safe[:24]
    return Selection(tuple(safe), conflicts)


def compose_answer(spec, selection):
    if selection.conflicts:
        return Answer("I don't know; please contact us to confirm the conflicting values.", confident=False)
    if not selection.evidence:
        return Answer("I don't know; please contact us for details.", confident=False)
    if spec.intent == "hours_compare" and len({e.passage.url for e in selection.evidence}) < 2:
        return Answer("I don't know; please contact us for branch-specific hours.", confident=False)
    chosen = selection.evidence
    if spec.intent == "definition":
        chosen = chosen[:1]
    if spec.intent in ("hours", "hours_compare"):
        groups = defaultdict(list)
        for evidence in chosen:
            groups[evidence.passage.url].append(evidence)
        schedules = []
        for group in groups.values():
            label = (spec.qualifiers["branch"].title() + " Branch" if spec.qualifiers["branch"]
                     else group[0].passage.title)
            schedules.append(f"{label}: " + "; ".join(e.text for e in group))
        text = " | ".join(schedules)
        if not spec.qualifiers["branch"]:
            text = "Published branch schedules vary; confirm the branch you plan to visit. " + text
    else:
        text = "; ".join(e.text for e in chosen)
    sources = []
    for evidence in chosen:
        p = evidence.passage
        if p.url not in {s.url for s in sources}:
            pos = p.text.find(evidence.value) if evidence.value else -1
            quote = p.text[max(0, pos - 100):pos + len(evidence.value) + 100] if pos >= 0 else p.text[:220]
            sources.append(Source(p.url, p.snapshot_id, quote))
    return Answer(text, sources)


def _promotion_conditions(record, index):
    """Render only mortgage conditions verified in the selected source notes."""
    if not record.supporting_claims or any(
            c.identity.family != "mortgage" or c.identity.measure != "interest_rate"
            for c in record.supporting_claims):
        return None
    notes = " ".join(record.source_notes)
    if not ("rates may be as low as quoted rates" in notes.lower()
            and "all loans are subject to approval" in notes.lower()
            and "subject to change" in notes.lower()
            and re.search(r"owner[- ]occupied", notes, re.I)):
        return None
    score = re.search(r"minimum credit score of\s+(\d{3})", notes, re.I)
    ltv = re.search(r"(\d{1,3})\s*%\s*loan[- ]to[- ]value", notes, re.I)
    lock = re.search(r"(\d{1,3})[- ]day rate lock and expire at ([^.]+?) daily", notes, re.I)
    if not all((score, ltv, lock)):
        return None
    points = set()
    for claim in record.supporting_claims:
        page = index.pages.get(claim.url)
        if page is None:
            return None
        lines = page.markdown.splitlines()
        pos = claim.line - 1
        if not 0 <= pos < len(lines) or lines[pos].strip() != claim.evidence:
            return None
        start = pos
        while start and lines[start - 1].strip().startswith("|"):
            start -= 1
        headers = [cell.strip().lower() for cell in lines[start].strip().strip("|").split("|")]
        cells = [cell.strip() for cell in lines[pos].strip().strip("|").split("|")]
        if len(headers) != len(cells) or headers.count("points") != 1:
            return None
        raw = cells[headers.index("points")]
        if not re.fullmatch(r"\d+(?:\.\d+)?%?", raw):
            return None
        try:
            points.add(Decimal(raw.rstrip("%")))
        except InvalidOperation:
            return None
    if len(points) != 1:
        return None
    point = next(iter(points))
    return (f"Quoted rates have {point:g} points and assume an owner-occupied residence, "
            f"a minimum credit score of {score.group(1)}, {ltv.group(1)}% loan-to-value, "
            f"and a {lock.group(1)}-day rate lock expiring at {lock.group(2)} daily. "
            "Loans are subject to approval; rates and terms can change.")


def _verified_promoted_answer(record, existing, index):
    """Build and check the final Answer after the shadow validator approves it."""
    if (not record.eligible or record.classification != "IMPROVEMENT"
            or record.snapshot_id != index.snapshot_id or not record.proposed_citations
            or len(record.proposed_citations) != len(record.supporting_claims)):
        return None, "promotion_gate_closed"
    if classify(record, existing, index)[0] != "IMPROVEMENT":
        return None, "final_evidence_check_failed"
    conditions = _promotion_conditions(record, index)
    if conditions is None or not record.proposed_answer or " Conditions: " not in record.proposed_answer:
        return None, "unsupported_rendered_conditions"
    core = record.proposed_answer.split(" Conditions: ", 1)[0]
    sources = []
    for claim, (url, quote) in zip(record.supporting_claims, record.proposed_citations):
        page = index.pages.get(url)
        if (page is None or url != claim.url or quote != claim.evidence
                or quote not in page.markdown or claim.snapshot_id != index.snapshot_id):
            return None, "unverified_promotion_citation"
        sources.append(Source(url, index.snapshot_id, quote))
    rendered = core + " " + conditions
    candidate = Answer(rendered, sources, confident=True)
    # The verified core contains every requested fact and row association.
    # The suffix is assembled only from checked row/footnote fields above.
    if (candidate.text != core + " " + _promotion_conditions(record, index)
            or candidate.text.split(" Quoted rates have ", 1)[0] != core
            or tuple((s.url, s.snapshot_id, s.quote) for s in candidate.sources) !=
            tuple((url, index.snapshot_id, quote) for url, quote in record.proposed_citations)):
        return None, "rendered_answer_mismatch"
    return candidate, "verified_factual_improvement"


class LexicalKnowledge:
    def __init__(self):
        self.snapshots = []
        self.unresolved_evidence = {}
        self.shadow_diagnostics = deque(maxlen=64)
        self.shadow_errors = deque(maxlen=64)
        self.promotion_diagnostics = deque(maxlen=64)
        self.conflict_shadow_diagnostics = deque(maxlen=2)
        self.conflict_shadow_errors = deque(maxlen=2)
        self.public_conflict_shadow_diagnostics = deque(maxlen=2)
        self._shadow_resolver = None

    def ingest(self, snapshot):
        self.snapshots.append(SnapshotIndex(snapshot))
        self.snapshots.sort(key=lambda s: s.taken_on)
        self._shadow_resolver = None

    def _select(self, as_of):
        eligible = [s for s in self.snapshots if as_of is None or s.taken_on <= as_of]
        return eligible[-1] if eligible else None

    def answer(self, question, as_of=None):
        index = self._select(as_of)
        answer = self._answer_existing(question, as_of)
        if index is not None:
            try:
                if self._shadow_resolver is None:
                    self._shadow_resolver = TemporalResolver([s.fact_index for s in self.snapshots])
                record = propose(question, parse_request(question), index,
                                 self._shadow_resolver, answer, as_of)
                if record.eligible:
                    status, reason = classify(record, answer, index)
                    record = replace(record, classification=status, classification_reason=reason)
                self.shadow_diagnostics.append(record)
                if record.classification == "IMPROVEMENT":
                    promoted, reason = _verified_promoted_answer(record, answer, index)
                    if promoted is not None:
                        self.promotion_diagnostics.append((index.snapshot_id, question, "PROMOTED", reason))
                        return promoted
                else:
                    reason = record.classification_reason or record.reason
                self.promotion_diagnostics.append((index.snapshot_id, question, "FALLBACK", reason))
            except Exception as exc:
                self.shadow_errors.append((index.snapshot_id, type(exc).__name__, str(exc)[:160]))
                self.promotion_diagnostics.append((index.snapshot_id, question, "FALLBACK", "validation_error"))
        return answer

    def _answer_existing(self, question, as_of=None):
        index = self._select(as_of)
        if index is None:
            return Answer("I don't know; please contact us for details.", confident=False)
        eligibility = _membership_eligibility_answer(index, question)
        if eligibility is not None:
            return eligibility
        spec = parse_request(question)
        if spec.intent != "legacy":
            retrieved = retrieve_candidates(index, question, spec)
            evidence = [assess_evidence(spec, rank, score, passage)
                        for rank, score, passage in retrieved]
            selection = select_evidence(spec, [item for item in evidence if item is not None])
            self.unresolved_evidence[(index.snapshot_id, question)] = selection.conflicts
            return compose_answer(spec, selection)
        hits = index.search(question)
        if not hits:
            return Answer("I don't know; please contact us for details.", confident=False)
        best = hits[0][1]
        wants_link = bool(re.search(r"\b(?:url|link|website)\b|\bonline\b.*\b(?:join|apply|open)\b|\b(?:join|apply|open)\b.*\bonline\b",
                                    question.lower()))
        link_match = wants_link and any(
            set(useful(question)) & set(useful(anchor)) and target.startswith(("https://", "http://"))
            for anchor, target in best.links)
        match_terms = set(useful(question)) & set(useful(
            best.text + " " + best.context + " " + best.title + " " +
            urlparse(best.url).path.replace("/", " ")))
        min_score = min(3.0, .3 * math.log(len(index.passages) + 1))
        if hits[0][0] < min_score or (len(set(useful(question))) >= 2 and len(match_terms) < 2 and not link_match):
            return Answer("I don't know; please contact us for details.", confident=False)
        best_score, best = hits[0]
        # Context establishes relevance, while the passage must still contain
        # the requested kind of evidence before it can be used as an answer.
        branch_name = re.search(r"\b([A-Z][A-Za-z-]+(?:\s+[A-Z][A-Za-z-]+){0,2})\s+[Bb]ranch\b", question)
        if branch_name and branch_name.group(1).lower() not in (
                best.text + " " + best.context + " " + best.title).lower():
            return Answer("I don't know; please contact us for that branch.", confident=False)
        if re.search(r"\baddress\b", question.lower()) and not ADDRESS.search(best.text):
            return Answer("I don't know; please contact us for the address.", confident=False)
        procedure = re.search(r"\bhow (?:can|do) i ([a-z]+)\b", question.lower())
        if procedure and procedure.group(1) not in words(best.text) and not link_match:
            return Answer("I don't know; please contact us for details.", confident=False)
        requested_terms = re.findall(r"\b\d{1,3}\s*[- ]\s*(?:month|year)\b", question.lower())
        normalized_best = re.sub(r"\s*[- ]\s*", " ", best.text.lower())
        if requested_terms and not all(re.sub(r"\s*[- ]\s*", " ", t) in normalized_best for t in requested_terms):
            return Answer("I don't know; please contact us for that term.", confident=False)
        if re.search(r"\b(?:rate|apr|apy|interest)\b", question.lower()) and not re.search(r"\d[\d,.]*\s*%", best.text):
            return Answer("I don't know; please contact us for the current rate.", confident=False)
        if re.search(r"\bhours?\b", question.lower()) and not TIME.search(best.text):
            return Answer("I don't know; please contact us for the branch hours.", confident=False)
        # A broad question can benefit from several distinct rows or pages.
        broad = bool(re.search(r"\b(?:rates|cards|options|hours|branches)\b", question.lower())) and not re.search(r"\b\d+(?:-month|-year)?\b", question.lower())
        selected = [best]
        if broad:
            for score, p in hits[1:]:
                if len(selected) >= 4 or score < best_score * .73:
                    break
                if p.text not in {x.text for x in selected} and (p.url == best.url or "cards" in question.lower()):
                    selected.append(p)
        body = " ".join(p.text for p in selected)
        # Long generic passages tend to bring unrelated tiers into the answer.
        body = body[:900].rsplit(" ", 1)[0] if len(body) > 900 else body
        sources = []
        for p in selected:
            if p.url not in {s.url for s in sources}:
                sources.append(Source(p.url, p.snapshot_id, p.text[:220]))
        if wants_link and sources:
            qterms = set(useful(question))
            candidates = [(len(qterms & set(useful(anchor))), target) for anchor, target in best.links
                          if target.startswith(("https://", "http://"))]
            candidates.sort(key=lambda x: -x[0])
            if candidates and candidates[0][0]:
                body = candidates[0][1]
                sources[0] = Source(best.url, best.snapshot_id, body)
            else:
                body = sources[0].url
        return Answer(body, sources)

    def conflicts(self):
        try:
            try:
                from .conflict_adapter import analyze, public_conflicts
            except ImportError:
                from conflict_adapter import analyze, public_conflicts
            resolver = TemporalResolver([s.fact_index for s in self.snapshots])
            report = analyze(self.snapshots, resolver)
            self.conflict_shadow_diagnostics.append(report)
            validated = public_conflicts(report, self.snapshots)
            self.public_conflict_shadow_diagnostics.append(validated)
            if validated.error or validated.rejected:
                self.conflict_shadow_errors.append(validated.error or
                                                   f"validation_rejected:{len(validated.rejected)}")
                return []
            return list(validated.conflicts)
        except Exception as exc:
            self.conflict_shadow_errors.append(f"{type(exc).__name__}: {exc}")
            return []


def build():
    return LexicalKnowledge()

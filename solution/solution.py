"""Local, snapshot-aware lexical knowledge system.

Only the supplied Snapshot objects are used.  Each full crawl gets its own
inverted index, so a historical query never sees a later page.
"""
from __future__ import annotations

import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(next(d / "harness" for d in Path(__file__).resolve().parents
                             if (d / "harness" / "contract.py").exists())))
from contract import Answer, Conflict, Source  # noqa: E402

TOKEN = re.compile(r"[a-z]+(?:'[a-z]+)?|\d+(?:[.,]\d+)*", re.I)
NUMBER = re.compile(r"(?:\$\s*)?\d[\d,]*(?:\.\d+)?\s*%?")
STOP = set("a an and are as at be by can do does for from have how i in is it me my of on or our please tell the to us was what when where which who with would you your dupaco rate rates interest apr apy current today".split())


def words(s):
    return [w.lower() for w in TOKEN.findall(s)]


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


class Passage(NamedTuple):
    text: str
    url: str
    title: str
    snapshot_id: str
    heading: str
    kind: str
    row_key: str = ""


def passages(page, sid):
    """Yield text blocks; table cells stay attached to their column labels."""
    lines = page.markdown.splitlines()
    heading = ""
    paragraph = []
    headers = None

    def flush():
        if paragraph:
            body = clean(" ".join(paragraph))
            paragraph.clear()
            if len(body) >= 18 and not body.startswith("http"):
                return Passage(body[:1100], page.url, page.title, sid, heading, "text")
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
                yield Passage(row, page.url, page.title, sid, heading, "table", cells[0])
            continue
        headers = None
        if line.startswith("#"):
            p = flush()
            if p:
                yield p
            heading = clean(line.lstrip("# "))
            continue
        if not line or line == "* * *":
            p = flush()
            if p:
                yield p
            continue
        if line.startswith("![") or line.startswith("[") and len(line) > 250:
            continue
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
            for p in passages(page, snap.snapshot_id):
                i = len(self.passages)
                self.passages.append(p)
                tf = Counter(useful(p.text))
                self.lengths.append(sum(tf.values()))
                for token, count in tf.items():
                    self.postings[token].append((i, count))
                    self.df[token] += 1
        self.avg_length = sum(self.lengths) / max(1, len(self.lengths))

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


class LexicalKnowledge:
    def __init__(self):
        self.snapshots = []

    def ingest(self, snapshot):
        self.snapshots.append(SnapshotIndex(snapshot))
        self.snapshots.sort(key=lambda s: s.taken_on)

    def _select(self, as_of):
        eligible = [s for s in self.snapshots if as_of is None or s.taken_on <= as_of]
        return eligible[-1] if eligible else None

    def answer(self, question, as_of=None):
        index = self._select(as_of)
        if index is None:
            return Answer("I don't know; please contact us for details.", confident=False)
        hits = index.search(question)
        if not hits:
            return Answer("I don't know; please contact us for details.", confident=False)
        match_terms = set(useful(question)) & set(useful(hits[0][1].text + " " + hits[0][1].title))
        min_score = min(3.0, .3 * math.log(len(index.passages) + 1))
        if hits[0][0] < min_score or (len(set(useful(question))) >= 2 and len(match_terms) < 2):
            return Answer("I don't know; please contact us for details.", confident=False)
        best_score, best = hits[0]
        requested_terms = re.findall(r"\b\d{1,3}\s*[- ]\s*(?:month|year)\b", question.lower())
        normalized_best = re.sub(r"\s*[- ]\s*", " ", best.text.lower())
        if requested_terms and not all(re.sub(r"\s*[- ]\s*", " ", t) in normalized_best for t in requested_terms):
            return Answer("I don't know; please contact us for that term.", confident=False)
        if re.search(r"\b(?:rate|apr|apy|interest)\b", question.lower()) and not re.search(r"\d[\d,.]*\s*%", best.text):
            return Answer("I don't know; please contact us for the current rate.", confident=False)
        if re.search(r"\bhours?\b", question.lower()) and not re.search(r"\b\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?)\b", best.text, re.I):
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
        if re.search(r"\b(?:url|link|website)\b", question.lower()) and sources:
            body = sources[0].url
        return Answer(body, sources)

    def conflicts(self):
        # Compare the same labelled fact or table row on the same URL.
        observed = defaultdict(dict)
        for index in self.snapshots:
            for p in index.passages:
                if p.kind == "table" and p.row_key:
                    key = (p.url, p.heading.lower(), p.row_key.lower())
                elif p.kind == "text" and ":" in p.text and re.search(r"\d+(?:\.\d+)?%", p.text):
                    key = (p.url, p.heading.lower(), p.text.split(":", 1)[0].lower())
                else:
                    continue
                observed[key][index.snapshot_id] = p
        out = []
        for (url, heading, row), by_snap in observed.items():
            variants = defaultdict(list)
            for sid, p in by_snap.items():
                values = tuple(re.findall(r"\d+(?:\.\d+)?%", p.text))
                if not values and "fee" in heading:
                    values = tuple(re.findall(r"\$\s*\d+(?:,\d{3})*(?:\.\d+)?", p.text))
                if values:
                    variants[values].append(p)
            if len(variants) < 2:
                continue
            latest = next((by_snap[s.snapshot_id] for s in reversed(self.snapshots) if s.snapshot_id in by_snap), None)
            if latest is None:
                continue
            values = [", ".join(v) for v in variants]
            sources = [Source(ps[-1].url, ps[-1].snapshot_id, ps[-1].text[:220]) for ps in variants.values()]
            winner = ", ".join(re.findall(r"\d+(?:\.\d+)?%", latest.text))
            out.append(Conflict(f"{heading}: {row}", values, sources, winner,
                                "Most recent full website snapshot"))
        return out


def build():
    return LexicalKnowledge()

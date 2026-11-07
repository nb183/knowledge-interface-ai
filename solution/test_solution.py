import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from contract import Answer, Page, Snapshot
from solution.solution import build, passages


def page(url, text, title="Rates"):
    return Page(url, title, text, "2026-01-01T00:00:00Z")


def test_historical_full_snapshot_selection():
    ks = build()
    ks.ingest(Snapshot("old", "2026-01-01", [page("https://example.test/rates", "# Certificates\n\n| Term | APY |\n| --- | --- |\n| 10 month jumbo | 3.90% |")]))
    ks.ingest(Snapshot("new", "2026-03-01", [page("https://example.test/rates", "# Certificates\n\n| Term | APY |\n| --- | --- |\n| 10 month jumbo | 4.00% |")]))
    old = ks.answer("10 month jumbo certificate APY", as_of="2026-02-01")
    new = ks.answer("10 month jumbo certificate APY")
    assert isinstance(old, Answer)
    assert "3.90%" in old.text and "4.00%" not in old.text
    assert "4.00%" in new.text and "3.90%" not in new.text
    assert old.sources[0].snapshot_id == "old"
    assert new.sources[0].snapshot_id == "new"


def test_table_rows_keep_column_headers():
    p = page("https://example.test/table", "# Savings\n\n| Account | Balance Tier | Dividend Rate | APY |\n| --- | --- | --- | --- |\n| Share Savings | $10,000-$24,999 | 0.15% | 0.15% |")
    rows = [x for x in passages(p, "one") if x.kind == "table"]
    assert len(rows) == 1
    assert "Balance Tier: $10,000-$24,999" in rows[0].text
    assert "Dividend Rate: 0.15%" in rows[0].text
    assert "APY: 0.15%" in rows[0].text


def test_provenance_and_repeat_determinism():
    ks = build()
    ks.ingest(Snapshot("crawl", "2026-01-01", [page("https://example.test/fees", "# Fees\n\nLost debit card replacement fee is $5.", "Fees")]))
    first = ks.answer("lost debit card replacement fee")
    second = ks.answer("lost debit card replacement fee")
    assert first == second
    assert first.sources[0].url == "https://example.test/fees"
    assert first.sources[0].snapshot_id == "crawl"
    assert "Lost debit card" in first.sources[0].quote


def test_unknown_abstains_and_pre_snapshot_date_abstains():
    ks = build()
    ks.ingest(Snapshot("crawl", "2026-01-01", [page("https://example.test/fees", "# Fees\n\nLost debit card replacement fee is $5.")]))
    assert not ks.answer("What is the rate for a submarine loan?").confident
    assert not ks.answer("lost debit card replacement fee", as_of="2025-12-31").confident

"""
Uji sim/sectors.get_stock_price — jalankan: python tests/test_sectors_price.py
Mode offline (tanpa kredit Sectors), API dipalsukan untuk menguji cache memori + disk + TTL,
source_kind, TTL berita (F19) dan jalur IHSG tanpa cache harga 6 jam (F20).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SIMPASAR_LLM"] = "off"

from sim import sectors  # noqa: E402

EXPECTED_KEYS = {"symbol", "name", "sector", "sector_name", "price", "date", "prev_close", "change",
                 "change_pct", "open", "high", "low", "volume", "market_cap", "history", "source", "status"}

FAKE_ROWS = [
    {"symbol": "BBCA", "date": "2026-09-05", "close": 6700, "open": 6650, "high": 6750, "low": 6600,
     "volume": 1000, "market_cap": 1.0e14},
    {"symbol": "BBCA", "date": "2026-09-07", "close": "6625", "open": 6750, "high": 6750, "low": 6600,
     "volume": 65118000, "market_cap": 817683406650000},
    {"symbol": "BBCA", "date": "2026-09-04", "close": 6400},
]


def run(coro):
    return asyncio.run(coro)


def fresh_cache_dir() -> None:
    sectors.CACHE_DIR = Path(tempfile.mkdtemp(prefix="simpasar-cache-")) / "sectors"
    sectors._price_cache.clear()
    sectors._last_price_status.clear()
    reset_ihsg_and_news()


def reset_ihsg_and_news() -> None:
    sectors._ihsg_cache = {}
    sectors._ihsg_fetched_at = 0.0
    sectors._ihsg_failed_at = 0.0
    sectors._ihsg_last_kind = "fallback"
    sectors._news_cache = []
    sectors._news_fetched_at = 0.0
    sectors._news_failed_at = 0.0


IHSG_ROWS = [
    {"index_code": "ihsg", "date": "2026-09-21", "price": 7000.0},
    {"index_code": "ihsg", "date": "2026-09-22", "price": 7070.0},
]


def test_offline_and_unknown() -> None:
    fresh_cache_dir()
    r = run(sectors.get_stock_price("BBCA"))
    assert EXPECTED_KEYS <= set(r), set(r)
    assert r["status"] == "offline" and r["price"] is None and r["history"] == [], r
    assert r["message"] == "Harga Sectors tidak tersedia (mode offline / kuota habis)"
    assert r["name"] == "PT Bank Central Asia Tbk." and r["sector_name"] == "Keuangan"
    assert all(r[k] is None for k in ("prev_close", "change", "change_pct", "open", "high", "low", "volume", "market_cap"))

    r = run(sectors.get_stock_price("ZZZZ"))
    assert r["status"] == "unknown" and r["price"] is None
    assert run(sectors.get_stock_price(""))["status"] == "unknown"
    assert run(sectors.get_stock_price(None))["status"] == "unknown"  # type: ignore[arg-type]

    # IHSG offline → baseline fallback (price ada, status "fallback"), bukan exception.
    r = run(sectors.get_stock_price("IHSG"))
    assert r["kind"] == "index" and r["price"] == 6850.0 and r["status"] == "fallback", r
    assert r["prev_close"] == 6820.0 and r["change"] == 30.0 and r["change_pct"] == 0.44
    assert r["history"] and r["history"][-1]["close"] == 6850.0
    assert r["source_kind"] == "fallback" and "fetched_at" not in r and r.get("message"), r
    assert run(sectors.get_stock_price("BBCA"))["source_kind"] is None
    assert not list(sectors.CACHE_DIR.glob("*.json")) if sectors.CACHE_DIR.exists() else True  # fallback tak di-cache


def test_success_cache_disk_and_stale() -> None:
    fresh_cache_dir()
    calls: list[tuple[str, dict]] = []
    original = sectors.call_sectors_mcp

    async def fake_call(tool: str, args: dict):
        calls.append((tool, args))
        return FAKE_ROWS

    sectors.call_sectors_mcp = fake_call
    try:
        r = run(sectors.get_stock_price("bbca.jk"))
        assert calls == [("fetch-daily-price", {"symbol": "BBCA"})], calls
        assert r["status"] == "success" and r["price"] == 6625.0 and r["date"] == "2026-09-07", r
        assert r["prev_close"] == 6700.0 and r["change"] == -75.0 and r["change_pct"] == -1.12
        assert [h["date"] for h in r["history"]] == ["2026-09-04", "2026-09-05", "2026-09-07"]
        assert r["market_cap"] == 817683406650000 and r["volume"] == 65118000
        assert r["source"] == "Sectors Financial API (fetch-daily-price)"
        assert r["source_kind"] == "sectors" and isinstance(r["fetched_at"], float)
        assert (sectors.CACHE_DIR / "price_BBCA.json").is_file()
        # peek tepat setelah pengambilan API: asal "sectors" (klien baru saja memakai 1 kredit).
        assert sectors.peek_cached_price("BBCA")["source_kind"] == "sectors"

        # Panggilan kedua: dari cache memori, API tidak dipanggil lagi.
        r2 = run(sectors.get_stock_price("BBCA"))
        assert r2["status"] == "cache" and r2["price"] == 6625.0 and len(calls) == 1
        assert r2["source_kind"] == "cache" and r2["fetched_at"] == r["fetched_at"] and "stale" not in r2
        assert sectors.peek_cached_price("BBCA")["source_kind"] == "cache"

        # Cache memori dihapus → dari disk, API tetap tidak dipanggil.
        sectors._price_cache.clear()
        r3 = run(sectors.get_stock_price("BBCA"))
        assert r3["status"] == "cache" and r3["price"] == 6625.0 and len(calls) == 1

        # peek tidak memanggil API.
        peek = sectors.peek_cached_price("BBCA")
        assert peek and peek["price"] == 6625.0 and peek["date"] == "2026-09-07" and len(calls) == 1

        # TTL habis → API dipanggil ulang.
        sectors._price_cache["BBCA"]["fetched_at"] -= sectors.PRICE_CACHE_TTL + 60
        sectors._write_disk_cache("BBCA", sectors._price_cache["BBCA"])
        r4 = run(sectors.get_stock_price("BBCA"))
        assert r4["status"] == "success" and len(calls) == 2

        # TTL habis dan API gagal → cache lama tetap dipakai (stale), bukan offline.
        sectors._price_cache["BBCA"]["fetched_at"] -= sectors.PRICE_CACHE_TTL + 60
        sectors._write_disk_cache("BBCA", sectors._price_cache["BBCA"])
        sectors.call_sectors_mcp = original
        r5 = run(sectors.get_stock_price("BBCA"))
        assert r5["status"] == "cache" and r5.get("stale") is True and r5["price"] == 6625.0, r5
        assert r5["source_kind"] == "stale" and r5["message"] and isinstance(r5["fetched_at"], float)
        peek = sectors.peek_cached_price("BBCA")
        assert peek["source_kind"] == "stale" and peek["stale"] is True
    finally:
        sectors.call_sectors_mcp = original


def test_weird_api_payloads() -> None:
    fresh_cache_dir()
    original = sectors.call_sectors_mcp
    try:
        for payload in (None, "error text", [], {}, {"results": []}, [{"date": "2026-01-01"}], 42, [1, 2]):
            async def fake_call(tool: str, args: dict, _p=payload):
                return _p
            sectors.call_sectors_mcp = fake_call
            sectors._price_cache.clear()
            r = run(sectors.get_stock_price("TLKM"))
            assert r["status"] == "offline" and r["price"] is None, (payload, r["status"])

        async def wrapped(tool: str, args: dict):
            return {"results": FAKE_ROWS}
        sectors.call_sectors_mcp = wrapped
        r = run(sectors.get_stock_price("BBCA"))
        assert r["status"] == "success" and r["price"] == 6625.0
    finally:
        sectors.call_sectors_mcp = original


def test_news_ttl_does_not_call_api_again() -> None:
    """F19: berita segar dilayani dari cache (2 kredit per pengambilan); API gagal tidak dicoba beruntun."""
    reset_ihsg_and_news()
    original = sectors.call_sectors_mcp
    calls: list[str] = []

    async def fake_call(tool: str, args: dict):
        calls.append(tool)
        if tool == "fetch-news":
            return {"results": [{"title": "Laba BBCA naik", "body": "tumbuh", "url": "u", "publish_date": "2026-09-22"}]}
        if tool == "fetch-filings":
            return {"results": [{"title": "X buys shares", "body": "", "source": "s", "date": "2026-09-22"}]}
        return None

    sectors.call_sectors_mcp = fake_call
    try:
        first = run(sectors.get_latest_news_and_filings())
        assert sorted(calls) == ["fetch-filings", "fetch-news"] and len(first) == 2, calls
        for _ in range(5):                                  # muat ulang berkali-kali: tanpa API
            again = run(sectors.get_latest_news_and_filings())
            assert again == first
        assert len(calls) == 2, calls

        # Permintaan paralel saat cache kosong: tetap satu pengambilan.
        reset_ihsg_and_news()
        calls.clear()

        async def burst():
            return await asyncio.gather(*[sectors.get_latest_news_and_filings() for _ in range(6)])
        results = run(burst())
        assert len(calls) == 2 and all(r == results[0] for r in results), calls

        # TTL habis: ambil lagi.
        sectors._news_fetched_at -= sectors.NEWS_CACHE_TTL + 1
        run(sectors.get_latest_news_and_filings())
        assert len(calls) == 4, calls

        # API gagal: cache lama dipakai, dan selama jeda coba-ulang API tidak dipanggil lagi.
        async def failing(tool: str, args: dict):
            calls.append(tool)
            return None
        sectors.call_sectors_mcp = failing
        sectors._news_fetched_at -= sectors.NEWS_CACHE_TTL + 1
        stale = run(sectors.get_latest_news_and_filings())
        assert stale == first and len(calls) == 6, calls
        assert run(sectors.get_latest_news_and_filings()) == first and len(calls) == 6, calls

        # Tanpa cache sama sekali: contoh berita offline, tetap tanpa panggilan beruntun.
        reset_ihsg_and_news()
        calls.clear()
        fallback = run(sectors.get_latest_news_and_filings())
        assert fallback and len(calls) == 2
        assert run(sectors.get_latest_news_and_filings()) == fallback and len(calls) == 2
    finally:
        sectors.call_sectors_mcp = original
        reset_ihsg_and_news()


def test_ihsg_skips_six_hour_price_cache() -> None:
    """F20: /api/stocks/IHSG/price mengikuti get_realtime_ihsg (TTL 15 menit), bukan cache harga 6 jam."""
    fresh_cache_dir()
    # Cache harga 6 jam lama untuk IHSG (dari versi sebelumnya) tidak boleh dipakai.
    sectors.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (sectors.CACHE_DIR / "price_IHSG.json").write_text(json.dumps({
        "fetched_at": time.time(), "source": "lama",
        "rows": [{"date": "2026-01-01", "close": 1234.0}]}), encoding="utf-8")

    original = sectors.call_sectors_mcp
    calls: list[str] = []

    async def fake_call(tool: str, args: dict):
        calls.append(tool)
        return IHSG_ROWS if tool == "fetch-index-daily" else None

    sectors.call_sectors_mcp = fake_call
    try:
        r = run(sectors.get_stock_price("IHSG"))
        assert calls == ["fetch-index-daily"], calls
        assert r["price"] == 7070.0 and r["status"] == "success" and r["source_kind"] == "sectors", r
        assert r["prev_close"] == 7000.0 and r["change"] == 70.0 and isinstance(r["fetched_at"], float)
        ihsg = run(sectors.get_realtime_ihsg())               # /api/ihsg: dari cache 15 menit
        assert ihsg["price"] == 7070.0 and ihsg["status"] == "success" and ihsg["source_kind"] == "cache"
        assert calls == ["fetch-index-daily"]
        # IHSG tidak pernah ditulis ke cache harga 6 jam.
        assert json.loads((sectors.CACHE_DIR / "price_IHSG.json").read_text(encoding="utf-8"))["source"] == "lama"
        assert "IHSG" not in sectors._price_cache

        r2 = run(sectors.get_stock_price("IHSG"))
        assert r2["status"] == "cache" and r2["source_kind"] == "cache" and len(calls) == 1
        assert sectors.peek_cached_price("IHSG")["source_kind"] == "cache"

        # TTL 15 menit habis: API dipanggil lagi (bukan 6 jam).
        sectors._ihsg_fetched_at -= sectors.IHSG_CACHE_TTL + 1
        sectors._ihsg_cache["fetched_at"] = sectors._ihsg_fetched_at
        r3 = run(sectors.get_stock_price("IHSG"))
        assert len(calls) == 2 and r3["status"] == "success" and r3["source_kind"] == "sectors"
        assert sectors.peek_cached_price("IHSG")["source_kind"] == "sectors"

        # TTL habis + API gagal: cache lama ditandai stale (bukan "success" segar).
        async def failing(tool: str, args: dict):
            calls.append(tool)
            return None
        sectors.call_sectors_mcp = failing
        sectors._ihsg_fetched_at -= sectors.IHSG_CACHE_TTL + 1
        sectors._ihsg_cache["fetched_at"] = sectors._ihsg_fetched_at
        r4 = run(sectors.get_stock_price("IHSG"))
        assert len(calls) == 3 and r4["price"] == 7070.0, r4
        assert r4["status"] == "cache" and r4["stale"] is True and r4["source_kind"] == "stale" and r4["message"]
        ihsg = run(sectors.get_realtime_ihsg())
        assert ihsg["stale"] is True and ihsg["status"] == "cache" and ihsg["source_kind"] == "stale"
        assert len(calls) == 3, calls                         # jeda coba-ulang: tidak memanggil API lagi

        # Belum pernah berhasil + API gagal: nilai cadangan (fallback), bukan harga "success".
        reset_ihsg_and_news()
        r5 = run(sectors.get_stock_price("IHSG"))
        assert r5["status"] == "fallback" and r5["source_kind"] == "fallback" and "fetched_at" not in r5
        assert sectors.peek_cached_price("IHSG")["source_kind"] == "fallback"
    finally:
        sectors.call_sectors_mcp = original
        reset_ihsg_and_news()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("SEMUA TES sectors_price LULUS")

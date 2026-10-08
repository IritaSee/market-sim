"""
Integrasi Sectors Financial MCP untuk mengambil data real-time IHSG dan emiten IDX.

Harga emiten (`get_stock_price`) memakai tool `fetch-daily-price` (1 kredit per
panggilan) dan di-cache di memori + disk (.cache/sectors/, TTL 6 jam) supaya
kredit tidak terpotong berulang untuk simbol yang sama. IHSG (`fetch-index-daily`)
dan berita (`fetch-news` + `fetch-filings`) punya cache memori sendiri (TTL 15 menit
dan 10 menit), plus jeda coba-ulang 60 detik setelah API gagal.

Setiap hasil harga memuat `source_kind` (lihat sim.market.SOURCE_KINDS):
"sectors" (baru dari API), "cache" (cache segar), "stale" (cache kedaluwarsa karena
API gagal), "fallback" (nilai cadangan IHSG), atau None (harga tidak tersedia).
"""
from __future__ import annotations

import asyncio
import os
import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

from .stocks import INDEX_SYMBOL, get_company, normalize_symbol

logger = logging.getLogger(__name__)

SECTORS_MCP_URL = os.environ.get("SECTORS_MCP_URL", "https://sectors-mcp.supertype.ai/mcp")
SECTORS_API_KEY = os.environ.get(
    "SECTORS_API_KEY",
    "5c1b7d3d6fb9953f23c5058c46e03f1941e6aefe4a909c1215e2f5499d70dfc1"
)

# Cache sederhana di memori untuk menghemat kuota API credit
_ihsg_cache: Dict[str, Any] = {}
_ihsg_fetched_at: float = 0.0
_ihsg_failed_at: float = 0.0      # waktu panggilan API IHSG terakhir yang gagal (untuk jeda coba-ulang)
_ihsg_last_kind: str = "fallback" # source_kind hasil get_realtime_ihsg terakhir
IHSG_CACHE_TTL: float = 15 * 60   # landing page & dashboard memanggil /api/ihsg tiap dibuka; 1 kredit per panggilan API
_news_cache: List[Dict[str, Any]] = []
_news_fetched_at: float = 0.0
_news_failed_at: float = 0.0
NEWS_CACHE_TTL: float = 10 * 60   # 2 kredit per pengambilan (fetch-news + fetch-filings)
# Setelah API gagal, jangan coba lagi selama jeda ini: setiap pembukaan halaman / klik "Muat ulang"
# saat Sectors bermasalah tidak boleh memicu panggilan (dan mungkin kredit) baru.
API_RETRY_AFTER: float = 60.0
STALE_MESSAGE = "Memakai cache lama karena Sectors tidak dapat dihubungi"
FALLBACK_MESSAGE = "Sectors offline — memakai nilai cadangan IHSG, bukan harga bursa terkini"
# Satu lock per event loop: permintaan paralel (landing + dashboard) menunggu panggilan pertama.
_ihsg_locks: Dict[int, asyncio.Lock] = {}
_news_locks: Dict[int, asyncio.Lock] = {}

# Cache harga per simbol: memori + file .cache/sectors/price_<SYMBOL>.json (relatif root repo).
PRICE_CACHE_TTL: float = 6 * 3600
CACHE_DIR: Path = Path(__file__).resolve().parent.parent / ".cache" / "sectors"
PRICE_SOURCE = "Sectors Financial API (fetch-daily-price)"
OFFLINE_MESSAGE = "Harga Sectors tidak tersedia (mode offline / kuota habis)"
_price_cache: Dict[str, Dict[str, Any]] = {}     # symbol -> {"fetched_at": epoch, "rows": [...], "source": str}
# Satu lock per simbol: request paralel untuk simbol yang sama (klik ganda, beberapa pengguna)
# menunggu panggilan pertama lalu membaca cache-nya, bukan memotong kredit N kali.
_price_locks: Dict[str, Dict[int, asyncio.Lock]] = {}
# Batas panggilan fetch-daily-price per jam (per proses) supaya kredit tidak terkuras bila
# endpoint harga dipanggil berulang untuk banyak simbol. 0 = tanpa batas.
try:
    PRICE_API_MAX_PER_HOUR = max(0, int(os.environ.get("SECTORS_PRICE_MAX_PER_HOUR", "120")))
except ValueError:
    PRICE_API_MAX_PER_HOUR = 120
_price_api_calls: List[float] = []
BUDGET_MESSAGE = "Batas pengambilan harga Sectors per jam tercapai; coba lagi nanti atau isi harga manual"
# Status hasil get_stock_price terakhir per simbol ("success"/"cache"). Dipakai peek_cached_price:
# bila klien baru saja mengambil harga langsung dari API lalu mengirimnya lewat set_symbol,
# asal harganya "sectors", bukan "cache".
_last_price_status: Dict[str, str] = {}


def _loop_lock(locks: Dict[int, asyncio.Lock]) -> asyncio.Lock:
    """asyncio.Lock terikat ke event loop tempat ia dipakai (tes memakai beberapa loop)."""
    return locks.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())


def price_source_kind(status: Optional[str], stale: bool = False) -> Optional[str]:
    """Petakan status harga (success|cache|fallback|offline|unknown) ke source_kind."""
    if status == "success":
        return "sectors"
    if status == "cache":
        return "stale" if stale else "cache"
    if status == "fallback":
        return "fallback"
    return None


def _analyze_sentiment_heuristic(text: str) -> float:
    """
    Hitung skor sentimen (-2.0 s/d +2.0) dari teks berita/filings secara deterministik/cepat.
    """
    t = text.lower()
    pos_keywords = [
        "buy", "buys", "bought", "divestment", "profit", "laba", "growth", "tumbuh",
        "dividend", "dividen", "investasi", "expansion", "ekspansi", "naik", "surge",
        "acquisition", "akuisisi", "bullish", "revenue", "pendapatan", "meningkat", "optimis"
    ]
    neg_keywords = [
        "sell", "sells", "sold", "loss", "rugi", "turun", "drop", "crash", "fall",
        "penurunan", "restructuring", "restrukturisasi", "default", "gagal", "scandal",
        "investigation", "penyelidikan", "lawsuit", "gugatan", "suspensi", "suspension",
        "utang", "debt", "risiko", "risk", "warning", "peringatan", "bearish"
    ]

    score = 0.0
    for w in pos_keywords:
        if w in t:
            score += 0.4
    for w in neg_keywords:
        if w in t:
            score -= 0.5

    # Batasi range -2.0 hingga 2.0
    return max(-2.0, min(2.0, score))


async def get_latest_news_and_filings() -> List[Dict[str, Any]]:
    """
    Ambil berita dan company filings terkini dari Sectors MCP,
    lengkap dengan estimasi dampak sentimen ke pasar.

    Cache segar (NEWS_CACHE_TTL) dilayani tanpa memanggil API: setiap pengambilan
    memotong 2 kredit, padahal tab berita dibuka di tiap pemuatan halaman.
    """
    if _news_cache and (time.time() - _news_fetched_at) < NEWS_CACHE_TTL:
        return list(_news_cache)
    async with _loop_lock(_news_locks):
        # Permintaan lain mungkin sudah mengisi cache selagi kita menunggu lock.
        now = time.time()
        if _news_cache and (now - _news_fetched_at) < NEWS_CACHE_TTL:
            return list(_news_cache)
        if (now - _news_failed_at) < API_RETRY_AFTER:
            return list(_news_cache) if _news_cache else _fallback_news()
        return await _fetch_news()


async def _fetch_news() -> List[Dict[str, Any]]:
    """Panggil fetch-news + fetch-filings (2 kredit). Dipanggil di bawah lock berita."""
    global _news_cache, _news_fetched_at, _news_failed_at

    items: List[Dict[str, Any]] = []

    # 1. Fetch News
    news_res = await call_sectors_mcp("fetch-news", {})
    if isinstance(news_res, dict) and "results" in news_res:
        for item in news_res["results"][:6]:
            title = item.get("title", "")
            body = item.get("body", "")
            sentiment = _analyze_sentiment_heuristic(f"{title} {body}")
            items.append({
                "type": "news",
                "title": title,
                "body": body,
                "url": item.get("url", ""),
                "date": item.get("publish_date") or item.get("date", ""),
                "sentiment_impact": sentiment,
                "sentiment_label": "BULLISH 🚀" if sentiment > 0.2 else "BEARISH 📉" if sentiment < -0.2 else "NETRAL ⚖️",
            })

    # 2. Fetch Filings
    filings_res = await call_sectors_mcp("fetch-filings", {})
    if isinstance(filings_res, dict) and "results" in filings_res:
        for item in filings_res["results"][:6]:
            title = item.get("title", "")
            body = item.get("body", "")
            sentiment = _analyze_sentiment_heuristic(f"{title} {body}")
            items.append({
                "type": "filing",
                "title": title,
                "body": body,
                "url": item.get("source", ""),
                "date": item.get("date", ""),
                "sentiment_impact": sentiment,
                "sentiment_label": "BULLISH 🚀" if sentiment > 0.2 else "BEARISH 📉" if sentiment < -0.2 else "NETRAL ⚖️",
            })

    if items:
        _news_cache = items
        _news_fetched_at = time.time()
        return list(items)

    _news_failed_at = time.time()
    if _news_cache:
        return list(_news_cache)
    return _fallback_news()


def _fallback_news() -> List[Dict[str, Any]]:
    """Contoh berita saat offline / kuota habis (tidak memanggil API)."""
    return [
        {
            "type": "filing",
            "title": "Edwin Soeryadjaya buys shares of Saratoga Investama Sedaya",
            "body": "Edwin Soeryadjaya bought 2,250,000 shares of Saratoga Investama Sedaya.",
            "url": "",
            "date": "2026-09-04",
            "sentiment_impact": 1.2,
            "sentiment_label": "BULLISH 🚀"
        },
        {
            "type": "news",
            "title": "Kinerja Emiten Perbankan IDX Mengalami Pertumbuhan Laba Bersih Kuartal II",
            "body": "BBCA dan BMRI mencatatkan pertumbuhan kredit dan efisiensi operasional.",
            "url": "",
            "date": "2026-09-04",
            "sentiment_impact": 0.8,
            "sentiment_label": "BULLISH 🚀"
        }
    ]


_ssl_context: Any = None
_offline_logged: set = set()


def sectors_online() -> bool:
    """False bila SECTORS_MCP_URL bukan http(s) (mis. offline:// dari dev_server) → semua panggilan dilewati."""
    return str(SECTORS_MCP_URL).lower().startswith(("http://", "https://"))


async def _get_ssl_context() -> Any:
    """
    Konteks SSL dibuat sekali di thread terpisah: httpx.AsyncClient() tanpa `verify` membangun
    konteks baru tiap panggilan (±1-2 detik di Windows) dan memblokir event loop simulasi.
    """
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = await asyncio.to_thread(httpx.create_ssl_context)
    return _ssl_context


async def call_sectors_mcp(tool_name: str, arguments: dict) -> Optional[Any]:
    """Panggil tool pada Sectors MCP Server via JSON-RPC. Mengembalikan None bila offline/gagal."""
    if not sectors_online():
        if tool_name not in _offline_logged:
            _offline_logged.add(tool_name)
            logger.warning(f"Sectors MCP offline ({SECTORS_MCP_URL}); {tool_name} dilewati, fallback dipakai.")
        return None

    headers = {
        "Authorization": f"Bearer {SECTORS_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }

    try:
        verify = await _get_ssl_context()
        async with httpx.AsyncClient(timeout=15.0, verify=verify) as client:
            res = await client.post(SECTORS_MCP_URL, json=payload, headers=headers)
            res.raise_for_status()

            # MCP mengembalikan Server-Sent Events (SSE) berawalan "data: "
            text = res.text
            saw_data_line = False
            for line in text.splitlines():
                if line.startswith("data: "):
                    saw_data_line = True
                    parsed = json.loads(line[6:])
                    if parsed.get("result") and "content" in parsed["result"]:
                        content_text = parsed["result"]["content"][0]["text"]
                        try:
                            return json.loads(content_text)
                        except json.JSONDecodeError:
                            return content_text
                    elif parsed.get("error"):
                        logger.error(f"Sectors MCP Error: {parsed['error']}")
                        return None
            if not saw_data_line and text.strip():
                # Beberapa server membalas JSON-RPC polos (bukan SSE); tangani bentuk yang sama.
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("result") and "content" in parsed["result"]:
                    content_text = parsed["result"]["content"][0]["text"]
                    try:
                        return json.loads(content_text)
                    except json.JSONDecodeError:
                        return content_text
    except Exception as e:
        logger.warning(f"Gagal memanggil Sectors MCP ({tool_name}): {e}")
        return None
    return None


IHSG_FALLBACK: Dict[str, Any] = {
    "source": "Fallback Baseline",
    "index_code": "IHSG",
    "name": "Indeks Harga Saham Gabungan",
    "price": 6850.0,
    "date": "2026-09-04",
    "prev_price": 6820.0,
    "change_pts": 30.0,
    "change_pct": 0.44,
    "history": [],
    "status": "fallback",
    "source_kind": "fallback",
    "message": FALLBACK_MESSAGE,
}


def _ihsg_from_cache(now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    Salinan cache IHSG dengan penanda asal, tanpa memanggil API:
    segar (≤ TTL) → source_kind "cache"; kedaluwarsa → status "cache", stale true, source_kind "stale".
    None bila belum pernah berhasil mengambil IHSG.
    """
    if not _ihsg_cache:
        return None
    now = time.time() if now is None else now
    out = dict(_ihsg_cache)
    if (now - _ihsg_fetched_at) < IHSG_CACHE_TTL:
        out["source_kind"] = "cache"
    else:
        out.update({"status": "cache", "stale": True, "source_kind": "stale", "message": STALE_MESSAGE})
    return out


async def get_realtime_ihsg() -> Dict[str, Any]:
    """
    Ambil data harian/realtime IHSG terbaru dari Sectors MCP (1 kredit per panggilan API).

    `status`: "success" (dari API, termasuk cache segar ≤ 15 menit), "cache" + `stale: true`
    (cache kedaluwarsa dipakai karena API gagal), atau "fallback" (nilai cadangan, belum pernah
    berhasil). `source_kind` membedakan "sectors" (baru saja dari API) dan "cache".
    """
    global _ihsg_last_kind
    result = await _load_ihsg()
    _ihsg_last_kind = result.get("source_kind") or "fallback"
    return result


async def _load_ihsg() -> Dict[str, Any]:
    # Cache segar (TTL 15 menit) dipakai dulu supaya setiap pembukaan halaman tidak memotong kredit.
    cached = _ihsg_from_cache()
    if cached is not None and cached.get("source_kind") == "cache":
        return cached
    async with _loop_lock(_ihsg_locks):
        cached = _ihsg_from_cache()
        if cached is not None and cached.get("source_kind") == "cache":
            return cached
        if (time.time() - _ihsg_failed_at) < API_RETRY_AFTER:
            return cached if cached is not None else dict(IHSG_FALLBACK)
        return await _fetch_ihsg()


async def _fetch_ihsg() -> Dict[str, Any]:
    """Panggil fetch-index-daily. Dipanggil di bawah lock IHSG."""
    global _ihsg_cache, _ihsg_fetched_at, _ihsg_failed_at

    data = await call_sectors_mcp("fetch-index-daily", {"index_code": "ihsg"})
    result = _parse_ihsg(data)
    if result is not None:
        _ihsg_cache = result
        _ihsg_fetched_at = result["fetched_at"]
        return dict(result)

    # API gagal / kuota habis: cache lama (ditandai stale) atau nilai cadangan IHSG.
    _ihsg_failed_at = time.time()
    cached = _ihsg_from_cache()
    return cached if cached is not None else dict(IHSG_FALLBACK)


def _parse_ihsg(data: Any) -> Optional[Dict[str, Any]]:
    """Bentuk hasil IHSG dari respons fetch-index-daily, atau None bila respons tidak bisa dipakai."""
    if not isinstance(data, list):
        return None

    def _pos(item: Any) -> Optional[float]:
        v = _to_float(item.get("price")) if isinstance(item, dict) else None
        return v if (v is not None and math.isfinite(v) and v > 0) else None

    rows = [(item, _pos(item)) for item in data if _pos(item) is not None]
    if not rows:
        return None
    # Urutkan berdasarkan tanggal, ambil item terakhir
    rows.sort(key=lambda pair: str(pair[0].get("date") or ""))
    latest, price = rows[-1]
    prev_price = rows[-2][1] if len(rows) > 1 else price
    change_pts = price - prev_price
    change_pct = (change_pts / prev_price * 100) if prev_price > 0 else 0.0
    return {
        "source": "Sectors Financial API (Real-time IDX)",
        "index_code": "IHSG",
        "name": "Indeks Harga Saham Gabungan",
        "price": price,
        "date": latest.get("date"),
        "prev_price": prev_price,
        "change_pts": round(change_pts, 2),
        "change_pct": round(change_pct, 2),
        "history": [{"date": item.get("date"), "price": value} for item, value in rows[-15:]],
        "status": "success",
        "source_kind": "sectors",
        "fetched_at": time.time(),
    }


# ------------------------------------------------------------------ #
#  Harga emiten (fetch-daily-price) dengan cache memori + disk         #
# ------------------------------------------------------------------ #

def _to_float(value: Any) -> Optional[float]:
    """Angka dari API bisa berupa string/None; kembalikan float atau None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None   # buang NaN


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"price_{symbol}.json"


def _read_disk_cache(symbol: str) -> Optional[Dict[str, Any]]:
    """Entri cache {"fetched_at","rows","source"} dari disk, atau None bila tidak ada/rusak."""
    path = _cache_path(symbol)
    try:
        if not path.is_file():
            return None
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            return None
        data["fetched_at"] = float(data.get("fetched_at") or 0)
        return data
    except Exception as exc:
        logger.warning(f"Cache harga {symbol} tidak terbaca: {exc}")
        return None


def _write_disk_cache(symbol: str, entry: Dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(symbol).with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False)
        tmp.replace(_cache_path(symbol))
    except Exception as exc:
        logger.warning(f"Cache harga {symbol} tidak tersimpan: {exc}")


def _is_fresh(entry: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    if not entry or not entry.get("rows"):
        return False
    now = time.time() if now is None else now
    return (now - float(entry.get("fetched_at") or 0)) < PRICE_CACHE_TTL


def _normalize_daily_rows(raw: Any) -> List[Dict[str, Any]]:
    """
    Bentuk baris harian dari fetch-daily-price menjadi
    {"date","open","high","low","close","volume","market_cap"} terurut by date.
    Menerima list langsung atau dict yang memuat list (mis. {"results":[...]}).
    """
    rows_in: Any = raw
    if isinstance(raw, dict):
        rows_in = next((v for v in raw.values() if isinstance(v, list)), [])
    if not isinstance(rows_in, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in rows_in:
        if not isinstance(item, dict):
            continue
        close = _to_float(item.get("close", item.get("price")))
        date_str = str(item.get("date") or "")
        if close is None or close <= 0 or not date_str:   # close 0 (mis. suspensi) bukan harga awal yang sah
            continue
        rows.append({
            "date":       date_str[:10],
            "open":       _to_float(item.get("open")),
            "high":       _to_float(item.get("high")),
            "low":        _to_float(item.get("low")),
            "close":      close,
            "volume":     _to_float(item.get("volume")),
            "market_cap": _to_float(item.get("market_cap")),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def _empty_price(symbol: str, meta: Optional[Dict[str, Any]], status: str, message: str) -> Dict[str, Any]:
    meta = meta or {}
    return {
        "symbol":      symbol,
        "name":        meta.get("name"),
        "sector":      meta.get("sector"),
        "sector_name": meta.get("sector_name"),
        "kind":        meta.get("kind", "stock"),
        "price":       None,
        "date":        None,
        "prev_close":  None,
        "change":      None,
        "change_pct":  None,
        "open":        None,
        "high":        None,
        "low":         None,
        "volume":      None,
        "market_cap":  None,
        "history":     [],
        "source":      PRICE_SOURCE,
        "status":      status,
        "source_kind": None,
        "message":     message,
    }


def _build_price_result(symbol: str, meta: Dict[str, Any], entry: Dict[str, Any], status: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = entry["rows"]
    latest = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    price = latest["close"]
    prev_close = prev["close"] if prev else None
    change = round(price - prev_close, 2) if prev_close is not None else None
    change_pct = round(change / prev_close * 100, 2) if (change is not None and prev_close) else None
    result: Dict[str, Any] = {
        "symbol":      symbol,
        "name":        meta.get("name"),
        "sector":      meta.get("sector"),
        "sector_name": meta.get("sector_name"),
        "kind":        meta.get("kind", "stock"),
        "price":       price,
        "date":        latest["date"] or None,
        "prev_close":  prev_close,
        "change":      change,
        "change_pct":  change_pct,
        "open":        latest.get("open"),
        "high":        latest.get("high"),
        "low":         latest.get("low"),
        "volume":      latest.get("volume"),
        "market_cap":  latest.get("market_cap"),
        "history":     [{k: r.get(k) for k in ("date", "open", "high", "low", "close", "volume")} for r in rows],
        "source":      entry.get("source") or PRICE_SOURCE,
        "status":      status,
        "source_kind": price_source_kind(status, bool(entry.get("stale"))),
    }
    if status != "fallback":                     # nilai cadangan tidak pernah diambil dari API
        result["fetched_at"] = entry.get("fetched_at")
    if entry.get("stale"):
        result["stale"] = True
        result["message"] = STALE_MESSAGE
    elif status == "fallback":
        result["message"] = FALLBACK_MESSAGE
    return result


def _ihsg_to_rows(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Petakan hasil get_realtime_ihsg ke baris harian (hanya close yang tersedia)."""
    rows: List[Dict[str, Any]] = []
    for item in data.get("history") or []:
        close = _to_float(item.get("price", item.get("close")))
        if close is not None and item.get("date"):
            rows.append({"date": str(item["date"])[:10], "open": None, "high": None, "low": None,
                         "close": close, "volume": None, "market_cap": None})
    latest_price = _to_float(data.get("price"))
    latest_date = str(data.get("date") or "")[:10]
    if not rows and latest_price is not None:
        # Tanpa histori (mis. baseline fallback): susun dua baris dari prev_price & price.
        prev = _to_float(data.get("prev_price"))
        if prev is not None:
            rows.append({"date": "", "open": None, "high": None, "low": None,
                         "close": prev, "volume": None, "market_cap": None})
    if latest_price is not None and not any(r["date"] == latest_date for r in rows):
        rows.append({"date": latest_date, "open": None, "high": None, "low": None,
                     "close": latest_price, "volume": None, "market_cap": None})
    rows.sort(key=lambda r: r["date"])
    return rows


def _symbol_meta(sym: str) -> Optional[Dict[str, Any]]:
    if sym == INDEX_SYMBOL:
        return {"symbol": INDEX_SYMBOL, "name": "Indeks Harga Saham Gabungan",
                "sector": "index", "sector_name": "Indeks", "kind": "index"}
    return get_company(sym)


def peek_cached_price(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Hasil harga dari cache (memori/disk, TTL diabaikan) TANPA memanggil API.
    Dipakai server untuk melengkapi tanggal/sumber/source_kind saat klien sudah mengirim harga:
    status "success" bila permintaan harga terakhir simbol ini memang mengambil entri tersebut
    dari API, "cache" bila entri masih segar, "cache" + stale bila kedaluwarsa.
    IHSG dibaca dari cache IHSG (atau nilai cadangan bila belum pernah berhasil).
    """
    sym = normalize_symbol(symbol)
    meta = _symbol_meta(sym)
    if meta is None:
        return None
    if sym == INDEX_SYMBOL:
        data = _ihsg_from_cache()
        if data is None:
            data = dict(IHSG_FALLBACK)
        elif data.get("source_kind") == "cache" and _ihsg_last_kind == "sectors":
            data["source_kind"] = "sectors"
        return _ihsg_price_result(meta, data)
    entry = _price_cache.get(sym) or _read_disk_cache(sym)
    if not entry or not entry.get("rows"):
        return None
    if not _is_fresh(entry):
        return _build_price_result(sym, meta, dict(entry, stale=True), "cache")
    status = "success" if _last_price_status.get(sym) == "success" else "cache"
    return _build_price_result(sym, meta, entry, status)


def _ihsg_price_result(meta: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """Petakan hasil get_realtime_ihsg ke bentuk harga emiten (status sesuai source_kind)."""
    rows = _ihsg_to_rows(data)
    if not rows:
        return _empty_price(INDEX_SYMBOL, meta, "offline", OFFLINE_MESSAGE)
    kind = data.get("source_kind")
    status = {"sectors": "success", "cache": "cache", "stale": "cache"}.get(kind, "fallback")
    entry = {
        "rows":       rows,
        "source":     data.get("source") or "Sectors Financial API (fetch-index-daily)",
        "fetched_at": data.get("fetched_at", _ihsg_fetched_at or None),
        "stale":      kind == "stale",
    }
    return _build_price_result(INDEX_SYMBOL, meta, entry, status)


async def get_stock_price(symbol: str) -> Dict[str, Any]:
    """
    Harga penutupan terakhir emiten dari Sectors (tool fetch-daily-price), dengan cache.

    Selalu mengembalikan dict; tidak pernah melempar. `status`:
        "success"  baru diambil dari API
        "cache"    dari cache memori/disk (TTL 6 jam; `stale: true` + `message` bila cache
                   kedaluwarsa dipakai karena API gagal)
        "offline"  API gagal & tidak ada cache -> price null, history []
        "unknown"  simbol bukan emiten BEI dan bukan IHSG
        "fallback" hanya IHSG: nilai cadangan saat offline (tanpa fetched_at)
    `source_kind`: sectors | cache | stale | fallback | None (lihat price_source_kind).
    IHSG tidak memakai cache harga 6 jam: selalu lewat get_realtime_ihsg (TTL 15 menit).
    """
    sym = normalize_symbol(symbol)
    meta = _symbol_meta(sym) if sym else None
    if not sym or meta is None:
        return _empty_price(sym or str(symbol), None, "unknown", "Simbol tidak dikenal")

    try:
        if sym == INDEX_SYMBOL:
            return _ihsg_price_result(meta, await get_realtime_ihsg())
        entry = _fresh_cached(sym)
        if entry is None:
            async with _loop_lock(_price_locks.setdefault(sym, {})):
                # Request lain untuk simbol yang sama mungkin sudah mengisi cache selagi kita menunggu.
                entry = _fresh_cached(sym)
                if entry is None:
                    result = await _fetch_price(sym, meta)
                    _last_price_status[sym] = result["status"]
                    return result
        _last_price_status[sym] = "cache"
        return _build_price_result(sym, meta, entry, "cache")
    except Exception as exc:
        logger.warning(f"get_stock_price({sym}) gagal: {exc}")
        return _empty_price(sym, meta, "offline", OFFLINE_MESSAGE)


def _fresh_cached(sym: str) -> Optional[Dict[str, Any]]:
    """Entri cache yang masih dalam TTL (memori, lalu disk), atau None."""
    now = time.time()
    entry = _price_cache.get(sym)
    if _is_fresh(entry, now):
        return entry
    disk = _read_disk_cache(sym)
    if disk is not None:
        _price_cache[sym] = disk
        if _is_fresh(disk, now):
            return disk
    return None


def _take_price_budget() -> bool:
    """True bila masih ada jatah panggilan fetch-daily-price dalam 1 jam terakhir (lalu dicatat)."""
    if PRICE_API_MAX_PER_HOUR <= 0:
        return True
    now = time.time()
    while _price_api_calls and _price_api_calls[0] < now - 3600:
        _price_api_calls.pop(0)
    if len(_price_api_calls) >= PRICE_API_MAX_PER_HOUR:
        return False
    _price_api_calls.append(now)
    return True


async def _fetch_price(sym: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    """Cache emiten tidak ada / kedaluwarsa -> panggil API (1 kredit). Dipanggil di bawah lock simbol."""
    now = time.time()
    message = OFFLINE_MESSAGE
    rows: List[Dict[str, Any]] = []
    source = PRICE_SOURCE
    api_ok = False
    if sectors_online() and not _take_price_budget():   # mode offline tidak memakai jatah
        message = BUDGET_MESSAGE
        logger.warning(f"Jatah fetch-daily-price ({PRICE_API_MAX_PER_HOUR}/jam) habis; {sym} tidak diambil.")
    else:
        raw = await call_sectors_mcp("fetch-daily-price", {"symbol": sym})
        rows = _normalize_daily_rows(raw)
        api_ok = bool(rows)

    if api_ok:
        new_entry = {"fetched_at": now, "rows": rows, "source": source, "symbol": sym}
        _price_cache[sym] = new_entry
        _write_disk_cache(sym, new_entry)
        return _build_price_result(sym, meta, new_entry, "success")

    # API gagal / jatah habis: pakai cache kedaluwarsa bila ada, kalau tidak -> offline.
    stale = _price_cache.get(sym) or _read_disk_cache(sym)
    if stale and stale.get("rows"):
        stale = dict(stale)
        stale["stale"] = True
        return _build_price_result(sym, meta, stale, "cache")
    return _empty_price(sym, meta, "offline", message)

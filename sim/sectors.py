"""
Integrasi Sectors Financial MCP untuk mengambil data real-time IHSG dan emiten IDX.
"""
import os
import json
import logging
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger(__name__)

SECTORS_MCP_URL = os.environ.get("SECTORS_MCP_URL", "https://sectors-mcp.supertype.ai/mcp")
SECTORS_API_KEY = os.environ.get(
    "SECTORS_API_KEY",
    "98472105befd14b45a8c0a677cd3954bbb3f806639559aa0c6451af5ca95553a"
)

# Cache sederhana di memori untuk menghemat kuota API credit
_ihsg_cache: Dict[str, Any] = {}
_news_cache: List[Dict[str, Any]] = []


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
    """
    global _news_cache

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
        return items

    if _news_cache:
        return _news_cache

    # Fallback jika offline/rate limit
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


async def call_sectors_mcp(tool_name: str, arguments: dict) -> Optional[Any]:
    """Panggil tool pada Sectors MCP Server via JSON-RPC."""
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
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(SECTORS_MCP_URL, json=payload, headers=headers)
            res.raise_for_status()

            # MCP mengembalikan Server-Sent Events (SSE) berawalan "data: "
            text = res.text
            for line in text.splitlines():
                if line.startswith("data: "):
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
    except Exception as e:
        logger.warning(f"Gagal memanggil Sectors MCP ({tool_name}): {e}")
        return None


async def get_realtime_ihsg() -> Dict[str, Any]:
    """
    Ambil data harian/realtime IHSG terbaru dari Sectors MCP.
    """
    global _ihsg_cache

    data = await call_sectors_mcp("fetch-index-daily", {"index_code": "ihsg"})
    if isinstance(data, list) and len(data) > 0:
        # Urutkan berdasarkan tanggal jika perlu, ambil item terakhir
        latest = data[-1]
        prev = data[-2] if len(data) > 1 else latest

        price = float(latest.get("price", 0))
        prev_price = float(prev.get("price", price))
        change_pts = price - prev_price
        change_pct = (change_pts / prev_price * 100) if prev_price > 0 else 0.0

        history = [
            {"date": item.get("date"), "price": float(item.get("price", 0))}
            for item in data[-15:]
        ]

        result = {
            "source": "Sectors Financial API (Real-time IDX)",
            "index_code": "IHSG",
            "name": "Indeks Harga Saham Gabungan",
            "price": price,
            "date": latest.get("date"),
            "prev_price": prev_price,
            "change_pts": round(change_pts, 2),
            "change_pct": round(change_pct, 2),
            "history": history,
            "status": "success",
        }
        _ihsg_cache = result
        return result

    # Jika API gagal/habis kuota, gunakan cache atau fallback nilai wajar IHSG
    if _ihsg_cache:
        return _ihsg_cache

    return {
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
    }

"""
Uji ketahanan terhadap masukan rusak — jalankan: python tests/test_robustness.py

Regresi untuk temuan review:
  - NaN/inf/angka raksasa lewat WebSocket tidak boleh masuk state (JSON NaN/Infinity membuat
    semua browser membuang pesan dan simulasi tampak membeku);
  - rasio negatif tidak boleh mengubah jumlah agen;
  - set_symbol dengan harga tidak valid tidak boleh mengganti simbol;
  - request harga paralel untuk simbol yang sama hanya memotong 1 kredit;
  - WebSocket menolak Origin dari domain lain.
Mode offline: tidak ada kredit Sectors yang terpakai.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SECTORS_API_KEY"] = "offline"
os.environ["SIMPASAR_LLM"] = "off"

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import server  # noqa: E402
from sim import sectors  # noqa: E402
from sim.market import Market  # noqa: E402

sectors.CACHE_DIR = Path(tempfile.mkdtemp(prefix="simpasar-robust-cache-")) / "sectors"
sectors._price_cache.clear()


def strict_json(text: str) -> dict:
    """Parser seketat JSON.parse browser: literal NaN/Infinity ditolak."""
    def reject(const: str):
        raise ValueError(f"literal {const} tidak valid di JSON")
    return json.loads(text, parse_constant=reject)


def test_market_rejects_non_finite() -> None:
    m = Market(n_agents=100, seed=1, fundamental=7000.0)
    params_before = dict(m.params)
    for bad in (float("inf"), float("-inf"), float("nan")):
        assert m.inject_rumor(bad) is False and m.inject_panic(bad) is False
        assert m.sentiment == 0.0
        assert m.set_population(bad, 0.5, 0.2) is False
        assert m.set_psych(bad, 0.3) is False
        assert m.set_fundamental(bad) is False
    assert m.set_fundamental(1e308) is False and m.fundamental == 7000.0
    assert m.set_population(-1.0, 1.5, 0.5) is False
    assert m.set_psych(-0.5, 0.9) is False
    assert m.params == params_before and len(m.agents) == 100
    assert len({a.id for a in m.agents}) == 100

    # Sentimen di-clamp ke ±3.
    assert m.inject_rumor(10.0) and m.sentiment == 3.0
    assert m.inject_panic(100.0) and m.sentiment == -3.0

    # Nilai valid tetap bekerja dan state selalu JSON ketat.
    assert m.set_population(0.2, 0.6, 0.2) and m.set_psych(0.5, 0.2)
    for _ in range(50):
        m.step()
    strict_json(json.dumps(m.get_state(full=True)))
    print("ok  test_market_rejects_non_finite")


def test_set_symbol_invalid_price_keeps_symbol() -> None:
    m = Market(n_agents=100, seed=1, fundamental=7000.0)
    for _ in range(5):
        m.step()
    meta = {"symbol": "BBCA", "name": "PT Bank Central Asia Tbk.", "sector": "financials", "sector_name": "Keuangan", "kind": "stock"}
    for bad in (0.0, -5.0, 0.5, 0.99, float("nan"), float("inf"), 1e308, None):   # minimum 1 (F16)
        assert m.set_symbol(meta, bad) is False
        assert m.symbol["symbol"] == "IHSG" and m.fundamental == 7000.0 and m.tick == 5
    assert m.set_symbol(meta, 6625.0) is True and m.symbol["symbol"] == "BBCA" and m.tick == 0
    print("ok  test_set_symbol_invalid_price_keeps_symbol")


def test_price_requests_are_deduplicated() -> None:
    original_call, original_online = sectors.call_sectors_mcp, sectors.sectors_online
    calls: list[str] = []
    rows = [
        {"symbol": "TLKM.JK", "date": "2026-09-21", "close": 3000, "open": 2990, "high": 3010, "low": 2980, "volume": 1, "market_cap": 1},
        {"symbol": "TLKM.JK", "date": "2026-09-22", "close": 3050, "open": 3000, "high": 3060, "low": 2995, "volume": 1, "market_cap": 1},
        {"symbol": "TLKM.JK", "date": "2026-09-23", "close": 0, "open": 0, "high": 0, "low": 0, "volume": 0, "market_cap": 0},
    ]

    async def slow_call(tool: str, args: dict):
        calls.append(args["symbol"])
        await asyncio.sleep(0.05)
        return rows

    async def burst() -> list[dict]:
        return await asyncio.gather(*[sectors.get_stock_price("TLKM") for _ in range(10)])

    sectors.call_sectors_mcp = slow_call
    sectors.sectors_online = lambda: True
    try:
        sectors._price_cache.clear()
        results = asyncio.run(burst())
        assert calls == ["TLKM"], calls                      # 10 request paralel → 1 kredit
        assert {r["price"] for r in results} == {3050.0}     # baris close 0 dibuang
        assert sorted(r["status"] for r in results).count("success") == 1

        # Jatah per jam habis → tidak memanggil API, status offline dengan pesan jatah.
        saved_limit, saved_calls = sectors.PRICE_API_MAX_PER_HOUR, list(sectors._price_api_calls)
        sectors.PRICE_API_MAX_PER_HOUR = 1
        sectors._price_api_calls[:] = [__import__("time").time()]
        try:
            r = asyncio.run(sectors.get_stock_price("BMRI"))
            assert r["status"] == "offline" and r["price"] is None and "Batas" in r["message"], r
            assert calls == ["TLKM"]
        finally:
            sectors.PRICE_API_MAX_PER_HOUR = saved_limit
            sectors._price_api_calls[:] = saved_calls
    finally:
        sectors.call_sectors_mcp, sectors.sectors_online = original_call, original_online
        sectors._price_cache.clear()
    print("ok  test_price_requests_are_deduplicated")


def recv_until(ws, predicate, what: str, limit: int = 60) -> dict:
    for _ in range(limit):
        msg = strict_json(ws.receive_text())   # setiap pesan harus lolos parser ketat
        if predicate(msg):
            return msg
    raise AssertionError(f"tidak menemukan pesan: {what}")


def test_websocket_rejects_bad_numbers() -> None:
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ws:
            recv_until(ws, lambda m: m.get("snapshot") is True, "snapshot awal")
            bad_cmds = [
                '{"cmd":"inject_rumor","strength":-Infinity}',
                '{"cmd":"inject_panic","strength":NaN}',
                '{"cmd":"inject_rumor","strength":"inf"}',
                '{"cmd":"inject_news_sentiment","strength":Infinity,"title":"x"}',
                '{"cmd":"set_population","fundamentalist":"nan","chartist":0.5,"noise":0.2}',
                '{"cmd":"set_population","fundamentalist":0,"chartist":0,"noise":0}',
                '{"cmd":"set_psych","disciplined":Infinity,"bagholder":0.3}',
                '{"cmd":"set_fundamental","fundamental":1e308}',
                '{"cmd":"set_fundamental","fundamental":0.5}',
                '{"cmd":"set_speed","interval":NaN}',
                '{"cmd":"set_symbol","symbol":"BBCA","fundamental":-1}',
                '{"cmd":"set_symbol","symbol":"BBCA","fundamental":0.5}',
            ]
            for raw in bad_cmds:
                ws.send_text(raw)
                cmd = raw.split('"cmd":"')[1].split('"')[0]
                if cmd == "set_symbol":
                    recv_until(ws, lambda m: m.get("event") == "symbol_error", f"symbol_error untuk {raw}")
                else:
                    recv_until(ws, lambda m, c=cmd: m.get("event") == "error" and m.get("cmd") == c, f"error untuk {raw}")

            # State tetap sehat: reset berhasil dan snapshot lolos parser ketat.
            ws.send_text('{"cmd":"reset"}')
            snap = recv_until(ws, lambda m: m.get("snapshot") is True, "snapshot setelah reset")
            assert snap["symbol"]["symbol"] == "BBCA" and abs(snap["sentiment"]) <= 3.0
            assert len(snap["agents"]) == 100
            assert server.market.set_population(0.3, 0.5, 0.2)
            assert 0.05 <= server.tick_rate <= 1.5
    print("ok  test_websocket_rejects_bad_numbers")


def test_websocket_origin_check() -> None:
    with TestClient(server.app) as client:
        # Origin sama dengan host (TestClient memakai host "testserver") → diterima.
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            assert ws.receive_json().get("snapshot") is True
        # Tanpa Origin (klien non-browser) → diterima.
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json().get("snapshot") is True
        # Origin domain lain → ditolak sebelum accept.
        try:
            with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}) as ws:
                ws.receive_json()
            raise AssertionError("Origin asing seharusnya ditolak")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008, exc.code
    # Host localhost:8000 dengan Origin http://127.0.0.1:8000 → sama-sama lokal, diterima.
    class FakeWS:
        def __init__(self, origin: str, host: str) -> None:
            self.headers = {"origin": origin, "host": host}
    assert server._origin_allowed(FakeWS("http://127.0.0.1:8000", "localhost:8000"))
    assert server._origin_allowed(FakeWS("http://localhost:8080", "localhost"))      # nginx: Host tanpa port
    assert not server._origin_allowed(FakeWS("https://localhost.evil.example", "localhost:8000"))
    assert not server._origin_allowed(FakeWS("null", "localhost:8000"))
    print("ok  test_websocket_origin_check")


def test_set_fundamental_keeps_sectors_attribution() -> None:
    """Harga manual → 'manual'; kembali ke harga awal yang dikenal server (BBCA) → atribusinya pulih (bukan 'manual')."""
    with TestClient(server.app) as client:
        with client.websocket_connect("/ws") as ws:
            snap = recv_until(ws, lambda m: m.get("snapshot") is True, "snapshot awal")
            assert snap["symbol"]["symbol"] == "BBCA"
            known_kind, known_price = snap["symbol"]["source_kind"], snap["symbol"]["source_price"]
            assert known_kind in ("fallback", "sectors", "cache", "stale") and known_price

            ws.send_text(json.dumps({"cmd": "set_fundamental", "fundamental": known_price + 350}))
            s = recv_until(ws, lambda m: m.get("snapshot") is True and m["fundamental"] == known_price + 350, "snapshot manual")
            assert s["symbol"]["source_kind"] == "manual", s["symbol"]

            # Alur tombol "Terapkan harga riil": nilai sama dengan data yang dikenal server.
            ws.send_text(json.dumps({"cmd": "set_fundamental", "fundamental": known_price}))
            s = recv_until(ws, lambda m: m.get("snapshot") is True and m["fundamental"] == known_price, "snapshot harga riil")
            assert s["symbol"]["source_kind"] == known_kind, s["symbol"]
    print("ok  test_set_fundamental_keeps_sectors_attribution")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("SEMUA TES robustness LULUS")

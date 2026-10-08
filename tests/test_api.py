"""
Uji REST + WebSocket server.py lewat fastapi.testclient — jalankan: python tests/test_api.py
Mode offline (SECTORS_MCP_URL=offline://x): tidak ada kredit Sectors yang terpakai
(API Sectors dipalsukan untuk menguji source_kind "sectors"/"cache"/"stale").

Termasuk regresi: tick_interval & speed_changed (F21), get_state tidak kena limiter +
balasan error saat perintah dibuang (F15), set_population/set_psych di-broadcast (F17),
harga awal minimum 1 (F16), symbol.source_kind, simbol awal BBCA (bukan IHSG) + field limits
(ARA/ARB).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)                                     # server.py memakai path relatif (frontend/, landing/)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SECTORS_API_KEY"] = "offline"
os.environ["SIMPASAR_LLM"] = "off"

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from sim import sectors  # noqa: E402

# Cache harga diarahkan ke folder sementara supaya hasil tidak tergantung cache lokal pengembang.
sectors.CACHE_DIR = Path(tempfile.mkdtemp(prefix="simpasar-api-cache-")) / "sectors"
sectors._price_cache.clear()

MAX_MESSAGES = 50


def recv_until(ws, predicate, what: str) -> dict:
    """Baca pesan WS sampai `predicate(msg)` benar (maks MAX_MESSAGES) — loop simulasi mengirim delta di antaranya."""
    for _ in range(MAX_MESSAGES):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError(f"tidak menemukan pesan: {what}")


def test_rest(client: TestClient) -> None:
    r = client.get("/api/stocks/search", params={"q": "bank bca"})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "bank bca" and body["results"][0]["symbol"] == "BBCA", body["results"][:2]
    assert body["count"] == len(body["results"]) <= 12

    r = client.get("/api/stocks/search", params={"q": "mandri", "limit": "3"})
    assert r.status_code == 200 and len(r.json()["results"]) == 3 and "BMRI" in [x["symbol"] for x in r.json()["results"]]
    r = client.get("/api/stocks/search", params={"q": "", "limit": "abc"})
    assert r.status_code == 200 and r.json()["results"][0]["symbol"] == "IHSG"
    r = client.get("/api/stocks/search", params={"q": "<script>" * 40, "limit": "999"})
    assert r.status_code == 200 and len(r.json()["results"]) <= 30
    r = client.get("/api/stocks/search")
    assert r.status_code == 200 and r.json()["results"][0]["symbol"] == "IHSG"

    r = client.get("/api/stocks/BBCA/price")
    assert r.status_code == 200, r.text
    price = r.json()
    assert price["status"] == "offline" and price["price"] is None and price["history"] == [], price
    assert price["symbol"] == "BBCA" and price["name"] == "PT Bank Central Asia Tbk."
    assert "message" in price

    r = client.get("/api/stocks/bbca.jk/price")
    assert r.status_code == 200 and r.json()["symbol"] == "BBCA"

    r = client.get("/api/stocks/ZZZZ/price")
    assert r.status_code == 404, r.text
    assert r.json()["status"] == "unknown" and r.json()["price"] is None
    r = client.get("/api/stocks/%3Cscript%3E/price")
    assert r.status_code == 404 and r.json()["status"] == "unknown"

    r = client.get("/api/stocks/IHSG/price")
    assert r.status_code == 200 and r.json()["kind"] == "index" and r.json()["price"] is not None

    r = client.get("/api/stocks")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 962 and len(data["companies"]) == 962 and "financials" in data["sectors"]
    assert set(data["companies"][0]) == {"symbol", "name", "sector"}

    r = client.get("/api/state")
    assert r.status_code == 200
    state = r.json()
    assert state["snapshot"] is True and "price_history" in state and "symbol" in state and "sim_time" in state
    assert state["speed"] == {"mult": server.speed_mult, "tf": server.candle_minutes} and "source_kind" in state["symbol"]
    assert state["symbol"]["symbol"] in ("IHSG", "BBCA", "TLKM")
    assert state["sim_time"]["minutes_per_day"] == 330
    assert "limits" in state and state["limits"]["rule"] in ("nominal", "percent", "index")

    r = client.get("/api/ihsg")
    assert r.status_code == 200 and r.json()["price"]
    r = client.get("/api/news")
    assert r.status_code == 200 and isinstance(r.json(), list) and r.json()
    r = client.get("/simulator")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_websocket(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first.get("snapshot") is True and "price_history" in first, first.keys()
        # Simulator dimulai di BBCA (bukan IHSG). Offline tanpa cache → nilai cadangan server.
        assert first["symbol"]["symbol"] == "BBCA" and first["symbol"]["kind"] == "stock" and "sim_time" in first
        assert first["symbol"]["name"] == "PT Bank Central Asia Tbk."
        assert first["symbol"]["source_price"] == server.START_FALLBACK_PRICE == 6200.0
        assert first["symbol"]["source_kind"] == "fallback"   # offline: nilai cadangan, bukan harga Sectors
        assert first["symbol"]["source"] == server.START_FALLBACK_SOURCE
        assert first["fundamental"] == 6200.0 and first["price_history"][0] == 6200.0
        assert first["speed"] == {"mult": 1.0, "tf": 5}
        # Batas ARA/ARB BBCA dari acuan 6200 (band >Rp5.000: +20% / −15%).
        lim = first["limits"]
        assert lim["applies"] is True and lim["ref"] == 6200.0 and lim["band"] == ">Rp5.000", lim
        assert (lim["ara"], lim["arb"], lim["ara_pct"], lim["arb_pct"]) == (7440.0, 5270.0, 0.2, 0.15), lim
        assert lim["day"] == 1 and lim["hit"] in (None, "ara", "arb")

        # Delta juga membawa limits.
        delta = recv_until(ws, lambda m: "event" not in m and m.get("snapshot") is False, "delta BBCA awal")
        assert delta["limits"]["ara"] == 7440.0 and 5270.0 <= delta["price"] <= 7440.0

        # Simbol tak dikenal → symbol_error ke pengirim.
        ws.send_json({"cmd": "set_symbol", "symbol": "ZZZZ"})
        err = recv_until(ws, lambda m: m.get("event") == "symbol_error", "symbol_error ZZZZ")
        assert err["symbol"] == "ZZZZ" and err["message"] == "Simbol tidak dikenal"

        # Klien sudah punya harga → set_symbol langsung sukses.
        ws.send_json({"cmd": "set_symbol", "symbol": "BBCA", "fundamental": 6625})
        changed = recv_until(ws, lambda m: m.get("event") == "symbol_changed", "symbol_changed BBCA")
        assert changed["symbol"]["symbol"] == "BBCA" and changed["fundamental"] == 6625.0, changed
        assert changed["symbol"]["kind"] == "stock" and changed["symbol"]["sector"] == "financials"
        assert changed["symbol"]["source_price"] == 6625.0
        assert changed["symbol"]["source_kind"] == "manual"   # tanpa data Sectors yang cocok

        snap = recv_until(ws, lambda m: m.get("snapshot") is True and m.get("fundamental") == 6625.0,
                          "snapshot BBCA 6625")
        assert snap["limits"]["ref"] == 6625.0 and snap["limits"]["ara"] == 7950.0
        assert snap["fundamental"] == 6625.0 and snap["price_history"][0] == 6625.0, snap["fundamental"]
        assert snap["sim_time"]["tick"] == snap["tick"]

        # Delta per tick: tanpa histori, snapshot false.
        delta = recv_until(ws, lambda m: "event" not in m and m.get("snapshot") is False, "delta tick")
        assert "price_history" not in delta and delta["symbol"]["symbol"] == "BBCA"
        assert delta["speed"] == {"mult": 1.0, "tf": 5} and delta["symbol"]["source_kind"] == "manual"
        # Satu pesan per candle 5 menit (bawaan): harga tiap menit ada di batch_prices dan berakhir di batas candle.
        assert 1 <= len(delta["batch_prices"]) <= 5 and len(delta["batch_volumes"]) == len(delta["batch_prices"])
        assert (delta["tick"] % 330) % 5 == 4, delta["tick"]

        # Tanpa fundamental & Sectors offline → symbol_error dengan price_status, simbol TIDAK berubah.
        ws.send_json({"cmd": "set_symbol", "symbol": "TLKM"})
        err = recv_until(ws, lambda m: m.get("event") == "symbol_error" and m.get("symbol") == "TLKM",
                         "symbol_error TLKM")
        assert err["price_status"] == "offline" and "Sectors" in err["message"], err
        assert server.market.symbol["symbol"] == "BBCA"

        # get_state → snapshot penuh; reset → snapshot penuh (broadcast).
        ws.send_json({"cmd": "get_state"})
        full = recv_until(ws, lambda m: m.get("snapshot") is True, "get_state snapshot")
        assert "price_history" in full and full["symbol"]["symbol"] == "BBCA"
        ws.send_json({"cmd": "reset"})
        reset_snap = recv_until(ws, lambda m: m.get("snapshot") is True and m.get("tick") == 0, "reset snapshot")
        assert reset_snap["price_history"] == [6625.0] and reset_snap["symbol"]["symbol"] == "BBCA"

        # Perintah rusak tidak memutus koneksi.
        ws.send_text("bukan json")
        ws.send_json(["bukan", "dict"])
        ws.send_json({"cmd": "inject_rumor", "strength": "abc"})
        err = recv_until(ws, lambda m: m.get("event") == "error", "event error")
        assert err["cmd"] == "inject_rumor"
        ws.send_json({"cmd": "pause"})
        recv_until(ws, lambda m: m.get("event") == "paused", "paused")
        ws.send_json({"cmd": "resume"})
        recv_until(ws, lambda m: m.get("event") == "resumed", "resumed")
        ws.send_json({"cmd": "set_fundamental", "fundamental": 7000})
        snap = recv_until(ws, lambda m: m.get("snapshot") is True and m.get("fundamental") == 7000.0, "set_fundamental")
        assert snap["price_history"] == [7000.0]
        assert snap["symbol"]["source_price"] == 7000.0 and snap["symbol"]["source_kind"] == "manual"

        # F16: harga awal minimum 1 — pesan error menyebut batas baru.
        ws.send_json({"cmd": "set_fundamental", "fundamental": 0.5})
        err = recv_until(ws, lambda m: m.get("event") == "error" and m.get("cmd") == "set_fundamental", "error 0,5")
        assert "harga awal harus angka antara 1 dan 1.000.000.000" in err["message"], err
        ws.send_json({"cmd": "set_symbol", "symbol": "TLKM", "fundamental": 0.5})
        err = recv_until(ws, lambda m: m.get("event") == "symbol_error", "symbol_error 0,5")
        assert "harga awal harus angka antara 1 dan 1.000.000.000" in err["message"], err
        assert server.market.fundamental == 7000.0
        ws.send_json({"cmd": "set_fundamental", "fundamental": 1})
        snap = recv_until(ws, lambda m: m.get("snapshot") is True and m.get("fundamental") == 1.0, "fundamental 1")
        assert snap["price_history"] == [1.0]


def test_speed_in_state(client: TestClient) -> None:
    """Kecepatan {mult, tf} di setiap state + event speed_changed ke semua klien (1x = 1 candle/detik)."""
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        snap = a.receive_json()
        assert snap["snapshot"] is True and snap["speed"] == {"mult": 1.0, "tf": 5}
        b.receive_json()
        a.send_json({"cmd": "set_speed", "mult": 2, "tf": 5})
        for ws in (a, b):
            ev = recv_until(ws, lambda m: m.get("event") == "speed_changed", "speed_changed")
            assert ev["speed"] == {"mult": 2.0, "tf": 5}, ev
            delta = recv_until(ws, lambda m: "event" not in m and m.get("snapshot") is False and m["speed"]["tf"] == 5, "delta 2x")
            assert (delta["tick"] % 330) % 5 == 4 and len(delta["batch_prices"]) <= 5
        assert client.get("/api/state").json()["speed"] == {"mult": 2.0, "tf": 5}
        b.send_json({"cmd": "get_state"})
        snap = recv_until(b, lambda m: m.get("snapshot") is True, "snapshot 2x")
        assert snap["speed"] == {"mult": 2.0, "tf": 5}

        # Pengali di luar 0,5–5x di-clamp ke tombol terdekat; timeframe tidak dikenal / nilai rusak → error.
        a.send_json({"cmd": "set_speed", "mult": 50, "tf": 30})
        ev = recv_until(a, lambda m: m.get("event") == "speed_changed", "speed_changed clamp")
        assert ev["speed"] == {"mult": 5.0, "tf": 30} and server.speed_mult == 5.0
        for bad in ({"mult": 1, "tf": 7}, {"mult": "cepat", "tf": 15}, {"mult": 1, "tf": "15"}):
            a.send_json({"cmd": "set_speed", **bad})
            recv_until(a, lambda m: m.get("event") == "error" and m.get("cmd") == "set_speed", f"error set_speed {bad}")
        assert (server.speed_mult, server.candle_minutes) == (5.0, 30)
        # Klien lama: {interval} (1x lama = 0,25 s) → pengali terdekat, timeframe tetap.
        a.send_json({"cmd": "set_speed", "interval": 0.125})
        recv_until(a, lambda m: m.get("event") == "speed_changed" and m["speed"] == {"mult": 2.0, "tf": 30}, "legacy 2x")
        a.send_json({"cmd": "set_speed", "mult": 1, "tf": 5})
        recv_until(a, lambda m: m.get("event") == "speed_changed" and m["speed"] == {"mult": 1.0, "tf": 5}, "speed 1x")
    assert (server.speed_mult, server.candle_minutes) == (1.0, 5)


def test_rate_limit_replies_and_get_state_exempt(client: TestClient) -> None:
    """F15: perintah yang dibuang limiter dibalas error (maks 1/detik); get_state tidak pernah dibuang."""
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["snapshot"] is True
        for _ in range(server.WS_MAX_CMDS_PER_SEC + 10):
            ws.send_json({"cmd": "inject_rumor", "strength": 0})
        ws.send_json({"cmd": "get_state"})
        limit_errors = 0
        for _ in range(MAX_MESSAGES):
            msg = ws.receive_json()
            if msg.get("event") == "error":
                assert msg["cmd"] == "inject_rumor" and msg["message"] == server.WS_RATE_LIMIT_MESSAGE, msg
                limit_errors += 1
            if msg.get("snapshot") is True:
                break
        else:
            raise AssertionError("get_state saat limiter habis tidak dibalas snapshot")
        assert limit_errors == 1, limit_errors                  # banyak perintah dibuang, satu balasan

        # Banyak get_state beruntun tetap dibalas (digabung), bukan dibuang.
        for _ in range(40):
            ws.send_json({"cmd": "get_state"})
        recv_until(ws, lambda m: m.get("snapshot") is True, "snapshot setelah banjir get_state")

        # Setelah token terisi lagi, perintah biasa diproses kembali.
        time.sleep(1.1)
        ws.send_json({"cmd": "pause"})
        recv_until(ws, lambda m: m.get("event") == "paused", "paused setelah limiter pulih")
        ws.send_json({"cmd": "resume"})
        recv_until(ws, lambda m: m.get("event") == "resumed", "resumed")


def test_population_broadcast(client: TestClient) -> None:
    """F17: set_population / set_psych mengirim snapshot penuh ke SEMUA klien, bukan hanya pengirim."""
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        a.receive_json()
        b.receive_json()
        a.send_json({"cmd": "set_population", "fundamentalist": 0.6, "chartist": 0.2, "noise": 0.2})
        for ws in (a, b):
            snap = recv_until(ws, lambda m: m.get("snapshot") is True and m["params"]["f_ratio"] == 0.6,
                              "snapshot populasi")
            assert snap["params"]["c_ratio"] == 0.2 and "price_history" in snap and "tick_interval" in snap
        b.send_json({"cmd": "set_psych", "disciplined": 0.5, "bagholder": 0.2})
        for ws in (a, b):
            snap = recv_until(ws, lambda m: m.get("snapshot") is True and m["params"]["d_ratio"] == 0.5,
                              "snapshot psikologi")
            assert snap["params"]["b_ratio"] == 0.2
        a.send_json({"cmd": "set_population", "fundamentalist": 0.3, "chartist": 0.5, "noise": 0.2})
        recv_until(b, lambda m: m.get("snapshot") is True and m["params"]["f_ratio"] == 0.3, "populasi default")


def test_source_kind(client: TestClient) -> None:
    """symbol.source_kind: sectors / cache / stale / manual / fallback (kontrak §2)."""
    sectors._price_cache.clear()
    sectors._last_price_status.clear()
    original_call, original_online = sectors.call_sectors_mcp, sectors.sectors_online
    api_up = {"value": True}
    calls: list[str] = []

    def rows_for(sym: str, close: float) -> list[dict]:
        return [{"symbol": sym, "date": "2026-09-21", "close": close - 50},
                {"symbol": sym, "date": "2026-09-22", "close": close}]

    async def fake_call(tool: str, args: dict):
        calls.append(f"{tool}:{args.get('symbol')}")
        if not api_up["value"] or tool != "fetch-daily-price":
            return None
        return rows_for(args["symbol"], {"TLKM": 3000.0, "ASII": 5000.0}.get(args["symbol"], 1000.0))

    def set_symbol(ws, payload: dict) -> dict:
        ws.send_json({"cmd": "set_symbol", **payload})
        changed = recv_until(ws, lambda m: m.get("event") in ("symbol_changed", "symbol_error"), f"set_symbol {payload}")
        assert changed["event"] == "symbol_changed", changed
        snap = recv_until(ws, lambda m: m.get("snapshot") is True, "snapshot setelah set_symbol")
        assert snap["symbol"] == changed["symbol"]              # event datang sebelum snapshot-nya
        return changed["symbol"]

    sectors.call_sectors_mcp = fake_call
    sectors.sectors_online = lambda: True
    try:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            # Server mengambil harga sendiri (tanpa fundamental dari klien).
            sym = set_symbol(ws, {"symbol": "TLKM"})
            assert sym["source_kind"] == "sectors" and sym["source_price"] == 3000.0 and sym["source_date"] == "2026-09-22"
            sym = set_symbol(ws, {"symbol": "TLKM"})
            assert sym["source_kind"] == "cache" and calls.count("fetch-daily-price:TLKM") == 1

            # Alur dashboard: REST dulu lalu set_symbol dengan harga itu.
            price = client.get("/api/stocks/ASII/price").json()
            assert price["status"] == "success" and price["source_kind"] == "sectors"
            sym = set_symbol(ws, {"symbol": "ASII", "fundamental": price["price"]})
            assert sym["source_kind"] == "sectors" and sym["source_date"] == "2026-09-22"
            price = client.get("/api/stocks/ASII/price").json()
            assert price["status"] == "cache" and price["source_kind"] == "cache"
            sym = set_symbol(ws, {"symbol": "ASII", "fundamental": price["price"]})
            assert sym["source_kind"] == "cache"

            # Harga berbeda dari data Sectors → isian manual.
            sym = set_symbol(ws, {"symbol": "ASII", "fundamental": 4321})
            assert sym["source_kind"] == "manual" and sym["source_date"] is None and "manual" in sym["source"].lower()

            # Cache kedaluwarsa + API gagal → stale.
            sectors._price_cache["TLKM"]["fetched_at"] -= sectors.PRICE_CACHE_TTL + 60
            sectors._write_disk_cache("TLKM", sectors._price_cache["TLKM"])
            api_up["value"] = False
            sym = set_symbol(ws, {"symbol": "TLKM"})
            assert sym["source_kind"] == "stale" and sym["source_price"] == 3000.0
            price = client.get("/api/stocks/TLKM/price").json()
            assert price["stale"] is True and price["source_kind"] == "stale" and price["message"]
            sym = set_symbol(ws, {"symbol": "TLKM", "fundamental": 3000})
            assert sym["source_kind"] == "stale"

            # IHSG offline → nilai cadangan (fallback), baik diambil server maupun dikirim klien.
            sectors._ihsg_cache = {}
            sectors._ihsg_failed_at = 0.0
            sym = set_symbol(ws, {"symbol": "IHSG"})
            assert sym["source_kind"] == "fallback" and sym["kind"] == "index"
            assert server.market.get_state(full=False)["limits"]["applies"] is False   # indeks tanpa ARA/ARB
            price = client.get("/api/stocks/IHSG/price").json()
            assert price["status"] == "fallback" and price["source_kind"] == "fallback"
            sym = set_symbol(ws, {"symbol": "IHSG", "fundamental": price["price"]})
            assert sym["source_kind"] == "fallback"
    finally:
        sectors.call_sectors_mcp, sectors.sectors_online = original_call, original_online
        sectors._price_cache.clear()
        sectors._last_price_status.clear()


def test_start_symbol() -> None:
    """Startup: harga BBCA dari cache Sectors (tanpa API); SIMPASAR_START_SYMBOL; nilai tidak dikenal → BBCA."""
    saved_env = os.environ.pop("SIMPASAR_START_SYMBOL", None)
    sectors._price_cache.clear()
    sectors._last_price_status.clear()
    try:
        assert server._start_symbol() == "BBCA"
        os.environ["SIMPASAR_START_SYMBOL"] = "zzzz"
        assert server._start_symbol() == "BBCA"
        os.environ["SIMPASAR_START_SYMBOL"] = "tlkm.jk"
        assert server._start_symbol() == "TLKM"
        os.environ.pop("SIMPASAR_START_SYMBOL")

        # Cache harga BBCA segar (seperti hasil fetch-daily-price sebelumnya) → dipakai saat startup.
        sectors._price_cache["BBCA"] = {
            "fetched_at": time.time(), "symbol": "BBCA", "source": sectors.PRICE_SOURCE,
            "rows": [{"date": "2026-09-21", "close": 6225.0}, {"date": "2026-09-22", "close": 6250.0}],
        }
        with TestClient(server.app) as client, client.websocket_connect("/ws") as ws:
            snap = ws.receive_json()
            assert snap["symbol"]["symbol"] == "BBCA" and snap["fundamental"] == 6250.0, snap["symbol"]
            assert snap["symbol"]["source_kind"] == "cache" and snap["symbol"]["source_date"] == "2026-09-22"
            assert snap["limits"]["ref"] == 6250.0 and snap["limits"]["ara"] == 7500.0 and snap["limits"]["arb"] == 5313.0

        # IHSG sebagai simbol awal (offline → nilai cadangan IHSG), tanpa batas ARA/ARB.
        os.environ["SIMPASAR_START_SYMBOL"] = "IHSG"
        with TestClient(server.app) as client:
            state = client.get("/api/state").json()
            assert state["symbol"]["symbol"] == "IHSG" and state["symbol"]["kind"] == "index"
            assert state["symbol"]["source_kind"] == "fallback" and state["limits"]["applies"] is False
    finally:
        os.environ.pop("SIMPASAR_START_SYMBOL", None)
        if saved_env is not None:
            os.environ["SIMPASAR_START_SYMBOL"] = saved_env
        sectors._price_cache.clear()
        sectors._last_price_status.clear()
        server.market.set_symbol(server._fallback_start_meta(), server.START_FALLBACK_PRICE)


if __name__ == "__main__":
    test_start_symbol()
    print("ok  test_start_symbol")
    with TestClient(server.app) as client:
        test_rest(client)
        print("ok  test_rest")
        test_websocket(client)
        print("ok  test_websocket")
        test_speed_in_state(client)
        print("ok  test_speed_in_state")
        test_rate_limit_replies_and_get_state_exempt(client)
        print("ok  test_rate_limit_replies_and_get_state_exempt")
        test_population_broadcast(client)
        print("ok  test_population_broadcast")
        test_source_kind(client)
        print("ok  test_source_kind")
    print("SEMUA TES api LULUS")

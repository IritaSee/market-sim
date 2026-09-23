"""
Uji protokol state sim/market.py — jalankan: python tests/test_market_protocol.py
snapshot penuh vs delta, set_symbol, sim_time, MAX_HISTORY 2000, source_kind,
harga minimum 1 dengan lantai harga relatif (F16).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SIMPASAR_LLM"] = "off"          # tanpa Gemini: tes harus cepat & deterministik

from sim.market import (  # noqa: E402
    DEFAULT_SYMBOL, MAX_HISTORY, MIN_FUNDAMENTAL, PRICE_FLOOR_RATIO, Market, valid_fundamental,
)
from sim.simtime import MINUTES_PER_DAY  # noqa: E402

BBCA_META = {
    "symbol": "BBCA", "name": "PT Bank Central Asia Tbk.", "sector": "financials",
    "sector_name": "Keuangan", "kind": "stock",
    "source_price": 6625.0, "source_date": "2026-09-07", "source": "Sectors Financial API (fetch-daily-price)",
}


def test_constants_and_default_symbol() -> None:
    assert MAX_HISTORY == 2000
    m = Market(n_agents=20, seed=1)
    assert m.symbol["symbol"] == "IHSG" and m.symbol["kind"] == "index"
    assert m.symbol["name"] == "Indeks Harga Saham Gabungan" and m.symbol["sector_name"] == "Indeks"
    assert set(DEFAULT_SYMBOL) == {"symbol", "name", "sector", "sector_name", "kind",
                                   "source_price", "source_date", "source", "source_kind"}
    assert m.symbol["source_kind"] is None                  # default awal: belum ada data


def test_delta_state_has_no_history() -> None:
    m = Market(n_agents=20, seed=1)
    s = m.get_state(full=False)
    assert "price_history" not in s and "volume_history" not in s, s.keys()
    assert s["snapshot"] is False
    for key in ("tick", "price", "fundamental", "status", "sentiment", "paused", "llm",
                "volume", "agents", "params", "psych_stats", "symbol", "sim_time"):
        assert key in s, key
    d = m.step()
    assert d["snapshot"] is False and d["tick"] == 1 and "price_history" not in d
    json.dumps(d)     # harus bisa diserialisasi


def test_full_state() -> None:
    m = Market(n_agents=20, seed=1)
    for _ in range(5):
        m.step()
    s = m.get_state(full=True)
    assert s["snapshot"] is True
    assert s["price_history"] and len(s["price_history"]) == 6 == len(s["volume_history"])
    assert s["price_history"][-1] == s["price"]
    assert s["symbol"]["symbol"] == "IHSG" and s["sim_time"]["tick"] == 5
    assert s["sim_time"]["time"] == "09:05" and s["sim_time"]["session"] == 1
    assert m.get_state()["snapshot"] is True    # default full=True
    json.dumps(s)


def test_set_symbol_resets() -> None:
    m = Market(n_agents=20, seed=1)
    for _ in range(10):
        m.step()
    assert m.tick == 10
    m.set_symbol(BBCA_META, 6625.0)
    assert m.symbol["symbol"] == "BBCA" and m.symbol["kind"] == "stock"
    assert m.symbol["source_price"] == 6625.0 and m.symbol["source_date"] == "2026-09-07"
    assert m.tick == 0 and m.fundamental == 6625.0 and m.price == 6625.0
    assert m.price_history == [6625.0] and m.volume_history == [0.0]
    s = m.get_state(full=True)
    assert s["symbol"]["symbol"] == "BBCA" and s["fundamental"] == 6625.0 and s["tick"] == 0
    assert s["sim_time"]["time"] == "09:00" and s["sim_time"]["day"] == 1

    # Meta minimal (tanpa source_*) → source_price diisi fundamental.
    m.set_symbol({"symbol": "tlkm", "name": "PT Telkom Indonesia (Persero) Tbk", "sector": "infrastructures",
                  "sector_name": "Infrastruktur", "kind": "stock"}, 3000)
    assert m.symbol["symbol"] == "TLKM" and m.symbol["source_price"] == 3000 and m.symbol["source_date"] is None
    assert m.symbol["source_kind"] == "manual"              # tanpa info asal → dianggap isian manual
    assert m.get_state(full=False)["symbol"]["source_kind"] == "manual"

    # source_kind eksplisit dipertahankan; nilai asing dibuang (None).
    m.set_symbol({**BBCA_META, "source_kind": "cache"}, 6625.0)
    assert m.symbol["source_kind"] == "cache"
    m.set_symbol({**BBCA_META, "source_kind": "<script>"}, 6625.0)
    assert m.symbol["source_kind"] is None

    # Fundamental tidak valid diabaikan (state tidak rusak).
    before = m.fundamental
    m.set_fundamental(-5)
    m.set_fundamental("abc")  # type: ignore[arg-type]
    assert m.fundamental == before


def test_history_capped_and_simtime_advances() -> None:
    m = Market(n_agents=10, seed=3)
    for _ in range(MINUTES_PER_DAY + 5):
        m.step()
    s = m.get_state(full=True)
    assert s["sim_time"]["day"] == 2 and s["sim_time"]["time"] == "09:05", s["sim_time"]
    for _ in range(2000):
        m.step()
    s = m.get_state(full=True)
    assert len(s["price_history"]) == MAX_HISTORY == len(s["volume_history"])
    assert s["tick"] == MINUTES_PER_DAY + 5 + 2000
    m.reset()
    assert m.tick == 0 and len(m.price_history) == 1 and m.get_state()["sim_time"]["time"] == "09:00"


def test_min_price_and_relative_floor() -> None:
    """F16: harga awal minimum 1; lantai harga relatif (0,5% fundamental), bukan 0,5 absolut."""
    assert MIN_FUNDAMENTAL == 1.0 and PRICE_FLOOR_RATIO == 0.005
    assert valid_fundamental(1) == 1.0 and valid_fundamental(0.99) is None and valid_fundamental(0.5) is None
    m = Market(n_agents=100, seed=1)
    assert m.set_fundamental(0.5) is False and m.fundamental == 100.0
    assert m.set_fundamental(1) is True

    statuses: dict[str, int] = {}
    prices: list[float] = []
    for _ in range(600):
        s = m.step()
        statuses[s["status"]] = statuses.get(s["status"], 0) + 1
        prices.append(m.price)
    # Dulu: harga melompat ke 0,5 di atas fundamental kecil lalu "Bubble" permanen.
    assert statuses.get("Normal", 0) > 300, statuses
    assert statuses.get("Bubble", 0) < 300, statuses
    assert min(prices) >= 1 * PRICE_FLOOR_RATIO and max(prices) < 5, (min(prices), max(prices))

    # Lantai relatif: sentimen panik ekstrem tidak menembus 0,5% fundamental dan tidak pernah 0/negatif.
    m = Market(n_agents=100, seed=2, fundamental=1.0)
    for _ in range(400):
        m.inject_panic(3.0)
        m.step()
        assert m.price >= m.fundamental * PRICE_FLOOR_RATIO - 1e-12


def test_paused_step_is_delta() -> None:
    m = Market(n_agents=10, seed=3)
    m.pause()
    d = m.step()
    assert d["paused"] is True and d["snapshot"] is False and d["tick"] == 0
    m.resume()
    assert m.step()["tick"] == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("SEMUA TES market_protocol LULUS")

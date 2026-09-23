"""
Uji batas Auto Rejection BEI (ARA/ARB) — jalankan: python tests/test_price_limits.py

  - tabel batas per band harga (nominal Rp1 untuk acuan Rp1–Rp10, persen untuk sisanya),
    aritmetika bilangan bulat (tanpa galat float);
  - IHSG (indeks) tidak dibatasi;
  - Market: harga saham tidak pernah menembus ARA/ARB, "hit" menandai harga yang terkunci;
  - pergantian hari bursa (330 tick): harga acuan = penutupan hari sebelumnya;
  - field "limits" ada di snapshot penuh & delta dan lolos JSON ketat.
Mode offline: tidak ada kredit Sectors yang terpakai.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SECTORS_API_KEY"] = "offline"
os.environ["SIMPASAR_LLM"] = "off"

from sim.idx_rules import price_limits  # noqa: E402
from sim.market import Market  # noqa: E402
from sim.simtime import MINUTES_PER_DAY  # noqa: E402

BBCA_META = {"symbol": "BBCA", "name": "PT Bank Central Asia Tbk.", "sector": "financials",
             "sector_name": "Keuangan", "kind": "stock"}
LIMIT_KEYS = {"applies", "ref", "ara", "arb", "ara_pct", "arb_pct", "rule", "band"}

# acuan → (ARA, ARB, rule, band, ara_pct)
BOUNDARIES = {
    5:    (6,    4,    "nominal", "Rp1–Rp10",       None),
    1:    (2,    1,    "nominal", "Rp1–Rp10",       None),
    10:   (11,   9,    "nominal", "Rp1–Rp10",       None),
    11:   (14,   10,   "percent", "Rp11–Rp200",     0.35),
    200:  (270,  170,  "percent", "Rp11–Rp200",     0.35),
    201:  (251,  171,  "percent", ">Rp200–Rp5.000", 0.25),
    5000: (6250, 4250, "percent", ">Rp200–Rp5.000", 0.25),
    5001: (6001, 4251, "percent", ">Rp5.000",       0.20),
    6200: (7440, 5270, "percent", ">Rp5.000",       0.20),
    6213: (7455, 5282, "percent", ">Rp5.000",       0.20),
}


def strict_json(obj: dict) -> dict:
    """Serialisasi + parse seketat JSON.parse browser (NaN/Infinity ditolak)."""
    def reject(const: str):
        raise ValueError(f"literal {const} tidak valid di JSON")
    return json.loads(json.dumps(obj, allow_nan=False), parse_constant=reject)


def half_up(x: float) -> int:
    return max(1, int(math.floor(x + 0.5)))


def stock_market(price: float, seed: int = 1) -> Market:
    m = Market(n_agents=100, seed=seed, fundamental=price)
    assert m.set_symbol(BBCA_META, price)
    return m


def test_boundary_table() -> None:
    for ref, (ara, arb, rule, band, ara_pct) in BOUNDARIES.items():
        lim = price_limits(ref)
        assert set(lim) == LIMIT_KEYS, lim
        assert lim["applies"] is True and lim["ref"] == float(ref), (ref, lim)
        assert (lim["ara"], lim["arb"]) == (ara, arb), (ref, lim)
        assert lim["rule"] == rule and lim["band"] == band and lim["ara_pct"] == ara_pct, (ref, lim)
        assert lim["arb_pct"] == (None if rule == "nominal" else 0.15), (ref, lim)
        assert isinstance(lim["ara"], float) and isinstance(lim["arb"], float)
    # Contoh pemilik produk: acuan Rp5 → ARA Rp6, ARB Rp4 (nominal Rp1, bukan persen).
    rp5 = price_limits(5)
    assert (rp5["ref"], rp5["ara"], rp5["arb"]) == (5.0, 6.0, 4.0)
    # Acuan dibulatkan ke rupiah terdekat (.5 ke atas), minimal Rp1.
    assert price_limits(10.5)["ref"] == 11.0 and price_limits(10.5)["rule"] == "percent"
    assert price_limits(10.49)["ref"] == 10.0 and price_limits(10.49)["rule"] == "nominal"
    assert price_limits(6212.5)["ref"] == 6213.0 and price_limits(6212.5)["ara"] == 7455.0
    assert price_limits(0.3)["ref"] == 1.0 and price_limits(0.3)["arb"] == 1.0
    for bad in (float("nan"), float("inf"), None, "abc"):
        strict_json(price_limits(bad))
    print("ok  test_boundary_table")


def test_integer_arithmetic_matches_definition() -> None:
    """ARA = acuan + floor(acuan × pct), ARB = ceil(acuan × 0,85) — untuk semua acuan 11..20000."""
    for acuan in range(11, 20001):
        lim = price_limits(acuan)
        pct = 35 if acuan <= 200 else 25 if acuan <= 5000 else 20
        assert lim["ara"] == acuan + (acuan * pct) // 100, acuan
        assert lim["arb"] == -((-acuan * 85) // 100), acuan          # ceil tanpa float
        assert lim["arb"] < acuan < lim["ara"], acuan
    for acuan in range(1, 11):
        lim = price_limits(acuan)
        assert lim["ara"] == acuan + 1 and lim["arb"] == max(1, acuan - 1)
    print("ok  test_integer_arithmetic_matches_definition")


def test_index_not_limited() -> None:
    lim = price_limits(7123.456, "index")
    assert lim["applies"] is False and lim["rule"] == "index" and lim["band"] == "Indeks"
    assert lim["ref"] == 7123.46 and lim["ara"] is None and lim["arb"] is None
    assert lim["ara_pct"] is None and lim["arb_pct"] is None

    # Market default (IHSG) tidak pernah di-clamp: rumor kuat bisa melewati +20% dalam sehari.
    m = Market(n_agents=100, seed=1, fundamental=6200.0)
    assert m.symbol["kind"] == "index"
    highest = 0.0
    for _ in range(200):
        m.inject_rumor(3.0)
        m.step()
        highest = max(highest, m.price)
    s = m.get_state(full=False)
    assert s["limits"]["applies"] is False and s["limits"]["hit"] is None
    assert highest > price_limits(6200.0)["ara"], highest
    print("ok  test_index_not_limited")


def test_market_clamps_to_ara() -> None:
    m = stock_market(6200.0)
    assert m.ref_price == 6200.0
    pinned = 0
    for _ in range(200):
        m.inject_rumor(3.0)
        m.step()
        lim = m.get_state(full=False)["limits"]
        assert m.price <= lim["ara"] + 1e-9 and m.price >= lim["arb"] - 1e-9, (m.price, lim)
        if m.price >= lim["ara"] - 1e-9:
            assert lim["hit"] == "ara", lim
            pinned += 1
    assert pinned > 0 and m.price == 7440.0, (pinned, m.price)
    print("ok  test_market_clamps_to_ara")


def test_market_clamps_to_arb() -> None:
    for start, arb in ((6200.0, 5270.0), (150.0, 128.0), (5.0, 4.0)):
        m = stock_market(start, seed=2)
        hit = False
        for _ in range(200):
            m.inject_panic(3.0)
            m.step()
            lim = m.get_state(full=False)["limits"]
            assert lim["arb"] == arb and m.price >= arb - 1e-9, (start, m.price, lim)
            if lim["hit"] == "arb":
                hit = True
        assert hit and m.price == arb, (start, m.price)
    print("ok  test_market_clamps_to_arb")


def test_day_rollover_sets_reference() -> None:
    m = stock_market(6200.0, seed=3)
    close_day1 = None
    for _ in range(MINUTES_PER_DAY + 40):
        m.inject_rumor(0.6)
        m.step()
        if m.tick < MINUTES_PER_DAY:
            assert m.get_state(full=False)["limits"]["ref"] == 6200.0
            assert m.get_state(full=False)["limits"]["day"] == 1
        if m.tick == MINUTES_PER_DAY - 1:
            close_day1 = m.price
    assert close_day1 is not None
    s = m.get_state(full=False)
    assert s["limits"]["day"] == 2 == s["sim_time"]["day"]
    assert s["limits"]["ref"] == float(half_up(close_day1)), (s["limits"], close_day1)
    assert s["limits"] == {**price_limits(close_day1), "hit": s["limits"]["hit"], "day": 2}

    # Reset / set_fundamental / set_symbol mengembalikan acuan ke harga awal.
    m.reset()
    assert m.ref_price == m.fundamental == 6200.0 and m.get_state()["limits"]["ref"] == 6200.0
    m.set_fundamental(150.0)
    assert m.ref_price == 150.0 and m.get_state()["limits"]["band"] == "Rp11–Rp200"
    m.set_symbol(BBCA_META, 6625.0)
    assert m.ref_price == 6625.0 and m.get_state()["limits"]["ara"] == 7950.0
    print("ok  test_day_rollover_sets_reference")


def test_paused_step_keeps_limits() -> None:
    m = stock_market(6200.0)
    for _ in range(MINUTES_PER_DAY - 1):
        m.step()
    m.pause()
    d = m.step()                                     # paused: tidak maju, acuan tidak berganti
    assert d["tick"] == MINUTES_PER_DAY - 1 and d["limits"]["ref"] == 6200.0 and d["limits"]["day"] == 1
    m.resume()
    d = m.step()
    assert d["tick"] == MINUTES_PER_DAY and d["limits"]["day"] == 2
    print("ok  test_paused_step_keeps_limits")


def test_limits_in_full_and_delta_state() -> None:
    m = stock_market(6200.0)
    for _ in range(5):
        m.step()
    for full in (True, False):
        s = strict_json(m.get_state(full=full))
        lim = s["limits"]
        assert set(lim) == LIMIT_KEYS | {"hit", "day"}, lim
        assert lim["applies"] is True and lim["ref"] == 6200.0 and lim["ara"] == 7440.0 and lim["arb"] == 5270.0
        assert lim["hit"] in (None, "ara", "arb") and lim["day"] == 1
    d = strict_json(m.step())
    assert "limits" in d and d["snapshot"] is False
    idx = strict_json(Market(n_agents=10, seed=1).get_state(full=True))["limits"]
    assert idx["applies"] is False and idx["hit"] is None and idx["day"] == 1
    print("ok  test_limits_in_full_and_delta_state")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("SEMUA TES price_limits LULUS")

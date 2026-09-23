"""
Uji sim/simtime.py — jalankan: python tests/test_simtime.py
Sesi BEI: 09:00–12:00 & 13:30–16:00 (330 menit/hari), kalender melewati akhir pekan.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sim.simtime import (  # noqa: E402
    MINUTES_PER_DAY, MONTHS_ID_SHORT, WEEKDAYS_ID, add_trading_days, next_trading_day,
    sim_time_info, tick_to_clock,
)


def test_constants() -> None:
    assert MINUTES_PER_DAY == 330
    assert WEEKDAYS_ID == ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
    assert MONTHS_ID_SHORT == ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def test_clock_boundaries() -> None:
    start = date(2026, 9, 21)  # Senin
    info = sim_time_info(0, start)
    assert info["time"] == "09:00" and info["day"] == 1 and info["session"] == 1, info
    assert info["date"] == "2026-09-21" and info["weekday"] == "Senin", info

    assert sim_time_info(179, start)["time"] == "11:59"
    assert sim_time_info(179, start)["session"] == 1
    assert sim_time_info(180, start)["time"] == "13:30"
    assert sim_time_info(180, start)["session"] == 2
    assert sim_time_info(329, start)["time"] == "15:59"
    assert sim_time_info(329, start)["day"] == 1

    day2 = sim_time_info(330, start)
    assert day2["time"] == "09:00" and day2["day"] == 2 and day2["session"] == 1, day2
    assert day2["date"] == "2026-09-22" and day2["weekday"] == "Selasa", day2

    assert tick_to_clock(209) == (0, 13, 59, 2)
    assert tick_to_clock(210) == (0, 14, 0, 2)
    assert tick_to_clock(60) == (0, 10, 0, 1)


def test_weekend_skip() -> None:
    start = date(2026, 9, 25)  # Jumat
    d1 = sim_time_info(0, start)
    d2 = sim_time_info(330, start)      # hari bursa berikutnya = Senin 28 Sep
    assert d1["weekday"] == "Jumat" and d1["date"] == "2026-09-25", d1
    assert d2["weekday"] == "Senin" and d2["date"] == "2026-09-28", d2
    assert d2["date_label"] == "28 Sep 2026"
    assert d2["label"].startswith("Sen 28 Sep · 09:00 · Sesi 1 · Hari 2"), d2["label"]

    # Tanggal awal jatuh di akhir pekan → digeser ke Senin.
    assert next_trading_day(date(2026, 9, 26)) == date(2026, 9, 28)
    assert next_trading_day(date(2026, 9, 27)) == date(2026, 9, 28)
    assert sim_time_info(0, date(2026, 9, 26))["weekday"] == "Senin"

    # 10 hari bursa dari Senin 21 Sep = Senin 5 Okt (dua akhir pekan dilewati).
    assert add_trading_days(date(2026, 9, 21), 10) == date(2026, 10, 5)
    far = sim_time_info(330 * 10, date(2026, 9, 21))
    assert far["date"] == "2026-10-05" and far["day"] == 11 and far["date_label"] == "5 Okt 2026", far


def test_robust_inputs() -> None:
    assert sim_time_info(-5, date(2026, 9, 21))["tick"] == 0
    assert sim_time_info("abc", date(2026, 9, 21))["time"] == "09:00"  # type: ignore[arg-type]
    assert sim_time_info(None)["day"] == 1  # type: ignore[arg-type]
    info = sim_time_info(15)                 # start_date default = hari ini (hari kerja)
    assert info["time"] == "09:15" and info["minutes_per_day"] == 330


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("SEMUA TES simtime LULUS")

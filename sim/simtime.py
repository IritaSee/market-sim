"""
Waktu simulasi — 1 tick = 1 menit waktu bursa.

Sesi perdagangan BEI yang dipakai (tanpa hari libur nasional):
    Sesi 1 : 09:00–12:00  (180 menit)
    Sesi 2 : 13:30–16:00  (150 menit)
    Total  : 330 menit per hari bursa

Kalender maju satu hari bursa setiap 330 tick dan melewati Sabtu/Minggu.
Modul ini tanpa dependensi luar; klien (frontend/app.js) mengimplementasikan
fungsi `tick_to_clock` yang identik supaya label sumbu-X chart sama persis
dengan `sim_time` yang dikirim server.
"""
from __future__ import annotations

from datetime import date, timedelta

SESSION1_MINUTES: int = 180          # 09:00–12:00
SESSION2_MINUTES: int = 150          # 13:30–16:00
MINUTES_PER_DAY: int = SESSION1_MINUTES + SESSION2_MINUTES   # 330

SESSION1_OPEN = (9, 0)
SESSION2_OPEN = (13, 30)

# Nama hari & bulan dalam Bahasa Indonesia (indeks mengikuti date.weekday(): 0 = Senin).
WEEKDAYS_ID: tuple[str, ...] = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
WEEKDAYS_ID_SHORT: tuple[str, ...] = ("Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min")
MONTHS_ID_SHORT: tuple[str, ...] = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
                                    "Jul", "Agu", "Sep", "Okt", "Nov", "Des")


def _safe_tick(tick: int | float | None) -> int:
    """Tick negatif / bukan angka dianggap 0 supaya fungsi tidak pernah melempar."""
    try:
        t = int(tick)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return t if t > 0 else 0


def is_trading_day(d: date) -> bool:
    """True bila bukan Sabtu/Minggu (hari libur nasional diabaikan)."""
    return d.weekday() < 5


def next_trading_day(d: date) -> date:
    """Geser ke hari kerja berikutnya bila `d` jatuh pada Sabtu/Minggu; selain itu kembalikan `d`."""
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def add_trading_days(start: date, n: int) -> date:
    """Tanggal hari bursa ke-`n` (0 = `start` itu sendiri) setelah `start`, melewati akhir pekan."""
    d = next_trading_day(start)
    if n <= 0:
        return d
    # Lompat per minggu penuh (5 hari bursa = 7 hari kalender) lalu sisanya satu-satu.
    weeks, rest = divmod(n, 5)
    d += timedelta(days=weeks * 7)
    for _ in range(rest):
        d += timedelta(days=1)
        d = next_trading_day(d)
    return d


def tick_to_clock(tick: int) -> tuple[int, int, int, int]:
    """
    Ubah tick menjadi (day_index, jam, menit, sesi).

    day_index dimulai dari 0. Menit ke-0..179 berada di Sesi 1 (mulai 09:00),
    menit ke-180..329 berada di Sesi 2 (mulai 13:30).
    """
    t = _safe_tick(tick)
    day_index, m = divmod(t, MINUTES_PER_DAY)
    if m < SESSION1_MINUTES:
        hh = SESSION1_OPEN[0] + m // 60
        mm = m % 60
        session = 1
    else:
        m2 = m - SESSION1_MINUTES
        total = SESSION2_OPEN[1] + m2
        hh = SESSION2_OPEN[0] + total // 60
        mm = total % 60
        session = 2
    return day_index, hh, mm, session


def format_date_id(d: date, with_year: bool = True) -> str:
    """'22 Sep 2026' (atau '22 Sep' bila with_year=False)."""
    base = f"{d.day} {MONTHS_ID_SHORT[d.month - 1]}"
    return f"{base} {d.year}" if with_year else base


def default_start_date(today: date | None = None) -> date:
    """Tanggal awal simulasi: hari ini, digeser ke hari kerja berikutnya bila akhir pekan."""
    return next_trading_day(today or date.today())


def sim_time_info(tick: int, start_date: date | None = None) -> dict:
    """
    Ringkasan waktu simulasi untuk dikirim ke klien.

    Contoh: {"tick": 15, "day": 1, "date": "2026-09-22", "weekday": "Senin",
             "weekday_short": "Sen", "date_label": "22 Sep 2026", "time": "09:15",
             "session": 1, "minutes_per_day": 330, "minute_of_day": 15,
             "label": "Sen 22 Sep · 09:15 · Sesi 1 · Hari 1"}
    """
    t = _safe_tick(tick)
    start = default_start_date() if start_date is None else next_trading_day(start_date)
    day_index, hh, mm, session = tick_to_clock(t)
    d = add_trading_days(start, day_index)
    time_str = f"{hh:02d}:{mm:02d}"
    weekday_short = WEEKDAYS_ID_SHORT[d.weekday()]
    return {
        "tick":            t,
        "day":             day_index + 1,
        "date":            d.isoformat(),
        "weekday":         WEEKDAYS_ID[d.weekday()],
        "weekday_short":   weekday_short,
        "date_label":      format_date_id(d),
        "time":            time_str,
        "session":         session,
        "minute_of_day":   t % MINUTES_PER_DAY,
        "minutes_per_day": MINUTES_PER_DAY,
        "label":           f"{weekday_short} {format_date_id(d, with_year=False)} · {time_str} · Sesi {session} · Hari {day_index + 1}",
    }

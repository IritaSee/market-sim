"""
Aturan bursa BEI yang dipakai simulasi — batas Auto Rejection (ARA/ARB).

ARA = Auto Rejection Atas (batas kenaikan harga harian), ARB = Auto Rejection Bawah
(batas penurunan harian). Batas dihitung dari harga acuan (penutupan hari bursa
sebelumnya; hari pertama simulasi = harga awal dari Sectors):

    Harga acuan          ARA                 ARB
    Rp1–Rp10             acuan + Rp1         acuan − Rp1 (minimal Rp1)
    Rp11–Rp200           +35%                −15%
    >Rp200–Rp5.000       +25%                −15%
    >Rp5.000             +20%                −15%

Contoh: acuan Rp5 → ARA Rp6, ARB Rp4 (nominal Rp1, bukan persentase).
Indeks (IHSG) tidak punya batas ARA/ARB.

Semua perhitungan saham memakai bilangan bulat rupiah supaya tidak ada galat pembulatan
float (mis. 6213 × 1,2 = 7455,599… tetap 7455): ARA = acuan + floor(acuan × pct),
ARB = acuan − floor(acuan × 15%) (= ceil(acuan × 0,85)). Fraksi harga (tick size) tidak
diterapkan.
"""
from __future__ import annotations

import math
from typing import Any

ARB_PCT_INT = 15   # ARB −15% untuk semua band persentase

# (batas atas acuan inklusif, label band, ARA % bulat). None = tanpa batas atas.
PERCENT_BANDS: tuple[tuple[int | None, str, int], ...] = (
    (200,  "Rp11–Rp200",      35),
    (5000, ">Rp200–Rp5.000",  25),
    (None, ">Rp5.000",        20),
)
NOMINAL_MAX = 10                 # acuan ≤ Rp10 → batas nominal Rp1
NOMINAL_BAND = "Rp1–Rp10"
INDEX_BAND = "Indeks"


def _round_half_up(value: float) -> int:
    """Pembulatan ke rupiah terdekat, .5 ke atas (round() Python membulatkan ke genap)."""
    return int(math.floor(value + 0.5))


def reference_price(ref: Any) -> int:
    """Harga acuan saham dalam rupiah bulat (minimal Rp1). Nilai rusak/non-finite → Rp1."""
    try:
        f = float(ref)
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(f):
        return 1
    return max(1, _round_half_up(f))


def price_limits(ref: Any, kind: str | None = "stock") -> dict:
    """
    Batas ARA/ARB untuk harga acuan `ref`.

    Mengembalikan dict (selalu JSON ketat, tanpa NaN/inf):
        applies  bool        False untuk indeks
        ref      float       acuan bulat (saham) / ref dibulatkan 2 desimal (indeks)
        ara/arb  float|None  harga batas atas/bawah (None untuk indeks)
        ara_pct  float|None  0.35/0.25/0.20 (None untuk band nominal & indeks)
        arb_pct  float|None  0.15 (None untuk band nominal & indeks)
        rule     "nominal" | "percent" | "index"
        band     "Rp1–Rp10" | "Rp11–Rp200" | ">Rp200–Rp5.000" | ">Rp5.000" | "Indeks"
    """
    if kind == "index":
        try:
            f = float(ref)
        except (TypeError, ValueError):
            f = 0.0
        return {
            "applies": False,
            "ref":     round(f, 2) if math.isfinite(f) else 0.0,
            "ara":     None,
            "arb":     None,
            "ara_pct": None,
            "arb_pct": None,
            "rule":    "index",
            "band":    INDEX_BAND,
        }

    acuan = reference_price(ref)
    if acuan <= NOMINAL_MAX:
        return {
            "applies": True,
            "ref":     float(acuan),
            "ara":     float(acuan + 1),
            "arb":     float(max(1, acuan - 1)),
            "ara_pct": None,
            "arb_pct": None,
            "rule":    "nominal",
            "band":    NOMINAL_BAND,
        }

    for upper, band, ara_pct_int in PERCENT_BANDS:
        if upper is None or acuan <= upper:
            break
    ara = acuan + (acuan * ara_pct_int) // 100
    arb = acuan - (acuan * ARB_PCT_INT) // 100
    return {
        "applies": True,
        "ref":     float(acuan),
        "ara":     float(ara),
        "arb":     float(arb),
        "ara_pct": ara_pct_int / 100,
        "arb_pct": ARB_PCT_INT / 100,
        "rule":    "percent",
        "band":    band,
    }


def limit_hit(price: float, limits: dict, tol: float = 1e-9) -> str | None:
    """"ara" bila harga menyentuh batas atas, "arb" bila menyentuh batas bawah, selain itu None."""
    if not limits.get("applies"):
        return None
    ara, arb = limits.get("ara"), limits.get("arb")
    if ara is not None and price >= ara - tol:
        return "ara"
    if arb is not None and price <= arb + tol:
        return "arb"
    return None

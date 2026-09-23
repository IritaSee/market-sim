"""
Uji sim/stocks.py — jalankan: python tests/test_stocks.py
Dataset 962 emiten, pencarian ticker/alias/nama/sektor/fuzzy, validasi alias,
tingkat skor (alias persis / nama lengkap persis), NFKC, awalan bursa "IDX:" (F18).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sim.stocks import (  # noqa: E402
    ALIASES_FILE, DEFAULT_SYMBOLS, INDEX_SYMBOL, POPULAR_SYMBOLS, get_company, get_symbol_meta,
    is_valid_symbol_format, load_aliases, load_companies, normalize_name_tokens, search_stocks,
)


def symbols(results: list[dict]) -> list[str]:
    return [r["symbol"] for r in results]


def test_dataset_loaded() -> None:
    data = load_companies()
    assert data["count"] == 962, data["count"]
    assert len(data["companies"]) == 962
    assert len(data["sectors"]) == 11
    assert all(len(c["symbol"]) == 4 and c["symbol"].isupper() for c in data["companies"])


def test_get_company() -> None:
    c = get_company("BBCA")
    assert c and c["name"] == "PT Bank Central Asia Tbk." and c["sector"] == "financials"
    assert c["sector_name"] == "Keuangan" and c["kind"] == "stock"
    assert get_company("bbca.jk")["symbol"] == "BBCA"
    assert get_company(" tlkm ")["symbol"] == "TLKM"
    assert get_company("ZZZZ") is None
    assert get_company(None) is None
    assert get_company(123) is None
    assert get_symbol_meta("ihsg")["kind"] == "index"
    assert get_symbol_meta("IHSG")["symbol"] == INDEX_SYMBOL
    assert get_symbol_meta("BMRI")["kind"] == "stock"
    assert is_valid_symbol_format("BBCA") and is_valid_symbol_format("ihsg")
    assert not is_valid_symbol_format("BB") and not is_valid_symbol_format("BBCA1") and not is_valid_symbol_format("")


def test_normalize_name() -> None:
    assert normalize_name_tokens("PT Bank Central Asia Tbk.") == ["bank", "central", "asia"]
    assert normalize_name_tokens("PT Bank Mandiri (Persero) Tbk") == ["bank", "mandiri"]
    assert normalize_name_tokens("Indah Kiat Pulp & Paper Tbk") == ["indah", "kiat", "pulp", "paper"]


def test_ticker_and_alias() -> None:
    r = search_stocks("BBCA")
    assert r[0]["symbol"] == "BBCA" and r[0]["score"] == 1.0 and r[0]["match"] == "ticker", r[0]
    assert search_stocks("bbca")[0]["symbol"] == "BBCA"
    assert search_stocks("bbca.jk")[0]["symbol"] == "BBCA"

    r = search_stocks("bank bca")
    assert r[0]["symbol"] == "BBCA", symbols(r)
    assert search_stocks("bca")[0]["symbol"] == "BBCA"      # alias persis mengalahkan prefix ticker BCAP
    assert search_stocks("bri")[0]["symbol"] == "BBRI"
    assert search_stocks("bni")[0]["symbol"] == "BBNI"
    assert search_stocks("mandiri")[0]["symbol"] == "BMRI"
    assert search_stocks("bank mandiri")[0]["symbol"] == "BMRI"
    assert search_stocks("telkom")[0]["symbol"] == "TLKM"
    assert search_stocks("gojek")[0]["symbol"] == "GOTO"
    assert search_stocks("tokopedia")[0]["symbol"] == "GOTO"
    assert search_stocks("chandra asri")[0]["symbol"] == "TPIA"
    assert search_stocks("adaro")[0]["symbol"] == "ADRO"
    assert search_stocks("xl")[0]["symbol"] == "EXCL"
    assert search_stocks("indosat")[0]["symbol"] == "ISAT"
    assert search_stocks("astra")[0]["symbol"] == "ASII"

    # Prefix ticker: "BB" → bank-bank besar BB* (populer di depan).
    r = search_stocks("bb")
    assert all(s.startswith("BB") for s in symbols(r)), symbols(r)
    assert symbols(r)[0] == "BBCA"


def test_name_tokens() -> None:
    r = search_stocks("bank central")
    assert r[0]["symbol"] == "BBCA", symbols(r)
    r = search_stocks("sido muncul")
    assert r[0]["symbol"] == "SIDO"
    r = search_stocks("unilever")
    assert r[0]["symbol"] == "UNVR"
    r = search_stocks("kalbe farma")
    assert r[0]["symbol"] == "KLBF"


def test_sector_keywords() -> None:
    r = search_stocks("bank")
    assert len(r) >= 10, len(r)
    for item in r:
        assert item["sector"] == "financials" or "bank" in item["name"].lower(), item

    r = search_stocks("batu bara")
    assert "PTBA" in symbols(r), symbols(r)
    assert all(x["sector"] == "energy" for x in r), symbols(r)

    r = search_stocks("rumah sakit")
    assert r and all(x["sector"] == "healthcare" for x in r), symbols(r)
    assert "SILO" in symbols(r) or "MIKA" in symbols(r) or "HEAL" in symbols(r)

    r = search_stocks("telekomunikasi")
    assert r and all(x["sector"] == "infrastructures" for x in r)


def test_fuzzy_typo() -> None:
    r = search_stocks("mandri")
    assert "BMRI" in symbols(r)[:3], symbols(r)
    r = search_stocks("telkm")
    assert "TLKM" in symbols(r)[:3], symbols(r)
    r = search_stocks("unilver")
    assert "UNVR" in symbols(r)[:3], symbols(r)
    r = search_stocks("bank mandri")
    assert r[0]["symbol"] == "BMRI", symbols(r)
    assert all(0.3 <= x["score"] <= 0.64 for x in search_stocks("telkm") if x["match"] == "fuzzy")


def test_index_entry() -> None:
    r = search_stocks("ihsg")
    assert r[0]["symbol"] == "IHSG" and r[0]["kind"] == "index" and r[0]["sector"] == "index", r[0]
    assert r[0]["name"] == "Indeks Harga Saham Gabungan" and r[0]["sector_name"] == "Indeks"
    for q in ("composite", "indeks", "index", "gabungan"):
        assert "IHSG" in symbols(search_stocks(q)), q
    assert all(x["kind"] == "stock" for x in search_stocks("bank"))


def test_empty_query_default() -> None:
    r = search_stocks("")
    assert r[0]["symbol"] == "IHSG", symbols(r)
    assert symbols(r)[1:] == list(DEFAULT_SYMBOLS)[:len(r) - 1], symbols(r)
    assert len(r) == 12
    assert search_stocks(None)[0]["symbol"] == "IHSG"  # type: ignore[arg-type]
    assert search_stocks("   ")[0]["symbol"] == "IHSG"


def test_result_shape_and_order() -> None:
    for q in ("bank", "bb", "batu bara", "mandri", ""):
        r = search_stocks(q, 30)
        seen = set()
        for item in r:
            for key in ("symbol", "name", "sector", "sector_name", "kind", "score", "match"):
                assert key in item, (q, key, item)
            assert item["symbol"] not in seen, (q, item["symbol"])
            seen.add(item["symbol"])
        scores = [x["score"] for x in r]
        assert scores == sorted(scores, reverse=True), (q, scores)
        for a, b in zip(r, r[1:]):
            if a["score"] == b["score"]:
                assert a["symbol"] < b["symbol"], (q, a["symbol"], b["symbol"])
    assert len(search_stocks("bank", 5)) == 5
    assert len(search_stocks("bank", 100)) == 30
    assert len(search_stocks("bank", 0)) == 1
    assert len(search_stocks("bank", "x")) == 12  # type: ignore[arg-type]


def test_weird_inputs_never_raise() -> None:
    weird = ["<script>alert(1)</script>", "x" * 500, "😀😀 bank", "'; DROP TABLE emiten;--",
             "\x00\x01", "bank\nbca", "   ", "ÅÆØ", "...", "&&&&", "----", "12345", "a" * 64 + "b" * 64]
    for q in weird:
        r = search_stocks(q)
        assert isinstance(r, list), q
        assert len(r) <= 12
    for q in (None, 123, 4.5, ["bank"], {"q": "bca"}, b"bbca", object()):
        assert isinstance(search_stocks(q), list), q  # type: ignore[arg-type]
    assert isinstance(search_stocks("bank", None), list)  # type: ignore[arg-type]


def test_aliases_valid() -> None:
    data = load_companies()
    known = {c["symbol"] for c in data["companies"]}
    with ALIASES_FILE.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    assert isinstance(raw, dict) and len(raw) >= 120, len(raw)
    for alias, target in raw.items():
        assert target in known, f"alias {alias!r} → {target} tidak ada di idx_companies.json"
        assert alias == alias.strip() and alias == alias.lower(), alias
    loaded = load_aliases()
    assert len(loaded) == len(raw), (len(loaded), len(raw))
    # Alias contoh dari kontrak.
    for alias, target in {"bca": "BBCA", "bri": "BBRI", "mandiri": "BMRI", "bni": "BBNI", "telkom": "TLKM",
                          "astra": "ASII", "gojek": "GOTO", "tokopedia": "GOTO", "chandra asri": "TPIA",
                          "adaro": "ADRO", "alamtri": "ADRO", "adaro andalan": "AADI", "xl": "EXCL",
                          "xl axiata": "EXCL", "xlsmart": "EXCL", "smartfren": "EXCL", "indosat": "ISAT"}.items():
        assert loaded.get(alias) == target, (alias, loaded.get(alias))
    for sym in POPULAR_SYMBOLS:
        assert sym in known, f"POPULAR_SYMBOLS memuat simbol tak dikenal: {sym}"


def test_exact_matches_beat_popularity_bonus() -> None:
    """F18: kecocokan persis tidak boleh kalah dari kecocokan superset emiten populer."""
    # "merdeka gold" adalah alias persis EMAS; MDKA (populer) hanya cocok lewat frasa "merdeka" + "gold".
    r = search_stocks("merdeka gold")
    assert r[0]["symbol"] == "EMAS" and r[0]["match"] == "alias", symbols(r)
    assert r[0]["score"] > next(x["score"] for x in r if x["symbol"] == "MDKA")

    # Setiap alias sehari-hari menjadi hasil teratas untuk target-nya sendiri.
    misses = [(alias, target, symbols(search_stocks(alias))[:2])
              for alias, target in load_aliases().items() if search_stocks(alias)[0]["symbol"] != target]
    assert not misses, misses[:10]

    # Nama lengkap persis mengalahkan emiten lain yang namanya memuat semua token query.
    r = search_stocks("PT Bank Pembangunan Daerah Banten Tbk.")
    assert r[0]["symbol"] == "BEKS" and r[0]["match"] == "name", symbols(r)
    r = search_stocks("Duta Pertiwi Tbk")
    assert r[0]["symbol"] == "DUTI", symbols(r)
    r = search_stocks("PT Bank Central Asia Tbk.")
    assert r[0]["symbol"] == "BBCA", symbols(r)

    # Jalur frasa alias tetap bekerja ("bank bca", "saham telkom").
    assert search_stocks("saham telkom")[0]["symbol"] == "TLKM"

    # Bonus popularitas tidak pernah membuat tingkat bertukar.
    from sim.stocks import (POPULARITY_BONUS_MAX, SCORE_ALIAS_EXACT, SCORE_ALIAS_PHRASE, SCORE_ALIAS_PREFIX,
                            SCORE_NAME_EXACT, SCORE_NAME_PREFIX, SCORE_TICKER_PREFIX)
    assert SCORE_TICKER_PREFIX + POPULARITY_BONUS_MAX < SCORE_ALIAS_EXACT
    assert SCORE_ALIAS_PHRASE + POPULARITY_BONUS_MAX < SCORE_ALIAS_EXACT
    assert SCORE_ALIAS_PREFIX + POPULARITY_BONUS_MAX < SCORE_TICKER_PREFIX
    assert SCORE_NAME_EXACT + POPULARITY_BONUS_MAX < SCORE_ALIAS_PREFIX
    assert SCORE_NAME_PREFIX + POPULARITY_BONUS_MAX < SCORE_NAME_EXACT


def test_unicode_and_exchange_prefix() -> None:
    """F18: NFKC ("ＢＢＣＡ" lebar penuh) dan awalan bursa "IDX:BBCA"."""
    r = search_stocks("ＢＢＣＡ")
    assert r[0]["symbol"] == "BBCA" and r[0]["match"] == "ticker", symbols(r)
    assert search_stocks("ｔｅｌｋｏｍ")[0]["symbol"] == "TLKM"
    for q in ("IDX:BBCA", "idx:bbca", "IDX: BBCA", "BEI:BBCA", "JK:BBCA"):
        r = search_stocks(q)
        assert r[0]["symbol"] == "BBCA" and r[0]["score"] == 1.0, (q, symbols(r))
    assert search_stocks("idx:tlkm")[0]["symbol"] == "TLKM"
    # "idx" sendiri tetap memunculkan IHSG.
    assert search_stocks("idx")[0]["symbol"] == INDEX_SYMBOL
    assert get_company("ＢＢＣＡ")["symbol"] == "BBCA"
    assert get_symbol_meta("IDX:BBCA")["symbol"] == "BBCA"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("SEMUA TES stocks LULUS")

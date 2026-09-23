"""
Daftar emiten BEI & pencarian simbol (ticker, nama sehari-hari, sektor, toleransi typo).

Sumber data: sim/data/idx_companies.json (snapshot Sectors, 962 emiten, 11 sektor IDX-IC)
dan sim/data/idx_aliases.json (nama sehari-hari -> ticker, mis. "bca" -> BBCA).

Aturan skor `search_stocks` (prioritas menurun):
    ticker persis            1.00   tanpa bonus
    alias persis             0.95   tanpa bonus (di atas prefix ticker: "bca" harus BBCA, bukan BCAP;
                                    "merdeka gold" harus EMAS, bukan MDKA lewat frasa "merdeka" + "gold")
    ticker prefix            0.90
    frasa alias + kata sisa  0.90   mis. "bank bca": alias "bca" + "bank" ada di nama BBCA
    alias prefix             0.85
    nama lengkap persis      0.80   "PT Bank Pembangunan Daerah Banten Tbk." -> BEKS
    token nama (prefix)      0.70   semua token query jadi prefix token nama yang dinormalisasi
    kata kunci sektor        0.50
    fuzzy (difflib)          0.30–0.60  ratio >= 0.72 terhadap ticker / token nama / alias
Emiten populer mendapat bonus kecil (<= +0.04) supaya BBCA muncul di atas BBHI untuk "bb",
dan PTBA masuk di daftar "batu bara". Jarak antar-tingkat 0,05 > bonus maksimum dan kecocokan
persis (ticker/alias) tidak diberi bonus, sehingga bonus tidak pernah membuat tingkat bertukar
(kecuali sektor dan fuzzy, yang memang saling menyisip).
Teks dinormalisasi NFKC ("ＢＢＣＡ" -> "bbca"); awalan bursa "IDX:" / "BEI:" / "JK:" dibuang.

Semua fungsi publik aman dipanggil dengan input apa pun (tidak melempar exception).
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
COMPANIES_FILE = DATA_DIR / "idx_companies.json"
ALIASES_FILE = DATA_DIR / "idx_aliases.json"

INDEX_SYMBOL = "IHSG"
INDEX_ENTRY: dict[str, Any] = {
    "symbol":      INDEX_SYMBOL,
    "name":        "Indeks Harga Saham Gabungan",
    "sector":      "index",
    "sector_name": "Indeks",
    "kind":        "index",
}
# Query yang memunculkan entri IHSG.
_INDEX_KEYWORDS = {"ihsg", "composite", "indeks", "index", "gabungan", "jci", "idx"}

# Emiten populer (urutan ≈ kapitalisasi / likuiditas). Dipakai untuk daftar default dan bonus urutan.
DEFAULT_SYMBOLS: tuple[str, ...] = ("BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII",
                                    "GOTO", "TPIA", "BREN", "AMMN", "UNVR")
POPULAR_SYMBOLS: tuple[str, ...] = DEFAULT_SYMBOLS + (
    "BRPT", "DSSA", "BYAN", "CUAN", "DCII", "PANI", "ICBP", "INDF", "ADRO", "AADI", "PTBA",
    "ITMG", "ANTM", "INCO", "MDKA", "MBMA", "NCKL", "PGAS", "MEDC", "AKRA", "UNTR", "SMGR",
    "INTP", "KLBF", "SIDO", "MIKA", "HEAL", "SILO", "CPIN", "JPFA", "MYOR", "GGRM", "HMSP",
    "ISAT", "EXCL", "TOWR", "TBIG", "MTEL", "JSMR", "PGEO", "BRIS", "ARTO", "BBTN", "BNGA",
    "BDMN", "NISP", "MEGA", "BJBR", "BJTM", "BTPS", "BSDE", "CTRA", "SMRA", "PWON", "AMRT",
    "MAPI", "MAPA", "ACES", "ERAA", "BUKA", "EMTK", "SCMA", "MNCN", "SRTG", "BUMI", "BRMS",
    "ENRG", "HRUM", "INDY", "ESSA", "TINS", "PTRO", "RAJA", "TOBA", "ELSA", "WIKA", "PTPP",
    "ADHI", "WSKT", "GIAA", "BIRD", "SMDR", "TMAS", "AALI", "LSIP", "TAPG", "AUTO", "KAEF",
    "LPKR", "KIJA", "BFIN", "ADMF", "PNBN", "SUPA", "BBHI", "BBYB", "INKP", "TKIM", "CMRY",
    "ULTJ", "ROTI", "MTDL", "WIFI", "SSIA", "KRAS", "SMCB",
)
POPULARITY_BONUS_MAX = 0.04

# Kata yang tidak informatif dalam nama emiten / query.
_NAME_STOPWORDS = {"pt", "tbk", "persero", "tbk."}
_QUERY_FILLERS = {"pt", "tbk", "persero", "saham", "emiten", "perusahaan", "ticker", "kode"}

MAX_QUERY_LEN = 64
_ALLOWED_RE = re.compile(r"[^a-z0-9 .&\-]+")
_SPLIT_RE = re.compile(r"[\s.&\-/,()]+")
_FUZZY_MIN_RATIO = 0.72
_FUZZY_MIN_TOKEN = 4
# Awalan bursa gaya TradingView/Bloomberg: "IDX:BBCA", "BEI: TLKM", "JK:ASII".
_EXCHANGE_PREFIX_RE = re.compile(r"^\s*(?:idx|bei|jk|jkse)\s*:\s*", re.IGNORECASE)

# Skor per tingkat (lihat docstring modul).
SCORE_TICKER_EXACT  = 1.00
SCORE_ALIAS_EXACT   = 0.95
SCORE_TICKER_PREFIX = 0.90
SCORE_ALIAS_PHRASE  = 0.90
SCORE_ALIAS_PREFIX  = 0.85
SCORE_NAME_EXACT    = 0.80
SCORE_NAME_PREFIX   = 0.70
SCORE_SECTOR        = 0.50

_companies_cache: dict[str, Any] | None = None
_index_cache: dict[str, Any] | None = None


# ------------------------------------------------------------------ #
#  Normalisasi                                                         #
# ------------------------------------------------------------------ #

def _nfkc(text: str) -> str:
    """Normalisasi Unicode NFKC: huruf lebar penuh ("ＢＢＣＡ") dan ligatur jadi ASCII biasa."""
    try:
        return unicodedata.normalize("NFKC", text)
    except Exception:
        return text


def _clean_text(text: str) -> str:
    """NFKC, huruf kecil, hanya a-z 0-9 spasi . & -, spasi ganda dirapikan."""
    t = _nfkc(str(text)).lower().replace("_", " ")
    t = _ALLOWED_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def normalize_name_tokens(name: str) -> list[str]:
    """
    Token nama emiten yang sudah dinormalisasi:
    buang "PT", "Tbk", "Tbk.", "(Persero)", tanda baca; huruf kecil.
    "PT Bank Central Asia Tbk." -> ["bank", "central", "asia"]
    """
    cleaned = _clean_text(name)
    return [tok for tok in _SPLIT_RE.split(cleaned) if tok and tok not in _NAME_STOPWORDS]


def sanitize_query(q: Any) -> str:
    """Potong 64 karakter, hanya huruf/angka/spasi/./&/-; selalu mengembalikan str."""
    if q is None:
        return ""
    try:
        text = q if isinstance(q, str) else str(q)
    except Exception:
        return ""
    text = _EXCHANGE_PREFIX_RE.sub("", _nfkc(text[:MAX_QUERY_LEN]))
    return _clean_text(text)


def normalize_symbol(symbol: Any) -> str:
    """'bbca.jk ' -> 'BBCA'; nilai bukan string -> ''."""
    if symbol is None:
        return ""
    try:
        s = _nfkc(str(symbol)).strip().upper()
    except Exception:
        return ""
    if "." in s:
        s = s.split(".", 1)[0]
    if ":" in s:                    # mis. "IDX:BBCA"
        s = s.rsplit(":", 1)[-1]
    return re.sub(r"[^A-Z0-9]", "", s)


# ------------------------------------------------------------------ #
#  Pemuatan data (cache modul)                                         #
# ------------------------------------------------------------------ #

def load_companies() -> dict[str, Any]:
    """Baca sim/data/idx_companies.json sekali; hasil di-cache di modul."""
    global _companies_cache
    if _companies_cache is None:
        with COMPANIES_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("sectors", {})
        data.setdefault("companies", [])
        data["count"] = len(data["companies"])
        _companies_cache = data
    return _companies_cache


def load_aliases() -> dict[str, str]:
    """Alias nama sehari-hari -> ticker. Kunci dinormalisasi; target yang tidak ada di daftar dibuang."""
    return dict(_index()["aliases"])


def sector_name(slug: str) -> str:
    """Nama sektor Bahasa Indonesia dari slug; slug tak dikenal dikembalikan apa adanya."""
    if slug == "index":
        return INDEX_ENTRY["sector_name"]
    info = load_companies()["sectors"].get(slug) or {}
    return info.get("name_id") or info.get("name_en") or str(slug)


def _index() -> dict[str, Any]:
    """Bangun indeks pencarian sekali: token nama, alias, kata kunci sektor, peringkat populer."""
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    data = load_companies()
    by_symbol: dict[str, dict[str, Any]] = {}
    tokens: dict[str, list[str]] = {}
    for c in data["companies"]:
        sym = normalize_symbol(c.get("symbol"))
        if not sym:
            continue
        entry = {
            "symbol":      sym,
            "name":        str(c.get("name", "")).strip(),
            "sector":      str(c.get("sector", "")),
            "sector_name": sector_name(str(c.get("sector", ""))),
            "kind":        "stock",
        }
        by_symbol[sym] = entry
        tokens[sym] = normalize_name_tokens(entry["name"])

    aliases: dict[str, str] = {}
    try:
        with ALIASES_FILE.open(encoding="utf-8") as fh:
            raw_aliases = json.load(fh)
    except (OSError, json.JSONDecodeError):
        raw_aliases = {}
    if isinstance(raw_aliases, dict):
        for key, target in raw_aliases.items():
            if str(key).startswith("_"):
                continue
            k = _clean_text(key)
            t = normalize_symbol(target)
            if k and t in by_symbol:
                aliases[k] = t
    # alias -> daftar token (untuk pencocokan frasa di dalam query)
    alias_tokens = {k: k.split(" ") for k in aliases}

    sector_keywords: dict[str, list[list[str]]] = {}
    for slug, info in data["sectors"].items():
        kws: list[list[str]] = []
        for kw in (info or {}).get("keywords", []):
            kk = _clean_text(kw)
            if kk:
                kws.append(kk.split(" "))
        for extra in ((info or {}).get("name_id"), (info or {}).get("name_en")):
            kk = _clean_text(extra or "")
            if kk:
                kws.append(kk.split(" "))
        sector_keywords[slug] = kws

    popularity: dict[str, float] = {}
    n_pop = len(POPULAR_SYMBOLS)
    for rank, sym in enumerate(POPULAR_SYMBOLS):
        if sym in by_symbol:
            popularity[sym] = round(POPULARITY_BONUS_MAX * (1.0 - rank / n_pop), 4)

    # Kandidat fuzzy per emiten: token nama + ticker, dan potongan alias (>= 4 huruf).
    alias_parts: dict[str, list[str]] = {}
    for alias, target in aliases.items():
        for part in alias.split(" "):
            if len(part) >= _FUZZY_MIN_TOKEN:
                alias_parts.setdefault(target, []).append(part)
    fuzzy_cands = {sym: (toks + [sym.lower()], alias_parts.get(sym, []))
                   for sym, toks in tokens.items()}

    _index_cache = {
        "by_symbol":       by_symbol,
        "tokens":          tokens,
        "aliases":         aliases,
        "alias_tokens":    alias_tokens,
        "sector_keywords": sector_keywords,
        "popularity":      popularity,
        "fuzzy_cands":     fuzzy_cands,
    }
    return _index_cache


# ------------------------------------------------------------------ #
#  Akses per simbol                                                    #
# ------------------------------------------------------------------ #

def get_company(symbol: Any) -> dict[str, Any] | None:
    """
    Metadata emiten {"symbol","name","sector","sector_name","kind":"stock"} atau None.
    Case-insensitive; menerima "BBCA.JK" -> "BBCA".
    """
    try:
        sym = normalize_symbol(symbol)
        entry = _index()["by_symbol"].get(sym)
        return dict(entry) if entry else None
    except Exception:
        return None


def get_symbol_meta(symbol: Any) -> dict[str, Any] | None:
    """Seperti get_company, tetapi juga mengenali IHSG (kind 'index')."""
    sym = normalize_symbol(symbol)
    if sym == INDEX_SYMBOL:
        return dict(INDEX_ENTRY)
    return get_company(sym)


def is_valid_symbol_format(symbol: Any) -> bool:
    """4 huruf kapital (ticker BEI) atau IHSG."""
    sym = normalize_symbol(symbol)
    return sym == INDEX_SYMBOL or bool(re.fullmatch(r"[A-Z]{4}", sym))


def list_companies() -> list[dict[str, str]]:
    """Daftar ringkas {"symbol","name","sector"} untuk prefetch klien."""
    return [{"symbol": c["symbol"], "name": c["name"], "sector": c["sector"]}
            for c in _index()["by_symbol"].values()]


# ------------------------------------------------------------------ #
#  Pencarian                                                           #
# ------------------------------------------------------------------ #

def _result(entry: dict[str, Any], score: float, match: str) -> dict[str, Any]:
    out = dict(entry)
    out["score"] = round(min(score, 1.0), 4)
    out["match"] = match
    return out


def default_results(limit: int = 12) -> list[dict[str, Any]]:
    """Query kosong: IHSG lalu emiten populer."""
    idx = _index()
    out: list[dict[str, Any]] = [_result(INDEX_ENTRY, 1.0, "default")]
    for i, sym in enumerate(DEFAULT_SYMBOLS):
        entry = idx["by_symbol"].get(sym)
        if entry:
            # Skor menurun mengikuti urutan kurasi supaya aturan "skor menurun lalu simbol" tetap konsisten.
            out.append(_result(entry, 0.9 - i * 0.01, "default"))
    return out[:max(1, limit)]


def _phrase_in(phrase: list[str], tokens: list[str]) -> bool:
    """True bila urutan token `phrase` muncul utuh (berbatas token) di `tokens`."""
    n = len(phrase)
    if n == 0 or n > len(tokens):
        return False
    return any(tokens[i:i + n] == phrase for i in range(len(tokens) - n + 1))


def _tokens_prefix_all(query_tokens: list[str], name_tokens: list[str]) -> bool:
    """Setiap token query (>= 2 huruf) menjadi prefix token nama yang berbeda."""
    used: set[int] = set()
    for qt in query_tokens:
        found = None
        for i, nt in enumerate(name_tokens):
            if i not in used and nt.startswith(qt):
                found = i
                break
        if found is None:
            return False
        used.add(found)
    return True


def _fuzzy_score(ratio: float) -> float:
    """Petakan ratio [0.72, 1.0] -> skor [0.30, 0.60]."""
    frac = (ratio - _FUZZY_MIN_RATIO) / (1.0 - _FUZZY_MIN_RATIO)
    return 0.30 + 0.30 * max(0.0, min(1.0, frac))


def _best_ratio(sm: SequenceMatcher, token_len: int, candidates: list[str]) -> float:
    """Ratio terbaik kandidat terhadap token query yang sudah dipasang sebagai seq2 di `sm`."""
    best = 0.0
    for cand in candidates:
        if abs(len(cand) - token_len) > 3:
            continue
        sm.set_seq1(cand)
        if sm.real_quick_ratio() < _FUZZY_MIN_RATIO or sm.quick_ratio() < _FUZZY_MIN_RATIO:
            continue
        r = sm.ratio()
        if r > best:
            best = r
    return best


def _search(q: str, limit: int) -> list[dict[str, Any]]:
    idx = _index()
    by_symbol: dict[str, dict[str, Any]] = idx["by_symbol"]
    name_tokens: dict[str, list[str]] = idx["tokens"]
    aliases: dict[str, str] = idx["aliases"]
    popularity: dict[str, float] = idx["popularity"]

    query = sanitize_query(q)
    if not query:
        return default_results(limit)

    q_tokens = [t for t in _SPLIT_RE.split(query) if t]
    if not q_tokens:
        return default_results(limit)
    # "bbca.jk" / "bbca jk" (akhiran Yahoo Finance) -> "bbca"
    if len(q_tokens) == 2 and q_tokens[1] == "jk":
        q_tokens = q_tokens[:1]
    q_joined = " ".join(q_tokens)
    q_compact = "".join(q_tokens)
    q_upper = normalize_symbol(q_compact)
    q_content = [t for t in q_tokens if t not in _QUERY_FILLERS] or q_tokens

    best: dict[str, tuple[float, str]] = {}

    def consider(sym: str, score: float, match: str, bonus: bool = True) -> None:
        if bonus:
            score += popularity.get(sym, 0.0)
        cur = best.get(sym)
        if cur is None or score > cur[0]:
            best[sym] = (score, match)

    # 1) ticker persis / prefix (query satu token alfanumerik)
    if len(q_tokens) == 1 and q_upper:
        if q_upper in by_symbol:
            consider(q_upper, SCORE_TICKER_EXACT, "ticker", bonus=False)
        if len(q_upper) < 4:
            for sym in by_symbol:
                if sym.startswith(q_upper):
                    consider(sym, SCORE_TICKER_PREFIX, "ticker")

    # 2) alias persis (0.95, tanpa bonus) / frasa alias + kata sisa (0.90) / prefix (0.85)
    content_joined = " ".join(q_content)
    for alias, target in aliases.items():
        if alias == q_joined or alias == content_joined:
            consider(target, SCORE_ALIAS_EXACT, "alias", bonus=False)
        elif len(q_joined) >= 2 and alias.startswith(q_joined):
            consider(target, SCORE_ALIAS_PREFIX, "alias")
        elif len(q_tokens) > 1:
            a_toks = idx["alias_tokens"][alias]
            if len(a_toks) < len(q_tokens) and _phrase_in(a_toks, q_tokens):
                rest = [t for t in q_tokens if t not in a_toks]
                target_tokens = name_tokens.get(target, [])
                if all(t in _QUERY_FILLERS or any(nt.startswith(t) for nt in target_tokens) for t in rest):
                    consider(target, SCORE_ALIAS_PHRASE, "alias")

    # 3) nama lengkap persis (0.80) / setiap token query (>= 2 huruf) jadi prefix token nama (0.70)
    q_name_exact = [t for t in q_tokens if t not in _NAME_STOPWORDS]
    name_q = [t for t in q_content if len(t) >= 2]
    if name_q or q_name_exact:
        for sym, toks in name_tokens.items():
            if q_name_exact and q_name_exact == toks:
                consider(sym, SCORE_NAME_EXACT, "name")
            elif name_q and _tokens_prefix_all(name_q, toks):
                consider(sym, SCORE_NAME_PREFIX, "name")

    # 4) kata kunci sektor
    sector_hits: set[str] = set()
    for slug, kw_list in idx["sector_keywords"].items():
        for kw in kw_list:
            kw_joined = " ".join(kw)
            if (kw_joined == q_joined
                    or (len(q_joined) >= 3 and kw_joined.startswith(q_joined))
                    or _phrase_in(kw, q_tokens)):
                sector_hits.add(slug)
                break
    if sector_hits:
        for sym, entry in by_symbol.items():
            if entry["sector"] in sector_hits:
                consider(sym, SCORE_SECTOR, "sector")

    # 5) entri IHSG
    index_score = 0.0
    for t in q_tokens:
        if t == "ihsg":
            index_score = max(index_score, 1.0)
        elif t in _INDEX_KEYWORDS or (len(t) >= 2 and "ihsg".startswith(t)):
            index_score = max(index_score, 0.9)

    # 6) fuzzy (typo) — hanya bila belum ada hit jelas (>= 0.9) dan hasil kuat belum memenuhi limit
    top_score = max((s for s, _ in best.values()), default=0.0)
    top_score = max(top_score, index_score)
    strong = sum(1 for s, _ in best.values() if s >= SCORE_NAME_PREFIX)
    # Maksimal 3 token: biaya fuzzy sebanding jumlah token × 962 emiten, dan nama sehari-hari jarang > 3 kata.
    fuzzy_q = [t for t in q_content if len(t) >= _FUZZY_MIN_TOKEN][:3]
    if fuzzy_q and top_score < 0.9 and strong < limit:
        matchers = [(SequenceMatcher(None, "", qt), len(qt)) for qt in fuzzy_q]
        for sym, (cands, alias_cands) in idx["fuzzy_cands"].items():
            ratios: list[float] = []
            via_alias = False
            for sm, qlen in matchers:
                r = _best_ratio(sm, qlen, cands)
                if alias_cands:
                    ra = _best_ratio(sm, qlen, alias_cands)
                    if ra > r:
                        r, via_alias = ra, True
                if r < _FUZZY_MIN_RATIO:
                    ratios = []
                    break
                ratios.append(r)
            if ratios:
                score = _fuzzy_score(sum(ratios) / len(ratios))
                if via_alias:
                    score = min(0.6, score + 0.04)   # alias adalah nama yang memang dipakai orang
                consider(sym, score, "fuzzy")

    results: list[dict[str, Any]] = []
    if index_score > 0:
        results.append(_result(INDEX_ENTRY, index_score, "ticker" if index_score >= 1.0 else "alias"))

    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    for sym, (score, match) in ordered:
        results.append(_result(by_symbol[sym], score, match))

    results.sort(key=lambda r: (-r["score"], r["symbol"]))
    return results[:limit]


@lru_cache(maxsize=512)
def _search_cached(query: str, limit: int) -> tuple[dict[str, Any], ...]:
    """Dataset statis, jadi hasil per (query, limit) aman di-cache."""
    return tuple(_search(query, limit))


def search_stocks(q: Any, limit: int = 12) -> list[dict[str, Any]]:
    """
    Cari emiten berdasarkan ticker, nama, alias sehari-hari, kata kunci sektor, atau typo.

    Mengembalikan list {"symbol","name","sector","sector_name","kind","score","match"}
    terurut skor menurun lalu simbol; unik per simbol. Tidak pernah melempar exception.
    """
    try:
        lim = int(limit)
    except (TypeError, ValueError, OverflowError):
        lim = 12
    lim = max(1, min(30, lim))
    try:
        return [dict(r) for r in _search_cached(sanitize_query(q), lim)]
    except Exception:
        try:
            return default_results(lim)
        except Exception:
            return []

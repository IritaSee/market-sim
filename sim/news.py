"""
Analisis "Input berita": pengguna menempelkan link berita (atau mengetik isinya sendiri),
server membaca judul & ringkasannya, menebak saham yang dibahas, lalu menilai sentimen
dan dampaknya ke nilai wajar. Hasilnya disuntik ke pasar lewat perintah WebSocket
`inject_news_sentiment`, sehingga agen bereaksi:
    - sentimen  → orang noise condong beli/jual (sama seperti Sebar rumor / Kabar buruk)
    - nilai wajar (berita fundamental) → orang fundamentalist menilai ulang harga yang pantas
    - judul berita → masuk ke prompt agen Gemini (bila aktif), lihat agents.set_news_context

Penilaian memakai Gemini bila tersedia (SIMPASAR_LLM tidak off + kunci API), dengan
cadangan kamus kata Indonesia/Inggris yang deterministik, jadi fitur ini tetap jalan
tanpa kunci API maupun koneksi.

Keamanan pengambilan URL (server mengambil halaman atas permintaan pengguna):
    - hanya http/https, port standar, tanpa kredensial di URL
    - host harus ter-resolve ke alamat publik (bukan loopback/privat/link-local/dll.),
      dicek ulang di setiap redirect (maks. 4), DAN alamat yang benar-benar tersambung
      diperiksa sebelum satu byte pun dikirim (mencegah DNS rebinding); proxy sistem diabaikan
    - batas waktu 8 detik per operasi, 15 detik total, ukuran unduhan 1,5 MB, maks. 3 unduhan
      berjalan bersamaan; HTML diurai di thread supaya loop simulasi tidak tersendat
"""
from __future__ import annotations

import asyncio
import html
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from .stocks import get_company, normalize_symbol

FETCH_TIMEOUT_S = 8.0           # per operasi socket
FETCH_DEADLINE_S = 15.0         # total satu unduhan (server yang mengirim sangat pelan tetap dihentikan)
FETCH_MAX_BYTES = 1_500_000
PARSE_MAX_CHARS = 600_000       # judul, meta & paragraf awal ada di bagian atas halaman
_FETCH_SLOTS = threading.BoundedSemaphore(3)
MAX_REDIRECTS = 4
MAX_URL_LEN = 2048
MAX_TEXT_LEN = 6000
LLM_TIMEOUT_S = 20.0
USER_AGENT = "Mozilla/5.0 (compatible; SimPasarNewsReader/1.0; +edu-simulation)"

MAX_SENTIMENT = 3.0          # = sim/market.py MAX_SENTIMENT
MAX_FUNDAMENTAL_PCT = 5.0    # dampak satu berita ke nilai wajar (persen) yang disarankan analisis


class NewsError(ValueError):
    """Masukan pengguna tidak valid (URL rusak, host privat, dll.) → pesan ramah ke klien."""


# ═══════════════════════════ URL & pengambilan halaman ═══════════════════════════

PRIVATE_MSG = "Link ini mengarah ke alamat lokal/privat, tidak bisa dibaca."


def _assert_public_ip(addr: str) -> None:
    """IP harus publik. IPv6 yang membungkus IPv4 (::ffff:a.b.c.d, ::a.b.c.d, 64:ff9b::/96) dicek IPv4-nya."""
    ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    if ip.version == 6:
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip in ipaddress.ip_network("64:ff9b::/96") or (int(ip) >> 32) == 0:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if not ip.is_global or ip.is_multicast:
        raise NewsError(PRIVATE_MSG)


def _check_public_host(host: str, port: int) -> None:
    """Penolakan awal yang murah: host yang ter-resolve ke alamat non-publik (SSRF ke jaringan lokal)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise NewsError("Alamat situs tidak ditemukan. Periksa lagi link-nya.") from exc
    if not infos:
        raise NewsError("Alamat situs tidak ditemukan. Periksa lagi link-nya.")
    for info in infos:
        _assert_public_ip(info[4][0])


def _guarded_create_connection(address, *args, **kwargs):
    """create_connection + cek IP yang BENAR-BENAR tersambung (tahan DNS rebinding), sebelum TLS/HTTP."""
    sock = socket.create_connection(address, *args, **kwargs)
    try:
        _assert_public_ip(sock.getpeername()[0])
    except BaseException:
        sock.close()
        raise
    return sock


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


def validate_url(raw: str) -> str:
    """URL http/https yang dirapikan, atau NewsError. Tidak melakukan koneksi."""
    url = (raw or "").strip()
    if not url:
        raise NewsError("Link berita kosong.")
    if len(url) > MAX_URL_LEN:
        raise NewsError("Link terlalu panjang.")
    scheme = re.match(r"^([a-z][a-z0-9+.-]*):", url, re.I)
    if scheme and scheme.group(1).lower() not in ("http", "https") and "://" in url[:16]:
        raise NewsError("Link harus diawali http:// atau https://")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise NewsError("Link harus diawali http:// atau https://")
    if parts.username or parts.password:
        raise NewsError("Link berisi nama pengguna/kata sandi, tidak bisa dipakai.")
    try:
        port = parts.port
    except ValueError as exc:
        raise NewsError("Port pada link tidak valid.") from exc
    if port not in (None, 80, 443):
        raise NewsError("Hanya link berita biasa (port 80/443) yang bisa dibaca.")
    # Spasi / karakter non-ASCII di path atau query (link hasil salin) di-percent-encode.
    safe = "/%:@!$&'()*+,;=-._~"
    path = urllib.parse.quote(parts.path or "/", safe=safe)
    query = urllib.parse.quote(parts.query, safe=safe + "?")
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, query, ""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = validate_url(newurl)
        parts = urllib.parse.urlsplit(url)
        _check_public_host(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
        return super().redirect_request(req, fp, code, msg, headers, url)


def _fetch_html(url: str) -> tuple[str, str]:
    """(html, url_akhir). Berjalan di thread (blocking); maks. 3 unduhan bersamaan."""
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise NewsError("Server sedang membaca berita lain, coba lagi sebentar.")
    try:
        return _fetch_html_locked(url)
    finally:
        _FETCH_SLOTS.release()


def _fetch_html_locked(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    _check_public_host(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    # ProxyHandler({}) = abaikan proxy dari environment/registry, supaya cek IP tersambung bermakna.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _GuardedHTTPHandler,
                                         _GuardedHTTPSHandler, _SafeRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.7",
    })
    deadline = time.monotonic() + FETCH_DEADLINE_S
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT_S) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if ctype and "html" not in ctype.lower() and "xml" not in ctype.lower():
                raise NewsError("Link ini bukan halaman berita (bukan HTML).")
            buf = bytearray()
            while len(buf) < FETCH_MAX_BYTES:
                if time.monotonic() > deadline:
                    if buf:
                        break                      # pakai yang sudah terbaca (judul ada di awal halaman)
                    raise NewsError("Situs terlalu lama mengirim halamannya.")
                chunk = resp.read1(65536)
                if not chunk:
                    break
                buf += chunk
            data = bytes(buf[:FETCH_MAX_BYTES])
            charset = resp.headers.get_content_charset() or ""
            final_url = resp.geturl()
    except NewsError:
        raise
    except urllib.error.HTTPError as exc:
        raise NewsError(f"Situs menolak dibaca (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
        raise NewsError("Situs tidak bisa dihubungi atau terlalu lama menjawab.") from exc
    if not charset:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', data[:4096], re.I)
        charset = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        text = data.decode(charset, errors="replace")
    except LookupError:
        text = data.decode("utf-8", errors="replace")
    return text, final_url


class _ArticleParser(HTMLParser):
    """Ambil <title>, meta (og/twitter/description/tanggal) dan paragraf awal artikel."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.paragraphs: list[str] = []
        self._in_title = False
        self._skip = 0
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "aside", "form"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "p" and not self._skip:
            self._in_p, self._buf = True, []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "aside", "form") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buf).split())
            if len(text) >= 40 and len(self.paragraphs) < 12:
                self.paragraphs.append(text)

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())
        elif self._in_p and not self._skip:
            self._buf.append(data)


def parse_article(page: str) -> dict[str, str]:
    p = _ArticleParser()
    try:
        p.feed(page)
    except Exception:   # HTML rusak: pakai apa yang sudah terbaca
        pass
    m = p.meta
    title = m.get("og:title") or m.get("twitter:title") or p.title
    desc = m.get("og:description") or m.get("description") or m.get("twitter:description") or ""
    return {
        "title": _clean(html.unescape(title))[:300],
        "description": _clean(html.unescape(desc))[:600],
        "body": _clean(" ".join(p.paragraphs))[:3000],
        "site": _clean(m.get("og:site_name", ""))[:80],
        "published": (m.get("article:published_time") or m.get("og:updated_time") or m.get("date")
                      or m.get("pubdate") or m.get("datepublished") or "")[:40],
    }


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def title_from_slug(url: str) -> str:
    """Judul cadangan dari URL: '.../tiga-hari-beruntun-saham-goto-...' → 'Tiga hari beruntun saham GOTO ...'."""
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    seg = urllib.parse.unquote(path.rsplit("/", 1)[-1]) if path else ""
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg, flags=re.I)
    words = [w for w in re.split(r"[-_+\s]+", seg) if w and not re.fullmatch(r"\d{4,}|[0-9a-f]{8,}", w, re.I)]
    if len(words) < 3:
        return ""
    out = []
    for i, w in enumerate(words):
        out.append(w.upper() if get_company(w) and len(w) == 4 else (w.capitalize() if i == 0 else w.lower()))
    return " ".join(out)[:200]


# ═══════════════════════════ Saham yang dibahas ═══════════════════════════

# Kata kapital 4 huruf yang lazim di berita tetapi bukan pembahasan emiten.
_TICKER_STOPWORDS = {"IHSG", "SAHAM", "YANG", "WIB", "IDX", "OJK", "TBK", "BUMN", "JAKARTA"}


def detect_tickers(title: str, text: str, url: str = "", lead: str = "") -> list[str]:
    """Ticker BEI yang disebut, urut dari yang paling dibahas (judul & URL dihitung lebih berat)."""
    scores: dict[str, float] = {}

    def add(sym: str, w: float) -> None:
        sym = normalize_symbol(sym)
        if len(sym) == 4 and sym not in _TICKER_STOPWORDS and get_company(sym):
            scores[sym] = scores.get(sym, 0.0) + w

    slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path).lower() if url else ""
    for src, w in ((title, 3.0), (lead, 2.0), (text, 1.0)):
        for m in re.finditer(r"\(([A-Z]{4})\)|\$([A-Z]{4})\b|\b([A-Z]{4})\b", src or ""):
            add(m.group(1) or m.group(2) or m.group(3), w)
        for m in re.finditer(r"\b(?:saham|emiten|kode)\s+([A-Za-z]{4})\b", src or "", re.I):
            add(m.group(1), w)
    for m in re.finditer(r"(?:saham|emiten)[-_]([a-z]{4})(?![a-z])", slug):
        add(m.group(1), 3.0)
    ranked = [s for s, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v >= 2.0]
    return ranked[:3]


# ═══════════════════════════ Penilaian sentimen (aturan) ═══════════════════════════

# (frasa, bobot). Frasa dicocokkan sebagai kata/frasa utuh, huruf kecil.
_POSITIVE = [
    ("melonjak", 1.3), ("meroket", 1.4), ("melesat", 1.3), ("terbang", 1.0), ("menguat", 1.0), ("naik", 0.7),
    ("ara", 1.2), ("auto reject atas", 1.2), ("rebound", 0.9), ("pulih", 0.7), ("rekor", 0.8), ("tertinggi", 0.8),
    ("laba bersih naik", 1.6), ("laba naik", 1.4), ("laba", 0.6), ("untung", 0.6), ("cuan", 0.7), ("dividen", 0.9),
    ("buyback", 1.1), ("akuisisi", 0.7), ("ekspansi", 0.7), ("kontrak baru", 1.0), ("tumbuh", 0.8), ("positif", 0.6),
    ("net buy", 1.0), ("beli bersih", 1.0), ("borong", 0.9), ("upgrade", 1.0), ("rekomendasi beli", 1.2),
    ("target harga naik", 1.2), ("optimis", 0.7), ("prospek cerah", 1.1), ("surplus", 0.6), ("melampaui ekspektasi", 1.2),
    ("surge", 1.2), ("rally", 1.0), ("profit", 0.7), ("growth", 0.7), ("record high", 1.0), ("beat", 0.6), ("bullish", 1.0),
]
_NEGATIVE = [
    ("anjlok", 1.4), ("ambles", 1.3), ("ambruk", 1.4), ("longsor", 1.2), ("merosot", 1.2), ("terjun", 1.1),
    ("tersungkur", 1.2), ("melemah", 1.0), ("turun", 0.7), ("terkoreksi", 0.8), ("koreksi", 0.6), ("minus", 0.6),
    ("arb", 1.2), ("auto reject bawah", 1.2), ("bawah gocap", 1.5), ("gocap", 0.6), ("terendah", 0.9), ("beruntun", 0.4),
    ("rugi bersih", 1.6), ("rugi", 1.0), ("boncos", 1.0), ("merugi", 1.1), ("defisit", 0.8), ("gagal bayar", 1.8),
    ("pailit", 1.8), ("bangkrut", 1.8), ("suspensi", 1.4), ("delisting", 1.8), ("phk", 1.0), ("fraud", 1.6),
    ("skandal", 1.4), ("gugatan", 1.0), ("penyelidikan", 1.0), ("investigasi", 1.0), ("denda", 0.9), ("utang", 0.5),
    ("net sell", 1.0), ("jual bersih", 1.0), ("tekanan jual", 1.0), ("downgrade", 1.0), ("rekomendasi jual", 1.2),
    ("pesimis", 0.8), ("negatif", 0.6), ("lesu", 0.8), ("tertekan", 0.9), ("tekanan", 0.8), ("waspada", 0.5),
    ("risiko", 0.4), ("di bawah rp50", 1.2), ("melorot", 1.1), ("tersengat", 0.8), ("terperosok", 1.2),
    ("crash", 1.4), ("plunge", 1.3), ("loss", 0.9), ("default", 1.4), ("bearish", 1.0), ("selloff", 1.2), ("miss", 0.5),
]
_FUNDAMENTAL_WORDS = (
    "laba", "rugi bersih", "pendapatan", "penjualan", "kinerja", "fundamental", "dividen", "akuisisi", "merger",
    "kontrak", "utang", "obligasi", "gagal bayar", "pailit", "ekspansi", "capex", "buyback", "rights issue",
    "private placement", "valuasi", "laporan keuangan", "earnings", "revenue", "profit", "per ", "pbv",
)
_RUMOR_WORDS = ("rumor", "isu", "kabar", "spekulasi", "gorengan", "bandar", "katanya", "dikabarkan", "viral")
_MARKET_WORDS = ("ihsg", "asing", "indeks", "bursa", "the fed", "suku bunga", "rupiah", "inflasi")


def _count_phrase(text: str, phrase: str) -> int:
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text))


def score_text(title: str, text: str, lead: str = "") -> tuple[float, list[str]]:
    """
    Skor −3…+3 dan kata kunci yang ditemukan. Judul paling menentukan reaksi pasar (bobot 2,5),
    lalu ringkasan/lead (1,5); isi artikel ikut dihitung tetapi dibatasi ±2,5 supaya paragraf lain
    (mis. tautan berita terkait) tidak membalik arah judul. Frasa panjang mengalahkan frasa pendeknya.
    """
    raw = 0.0
    hits: list[tuple[str, float]] = []
    for src, w, cap in ((title.lower(), 2.5, None), (lead.lower()[:800], 1.5, None), (text.lower()[:2500], 1.0, 2.5)):
        part = 0.0
        used: list[str] = []
        for lexicon, sign in ((_POSITIVE, 1), (_NEGATIVE, -1)):
            for phrase, weight in sorted(lexicon, key=lambda pw: -len(pw[0])):
                n = _count_phrase(src, phrase)
                if not n or any(phrase in u for u in used):   # "laba" tidak dihitung lagi bila "laba naik" cocok
                    continue
                used.append(phrase)
                contrib = sign * weight * w * min(n, 3)
                part += contrib
                hits.append((phrase, contrib))
        raw += part if cap is None else max(-cap, min(cap, part))
    score = MAX_SENTIMENT * math.tanh(raw / 5.0)
    keywords = [p for p, _ in sorted(hits, key=lambda h: -abs(h[1]))]
    seen: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.append(k)
    return round(score, 1), seen[:6]


def classify(title: str, text: str) -> str:
    t = f"{title} {text}".lower()
    if any(w in t for w in _FUNDAMENTAL_WORDS):
        return "fundamental"
    if any(w in t for w in _RUMOR_WORDS):
        return "rumor"
    if any(w in t for w in _MARKET_WORDS):
        return "pasar"
    return "lainnya"


def _label(score: float) -> str:
    return "Positif" if score > 0.2 else "Negatif" if score < -0.2 else "Netral"


def analyze_rules(title: str, text: str, lead: str = "") -> dict[str, Any]:
    score, keywords = score_text(title, text, lead)
    category = classify(title, f"{lead} {text}")
    fundamental_pct = round(max(-MAX_FUNDAMENTAL_PCT, min(MAX_FUNDAMENTAL_PCT, score * 1.2)), 1) if category == "fundamental" else 0.0
    if keywords:
        reason = "Kata kunci: " + ", ".join(keywords) + "."
    else:
        reason = "Tidak ada kata kunci sentimen yang jelas, dianggap netral."
    return {"sentiment": score, "label": _label(score), "category": category,
            "fundamental_pct": fundamental_pct, "reason": reason, "method": "aturan"}


# ═══════════════════════════ Penilaian sentimen (Gemini) ═══════════════════════════

_LLM_PROMPT = """Kamu analis pasar saham Indonesia. Nilai berita berikut untuk simulasi pasar edukatif.
Saham yang sedang disimulasikan: {symbol}.

Judul: {title}
Ringkasan: {desc}
Isi (potongan): {body}
Sumber: {source}

Jawab HANYA JSON dengan kunci:
- "sentiment": angka -3 (sangat negatif, memicu panik jual) sampai +3 (sangat positif, memicu euforia beli)
- "category": salah satu "fundamental" (kinerja/laporan keuangan/aksi korporasi), "rumor", "pasar" (makro/indeks), "lainnya"
- "fundamental_pct": perubahan nilai wajar saham yang masuk akal dalam persen, -5 sampai 5 (0 bila bukan berita fundamental)
- "tickers": daftar kode saham BEI 4 huruf yang dibahas (boleh kosong)
- "summary": satu kalimat ringkasan berita dalam Bahasa Indonesia
- "reason": alasan singkat penilaianmu (maks. 20 kata, Bahasa Indonesia)"""


NEWS_LLM_PER_MIN = 4
NEWS_LLM_MAX_PER_PROCESS = 200
_news_llm_lock = threading.Lock()
_news_llm_times: list[float] = []
_news_llm_total = 0


def _llm_enabled() -> bool:
    return os.environ.get("SIMPASAR_LLM", "on").strip().lower() not in {"0", "off", "false", "no", "disable", "disabled"}


def _reserve_llm_call() -> bool:
    """
    Boleh memakai Gemini untuk analisis berita? Ikut status penasihat agen (mati / tanpa kunci /
    backoff 429 / kuota sesi habis → tidak), plus batas sendiri supaya kuota tidak terkuras.
    """
    global _news_llm_total
    if not _llm_enabled():
        return False
    try:
        from .agents import get_llm_advisor
        advisor = get_llm_advisor()
        if advisor is None:
            return False
        st = advisor.stats()
        if not st.get("enabled", True) or float(st.get("backoff_s") or 0) > 0:
            return False
    except Exception:
        return False
    now = time.monotonic()
    with _news_llm_lock:
        _news_llm_times[:] = [t for t in _news_llm_times if now - t < 60]
        if len(_news_llm_times) >= NEWS_LLM_PER_MIN or _news_llm_total >= NEWS_LLM_MAX_PER_PROCESS:
            return False
        _news_llm_times.append(now)
        _news_llm_total += 1
    return True


def _gemini_analyze_sync(article: dict[str, str], symbol: str) -> dict[str, Any] | None:
    from .agents import get_gemini_client   # impor lambat: agents memuat SDK Gemini
    client = get_gemini_client()
    if client is None:
        return None
    from google.genai import types as genai_types
    prompt = _LLM_PROMPT.format(
        symbol=symbol or "-", title=article.get("title") or "-", desc=article.get("description") or "-",
        body=(article.get("body") or "-")[:2500], source=article.get("site") or article.get("url") or "-",
    )
    resp = client.models.generate_content(
        model=os.environ.get("SIMPASAR_LLM_MODEL", "").strip() or "gemini-3.1-flash-lite",
        contents=prompt,
        config=genai_types.GenerateContentConfig(temperature=0.2, max_output_tokens=400,
                                                 response_mime_type="application/json"),
    )
    data = json.loads(getattr(resp, "text", "") or "{}")
    if not isinstance(data, dict):
        return None
    score = float(data.get("sentiment", 0.0))
    fpct = float(data.get("fundamental_pct", 0.0))
    if not (math.isfinite(score) and math.isfinite(fpct)):
        return None
    score = round(max(-MAX_SENTIMENT, min(MAX_SENTIMENT, score)), 1)
    category = str(data.get("category", "lainnya")).lower()
    if category not in ("fundamental", "rumor", "pasar", "lainnya"):
        category = "lainnya"
    tickers = [normalize_symbol(t) for t in data.get("tickers", []) if isinstance(t, str)]
    return {
        "sentiment": score, "label": _label(score), "category": category,
        "fundamental_pct": round(max(-MAX_FUNDAMENTAL_PCT, min(MAX_FUNDAMENTAL_PCT, fpct)), 1),
        "reason": _clean(str(data.get("reason", "")))[:200] or "Dinilai oleh Gemini.",
        "summary": _clean(str(data.get("summary", "")))[:300],
        "tickers": [t for t in tickers if get_company(t)][:5],
        "method": "gemini",
    }


# ═══════════════════════════ Titik masuk ═══════════════════════════

async def analyze_news(url: str | None, text: str | None, current_symbol: str = "") -> dict[str, Any]:
    """
    Baca & nilai berita. Melempar NewsError untuk masukan yang tidak bisa dipakai sama sekali.
    Bila halaman gagal dibaca tetapi URL valid, judul ditebak dari URL (warning diisi).
    """
    text = _clean(text)[:MAX_TEXT_LEN] if text else ""
    article = {"title": "", "description": "", "body": "", "site": "", "published": "", "url": ""}
    warning = None
    fetched = False
    if url:
        url = validate_url(url)
        article["url"] = url
        try:
            page, final_url = await asyncio.wait_for(asyncio.to_thread(_fetch_html, url), FETCH_DEADLINE_S + 5)
            article.update(await asyncio.to_thread(parse_article, page[:PARSE_MAX_CHARS]))
            article["url"] = final_url
            fetched = bool(article["title"])
        except asyncio.TimeoutError:
            warning = "Situs terlalu lama menjawab. Judul ditebak dari link-nya."
        except NewsError as exc:
            if str(exc) == PRIVATE_MSG or "tidak ditemukan" in str(exc):
                raise
            warning = f"{exc} Judul ditebak dari link-nya."
        if not article["title"]:
            article["title"] = title_from_slug(url)
            if not warning:
                warning = "Halaman tidak memuat judul; judul ditebak dari link-nya."
    if text:
        rest = text
        if not article["title"]:
            parts = re.split(r"(?<=[.!?])\s", text, maxsplit=1)
            article["title"] = parts[0][:200]
            rest = parts[1] if len(parts) > 1 else ""   # kalimat judul tidak dihitung dua kali
        article["body"] = (rest + " " + article["body"]).strip()[:3000]
    if not article["title"]:
        raise NewsError("Tidak ada judul atau isi berita yang bisa dibaca. Tempelkan isi beritanya di kolom teks.")

    result = None
    if _reserve_llm_call():
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_gemini_analyze_sync, article, current_symbol), LLM_TIMEOUT_S)
        except Exception:   # kuota habis, timeout, JSON rusak → aturan
            result = None
    if result is None:
        result = analyze_rules(article["title"], article["body"], article["description"])

    tickers = detect_tickers(article["title"], article["body"], article["url"], article["description"])
    for t in result.pop("tickers", []):
        if t not in tickers:
            tickers.append(t)
    companies = [{"symbol": t, "name": (get_company(t) or {}).get("name", "")} for t in tickers[:5]]
    host = (urllib.parse.urlsplit(article["url"]).hostname or "") if article["url"] else ""
    summary = result.pop("summary", "") or article["description"] or article["body"][:220]
    if summary and summary[-1] not in ".!?…\"'”)":
        summary = summary.rstrip(" ,;:-(") + "…"   # og:description sering terpotong di tengah kalimat
    return {
        "ok": True,
        "url": article["url"],
        "source": article["site"] or (host[4:] if host.startswith("www.") else host) or "Teks kamu",
        "title": article["title"],
        "summary": summary[:300],
        "published": article["published"],
        "tickers": companies,
        "primary": companies[0]["symbol"] if companies else None,
        "fetched": fetched,
        "warning": warning,
        **result,
    }

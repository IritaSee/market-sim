"""
SimPasar IDX — FastAPI server dengan WebSocket real-time dan integrasi Sectors MCP.

Jalankan:
    python server.py
    atau:
    uvicorn server:app --reload --port 8000

Buka di browser:
    Landing Page : http://localhost:8000
    Simulator    : http://localhost:8000/simulator

Protokol WebSocket (ringkas, detail di README):
    - Saat konek / reset / set_fundamental / set_population / set_psych / set_symbol / get_state:
      state penuh (snapshot: true, berisi price_history & volume_history).
    - Tiap tick: state delta (snapshot: false, tanpa histori; klien push price/volume).
    - Setiap state memuat tick_interval (detik per tick), symbol.source_kind, dan limits
      (batas ARA/ARB harian dari harga acuan; indeks → applies false).
    - Simulasi dimulai di BBCA (SIMPASAR_START_SYMBOL untuk mengganti); IHSG tetap bisa dipilih.
    - Pesan dengan "event": paused, resumed, news_injected, symbol_changed, symbol_error,
      speed_changed, error.
    - Pengiriman tidak pernah memblok loop simulasi: tiap koneksi punya antrean + task penulis
      sendiri (lihat WSClient). Klien lambat kehilangan delta lalu menerima snapshot penuh.
"""
import os
import sys
import asyncio
import json
import math
import socket
import struct
import time
import traceback
from collections import deque
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# Log bisa memuat karakter non-ASCII (alasan LLM); jangan biarkan encoding console atau
# stdout yang dialihkan ke file (cp1252 di Windows) mematikan loop simulasi.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sim.env import load_env_file

# Muat .env di root repo (mis. GEMINI_API_KEY) sebelum modul sim membaca environment.
# Variabel yang sudah diset di shell tetap diprioritaskan.
load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from sim.agents import get_gemini_client, llm_status, shutdown_llm_advisor  # noqa: E402
from sim.market import FUNDAMENTAL_RANGE_MSG, MAX_SENTIMENT, Market, valid_fundamental  # noqa: E402
from sim.sectors import (  # noqa: E402
    get_realtime_ihsg, get_latest_news_and_filings, get_stock_price, peek_cached_price,
)
from sim.stocks import (  # noqa: E402
    get_symbol_meta, is_valid_symbol_format, list_companies, load_companies, normalize_symbol, search_stocks,
)

app = FastAPI(title="SimPasar IDX")

# ------------------------------------------------------------------ #
#  State global                                                       #
# ------------------------------------------------------------------ #

# Simbol awal simulator: BBCA (bukan IHSG). Bisa diganti lewat SIMPASAR_START_SYMBOL (emiten BEI
# atau IHSG); nilai tidak dikenal → BBCA. IHSG tetap bisa dipilih dari dashboard.
DEFAULT_START_SYMBOL = "BBCA"
# Harga penutupan BBCA terakhir yang diketahui (22 Sep 2026). Dipakai sebelum/bila harga Sectors
# tidak tersedia, sehingga snapshot PERTAMA pun sudah BBCA dengan harga yang wajar.
START_FALLBACK_PRICE = 6200.0
START_FALLBACK_DATE = "2026-09-22"
START_FALLBACK_SOURCE = "Nilai cadangan: harga Sectors belum tersedia"


def _start_symbol() -> str:
    """Simbol awal dari SIMPASAR_START_SYMBOL (default BBCA; tidak dikenal → BBCA)."""
    raw = os.environ.get("SIMPASAR_START_SYMBOL", "").strip()
    if raw:
        meta = get_symbol_meta(raw)
        if meta is not None:
            return meta["symbol"]
        print(f"[stocks] SIMPASAR_START_SYMBOL={raw[:16]!r} tidak dikenal; memakai {DEFAULT_START_SYMBOL}", flush=True)
    return DEFAULT_START_SYMBOL


def _fallback_start_meta() -> dict:
    """Metadata BBCA + atribusi nilai cadangan (source_kind "fallback")."""
    meta = get_symbol_meta(DEFAULT_START_SYMBOL) or {
        "symbol": DEFAULT_START_SYMBOL, "name": "PT Bank Central Asia Tbk.",
        "sector": "financials", "sector_name": "Keuangan", "kind": "stock",
    }
    meta.update({
        "source_price": START_FALLBACK_PRICE,
        "source_date":  START_FALLBACK_DATE,
        "source":       START_FALLBACK_SOURCE,
        "source_kind":  "fallback",
    })
    return meta


market     = Market(n_agents=100, seed=42, fundamental=START_FALLBACK_PRICE)
market.set_symbol(_fallback_start_meta(), START_FALLBACK_PRICE)
clients:   "set[WSClient]" = set()
tick_rate  = 0.25   # detik antar tick (≈4 tick/detik default 1x, realistis)

TICK_INTERVAL_MIN = 0.05           # 5x
TICK_INTERVAL_MAX = 1.5
WS_MAX_MESSAGE_BYTES = 64 * 1024   # perintah klien kecil (<1 KB); tolak payload raksasa sebelum json.loads
WS_MAX_CMDS_PER_SEC  = 20          # pembatas per koneksi (get_state dikecualikan)
WS_RATE_LIMIT_MESSAGE = "Terlalu banyak perintah, coba lagi sebentar"
WS_QUEUE_MAX       = 16            # pesan tertunda per klien; lebih dari ini → delta dibuang, snapshot menyusul
WS_QUEUE_HARD_MAX  = 64            # termasuk event; lebih dari ini klien dianggap macet dan diputus
WS_STALL_TIMEOUT   = 10.0          # detik; satu pengiriman yang tidak selesai selama ini → klien diputus
WS_SNAPSHOT_MIN_INTERVAL = 0.2     # detik; snapshot atas permintaan (get_state / tertinggal) maks. 5x/detik per klien
MANUAL_SOURCE = "Diisi manual oleh pengguna"


# ------------------------------------------------------------------ #
#  Serialisasi & state                                                #
# ------------------------------------------------------------------ #

def _dumps(payload: dict) -> str | None:
    """
    JSON ketat (allow_nan=False): NaN/Infinity tidak valid bagi JSON.parse browser dan akan
    membuat semua klien membuang pesan diam-diam. Payload seperti itu dicatat lalu dilewati.
    """
    try:
        return json.dumps(payload, allow_nan=False)
    except ValueError as exc:
        print(f"[ws] payload dengan angka non-finite tidak dikirim: {exc}", flush=True)
        return None


def _with_speed(state: dict) -> dict:
    """Tambahkan kecepatan server (detik per tick) ke state, supaya tombol kecepatan klien tersinkron."""
    state["tick_interval"] = round(float(tick_rate), 4)
    return state


def current_state(full: bool = True) -> dict:
    """State pasar (snapshot penuh atau delta) + tick_interval. Satu-satunya sumber state untuk klien."""
    return _with_speed(market.get_state(full=full))


def _snapshot_text() -> str | None:
    """Snapshot penuh terbaru, dibuat saat akan dikirim (dipakai penulis klien yang tertinggal)."""
    return _dumps(current_state(full=True))


# ------------------------------------------------------------------ #
#  Pengiriman per klien (antrean + task penulis)                      #
# ------------------------------------------------------------------ #

def _find_transport(ws: Any) -> Any:
    """
    Transport asyncio di balik WebSocket Starlette (best effort). ASGI tidak punya "abort",
    dan websocket.close() maupun transport.close() menunggu buffer tulis terkirim — yang tidak
    pernah terjadi bila peer berhenti membaca. Rantai fungsi send (pembungkus Starlette/FastAPI)
    ditelusuri sampai method milik protokol uvicorn yang punya .transport.
    """
    stack = [getattr(ws, "_send", None)]
    seen: set[int] = set()
    for _ in range(32):
        if not stack:
            break
        fn = stack.pop()
        if fn is None or id(fn) in seen:
            continue
        seen.add(id(fn))
        transport = getattr(getattr(fn, "__self__", None), "transport", None)
        if transport is not None and callable(getattr(transport, "abort", None)):
            return transport
        for cell in getattr(fn, "__closure__", None) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value):
                stack.append(value)
    return None


def _hard_abort(transport: Any) -> None:
    """
    Putus paksa (RST). transport.abort() saja menutup socket secara "sopan": FIN antre di
    belakang data yang tidak pernah dibaca peer, sehingga kernel menahan koneksi yatim
    (FIN_WAIT_1) bermenit-menit. SO_LINGER {on, 0 detik} membuat close() membuang buffer.
    """
    sock = transport.get_extra_info("socket")
    if sock is not None:
        # struct linger: dua u_short di Windows, dua int di POSIX.
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        with suppress(OSError, ValueError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    transport.abort()
    if sys.platform == "win32" and sock is not None:
        # Loop proactor (Windows) memanggil shutdown() sebelum close() saat menutup transport, dan di
        # Windows shutdown() membatalkan efek linger 0 (FIN tetap antre). Tutup socket aslinya sekarang
        # (abort() sudah membatalkan I/O yang tertunda); proactor melewati shutdown bila fileno() == -1.
        raw = getattr(sock, "_sock", None)
        if isinstance(raw, socket.socket):
            with suppress(OSError):
                raw.close()


class WSClient:
    """
    Satu koneksi WebSocket dengan antrean keluar dan SATU task penulis.

    - send()/broadcast() tidak pernah menunggu I/O: pesan hanya dimasukkan ke antrean.
    - Urutan pesan per klien tetap (satu penulis, FIFO), termasuk balasan ke pengirim.
    - Antrean penuh (klien lambat) → semua state tertunda dibuang dan klien ditandai butuh
      snapshot; snapshot penuh TERBARU dibuat saat antrean lega, setelah event yang masih
      tertunda (mis. symbol_changed tetap datang sebelum snapshot-nya).
    - Satu pengiriman macet > WS_STALL_TIMEOUT detik → klien diputus (transport di-abort).
    """

    def __init__(self, ws: Any, snapshot_text: Callable[[], str | None] = _snapshot_text) -> None:
        self.ws = ws
        self._snapshot_text = snapshot_text
        self._queue: deque[tuple[bool, str]] = deque()   # (is_state, teks JSON)
        self._need_snapshot = False
        self._wake = asyncio.Event()
        self.closed = False
        self.stalled = False
        self.dropped = 0                                   # jumlah state yang dibuang (diagnostik)
        self.writer: asyncio.Task | None = None
        self._last_snapshot_at = float("-inf")

    def start(self) -> "WSClient":
        if self.writer is None:
            self.writer = asyncio.create_task(self._run())
        return self

    @property
    def pending(self) -> int:
        return len(self._queue)

    @property
    def needs_snapshot(self) -> bool:
        return self._need_snapshot

    def send(self, payload: dict) -> bool:
        """Serialisasi lalu antrekan satu pesan. False bila dibuang / klien sudah tutup."""
        text = _dumps(payload)
        return text is not None and self.send_text(text, "event" not in payload)

    def send_text(self, text: str, is_state: bool) -> bool:
        if self.closed:
            return False
        if is_state:
            if self._need_snapshot:
                self.dropped += 1                 # snapshot segar akan menggantikannya
                return False
            if len(self._queue) >= WS_QUEUE_MAX:
                self.request_snapshot()           # klien tertinggal: lompat ke snapshot terbaru
                self.dropped += 1
                return False
        elif len(self._queue) >= WS_QUEUE_HARD_MAX:
            print("[ws] antrean event klien penuh; koneksi diputus", flush=True)
            self.close(stalled=True)
            return False
        self._queue.append((is_state, text))
        self._wake.set()
        return True

    def request_snapshot(self) -> None:
        """Minta snapshot penuh (dibuat saat dikirim). State tertunda dibuang karena akan digantikannya."""
        if self.closed:
            return
        if any(is_state for is_state, _ in self._queue):
            kept = [(s, t) for s, t in self._queue if not s]
            self.dropped += len(self._queue) - len(kept)
            self._queue = deque(kept)
        self._need_snapshot = True
        self._wake.set()

    def close(self, stalled: bool = False) -> None:
        """
        Tandai tutup dan batalkan penulis (tanpa menunggu); endpoint lalu membersihkan koneksi.
        stalled=True → transport di-abort saat aclose(). Aman dipanggil berkali-kali.
        """
        if stalled:
            self.stalled = True
        self.closed = True
        self._queue.clear()
        self._wake.set()
        writer = self.writer
        if writer is not None and not writer.done():
            writer.cancel()

    async def aclose(self) -> None:
        """Tutup, abort transport bila macet, dan tunggu task penulis selesai (tidak ada task bocor)."""
        self.close()
        if self.stalled:
            transport = _find_transport(self.ws)
            if transport is not None:
                with suppress(Exception):
                    _hard_abort(transport)
        writer = self.writer
        if writer is not None and writer is not asyncio.current_task():
            writer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await writer

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                if self._queue:
                    _, text = self._queue.popleft()
                elif self._need_snapshot:
                    # Membuat snapshot memakan CPU loop (±3–5 ms); banjir get_state dari satu klien
                    # tidak boleh memperlambat simulasi. Permintaan digabung, tidak pernah dibuang.
                    wait = self._last_snapshot_at + WS_SNAPSHOT_MIN_INTERVAL - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        continue
                    self._need_snapshot = False
                    self._last_snapshot_at = loop.time()
                    text = self._snapshot_text()
                    if text is None:
                        continue
                else:
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                await asyncio.wait_for(self.ws.send_text(text), WS_STALL_TIMEOUT)
        except asyncio.TimeoutError:
            self.stalled = True
            print(f"[ws] klien tidak membaca selama > {WS_STALL_TIMEOUT:g} s; koneksi diputus", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass                                  # koneksi putus saat mengirim
        finally:
            self.closed = True
            self._queue.clear()


def broadcast(payload: dict) -> int:
    """
    Antrekan satu pesan ke semua klien (serialisasi sekali). Tidak pernah menunggu I/O klien,
    jadi loop simulasi dan handler perintah tidak bisa dibekukan oleh klien yang lambat/macet.
    Mengembalikan jumlah klien yang menerima pesan di antreannya.
    """
    text = _dumps(payload)
    if text is None:
        return 0
    is_state = "event" not in payload
    delivered = 0
    for client in list(clients):
        if client.closed:
            clients.discard(client)
            continue
        if client.send_text(text, is_state):
            delivered += 1
    return delivered


# ------------------------------------------------------------------ #
#  Simulation loop                                                    #
# ------------------------------------------------------------------ #

async def simulation_loop() -> None:
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    while True:
        try:
            if not market.is_paused and clients:
                state = _with_speed(market.step())   # delta: snapshot=false, tanpa histori
                broadcast(state)
                if state["tick"] % 40 == 0:
                    llm = state.get("llm", {})
                    llm_part = (f"llm dipakai {llm.get('applied', 0)}/{llm.get('requested', 0)} request, error {llm.get('errors', 0)}"
                                if llm.get("enabled") else f"llm {llm.get('reason')}")
                    sym = state.get("symbol", {}).get("symbol", "?")
                    clock = state.get("sim_time", {}).get("label", "")
                    print(f"[sim] {sym} tick {state['tick']} ({clock}) harga {state['price']:.2f} {state['status']} | {llm_part}", flush=True)
        except Exception:
            # Satu tick yang gagal tidak boleh mematikan loop simulasi untuk selamanya.
            traceback.print_exc()
        # Jadwal berbasis tenggat: waktu proses per tick tidak menambah interval, sehingga
        # 5x (0,05 s) benar-benar ≈ 20 tick/detik. Tertinggal jauh → jangan mengejar beruntun.
        now = loop.time()
        next_at = max(next_at + tick_rate, now - tick_rate)
        await asyncio.sleep(max(0.0, next_at - now))


def _silence_genai_aclose_bug(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(exc, AttributeError) and "_async_httpx_client" in str(exc):
        return
    loop.default_exception_handler(context)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.get_running_loop().set_exception_handler(_silence_genai_aclose_bug)
    print(f"[llm] status agen LLM: {llm_status()}", flush=True)
    if os.environ.get("GOOGLE_GENAI_DEBUG") and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print("[llm] PERINGATAN: GOOGLE_GENAI_DEBUG aktif; SDK google-genai mencetak header kunci API ke log.", flush=True)
    try:
        print(f"[stocks] {load_companies()['count']} emiten BEI termuat (snapshot {load_companies().get('snapshot_date')})", flush=True)
    except Exception:
        traceback.print_exc()
    # Harga awal simbol awal (BBCA) dari Sectors: cache memori/disk 6 jam dulu, baru API
    # (maks. 1 kredit; mode offline membaca cache disk lama). Gagal → tetap nilai cadangan.
    start_symbol = _start_symbol()
    try:
        info = await get_stock_price(start_symbol)
        price = valid_fundamental(info.get("price")) if info else None
        meta = get_symbol_meta(start_symbol)
        if price is not None and meta is not None:
            meta.update({
                "source_price": price,
                "source_date":  info.get("date"),
                "source":       info.get("source"),
                "source_kind":  info.get("source_kind"),
            })
            market.set_symbol(meta, price)
            print(f"[sectors] harga awal {start_symbol} {price:.2f} ({info.get('source_kind')}, {info.get('source')})", flush=True)
        else:
            # Tetap/kembali ke nilai cadangan BBCA (startup idempoten, mis. TestClient berulang).
            market.set_symbol(_fallback_start_meta(), START_FALLBACK_PRICE)
            print(f"[sectors] harga {start_symbol} tidak tersedia ({info.get('status') if info else '?'}); "
                  f"simulasi {DEFAULT_START_SYMBOL} di nilai cadangan {START_FALLBACK_PRICE:.2f}", flush=True)
    except Exception:
        traceback.print_exc()
    asyncio.create_task(simulation_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    shutdown_llm_advisor()
    client = get_gemini_client()
    if client is not None:
        try:
            await client.aio.aclose()
        except Exception:
            pass


# ------------------------------------------------------------------ #
#  REST Endpoints                                                     #
# ------------------------------------------------------------------ #

@app.get("/simulator")
async def serve_simulator() -> FileResponse:
    """Halaman Dashboard Simulator Interaktif."""
    return FileResponse("frontend/index.html")


@app.get("/api/ihsg")
async def api_ihsg():
    """Ambil data real-time IHSG dari Sectors MCP."""
    data = await get_realtime_ihsg()
    return JSONResponse(data)


@app.get("/api/news")
async def api_news():
    """Ambil berita & company filings terkini dari Sectors MCP beserta skor sentimen."""
    items = await get_latest_news_and_filings()
    return JSONResponse(items)


@app.get("/api/state")
async def api_state():
    """Snapshot penuh state simulasi (sama dengan pesan pertama di WebSocket)."""
    return JSONResponse(current_state(full=True))


def _parse_limit(raw: str | None, default: int = 12, lo: int = 1, hi: int = 30) -> int:
    try:
        val = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, val))


@app.get("/api/stocks/search")
async def api_stocks_search(q: str = "", limit: str = "12"):
    """
    Cari emiten BEI: ticker (BBCA), nama sehari-hari (bank bca, telkom),
    sektor (bank, batu bara), dengan toleransi typo (mandri → BMRI).
    Query kosong → IHSG + emiten populer.
    """
    lim = _parse_limit(limit)
    query = (q or "")[:64]
    try:
        # Pencarian fuzzy (difflib) memakan CPU belasan ms per query unik; jalankan di thread
        # supaya loop simulasi & WebSocket tidak tersendat.
        results = await asyncio.to_thread(search_stocks, query, lim)
    except Exception:
        results = []
    return JSONResponse({"query": query, "count": len(results), "results": results})


@app.get("/api/stocks")
async def api_stocks():
    """Daftar seluruh emiten (untuk prefetch klien) + metadata sektor."""
    try:
        data = load_companies()
        return JSONResponse({
            "count":         data.get("count", 0),
            "snapshot_date": data.get("snapshot_date"),
            "source":        data.get("source"),
            "sectors":       data.get("sectors", {}),
            "companies":     list_companies(),
        })
    except Exception as exc:
        return JSONResponse({"count": 0, "companies": [], "message": f"Daftar emiten tidak termuat: {exc}"}, status_code=500)


@app.get("/api/stocks/{symbol}/price")
async def api_stock_price(symbol: str):
    """
    Harga penutupan terakhir dari Sectors (fetch-daily-price, 1 kredit, di-cache 6 jam).
    Status di body: success | cache (+ stale) | offline | fallback (IHSG) | unknown (HTTP 404).
    """
    sym = normalize_symbol(symbol)
    if not is_valid_symbol_format(sym) or get_symbol_meta(sym) is None:
        return JSONResponse({
            "symbol":  sym or str(symbol)[:16],
            "status":  "unknown",
            "price":   None,
            "history": [],
            "message": "Simbol tidak dikenal (bukan emiten BEI dalam daftar dan bukan IHSG)",
        }, status_code=404)
    data = await get_stock_price(sym)
    return JSONResponse(data)


@app.get("/paper.pdf")
async def serve_paper():
    """Download/baca draf paper PDF jika ada."""
    for f in os.listdir("."):
        if f.lower().endswith(".pdf"):
            return FileResponse(f)
    return JSONResponse({"message": "Draf paper PDF belum tersedia di root folder."}, status_code=404)


# ------------------------------------------------------------------ #
#  WebSocket endpoint                                                 #
# ------------------------------------------------------------------ #

def _bounded_float(value, lo: float, hi: float) -> float:
    """
    float finite yang di-clamp ke [lo, hi]. Melempar ValueError untuk bool/None/NaN/inf/string
    aneh, sehingga handle_command membalas event "error" dan state pasar tidak tersentuh.
    """
    if value is None or isinstance(value, bool):
        raise ValueError("angka wajib diisi")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("angka harus finite")
    return max(lo, min(hi, f))


def _fundamental_or_error(value) -> float:
    f = valid_fundamental(value)
    if f is None:
        raise ValueError(FUNDAMENTAL_RANGE_MSG)
    return f


class _RateLimiter:
    """Token bucket sederhana per koneksi WebSocket."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self.tokens, self.last = burst, time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


def _origin_allowed(websocket: WebSocket) -> bool:
    """
    Tolak cross-site WebSocket hijacking: halaman dari domain lain yang dibuka pengguna tidak boleh
    mengendalikan simulasi di localhost. Tanpa header Origin (klien non-browser) → diizinkan.
    Host dibandingkan tanpa port (nginx meneruskan Host tanpa port). Tambahan lewat
    SIMPASAR_ALLOWED_ORIGINS (pisahkan dengan koma), mis. "https://simpasar.example".
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    extra = {o.strip().rstrip("/").lower() for o in os.environ.get("SIMPASAR_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    if origin.rstrip("/").lower() in extra:
        return True
    origin_host = (urlsplit(origin).hostname or "").lower()
    host = (websocket.headers.get("host") or "").lower()
    host_name = (urlsplit(f"//{host}").hostname or "").lower()
    local = {"localhost", "127.0.0.1", "::1"}
    return bool(origin_host) and (origin_host == host_name or (origin_host in local and host_name in local))


def _price_matches(a: Any, b: float) -> bool:
    try:
        return a is not None and abs(float(a) - b) <= max(1e-6, abs(b) * 1e-9)
    except (TypeError, ValueError):
        return False


def _source_for_client_price(symbol: str, fundamental: float) -> dict:
    """
    Asal harga untuk harga awal yang dikirim klien (set_symbol dengan fundamental, atau
    set_fundamental): bila cocok dengan data Sectors/cache/nilai cadangan simbol itu, pakai
    atribusinya (tanpa memanggil API); selain itu dicatat sebagai isian manual.
    """
    cached = None
    try:
        cached = peek_cached_price(symbol)
    except Exception:
        cached = None
    if cached and _price_matches(cached.get("price"), fundamental):
        return {"source_price": cached["price"], "source_date": cached.get("date"),
                "source": cached.get("source"), "source_kind": cached.get("source_kind")}
    if str(symbol).upper() == DEFAULT_START_SYMBOL and _price_matches(START_FALLBACK_PRICE, fundamental):
        # Nilai cadangan harga awal server (BBCA) juga "dikenal": atribusinya tidak jadi manual.
        fb = _fallback_start_meta()
        return {k: fb[k] for k in ("source_price", "source_date", "source", "source_kind")}
    return {"source_price": fundamental, "source_date": None, "source": MANUAL_SOURCE, "source_kind": "manual"}


async def handle_set_symbol(client: WSClient, msg: dict) -> None:
    """
    Alur set_symbol:
      1) validasi simbol (emiten di daftar atau IHSG) → symbol_error bila tidak dikenal;
      2) fundamental dari klien (≥ 1) dipakai langsung; bila tidak ada, ambil dari Sectors
         (cache dulu, baru API) → symbol_error {price_status} bila harga tidak tersedia;
      3) market.set_symbol → broadcast symbol_changed lalu snapshot penuh.
    symbol.source_kind: sectors | cache | stale | fallback (dari Sectors / nilai cadangan IHSG/BBCA)
    atau manual (harga dari klien yang tidak cocok dengan data Sectors mana pun).
    """
    raw_symbol = str(msg.get("symbol", "")).strip()
    meta = get_symbol_meta(raw_symbol)
    if meta is None:
        client.send({
            "event":   "symbol_error",
            "symbol":  raw_symbol[:16],
            "message": "Simbol tidak dikenal",
        })
        return

    raw_fundamental = msg.get("fundamental")
    fundamental = valid_fundamental(raw_fundamental)
    if raw_fundamental is not None and fundamental is None:
        client.send({
            "event":   "symbol_error",
            "symbol":  meta["symbol"],
            "message": f"Harga awal tidak valid: {FUNDAMENTAL_RANGE_MSG}",
        })
        return
    if fundamental is not None:
        # Klien sudah mengambil harga lewat REST; lengkapi tanggal/sumber dari cache tanpa memanggil API.
        meta.update(_source_for_client_price(meta["symbol"], fundamental))
    else:
        info = await get_stock_price(meta["symbol"])
        fundamental = valid_fundamental(info.get("price"))
        if fundamental is None:
            client.send({
                "event":        "symbol_error",
                "symbol":       meta["symbol"],
                "message":      info.get("message") or "Harga Sectors tidak tersedia",
                "price_status": info.get("status", "offline"),
            })
            return
        meta.update({"source_price": fundamental, "source_date": info.get("date"),
                     "source": info.get("source"), "source_kind": info.get("source_kind")})

    if not market.set_symbol(meta, fundamental):
        client.send({"event": "symbol_error", "symbol": meta["symbol"], "message": "Harga awal tidak valid"})
        return
    # Urutan per klien terjaga (satu antrean): symbol_changed selalu sebelum snapshot-nya.
    broadcast({"event": "symbol_changed", "symbol": dict(market.symbol), "fundamental": fundamental})
    broadcast(current_state(full=True))


async def handle_command(client: WSClient, msg: dict) -> None:
    global tick_rate
    cmd = str(msg.get("cmd", ""))

    # Semua angka dari klien divalidasi (finite + batas) sebelum menyentuh state pasar;
    # nilai tidak valid melempar ValueError → event "error" ke pengirim saja.
    if cmd == "inject_rumor":
        market.inject_rumor(_bounded_float(msg.get("strength", 1.0), -MAX_SENTIMENT, MAX_SENTIMENT))

    elif cmd == "inject_news_sentiment":
        # Injeksi sentimen berdasarkan berita nyata
        strength = _bounded_float(msg.get("strength", 1.0), -MAX_SENTIMENT, MAX_SENTIMENT)
        title = str(msg.get("title", "Berita IDX"))[:300]
        if strength >= 0:
            market.inject_rumor(strength)
        else:
            market.inject_panic(abs(strength))
        broadcast({
            "event": "news_injected",
            "title": title,
            "strength": strength,
            "sentiment": market.sentiment
        })

    elif cmd == "inject_panic":
        market.inject_panic(_bounded_float(msg.get("strength", 1.0), -MAX_SENTIMENT, MAX_SENTIMENT))

    elif cmd == "set_population":
        ok = market.set_population(
            _bounded_float(msg.get("fundamentalist", 0.30), 0.0, 1.0),
            _bounded_float(msg.get("chartist",      0.50), 0.0, 1.0),
            _bounded_float(msg.get("noise",         0.20), 0.0, 1.0),
        )
        if not ok:
            raise ValueError("komposisi investor tidak valid")
        # Semua klien: agen dibangun ulang (id sama), slider & alasan Gemini klien lain harus ikut.
        broadcast(current_state(full=True))

    elif cmd == "pause":
        market.pause()
        broadcast({"event": "paused"})

    elif cmd == "resume":
        market.resume()
        broadcast({"event": "resumed"})

    elif cmd == "reset":
        market.reset()
        broadcast(current_state(full=True))

    elif cmd == "set_fundamental":
        fundamental = _fundamental_or_error(msg.get("fundamental", 100.0))
        market.set_fundamental(fundamental)
        if not _price_matches(market.symbol.get("source_price"), fundamental):
            # Harga awal berubah: pakai atribusi Sectors/cache bila cocok (mis. "Terapkan IHSG riil"),
            # selain itu catat sebagai isian manual — sama dengan jalur set_symbol.
            market.symbol.update(_source_for_client_price(market.symbol.get("symbol", ""), fundamental))
        broadcast(current_state(full=True))

    elif cmd == "set_symbol":
        await handle_set_symbol(client, msg)

    elif cmd == "set_psych":
        ok = market.set_psych(
            _bounded_float(msg.get("disciplined", 0.34), 0.0, 1.0),
            _bounded_float(msg.get("bagholder",   0.33), 0.0, 1.0),
        )
        if not ok:
            raise ValueError("komposisi psikologi tidak valid")
        broadcast(current_state(full=True))

    elif cmd == "set_speed":
        # interval antar tick: 0.05 s (5x) – 1.5 s (lambat); semua klien menandai tombol yang sama
        tick_rate = _bounded_float(msg.get("interval", 0.25), TICK_INTERVAL_MIN, TICK_INTERVAL_MAX)
        broadcast({"event": "speed_changed", "tick_interval": round(tick_rate, 4)})

    elif cmd == "get_state":
        client.request_snapshot()


async def _read_commands(client: WSClient) -> None:
    """Baca perintah klien sampai koneksi putus. Balasan lewat antrean klien (urutan terjaga)."""
    websocket = client.ws
    limiter = _RateLimiter(rate=WS_MAX_CMDS_PER_SEC, burst=WS_MAX_CMDS_PER_SEC)
    last_limit_reply = float("-inf")
    while not client.closed:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        raw = message.get("text")
        if raw is None or len(raw) > WS_MAX_MESSAGE_BYTES:
            # Frame biner diabaikan. Frame > 64 KB normalnya sudah ditolak uvicorn (ws_max_size,
            # koneksi ditutup 1009); cek ini cadangan bila server dijalankan tanpa opsi itu.
            continue
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(msg, dict):
            continue
        cmd = str(msg.get("cmd", ""))[:40]
        if cmd == "get_state":
            # Tidak kena limiter: klien yang menunggu resinkron harus selalu mendapat snapshot.
            # Permintaan beruntun digabung (satu snapshot per giliran penulis).
            client.request_snapshot()
            continue
        if not limiter.allow():
            now = time.monotonic()
            if now - last_limit_reply >= 1.0:          # maks. 1 balasan per detik per koneksi
                last_limit_reply = now
                client.send({"event": "error", "cmd": cmd, "message": WS_RATE_LIMIT_MESSAGE})
            continue
        try:
            await handle_command(client, msg)
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            # Perintah yang rusak (mis. strength bukan angka) tidak boleh memutus koneksi.
            print(f"[ws] perintah {cmd!r} gagal: {type(exc).__name__}: {exc}", flush=True)
            client.send({
                "event":   "error",
                "cmd":     cmd,
                "message": (f"Perintah ditolak: {exc}" if isinstance(exc, ValueError)
                            else f"Perintah gagal diproses: {type(exc).__name__}")[:160],
            })


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket):
        print(f"[ws] koneksi ditolak dari Origin {websocket.headers.get('origin')!r}", flush=True)
        await websocket.close(code=1008)
        return
    await websocket.accept()
    client = WSClient(websocket)
    client.send(current_state(full=True))    # snapshot awal: selalu pesan pertama klien ini
    client.start()
    clients.add(client)
    reader = asyncio.create_task(_read_commands(client))
    try:
        # Selesai bila klien memutus (reader) ATAU penulis berhenti (koneksi putus / klien macet).
        await asyncio.wait({reader, client.writer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        clients.discard(client)
        reader.cancel()
        await client.aclose()
        with suppress(asyncio.CancelledError, Exception):
            await reader


# ------------------------------------------------------------------ #
#  Static Routes & Landing Page                                       #
# ------------------------------------------------------------------ #

app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
app.mount("/", StaticFiles(directory="landing", html=True), name="landing")


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, ws_max_size=WS_MAX_MESSAGE_BYTES)

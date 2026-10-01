"""
Market Mechanism Engine — harga terbentuk dari agregasi order beli/jual tiap tick.

Model market-maker dengan price impact dari net order flow:
    P(t+1) = P(t) * exp(lambda * mean_net_order)

Tidak ada formula "harga = f(sentimen)" langsung — semua lewat agregasi order agen.

Saham (symbol.kind != "index") dibatasi Auto Rejection BEI (ARA/ARB, lihat sim/idx_rules.py)
terhadap harga acuan `ref_price`: harga awal pada hari pertama, lalu penutupan hari bursa
sebelumnya setiap 330 tick. IHSG (indeks) tidak dibatasi.
"""
from __future__ import annotations
import math
from datetime import date
import numpy as np
from .agents import Agent, build_agents, get_llm_advisor, llm_status, set_news_context
from .idx_rules import limit_hit, price_limits
from .simtime import MINUTES_PER_DAY, default_start_date, sim_time_info

# Parameter default — dioptimasi via tuning/grid_search.py (576 kombinasi × 8 seeds × 600 tick)
# Target kalibrasi: harga berkisar ~60-190 di sekitar fundamental 100
# Hasil: max_ratio≈1.88  min_ratio≈0.60  stable 100%  score=10.00
DEFAULT_PARAMS: dict = {
    "lambda_price":            0.12,   # dampak harga per unit net order
    "fundamentalist_alpha":    2.50,   # kekuatan revert ke fair value
    "chartist_factor":         2.80,   # amplifikasi momentum
    "chartist_momentum_window": 8,     # lookback ticks
    "noise_sigma":             0.42,   # volatilitas agen noise
    "sentiment_sensitivity":   1.40,   # amplifikasi rumor ke noise agent
    # rasio info_style — 30/50/20 (divalidasi grid search)
    "fundamentalist_ratio":    0.30,
    "chartist_ratio":          0.50,
    "noise_ratio":             0.20,
    # rasio psych_profile — default merata
    "disciplined_ratio":       0.34,
    "bagholder_ratio":         0.33,
    # averager = sisanya
}

BUBBLE_THRESHOLD = 1.25   # harga > 125% fundamental → Bubble
CRASH_THRESHOLD  = 0.82   # harga < 82%  fundamental → Panik-Crash
MAX_HISTORY      = 2000   # 1 tick = 1 menit bursa → ±6 hari bursa (330 menit/hari)
HISTORY_WINDOW   = MAX_HISTORY   # kompatibilitas: snapshot penuh mengirim seluruh histori

# Batas masukan dari klien. Nilai di luar batas (termasuk NaN/inf) ditolak supaya state tidak
# pernah berisi angka non-finite: json.dumps akan memancarkan NaN/Infinity yang gagal di-parse
# browser, sehingga SEMUA klien membeku.
MIN_FUNDAMENTAL  = 1.0    # harga minimum BEI 1 rupiah (papan pemantauan khusus)
MAX_FUNDAMENTAL  = 1e9    # harga saham BEI tertinggi ± 1e6 rupiah; IHSG ± 1e4
MAX_SENTIMENT    = 3.0
# Berita bisa menggeser nilai wajar (orang fundamentalist menilai ulang). Satu berita maks. ±10%,
# dan total pergeseran dari harga awal dibatasi ±50% supaya simulasi tidak lepas kendali.
MAX_NEWS_SHIFT_PCT   = 10.0
MAX_NEWS_DRIFT_RATIO = 0.5
# "Sedang untung" / "sedang rugi" untuk statistik & grid: P&L di luar ±2%.
PNL_STATE_EPS = 0.02
FUNDAMENTAL_RANGE_MSG = "harga awal harus angka antara 1 dan 1.000.000.000"

# Lantai harga relatif terhadap fundamental (0,5%). Lantai absolut lama (0,5) membuat saham
# berharga < 0,4 langsung terkunci di atas fundamental → status "Bubble" permanen.
PRICE_FLOOR_RATIO = 0.005

# Asal harga awal simbol (field symbol.source_kind):
#   "sectors"  harga baru dari API Sectors      "cache"    cache Sectors yang masih segar
#   "stale"    cache kedaluwarsa (API gagal)    "manual"   diisi pengguna
#   "fallback" nilai cadangan (IHSG / harga awal server) saat offline
#   None       belum ada data (default awal)
SOURCE_KINDS = ("sectors", "cache", "stale", "manual", "fallback")


def _is_finite(*values: float) -> bool:
    try:
        return all(math.isfinite(float(v)) for v in values)
    except (TypeError, ValueError):
        return False


def valid_fundamental(value) -> float | None:
    """float dalam [MIN_FUNDAMENTAL, MAX_FUNDAMENTAL], atau None (termasuk bool/NaN/inf/string aneh)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if (math.isfinite(f) and MIN_FUNDAMENTAL <= f <= MAX_FUNDAMENTAL) else None

# Simbol default Market(): IHSG sebagai indeks (templat netral untuk set_symbol). Server memulai
# simulasi di BBCA lewat set_symbol (lihat server.py, SIMPASAR_START_SYMBOL).
DEFAULT_SYMBOL: dict = {
    "symbol":       "IHSG",
    "name":         "Indeks Harga Saham Gabungan",
    "sector":       "index",
    "sector_name":  "Indeks",
    "kind":         "index",
    "source_price": None,     # harga Sectors yang menjadi fundamental awal
    "source_date":  None,     # tanggal harga tersebut
    "source":       None,     # deskripsi sumber (mis. "Sectors Financial API (fetch-daily-price)")
    "source_kind":  None,     # salah satu SOURCE_KINDS, atau None sebelum ada data
}


class Market:
    def __init__(
        self,
        n_agents:    int   = 100,
        fundamental: float = 100.0,
        params:      dict | None = None,
        seed:        int  | None = None,
    ):
        self.fundamental = fundamental
        self.base_fundamental = fundamental   # nilai wajar awal (sebelum digeser berita); dipakai reset
        self.price       = fundamental
        # Harga acuan ARA/ARB: harga awal di hari pertama, lalu penutupan hari sebelumnya.
        self.ref_price   = fundamental
        self.params      = {**DEFAULT_PARAMS, **(params or {})}
        self.n_agents    = n_agents
        self._seed       = seed

        self._rng              = np.random.default_rng(seed)
        self.price_history:    list[float] = [fundamental]
        # Volume = total |order| seluruh agen per tick (intensitas transaksi).
        # Tick 0 adalah harga awal tanpa transaksi → volume 0.
        self.volume_history:   list[float] = [0.0]
        self.volume:           float = 0.0
        self.tick:             int   = 0
        self.sentiment:        float = 0.0
        self.sentiment_decay:  float = 0.72
        self.is_paused:        bool  = False

        # Simbol yang disimulasikan (lihat DEFAULT_SYMBOL) & tanggal awal kalender simulasi.
        self.symbol:     dict = dict(DEFAULT_SYMBOL)
        self.start_date: date = default_start_date()

        self.agents: list[Agent] = self._make_agents()
        self._order_log: list[float] = []

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _limits(self) -> dict:
        """Batas ARA/ARB dari harga acuan saat ini (indeks → applies False)."""
        return price_limits(self.ref_price, self.symbol.get("kind"))

    def _make_agents(self, params: dict | None = None) -> list[Agent]:
        p = self.params if params is None else params
        return build_agents(
            self.n_agents,
            p["fundamentalist_ratio"],
            p["chartist_ratio"],
            self._rng,
            d_ratio=p["disciplined_ratio"],
            b_ratio=p["bagholder_ratio"],
        )

    # ------------------------------------------------------------------ #
    #  Core step                                                           #
    # ------------------------------------------------------------------ #

    def step(self) -> dict:
        """Satu tick (= 1 menit bursa). Mengembalikan state delta (tanpa histori)."""
        if self.is_paused:
            return self.get_state(full=False)

        # Tick berikutnya membuka hari bursa baru → harga acuan = penutupan kemarin.
        if (self.tick + 1) % MINUTES_PER_DAY == 0:
            self.ref_price = self.price

        orders: list[float] = []
        for agent in self.agents:
            o = agent.decide(
                self.price, self.fundamental,
                self.price_history, self.sentiment,
                self.params, self._rng,
            )
            orders.append(o)

        # Kirim sebagian kecil permintaan LLM yang ditawarkan agen selama tick ini (non-blocking).
        # Jawaban baru dipakai saat agen yang sama bereaksi lagi setelah jawaban tiba,
        # biasanya belasan sampai puluhan tick kemudian, dan hanya bila masih relevan.
        advisor = get_llm_advisor()
        if advisor is not None:
            advisor.end_tick()

        net_order = float(np.mean(orders))
        self._order_log.append(net_order)
        if len(self._order_log) > MAX_HISTORY:
            self._order_log.pop(0)
        self.volume = float(np.sum(np.abs(orders)))

        price_return = self.params["lambda_price"] * net_order
        # float Python (nilai identik) supaya get_state tidak membulatkan ratusan numpy.float64 tiap tick.
        floor = self.fundamental * PRICE_FLOOR_RATIO
        new_price = float(max(floor, self.price * np.exp(price_return)))
        limits = self._limits()
        if limits["applies"] and math.isfinite(new_price):
            # Auto Rejection: order di luar [ARB, ARA] ditolak bursa → harga tertahan di batas.
            new_price = min(limits["ara"], max(limits["arb"], new_price))
        if math.isfinite(new_price):   # jangan pernah biarkan inf masuk state (JSON rusak di browser)
            self.price = new_price
        if not math.isfinite(self.volume):
            self.volume = 0.0

        self.price_history.append(self.price)
        self.volume_history.append(self.volume)
        if len(self.price_history) > MAX_HISTORY:
            self.price_history.pop(0)
        if len(self.volume_history) > MAX_HISTORY:
            self.volume_history.pop(0)

        self.sentiment *= self.sentiment_decay
        if abs(self.sentiment) < 1e-4:
            self.sentiment = 0.0

        self.tick += 1
        return self.get_state(full=False)

    # ------------------------------------------------------------------ #
    #  Controls                                                            #
    # ------------------------------------------------------------------ #

    def _add_sentiment(self, delta: float) -> bool:
        if not _is_finite(delta):
            return False
        self.sentiment = max(-MAX_SENTIMENT, min(MAX_SENTIMENT, self.sentiment + float(delta)))
        return True

    def inject_rumor(self, strength: float = 1.0) -> bool:
        return self._add_sentiment(strength)

    def inject_panic(self, strength: float = 1.0) -> bool:
        return _is_finite(strength) and self._add_sentiment(-float(strength))

    def shift_fundamental(self, pct: float) -> float | None:
        """
        Geser nilai wajar sebesar `pct` persen tanpa memulai ulang simulasi (berita fundamental:
        orang fundamentalist menilai ulang harga yang pantas). Dibatasi ±MAX_NEWS_SHIFT_PCT per
        berita dan ±MAX_NEWS_DRIFT_RATIO dari nilai wajar awal. Mengembalikan nilai wajar baru,
        atau None bila ditolak.
        """
        if not _is_finite(pct):
            return None
        pct = max(-MAX_NEWS_SHIFT_PCT, min(MAX_NEWS_SHIFT_PCT, float(pct)))
        lo = self.base_fundamental * (1 - MAX_NEWS_DRIFT_RATIO)
        hi = self.base_fundamental * (1 + MAX_NEWS_DRIFT_RATIO)
        target = valid_fundamental(max(lo, min(hi, self.fundamental * (1 + pct / 100.0))))
        if target is None:
            return None
        self.fundamental = target
        return target

    def set_population(self, fundamentalist: float, chartist: float, noise: float) -> bool:
        """Ubah rasio info_style. Rasio harus finite dan ≥ 0; False (tanpa perubahan) bila ditolak."""
        if not _is_finite(fundamentalist, chartist, noise):
            return False
        f, c, n = float(fundamentalist), float(chartist), float(noise)
        total = f + c + n
        if min(f, c, n) < 0 or total <= 0:
            return False
        # Bangun agen dengan parameter kandidat dulu; params baru ditulis setelah berhasil,
        # sehingga kegagalan tidak pernah meninggalkan state setengah rusak.
        params = {**self.params, "fundamentalist_ratio": f / total, "chartist_ratio": c / total, "noise_ratio": n / total}
        agents = self._make_agents(params)
        self.params, self.agents = params, agents
        return True

    def set_psych(self, disciplined: float, bagholder: float) -> bool:
        """Ubah rasio psych_profile; averager = sisanya. False (tanpa perubahan) bila ditolak."""
        if not _is_finite(disciplined, bagholder):
            return False
        d, b = float(disciplined), float(bagholder)
        if min(d, b) < 0 or d + b <= 0 or d + b > 1.0:
            return False
        params = {**self.params, "disciplined_ratio": d, "bagholder_ratio": b}
        agents = self._make_agents(params)
        self.params, self.agents = params, agents
        return True

    def set_fundamental(self, fundamental: float) -> bool:
        """Update fundamental baseline price (misal disinkronkan dengan harga Sectors); histori direset."""
        fundamental = valid_fundamental(fundamental)
        if fundamental is None:
            return False
        self.fundamental = fundamental
        self.base_fundamental = fundamental
        self.price = fundamental
        self.ref_price = fundamental
        self.price_history = [fundamental]
        self.volume_history = [0.0]
        self.volume = 0.0
        self.tick = 0
        self.sentiment = 0.0
        self._order_log = []
        self.start_date = default_start_date()
        self.agents = self._make_agents()
        set_news_context("", 0.0)                 # berita lama tidak berlaku untuk simulasi baru
        return True

    def set_symbol(self, meta: dict, fundamental: float) -> bool:
        """
        Ganti simbol yang disimulasikan lalu mulai ulang di `fundamental` (harga awal).

        `meta` berisi symbol/name/sector/sector_name/kind dan opsional
        source_price/source_date/source (asal harga, mis. Sectors).
        False (simbol TIDAK diganti) bila `fundamental` tidak valid, supaya simbol
        baru tidak pernah tampil dengan harga/histori milik simbol lama.
        """
        fundamental = valid_fundamental(fundamental)
        if fundamental is None:
            return False
        new_symbol = dict(DEFAULT_SYMBOL)
        for key in new_symbol:
            if key in meta and meta[key] is not None:
                new_symbol[key] = meta[key]
        new_symbol["symbol"] = str(new_symbol["symbol"]).upper()
        if new_symbol.get("source_price") is None:
            # Tanpa info asal: harga awal = fundamental yang diberikan langsung (manual).
            new_symbol["source_price"] = fundamental
            if new_symbol.get("source_kind") is None:
                new_symbol["source_kind"] = "manual"
        if new_symbol.get("source_kind") not in SOURCE_KINDS:
            new_symbol["source_kind"] = None
        self.symbol = new_symbol
        return self.set_fundamental(fundamental)

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self._seed = seed
        self._rng          = np.random.default_rng(self._seed)
        self.fundamental   = self.base_fundamental   # pergeseran nilai wajar dari berita ikut dibatalkan
        self.price         = self.fundamental
        self.ref_price     = self.fundamental
        self.price_history = [self.fundamental]
        self.volume_history = [0.0]
        self.volume        = 0.0
        self.tick          = 0
        self.sentiment     = 0.0
        self._order_log    = []
        self.start_date    = default_start_date()
        self.agents        = self._make_agents()
        set_news_context("", 0.0)

    def pause(self)  -> None: self.is_paused = True
    def resume(self) -> None: self.is_paused = False

    # ------------------------------------------------------------------ #
    #  State snapshot                                                      #
    # ------------------------------------------------------------------ #

    def get_state(self, full: bool = True) -> dict:
        """
        State untuk klien. full=True → snapshot (sertakan price_history & volume_history,
        "snapshot": true); full=False → delta per tick tanpa histori ("snapshot": false),
        klien menambahkan `price`/`volume` sendiri.
        """
        ratio  = self.price / self.fundamental
        status = (
            "Bubble"      if ratio > BUBBLE_THRESHOLD else
            "Panik-Crash" if ratio < CRASH_THRESHOLD  else
            "Normal"
        )

        # ── Agregat psych stats ──────────────────────────────────────
        in_position   = [a for a in self.agents if a.position > 0]
        pain_agents   = [a for a in in_position if a.in_pain]
        avg_pnl       = float(np.mean([a.pnl_pct for a in in_position])) if in_position else 0.0
        adding        = [a for a in self.agents if a.last_added]   # benar-benar menambah posisi tick ini
        averager_act  = len(adding)
        averaging_up  = sum(1 for a in adding if a.pnl_pct >= 0)
        in_profit     = sum(1 for a in in_position if a.pnl_pct > PNL_STATE_EPS)

        limits = self._limits()
        limits["hit"] = limit_hit(self.price, limits)       # "ara" | "arb" | None
        limits["day"] = self.tick // MINUTES_PER_DAY + 1    # hari bursa ke-n (1-based)

        state = {
            "tick":        self.tick,
            "price":       round(self.price, 2),
            "fundamental": self.fundamental,
            "status":      status,
            "sentiment":   round(self.sentiment, 3),
            "paused":      self.is_paused,
            "llm":         llm_status(),
            "volume":      round(self.volume, 2),
            "agents":      [a.to_dict() for a in self.agents],
            "symbol":      dict(self.symbol),
            "sim_time":    sim_time_info(self.tick, self.start_date),
            "limits":      limits,
            "snapshot":    bool(full),
            "params": {
                "f_ratio": round(self.params["fundamentalist_ratio"], 2),
                "c_ratio": round(self.params["chartist_ratio"], 2),
                "n_ratio": round(self.params["noise_ratio"], 2),
                "d_ratio": round(self.params["disciplined_ratio"], 2),
                "b_ratio": round(self.params["bagholder_ratio"], 2),
            },
            "psych_stats": {
                "in_position":  len(in_position),
                "in_pain":      len(pain_agents),
                "averaging":    averager_act,                  # total sedang nambah posisi
                "averaging_up":   averaging_up,                # nambah saat untung (average up)
                "averaging_down": averager_act - averaging_up, # nambah saat rugi (average down)
                "in_profit":    in_profit,
                "avg_pnl_pct":  round(avg_pnl * 100, 1),
            },
        }
        if full:
            state["price_history"]  = [round(p, 2) for p in self.price_history[-MAX_HISTORY:]]
            state["volume_history"] = [round(v, 2) for v in self.volume_history[-MAX_HISTORY:]]
        return state

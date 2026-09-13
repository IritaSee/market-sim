"""
Market Mechanism Engine — harga terbentuk dari agregasi order beli/jual tiap tick.

Model market-maker dengan price impact dari net order flow:
    P(t+1) = P(t) * exp(lambda * mean_net_order)

Tidak ada formula "harga = f(sentimen)" langsung — semua lewat agregasi order agen.
"""
from __future__ import annotations
import numpy as np
from .agents import Agent, build_agents, get_llm_advisor, llm_status

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
MAX_HISTORY      = 500
HISTORY_WINDOW   = 400    # tick terakhir yang dikirim ke klien (harga & volume) untuk candle chart


class Market:
    def __init__(
        self,
        n_agents:    int   = 100,
        fundamental: float = 100.0,
        params:      dict | None = None,
        seed:        int  | None = None,
    ):
        self.fundamental = fundamental
        self.price       = fundamental
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

        self.agents: list[Agent] = self._make_agents()
        self._order_log: list[float] = []

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _make_agents(self) -> list[Agent]:
        return build_agents(
            self.n_agents,
            self.params["fundamentalist_ratio"],
            self.params["chartist_ratio"],
            self._rng,
            d_ratio=self.params["disciplined_ratio"],
            b_ratio=self.params["bagholder_ratio"],
        )

    # ------------------------------------------------------------------ #
    #  Core step                                                           #
    # ------------------------------------------------------------------ #

    def step(self) -> dict:
        if self.is_paused:
            return self.get_state()

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
        self.volume = float(np.sum(np.abs(orders)))

        price_return = self.params["lambda_price"] * net_order
        # float Python (nilai identik) supaya get_state tidak membulatkan ratusan numpy.float64 tiap tick.
        self.price   = float(max(0.5, self.price * np.exp(price_return)))

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
        return self.get_state()

    # ------------------------------------------------------------------ #
    #  Controls                                                            #
    # ------------------------------------------------------------------ #

    def inject_rumor(self, strength: float = 1.0) -> None:
        self.sentiment = min(3.0, self.sentiment + strength)

    def inject_panic(self, strength: float = 1.0) -> None:
        self.sentiment = max(-3.0, self.sentiment - strength)

    def set_population(self, fundamentalist: float, chartist: float, noise: float) -> None:
        total = fundamentalist + chartist + noise
        if total <= 0:
            return
        self.params["fundamentalist_ratio"] = fundamentalist / total
        self.params["chartist_ratio"]       = chartist / total
        self.params["noise_ratio"]          = noise / total
        self.agents = self._make_agents()

    def set_psych(self, disciplined: float, bagholder: float) -> None:
        """Ubah rasio psych_profile; averager = sisanya."""
        total = disciplined + bagholder
        if total <= 0 or (disciplined + bagholder) > 1.0:
            return
        self.params["disciplined_ratio"] = disciplined
        self.params["bagholder_ratio"]   = bagholder
        self.agents = self._make_agents()

    def set_fundamental(self, fundamental: float) -> None:
        """Update fundamental baseline price (misal disinkronkan dengan IHSG riil)."""
        if fundamental <= 0:
            return
        self.fundamental = fundamental
        self.price = fundamental
        self.price_history = [fundamental]
        self.volume_history = [0.0]
        self.volume = 0.0
        self.tick = 0
        self.sentiment = 0.0
        self._order_log = []
        self.agents = self._make_agents()

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self._seed = seed
        self._rng          = np.random.default_rng(self._seed)
        self.price         = self.fundamental
        self.price_history = [self.fundamental]
        self.volume_history = [0.0]
        self.volume        = 0.0
        self.tick          = 0
        self.sentiment     = 0.0
        self._order_log    = []
        self.agents        = self._make_agents()

    def pause(self)  -> None: self.is_paused = True
    def resume(self) -> None: self.is_paused = False

    # ------------------------------------------------------------------ #
    #  State snapshot                                                      #
    # ------------------------------------------------------------------ #

    def get_state(self) -> dict:
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
        averager_act  = sum(1 for a in self.agents
                            if a.psych_profile == "averager" and a.last_action == "buy"
                            and a.position > 1.05)  # beli tambahan (position > baseline)

        return {
            "tick":        self.tick,
            "price":       round(self.price, 2),
            "fundamental": self.fundamental,
            "status":      status,
            "sentiment":   round(self.sentiment, 3),
            "paused":      self.is_paused,
            "llm":         llm_status(),
            "volume":      round(self.volume, 2),
            "agents":      [a.to_dict() for a in self.agents],
            "price_history":  [round(p, 2) for p in self.price_history[-HISTORY_WINDOW:]],
            "volume_history": [round(v, 2) for v in self.volume_history[-HISTORY_WINDOW:]],
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
                "averaging":    averager_act,
                "avg_pnl_pct":  round(avg_pnl * 100, 1),
            },
        }

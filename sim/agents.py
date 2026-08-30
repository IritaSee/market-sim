"""
Agen-agen pasar modal — dua sumbu:
  - info_style   : cara baca informasi pasar (fundamentalist / chartist / noise)
  - psych_profile: respons psikologis terhadap P&L (disciplined / bagholder / averager)

Psych profile MENG-OVERRIDE sinyal info_style saat threshold P&L terlewati.
pain_threshold & greed_threshold di-SAMPLE RANDOM per agen → mencegah gelombang
aksi serentak yang terlalu sempurna.
"""
from __future__ import annotations
import numpy as np
from dataclasses import dataclass, field
from typing import Literal

AgentType   = Literal["fundamentalist", "chartist", "noise"]
PsychProfile = Literal["disciplined", "bagholder", "averager"]
Action       = Literal["buy", "sell", "hold"]


@dataclass
class Agent:
    id:           int
    agent_type:   AgentType
    psych_profile: PsychProfile = "disciplined"

    # ── Posisi & modal ────────────────────────────────────────────────
    position:          float = 0.0   # 0 = flat; 1.0 = posisi penuh; >1.0 = averaging down
    entry_price:       float = 0.0   # harga rata-rata masuk (update saat averaging)
    capital_remaining: float = 1.0   # sisa modal untuk averaging (1.0 = penuh)

    # ── Threshold psikologis (di-sample random per agen saat dibuat) ──
    pain_threshold:       float = 0.10  # % loss sebelum bereaksi
    greed_threshold:      float = 0.15  # % profit sebelum take-profit
    reaction_delay_prob:  float = 0.80  # prob bereaksi di tick ini

    # ── State terakhir ────────────────────────────────────────────────
    last_order:  float  = 0.0
    last_action: Action = "hold"
    pnl_pct:     float  = 0.0   # untuk visualisasi
    in_pain:     bool   = False

    # ── Sinyal dasar dari info_style (di-override oleh subclass) ─────
    def _base_signal(
        self,
        price: float,
        fundamental: float,
        price_history: list[float],
        sentiment: float,
        params: dict,
        rng: np.random.Generator,
    ) -> float:
        raise NotImplementedError

    # ── Override psikologis berdasarkan P&L ──────────────────────────
    def _psych_override(
        self,
        base_order: float,
        price: float,
        rng: np.random.Generator,
    ) -> float:
        """
        Return final order setelah filter psych_profile.
        Hanya aktif jika agen punya posisi (position > 0).
        """
        if self.position <= 0 or self.entry_price <= 0:
            return base_order

        pnl = float((price - self.entry_price) / self.entry_price)
        self.pnl_pct = pnl

        # Probabilistik — tidak semua agen bereaksi di tick yang sama
        if rng.random() > self.reaction_delay_prob:
            return base_order

        self.in_pain = bool(pnl < -self.pain_threshold)

        if self.psych_profile == "disciplined":
            if pnl < -self.pain_threshold:
                return -1.5   # CUT LOSS — jual semua
            if pnl > self.greed_threshold:
                return -1.5   # TAKE PROFIT — jual semua

        elif self.psych_profile == "bagholder":
            if pnl < -self.pain_threshold:
                # Tahan! Jual hanya 5-20% dari sinyal jual
                if base_order < 0:
                    partial = rng.uniform(0.05, 0.20)
                    return base_order * partial
                return base_order   # sinyal beli tetap jalan
            if pnl > self.greed_threshold:
                return -1.5   # Take profit sama seperti disciplined

        elif self.psych_profile == "averager":
            if pnl < -self.pain_threshold and self.capital_remaining > 0.15:
                # AVERAGING DOWN — beli tambahan
                buy_qty = min(self.capital_remaining * 0.55, 0.8)
                # Update entry_price ke rata-rata tertimbang baru
                total = self.position + buy_qty
                self.entry_price = (
                    self.entry_price * self.position + price * buy_qty
                ) / total
                self.position = min(2.5, total)
                self.capital_remaining = max(0.0, self.capital_remaining - buy_qty)
                return 1.5   # STRONG BUY
            if pnl > self.greed_threshold:
                return -1.5   # Take profit

        return base_order

    # ── Update state posisi setelah order dikirim ────────────────────
    def _update_position(self, order: float, price: float) -> None:
        if self.position <= 0:
            # Saat ini flat — masuk posisi kalau sinyal beli cukup kuat
            if order > 0.35:
                self.position         = 1.0
                self.entry_price      = price
                self.capital_remaining = 1.0
                self.pnl_pct          = 0.0
        else:
            # Punya posisi — keluar kalau sinyal jual kuat
            if order < -1.0:
                self.position          = 0.0
                self.entry_price       = 0.0
                self.capital_remaining = 1.0
                self.pnl_pct           = 0.0
                self.in_pain           = False

    # ── Entry point utama dipanggil dari Market.step() ───────────────
    def decide(
        self,
        price: float,
        fundamental: float,
        price_history: list[float],
        sentiment: float,
        params: dict,
        rng: np.random.Generator,
    ) -> float:
        base  = self._base_signal(price, fundamental, price_history, sentiment, params, rng)
        final = self._psych_override(base, price, rng)
        self._update_position(final, price)

        self.last_order  = final
        self.last_action = "buy" if final > 0.05 else "sell" if final < -0.05 else "hold"
        return final

    # ── Snapshot untuk WebSocket ─────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id":     self.id,
            "type":   self.agent_type,
            "psych":  self.psych_profile,
            "action": self.last_action,
            "order":  round(float(self.last_order), 3),
            "pnl":    round(float(self.pnl_pct) * 100, 1),
            "pos":    round(float(self.position), 2),
            "pain":   bool(self.in_pain),
        }


# ── Subclass per info_style ───────────────────────────────────────────


class FundamentalistAgent(Agent):
    def __init__(self, agent_id: int):
        super().__init__(agent_id, "fundamentalist")

    def _base_signal(self, price, fundamental, price_history, sentiment, params, rng):
        deviation = (price - fundamental) / fundamental
        raw = -params["fundamentalist_alpha"] * deviation
        return float(np.clip(raw + rng.normal(0.0, 0.06), -1.5, 1.5))


class ChartistAgent(Agent):
    def __init__(self, agent_id: int):
        super().__init__(agent_id, "chartist")

    def _base_signal(self, price, fundamental, price_history, sentiment, params, rng):
        w = params["chartist_momentum_window"]
        if len(price_history) < w + 1:
            return float(rng.normal(0.0, 0.1))
        past = price_history[-(w + 1)]
        momentum = (price - past) / (past + 1e-9)
        raw = params["chartist_factor"] * momentum
        return float(np.clip(raw + rng.normal(0.0, 0.06), -1.5, 1.5))


class NoiseAgent(Agent):
    def __init__(self, agent_id: int):
        super().__init__(agent_id, "noise")

    def _base_signal(self, price, fundamental, price_history, sentiment, params, rng):
        base = rng.normal(0.0, params["noise_sigma"])
        kick = params["sentiment_sensitivity"] * sentiment
        return float(np.clip(base + kick, -1.5, 1.5))


# ── Factory ────────────────────────────────────────────────────────────


def build_agents(
    n_agents:   int,
    f_ratio:    float,
    c_ratio:    float,
    rng:        np.random.Generator,
    d_ratio:    float = 0.34,   # disciplined
    b_ratio:    float = 0.33,   # bagholder
    # averager  = 1 - d - b
) -> list[Agent]:
    """Buat populasi agen — crossproduct info_style × psych_profile."""

    # ── Info-style pool ──
    n_f = round(n_agents * f_ratio)
    n_c = round(n_agents * c_ratio)
    n_n = n_agents - n_f - n_c

    pool: list[Agent] = []
    for i in range(n_f):
        pool.append(FundamentalistAgent(i))
    for i in range(n_c):
        pool.append(ChartistAgent(n_f + i))
    for i in range(n_n):
        pool.append(NoiseAgent(n_f + n_c + i))

    rng.shuffle(pool)

    # ── Psych-profile assignment (random, terpisah dari info_style) ──
    n_d = round(n_agents * d_ratio)
    n_b = round(n_agents * b_ratio)
    n_a = n_agents - n_d - n_b

    psych_list = (
        ["disciplined"] * n_d
        + ["bagholder"]  * n_b
        + ["averager"]   * n_a
    )
    rng.shuffle(psych_list)

    for idx, (agent, psych) in enumerate(zip(pool, psych_list)):
        agent.id           = idx
        agent.psych_profile = psych

        # Sample threshold unik per agen — mencegah aksi massal di tick yang sama
        # Konteks IDX retail: investor retail cenderung tahan lama sebelum cut-loss
        # pain_threshold ~0.25 = 25% loss dulu baru react (sesuai perilaku "nyangkut")
        # greed_threshold ~0.45 = 45% profit dulu baru take-profit (agar bubble bisa terbentuk)
        agent.pain_threshold      = float(np.clip(rng.normal(0.25, 0.08), 0.10, 0.50))
        agent.greed_threshold     = float(np.clip(rng.normal(0.45, 0.10), 0.20, 0.80))
        agent.reaction_delay_prob = float(rng.uniform(0.50, 0.80))

    return pool

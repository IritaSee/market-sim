"""
Agen-agen pasar modal — dua sumbu:
  - info_style   : cara baca informasi pasar (fundamentalist / chartist / noise)
  - psych_profile: respons psikologis terhadap P&L (disciplined / bagholder / averager)

Psych profile MENG-OVERRIDE sinyal info_style saat agen sedang punya posisi.
Override ini didorong oleh LLM yang benar-benar berperan sebagai persona
psikologis agen (lihat _PERSONA_DESCRIPTIONS) — bukan cuma mengulang rumus
threshold, tapi menimbang mood, keraguan, dan konteks momen itu sendiri.
pain_threshold & greed_threshold di-SAMPLE RANDOM per agen dan hanya jadi
referensi longgar buat LLM (bukan garis pemicu keras) → mencegah gelombang
aksi serentak yang terlalu sempurna. Fallback deterministik (dipakai kalau
LLM tidak tersedia) juga sengaja diberi jitter, bukan step function murni.
"""
from __future__ import annotations
import os
import json
from google import genai

_gemini_client = None
def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key:
            _gemini_client = genai.Client(api_key=api_key)
        else:
            try:
                _gemini_client = genai.Client()
            except Exception:
                pass
    return _gemini_client

import numpy as np
from dataclasses import dataclass, field
from typing import Literal

AgentType   = Literal["fundamentalist", "chartist", "noise"]
PsychProfile = Literal["disciplined", "bagholder", "averager"]
Action       = Literal["buy", "sell", "hold"]

# ── Personas fed to the LLM — deep behavioral traits, not formulas ─────
# Deliberately avoids hard "if loss > X then sell" language so the model
# reasons about the person's psychology instead of re-deriving the old
# deterministic rule in prose.
_PERSONA_DESCRIPTIONS: dict[str, str] = {
    "disciplined": (
        "A systematic trader who genuinely tries to follow a plan. You're not a robot, though — "
        "doubt creeps in as losses linger, and impatience creeps in as gains linger. You lean "
        "toward cutting losers and banking winners once the discomfort gets real, but the exact "
        "moment you finally act depends on your mood, how the price has been moving, and whether "
        "today you're feeling confident or shaky."
    ),
    "bagholder": (
        "Someone who hates admitting a trade was wrong. When a position turns red you rationalize — "
        "'it'll bounce back', 'I'm not selling at a loss', 'this is just noise'. You anchor hard to "
        "your entry price, as if that's what the stock 'should' be worth. Mostly you freeze and hold, "
        "maybe trimming a sliver to ease the anxiety, but if the pain becomes truly unbearable you can "
        "suddenly capitulate and dump far more than usual — panic-selling near the bottom. When you're "
        "finally green, you grab the profit fast and with relief, sometimes too early, because you "
        "remember exactly what holding a loser felt like."
    ),
    "averager": (
        "A conviction investor who sees dips as a discount, not a warning — 'more shares, lower "
        "average'. You genuinely believe in the trade. But you're not infinitely confident: as losses "
        "deepen and your dry powder runs low, doubt creeps in and you buy smaller, more hesitant "
        "amounts, or stop averaging altogether once capital is scarce. When the trade finally turns "
        "profitable, you take the win — averaging down was stressful and you want the relief."
    ),
}


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
    last_reason: str    = ""   # alasan singkat dari LLM (kosong kalau fallback)
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
        self.last_reason = ""

        client = get_gemini_client()
        if client is not None:
            try:
                persona = _PERSONA_DESCRIPTIONS[self.psych_profile]
                prompt = (
                    f"You are role-playing as one individual retail investor inside a stock-market "
                    f"simulation. Think and react as this specific person would — not as a formula.\n\n"
                    f"Who you are: {persona}\n\n"
                    f"Your trading style is '{self.agent_type}', which just produced a cold, rational "
                    f"base signal of {base_order:+.3f} (range -1.5 = strong sell, +1.5 = strong buy) "
                    f"before your feelings about your own position get involved.\n\n"
                    f"Your situation right now:\n"
                    f"- Current price: {price:.2f}\n"
                    f"- Your average entry price: {self.entry_price:.2f}\n"
                    f"- Unrealized P&L: {pnl * 100:+.2f}%\n"
                    f"- Position size: {self.position:.2f} (1.0 = one full position)\n"
                    f"- Capital you still have free to deploy: {self.capital_remaining * 100:.0f}%\n"
                    f"- Roughly speaking, real pain starts creeping in somewhere around a "
                    f"{self.pain_threshold * 100:.0f}% loss, and the itch to take profit builds "
                    f"somewhere around a {self.greed_threshold * 100:.0f}% gain — but these are only "
                    f"loose feelings, not tripwires. Whether you act earlier out of anxiety, later out "
                    f"of stubbornness or hope, or not at all, is a judgment call only you would make.\n"
                    f"- A personal 'gut feeling' seed for this exact moment, just so your reaction isn't "
                    f"mechanically identical every time you find yourself in a similar spot: "
                    f"{rng.uniform(0, 1):.3f}\n\n"
                    f"Given who you are and how this specific moment feels to you, what do you actually "
                    f"do? Respond with JSON matching the schema: a float 'order' between -1.5 (panic-sell "
                    f"everything) and +1.5 (buy aggressively), and a short 'reason' (<=15 words) capturing "
                    f"the feeling behind it."
                )

                interaction = client.interactions.create(
                    model="gemini-3.1-flash-lite",
                    input=prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "order":  {"type": "number"},
                                "reason": {"type": "string"},
                            },
                            "required": ["order"],
                        },
                    },
                )
                data = json.loads(interaction.output_text.strip())
                self.last_reason = str(data.get("reason", ""))
                val = float(data["order"])
                return float(np.clip(val, -1.5, 1.5))
            except Exception:
                pass

        # ── Fallback rules — only reached if the LLM call is unavailable ──
        # or errors out. Kept intentionally non-deterministic (severity
        # scales with distance past threshold, plus per-agent jitter) so a
        # missing API key doesn't collapse everyone back into a step function.
        if self.psych_profile == "disciplined":
            if pnl < -self.pain_threshold:
                severity = 0.5 + min(1.0, (-pnl - self.pain_threshold) / self.pain_threshold)
                return -1.5 * float(np.clip(severity * rng.uniform(0.6, 1.0), 0.3, 1.0))
            if pnl > self.greed_threshold:
                severity = 0.5 + min(1.0, (pnl - self.greed_threshold) / self.greed_threshold)
                return -1.5 * float(np.clip(severity * rng.uniform(0.6, 1.0), 0.3, 1.0))

        elif self.psych_profile == "bagholder":
            if pnl < -self.pain_threshold:
                # Tahan! Jual hanya sebagian kecil, kecuali sudah benar-benar tak tertahankan
                if base_order < 0:
                    capitulate = -pnl > 2 * self.pain_threshold and rng.random() < 0.15
                    partial = rng.uniform(0.6, 1.0) if capitulate else rng.uniform(0.05, 0.20)
                    return base_order * partial
                return base_order   # sinyal beli tetap jalan
            if pnl > self.greed_threshold:
                relief = rng.uniform(0.7, 1.0)   # buru-buru ambil untung, kadang terlalu cepat
                return -1.5 * float(relief)

        elif self.psych_profile == "averager":
            if pnl < -self.pain_threshold and self.capital_remaining > 0.15:
                # AVERAGING DOWN — beli tambahan, makin ragu saat modal menipis
                confidence = float(np.clip(self.capital_remaining * rng.uniform(0.7, 1.0), 0.15, 1.0))
                buy_qty = min(self.capital_remaining * 0.55 * confidence, 0.8)
                total = self.position + buy_qty
                self.entry_price = (
                    self.entry_price * self.position + price * buy_qty
                ) / total
                self.position = min(2.5, total)
                self.capital_remaining = max(0.0, self.capital_remaining - buy_qty)
                return 1.5 * confidence
            if pnl > self.greed_threshold:
                return -1.5 * float(rng.uniform(0.7, 1.0))

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

        reason_suffix = f" — \"{self.last_reason}\"" if self.last_reason else ""
        print(
            f"[agent {self.id:>3}] {self.agent_type:<14} {self.psych_profile:<11} "
            f"base={base:+.3f} final={final:+.3f} action={self.last_action:<4} "
            f"pnl={self.pnl_pct * 100:+.2f}% pos={self.position:.2f}{reason_suffix}"
        )

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
            "reason": self.last_reason,
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

"""
Agen-agen pasar modal — dua sumbu:
  - info_style   : cara baca informasi pasar (fundamentalist / chartist / noise)
  - psych_profile: respons psikologis terhadap P&L (disciplined / bagholder / averager)

Psych profile MENG-OVERRIDE sinyal info_style saat agen sedang punya posisi.
Sebagian keputusan override dibantu LLM yang berperan sebagai persona
psikologis agen (lihat _PERSONA_DESCRIPTIONS) — bukan cuma mengulang rumus
threshold, tapi menimbang mood, keraguan, dan konteks momen itu sendiri.
pain_threshold & greed_threshold di-SAMPLE RANDOM per agen dan hanya jadi
referensi longgar buat LLM (bukan garis pemicu keras) → mencegah gelombang
aksi serentak yang terlalu sempurna. Aturan fallback (dipakai setiap kali belum
ada jawaban LLM yang relevan) juga sengaja diberi jitter, bukan step function murni.

Aturan fallback memutuskan secara default. Pemanggilan LLM bersifat asinkron dan
dibatasi anggaran (lihat sim/llm_advisor.py): tick simulasi tidak pernah menunggu
jaringan, dan jawaban LLM sesekali meng-override keputusan saat agen yang sama
bereaksi lagi setelah jawaban tiba, selama situasinya masih relevan.
Log per agen hanya dicetak saat jawaban LLM dipakai, atau untuk semua keputusan
bila SIMPASAR_AGENT_LOG=1.
"""
from __future__ import annotations
import math
import os
import threading
from google import genai
from google.genai import types as genai_types

from .llm_advisor import LLMAdvisor

_DEFAULT_LLM_MODEL = "gemini-3.1-flash-lite"
_PLACEHOLDER_KEYS = {"your_api_key_here", "your-api-key", "changeme", "<your_api_key>"}
# Log keputusan setiap agen setiap tick sangat bising (~10 KB/tick); default hanya saat jawaban LLM dipakai.
_AGENT_LOG = os.environ.get("SIMPASAR_AGENT_LOG", "").strip().lower() in {"1", "on", "true", "yes"}
_LLM_OFF_VALUES = {"0", "off", "false", "no", "disable", "disabled"}


def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        return float(default)
    # nan/inf dari env (mis. SIMPASAR_LLM_RPM=inf) kembali ke default, bukan membuat crash.
    return value if math.isfinite(value) else float(default)


def _usable_key(value: str | None) -> str | None:
    """Kunci API yang layak dipakai, atau None untuk kosong/placeholder dari .env.example."""
    key = (value or "").strip()
    return key if key and key.lower() not in _PLACEHOLDER_KEYS else None


def _safe_print(line: str) -> None:
    """print yang tidak pernah melempar karena encoding console (mis. cp1252 di Windows)."""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)


_gemini_client = None
def get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        raw_keys = (os.environ.get("GEMINI_API_KEY"), os.environ.get("GOOGLE_API_KEY"))
        api_key = next((k for k in map(_usable_key, raw_keys) if k), None)
        try:
            # Batas waktu per request 1-120 detik supaya worker LLM tidak tertahan terlalu lama.
            timeout_ms = int(max(1_000, min(120_000, _env_number("SIMPASAR_LLM_TIMEOUT_MS", 25_000))))
            http_options = genai_types.HttpOptions(
                timeout=timeout_ms,
                # Matikan retry status bawaan SDK (599 = kode yang tidak pernah dikirim server), karena
                # retry SDK tidak terhitung anggaran rpm; 429/5xx ditangani backoff advisor.
                retry_options=genai_types.HttpRetryOptions(attempts=1, http_status_codes=[599]),
            )
        except Exception:
            http_options = None
        try:
            if api_key:
                _gemini_client = genai.Client(api_key=api_key, http_options=http_options)
            elif not any((k or "").strip() for k in raw_keys):
                # Tanpa variabel kunci sama sekali: biarkan SDK memakai kredensial lain (mis. Vertex AI ADC).
                _gemini_client = genai.Client(http_options=http_options)
        except Exception:
            _gemini_client = None
    return _gemini_client


def llm_enabled_by_env() -> bool:
    """False bila SIMPASAR_LLM diset off/0/false (misalnya lewat --no-llm di dev_server)."""
    return os.environ.get("SIMPASAR_LLM", "on").strip().lower() not in _LLM_OFF_VALUES


_llm_advisor: LLMAdvisor | None = None
_llm_advisor_state = "unresolved"   # unresolved | active | disabled | no_key | invalid_config
_llm_advisor_lock = threading.Lock()


def get_llm_advisor() -> LLMAdvisor | None:
    """
    Penasihat LLM bersama untuk semua agen, atau None bila LLM dimatikan atau tidak
    ada kunci. Konfigurasi dibaca sekali dari environment:
    SIMPASAR_LLM, SIMPASAR_LLM_MODEL, SIMPASAR_LLM_RPM (default 3),
    SIMPASAR_LLM_MAX_REQUESTS (default 1000 per proses, 0 = tanpa batas),
    SIMPASAR_LLM_BATCH_SIZE (default 11),
    SIMPASAR_LLM_SENTIMENT_WEIGHT (default 0.25),
    SIMPASAR_LLM_SENTIMENT_HALFLIFE_S (default 60),
    SIMPASAR_LLM_CONCURRENCY (default 1), SIMPASAR_LLM_TIMEOUT_MS (default 25000).
    """
    global _llm_advisor, _llm_advisor_state
    if _llm_advisor_state != "unresolved":
        return _llm_advisor
    with _llm_advisor_lock:
        if _llm_advisor_state != "unresolved":
            return _llm_advisor
        rpm = _env_number("SIMPASAR_LLM_RPM", 3.0)
        max_requests = max(0, int(_env_number("SIMPASAR_LLM_MAX_REQUESTS", 1000)))
        concurrency = min(16, int(_env_number("SIMPASAR_LLM_CONCURRENCY", 1)))
        batch_size = max(1, min(16, int(_env_number("SIMPASAR_LLM_BATCH_SIZE", 11))))
        sentiment_weight = max(0.0, min(1.0, _env_number("SIMPASAR_LLM_SENTIMENT_WEIGHT", 0.25)))
        sentiment_halflife_s = max(1.0, min(3600.0, _env_number("SIMPASAR_LLM_SENTIMENT_HALFLIFE_S", 60.0)))
        if not llm_enabled_by_env() or rpm <= 0 or concurrency <= 0:
            _llm_advisor_state = "disabled"
            return None
        try:
            client = get_gemini_client()
        except Exception:
            client = None
        if client is None:
            _llm_advisor_state = "no_key"
            return None
        secrets = tuple(v for v in (os.environ.get("GEMINI_API_KEY"), os.environ.get("GOOGLE_API_KEY")) if v)
        try:
            _llm_advisor = LLMAdvisor(
                client,
                model=os.environ.get("SIMPASAR_LLM_MODEL", "").strip() or _DEFAULT_LLM_MODEL,
                rpm=rpm,
                concurrency=concurrency,
                batch_size=batch_size,
                sentiment_weight=sentiment_weight,
                sentiment_halflife_s=sentiment_halflife_s,
                max_requests=max_requests,
                secrets=secrets,
                logger=_safe_print,
            )
        except Exception as exc:  # konfigurasi rusak tidak boleh menghentikan simulasi
            _safe_print(f"[llm] agen LLM dimatikan, konfigurasi tidak valid: {type(exc).__name__}: {exc}")
            _llm_advisor = None
            _llm_advisor_state = "invalid_config"
            return None
        _llm_advisor_state = "active"
        return _llm_advisor


def llm_status() -> dict:
    """Ringkasan status agen LLM untuk dikirim ke klien (tanpa rahasia)."""
    advisor = get_llm_advisor()
    if advisor is not None:
        return advisor.stats()
    return {"enabled": False, "reason": _llm_advisor_state}


def shutdown_llm_advisor() -> None:
    """Hentikan advisor dan reset resolusi, sehingga startup berikutnya membuat advisor baru."""
    global _llm_advisor, _llm_advisor_state
    with _llm_advisor_lock:
        if _llm_advisor is not None:
            _llm_advisor.shutdown()
        _llm_advisor = None
        _llm_advisor_state = "unresolved"

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


def _persona_header(agent_type: str, psych_profile: str) -> str:
    persona = _PERSONA_DESCRIPTIONS[psych_profile]
    return (
        "You are role-playing as individual retail investors inside a stock-market "
        "simulation. Think and react as each specific person would — not as a formula.\n\n"
        f"Shared persona: {persona}\n\n"
        f"Their trading style is '{agent_type}', which produces each person's cold, "
        "rational base signal before feelings about their own position get involved."
    )


def _agent_situation_line(agent: "Agent", base_order: float, price: float, gut_seed: float) -> str:
    pnl = float((price - agent.entry_price) / agent.entry_price) if agent.entry_price > 0 else 0.0
    return (
        f"[AGENT {agent.id}]\n"
        f"- Base signal: {base_order:+.3f} (range -1.5 = strong sell, +1.5 = strong buy)\n"
        f"- Current price: {price:.2f}\n"
        f"- Average entry price: {agent.entry_price:.2f}\n"
        f"- Unrealized P&L: {pnl * 100:+.2f}%\n"
        f"- Position size: {agent.position:.2f} (1.0 = one full position)\n"
        f"- Capital remaining: {agent.capital_remaining * 100:.0f}%\n"
        f"- Pain threshold: {agent.pain_threshold * 100:.0f}% loss\n"
        f"- Greed threshold: {agent.greed_threshold * 100:.0f}% gain\n"
        f"- Reaction delay probability: {agent.reaction_delay_prob:.2f}\n"
        f"- Gut-feeling seed: {gut_seed:.3f}\n"
        "Choose this agent's order from -1.5 (panic-sell everything) to +1.5 "
        "(buy aggressively), and a short reason (15 words or fewer) capturing the feeling."
    )


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

        advisor = get_llm_advisor()
        if advisor is not None:
            # Jawaban LLM dari permintaan sebelumnya (non-blocking), hanya dipakai bila
            # masih relevan: belum kedaluwarsa dan P&L agen belum bergeser jauh. Lihat LLMAdvisor.collect.
            advice = advisor.collect(self, price)
            if advice is not None:
                order, reason = advice
                self.last_reason = reason
                self._llm_fresh = True   # log agen hanya dicetak pada tick jawaban LLM dipakai
                return float(np.clip(order, -1.5, 1.5))
            # Tawarkan agen ini untuk dinilai LLM. Prompt dibangun di akhir tick
            # (thread simulasi) hanya bila anggaran request masih tersedia.
            advisor.offer(
                self,
                lambda b=base_order, p=price: _agent_situation_line(self, b, p, advisor.gut_seed()),
                price,
                group_key=(self.agent_type, self.psych_profile),
                build_header=lambda: _persona_header(self.agent_type, self.psych_profile),
            )

        def with_sentiment(order: float) -> float:
            if advisor is None or advisor.sentiment_weight == 0:
                return order
            sentiment = advisor.sentiment(self.psych_profile)
            if sentiment is None:
                return order
            return float(np.clip(
                order + advisor.sentiment_weight * sentiment * rng.uniform(0.5, 1.0),
                -1.5,
                1.5,
            ))

        # ── Fallback rules — used whenever no fresh LLM advice is ready ──
        # (no key, budget spent, request still in flight, or an error).
        # Kept intentionally non-deterministic (severity
        # scales with distance past threshold, plus per-agent jitter) so a
        # missing API key doesn't collapse everyone back into a step function.
        if self.psych_profile == "disciplined":
            if pnl < -self.pain_threshold:
                severity = 0.5 + min(1.0, (-pnl - self.pain_threshold) / self.pain_threshold)
                return with_sentiment(-1.5 * float(np.clip(severity * rng.uniform(0.6, 1.0), 0.3, 1.0)))
            if pnl > self.greed_threshold:
                severity = 0.5 + min(1.0, (pnl - self.greed_threshold) / self.greed_threshold)
                return with_sentiment(-1.5 * float(np.clip(severity * rng.uniform(0.6, 1.0), 0.3, 1.0)))

        elif self.psych_profile == "bagholder":
            if pnl < -self.pain_threshold:
                # Tahan! Jual hanya sebagian kecil, kecuali sudah benar-benar tak tertahankan
                if base_order < 0:
                    capitulate = -pnl > 2 * self.pain_threshold and rng.random() < 0.15
                    partial = rng.uniform(0.6, 1.0) if capitulate else rng.uniform(0.05, 0.20)
                    return with_sentiment(base_order * partial)
                return with_sentiment(base_order)
            if pnl > self.greed_threshold:
                relief = rng.uniform(0.7, 1.0)   # buru-buru ambil untung, kadang terlalu cepat
                return with_sentiment(-1.5 * float(relief))

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
                return with_sentiment(1.5 * confidence)
            if pnl > self.greed_threshold:
                return with_sentiment(-1.5 * float(rng.uniform(0.7, 1.0)))

        return with_sentiment(base_order)

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
        fresh, self._llm_fresh = getattr(self, "_llm_fresh", False), False
        if fresh or _AGENT_LOG:
            _safe_print(
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

"""
Penasihat LLM asinkron untuk agen SimPasar IDX.

Kenapa modul ini ada
--------------------
Versi sebelumnya memanggil Gemini secara sinkron di dalam Agent.decide(). Hasil
pengukuran (seed 42, fundamental IHSG 6.850): rata-rata 41 agen per tick layak
dipanggil, sedangkan satu panggilan gemini-3.1-flash-lite memakan sekitar 5 detik.
Satu tick jadi menunggu beberapa menit, event loop FastAPI membeku, dan jumlah
request bisa mencapai ribuan per jam selama tab simulator terbuka.

Desain
------
- Aturan (rule-based) tetap memutuskan secara default. Tick simulasi tidak pernah
  menunggu jaringan: selama tick, agen yang layak hanya menawarkan diri lewat offer().
  Di end_tick(), advisor memilih sebagian kecil kandidat sesuai anggaran, mendahulukan
  momen psikologis penting (rugi mendekati ambang sakit atau untung mendekati ambang
  serakah) lalu agen yang paling lama belum dilayani, dan mengirim prompt ke worker.
- Jawaban LLM diterapkan lewat collect() saat agen yang sama bereaksi lagi setelah
  jawaban tiba (biasanya belasan sampai puluhan tick kemudian), asalkan masih relevan:
  umur jawaban paling lama max_age_s dan P&L agen belum bergeser lebih dari max_pnl_shift
  (poin persen) dibanding saat prompt dibuat. Prompt bergantung pada P&L, bukan harga mentah:
  dalam rentang latensi Gemini (18-43 tick) harga simulasi sering bergerak lebih dari 5 persen,
  sehingga gerbang harga mentah membuang sebagian besar jawaban. Alasan pembuangan dihitung
  terpisah di stats().
- Anggaran berlapis: token bucket `rpm`, paling banyak `concurrency` request berjalan,
  satu request aktif per agen, batas total `max_requests` per proses, backoff eksponensial
  saat error (minimal 30 detik untuk 429), dan berhenti permanen bila kunci/izin ditolak
  (401/403), sehingga kunci salah tidak dipanggil terus-menerus.
- Hanya ringkasan error kasar (nama exception + kode status) yang masuk stats() dan
  dikirim ke browser; detail yang sudah diredaksi hanya dicatat lewat logger server.
- RNG pasar tidak pernah dipakai advisor. Angka "gut feeling" di prompt berasal dari
  RNG milik advisor (gut_seed), sehingga waktu anggaran request tidak menggeser aliran
  acak simulasi; tanpa jawaban yang diterapkan, lintasan harga identik dengan mode aturan.

offer(), end_tick(), collect(), gut_seed(), stats() dan shutdown() dipanggil dari
thread simulasi. Worker thread berjenis daemon dan tetap hidup walau SDK melempar
BaseException, sehingga advisor tidak macet diam-diam.
"""
from __future__ import annotations

import json
import math
import queue
import random
import sys
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Iterable

RESPONSE_FORMAT: dict = {
    "type": "text",
    "mime_type": "application/json",
    "schema": {
        "type": "object",
        "properties": {
            "order": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["order"],
    },
}

_RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "quota", "Quota", "rate limit", "Rate limit", "Too Many Requests")
_AUTH_MARKERS = ("API_KEY_INVALID", "API key not valid", "API_KEY_SERVICE_BLOCKED", "PERMISSION_DENIED")
_MAX_PENDING_CANDIDATES = 4096


def _default_log(line: str) -> None:
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - logging tidak boleh menjatuhkan worker
        pass


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _is_interesting(agent: Any) -> bool:
    """Momen yang layak dinilai LLM: rugi mendekati ambang sakit atau untung mendekati ambang serakah."""
    entry = getattr(agent, "entry_price", 0.0) or 0.0
    pain = getattr(agent, "pain_threshold", None)
    greed = getattr(agent, "greed_threshold", None)
    if entry <= 0 or pain is None or greed is None:
        return False
    pnl = getattr(agent, "pnl_pct", 0.0) or 0.0
    return pnl <= -0.6 * pain or pnl >= 0.6 * greed


class LLMAdvisor:
    """Antrian permintaan LLM ber-anggaran untuk keputusan psikologis agen."""

    def __init__(
        self,
        client: Any,
        model: str,
        *,
        rpm: float = 12.0,
        concurrency: int = 4,
        max_requests: int = 0,
        max_age_s: float = 45.0,
        max_pnl_shift: float = 0.15,
        secrets: Iterable[str] = (),
        clock: Callable[[], float] = time.monotonic,
        seed: int | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(rpm, bool) or not isinstance(rpm, (int, float)) or not math.isfinite(rpm) or rpm <= 0:
            raise ValueError("rpm harus angka hingga > 0")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("concurrency harus bilangan bulat >= 1")
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 0:
            raise ValueError("max_requests harus bilangan bulat >= 0 (0 = tanpa batas)")
        self._client = client
        self.model = model
        self.rpm = float(rpm)
        self.concurrency = int(concurrency)
        self.max_requests = int(max_requests)
        self.max_age_s = float(max_age_s)
        self.max_pnl_shift = float(max_pnl_shift)
        self._secrets = tuple(s for s in secrets if s)
        self._clock = clock
        self._rand = random.Random(seed)
        self._log = logger or _default_log

        self._lock = threading.Lock()
        self._capacity = float(max(1, min(self.concurrency, math.ceil(self.rpm))))
        self._tokens = self._capacity
        self._last_refill = clock()
        self._inflight = 0
        self._backoff_until = 0.0
        self._consecutive_errors = 0
        self._serial = 0
        self._last_error = ""
        self._last_error_detail = ""
        self._disabled_reason = ""
        self._stats = {
            "requested": 0, "ok": 0, "errors": 0, "applied": 0, "discarded": 0,
            "discarded_old": 0, "discarded_changed": 0, "discarded_drift": 0,
        }
        self._usage = {"input_tokens": 0, "output_tokens": 0, "thought_tokens": 0}
        self._closed = False

        self._candidates: list[tuple[Any, Callable[[], str], float]] = []
        self._jobs: queue.Queue = queue.Queue()
        self._workers: list[threading.Thread] = []
        for i in range(self.concurrency):
            worker = threading.Thread(target=self._worker, name=f"simpasar-llm-{i}", daemon=True)
            worker.start()
            self._workers.append(worker)

    # ── API untuk thread simulasi ────────────────────────────────────────
    def offer(self, agent: Any, build_prompt: Callable[[], str], price: float) -> None:
        """Catat agen sebagai kandidat LLM untuk tick ini (tanpa I/O)."""
        if self._closed or self._disabled_reason or getattr(agent, "_llm_future", None) is not None:
            return
        if len(self._candidates) >= _MAX_PENDING_CANDIDATES:
            return
        self._candidates.append((agent, build_prompt, float(price)))

    def gut_seed(self) -> float:
        """Angka acak untuk prompt dari RNG advisor (bukan RNG pasar)."""
        return self._rand.random()

    def end_tick(self) -> int:
        """Kirim sebagian kandidat sesuai anggaran. Mengembalikan jumlah request baru."""
        candidates, self._candidates = self._candidates, []
        if self._closed or self._disabled_reason or not candidates:
            return 0
        budget_hit = False
        with self._lock:
            now = self._clock()
            self._refill(now)
            if now < self._backoff_until:
                return 0
            remaining = (self.max_requests - self._stats["requested"]) if self.max_requests else self.concurrency
            if remaining <= 0:
                self._disabled_reason = "budget_exhausted"
                budget_hit = True
                slots = 0
            else:
                slots = min(int(math.floor(self._tokens)), self.concurrency - self._inflight, remaining)
        if budget_hit:
            self._log(f"[llm] batas {self.max_requests} request per sesi tercapai; agen LLM berhenti sampai server dimulai ulang.")
            return 0
        if slots <= 0:
            return 0

        seen: set[int] = set()
        pool: list[tuple[Any, Callable[[], str], float]] = []
        for cand in candidates:
            agent = cand[0]
            if id(agent) in seen or agent.position <= 0 or getattr(agent, "_llm_future", None) is not None:
                continue
            seen.add(id(agent))
            pool.append(cand)
        self._rand.shuffle(pool)
        # Momen penting dulu, lalu agen yang belum pernah (0) atau paling lama tidak dilayani.
        pool.sort(key=lambda c: (0 if _is_interesting(c[0]) else 1, getattr(c[0], "_llm_served", 0)))

        submitted = 0
        for agent, build_prompt, price in pool[:slots]:
            with self._lock:
                self._refill(self._clock())
                if self._tokens < 1 or self._inflight >= self.concurrency:
                    break
                self._tokens -= 1
                self._inflight += 1
                self._serial += 1
                serial = self._serial
            try:
                prompt = build_prompt()
            except Exception as exc:  # noqa: BLE001 - prompt rusak tidak boleh menghentikan tick
                detail = self._redact(f"prompt {type(exc).__name__}: {exc}")[:300]
                with self._lock:
                    self._tokens = min(self._capacity, self._tokens + 1)
                    self._inflight -= 1
                    self._stats["errors"] += 1
                    self._last_error = f"prompt {type(exc).__name__}"
                    self._last_error_detail = detail
                self._log(f"[llm] gagal membangun prompt: {detail}")
                continue
            future: Future = Future()
            agent._llm_future = future
            agent._llm_meta = (agent.position, agent.entry_price, price, self._clock())
            agent._llm_served = serial
            with self._lock:
                self._stats["requested"] += 1
            self._jobs.put((future, prompt))
            submitted += 1
        return submitted

    def collect(self, agent: Any, price: float) -> tuple[float, str] | None:
        """Ambil jawaban LLM yang sudah selesai dan masih relevan untuk agen ini."""
        future = getattr(agent, "_llm_future", None)
        if future is None or not future.done():
            return None
        meta = getattr(agent, "_llm_meta", None)
        agent._llm_future = None
        agent._llm_meta = None
        try:
            result = future.result()
        except Exception:  # noqa: BLE001 - worker selalu set_result, ini hanya jaring pengaman
            result = None
        if result is None or meta is None:
            return None
        _position, entry_price, req_price, submitted_at = meta
        if self._clock() - submitted_at > self.max_age_s:
            why = "old"
        elif entry_price <= 0 or agent.entry_price <= 0:
            why = "changed"
        else:
            # Averaging down menggeser harga dan entry bersamaan, sehingga P&L relatif stabil dan jawaban tetap relevan.
            pnl_then = (req_price - entry_price) / entry_price
            pnl_now = (price - agent.entry_price) / agent.entry_price
            why = "drift" if abs(pnl_now - pnl_then) > self.max_pnl_shift else None
        with self._lock:
            if why is None:
                self._stats["applied"] += 1
            else:
                self._stats["discarded"] += 1
                self._stats["discarded_" + why] += 1
        return result if why is None else None

    def stats(self) -> dict:
        with self._lock:
            now = self._clock()
            self._refill(now)
            reason = "closed" if self._closed else (self._disabled_reason or None)
            return {
                "enabled": reason is None,
                "reason": reason,
                "model": self.model,
                "rpm": self.rpm,
                "concurrency": self.concurrency,
                "max_requests": self.max_requests,
                "workers_alive": sum(1 for w in self._workers if w.is_alive()),
                "inflight": self._inflight,
                "tokens": round(self._tokens, 2),
                "backoff_s": round(max(0.0, self._backoff_until - now), 1),
                "last_error": self._last_error,
                **self._stats,
                **self._usage,
            }

    def shutdown(self) -> None:
        """Berhenti menerima permintaan baru dan hentikan worker setelah antrean kosong."""
        self._closed = True
        self._candidates = []
        for _ in self._workers:
            self._jobs.put(None)

    # ── Internal ─────────────────────────────────────────────────────────
    def _refill(self, now: float) -> None:
        """Isi ulang token bucket. Pemanggil wajib memegang self._lock."""
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self.rpm / 60.0)

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            for variant in {secret, secret.strip(), repr(secret)[1:-1]}:
                if variant and len(variant) >= 8:
                    text = text.replace(variant, "[redacted]")
        return text

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            future, prompt = job
            result = None
            try:
                try:
                    order, reason, usage = self._call(prompt)
                    result = (order, reason)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:  # noqa: BLE001 - worker harus tetap hidup
                    result = None
                    self._on_error(exc)
                else:
                    recovered = 0
                    with self._lock:
                        self._stats["ok"] += 1
                        for key, value in zip(("input_tokens", "output_tokens", "thought_tokens"), usage):
                            self._usage[key] += value
                        recovered, self._consecutive_errors = self._consecutive_errors, 0
                    if recovered:
                        self._log(f"[llm] Gemini pulih setelah {recovered} error beruntun.")
            finally:
                with self._lock:
                    self._inflight -= 1
                if not future.done():
                    future.set_result(result)

    def _call(self, prompt: str) -> tuple[float, str, tuple[int, int, int]]:
        interaction = self._client.interactions.create(
            model=self.model,
            input=prompt,
            response_format=RESPONSE_FORMAT,
        )
        data = json.loads((interaction.output_text or "").strip())
        if not isinstance(data, dict):
            raise ValueError("jawaban model bukan objek JSON")
        raw_order = data.get("order")
        if isinstance(raw_order, bool) or not isinstance(raw_order, (int, float)):
            raise ValueError("order harus berupa angka")
        order = float(raw_order)
        if not math.isfinite(order):
            raise ValueError("order bukan angka hingga")
        order = max(-1.5, min(1.5, order))
        raw_reason = data.get("reason")
        reason = " ".join(raw_reason.split())[:160] if isinstance(raw_reason, str) else ""
        usage = getattr(interaction, "usage", None)

        def _count(name: str) -> int:
            value = getattr(usage, name, 0) if usage is not None else 0
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

        return order, reason, (_count("total_input_tokens"), _count("total_output_tokens"), _count("total_thought_tokens"))

    def _on_error(self, exc: BaseException) -> None:
        try:
            message = str(exc)
        except Exception:  # noqa: BLE001
            message = ""
        name = type(exc).__name__
        status = _status_code(exc)
        auth = status in (401, 403) or any(marker in message for marker in _AUTH_MARKERS)
        rate_limited = status == 429 or any(marker in message for marker in _RATE_LIMIT_MARKERS)
        coarse = f"{name} {status}" if status is not None else name
        detail = self._redact(f"{name}: {message}")[:300]
        try:
            now = self._clock()
        except Exception:  # noqa: BLE001
            now = time.monotonic()
        newly_disabled = False
        with self._lock:
            self._stats["errors"] += 1
            self._consecutive_errors += 1
            first_in_streak = self._consecutive_errors == 1
            delay = min(60.0, 2.0 ** min(self._consecutive_errors, 6))
            if rate_limited:
                delay = max(delay, 30.0)
            self._backoff_until = max(self._backoff_until, now + delay)
            # Defensif: SDK google-genai dapat saja mengulang request sebelum error sampai ke sini.
            self._tokens = max(-self._capacity, self._tokens - 1)
            self._last_error = coarse            # hanya ringkasan kasar yang dikirim ke browser
            self._last_error_detail = detail     # detail teredaksi hanya untuk log server
            if auth and not self._disabled_reason:
                self._disabled_reason = "auth_error"
                newly_disabled = True
        if newly_disabled:
            self._log(f"[llm] Gemini menolak kunci atau izin ({coarse}); agen LLM dihentikan untuk sesi ini. Detail: {detail}")
        elif first_in_streak:
            self._log(f"[llm] error dari Gemini ({coarse}); backoff {delay:.0f} detik. Detail: {detail}")

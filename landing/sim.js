/**
 * sim.js — port ringan (JavaScript) dari sim/agents.py dan sim/market.py.
 *
 * Tujuan: demo live di landing page tanpa server. Logika agen dan mekanisme
 * harga mengikuti kode Python aslinya (parameter default hasil grid search),
 * minus lapisan LLM (Gemini) yang hanya tersedia di server penuh.
 *
 * Diekspos sebagai window.SimPasar = { Market, DEFAULT_PARAMS }.
 */
(function () {
  "use strict";

  // ── Parameter default (identik dengan sim/market.py::DEFAULT_PARAMS) ──
  const DEFAULT_PARAMS = {
    lambda_price: 0.12,
    fundamentalist_alpha: 2.5,
    chartist_factor: 2.8,
    chartist_momentum_window: 8,
    noise_sigma: 0.42,
    sentiment_sensitivity: 1.4,
    fundamentalist_ratio: 0.3,
    chartist_ratio: 0.5,
    noise_ratio: 0.2,
    disciplined_ratio: 0.34,
    bagholder_ratio: 0.33,
  };

  const BUBBLE_THRESHOLD = 1.25;
  const AVERAGE_UP_FRACTION = 0.5;   // = sim/agents.py: averager menambah posisi saat untung > 0,5 × ambang serakah
  const CRASH_THRESHOLD = 0.82;
  const MAX_HISTORY = 500;

  // ── RNG deterministik (mulberry32) + Box-Muller ──────────────────────
  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  class RNG {
    constructor(seed) {
      this.r = mulberry32(seed == null ? (Math.random() * 2 ** 32) >>> 0 : seed);
    }
    random() { return this.r(); }
    uniform(a, b) { return a + (b - a) * this.r(); }
    normal(mean = 0, sd = 1) {
      let u = 0;
      while (u === 0) u = this.r();
      const v = this.r();
      return mean + sd * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
    }
    shuffle(arr) {
      for (let i = arr.length - 1; i > 0; i--) {
        const j = Math.floor(this.r() * (i + 1));
        [arr[i], arr[j]] = [arr[j], arr[i]];
      }
      return arr;
    }
  }

  const clip = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

  // ── Agen ─────────────────────────────────────────────────────────────
  function makeAgent(id, type) {
    return {
      id, type, psych: "disciplined",
      position: 0, entry: 0, capital: 1,
      pain: 0.1, greed: 0.15, reactProb: 0.8,
      order: 0, action: "hold", pnl: 0, inPain: false,
    };
  }

  function baseSignal(a, m) {
    const P = m.params, rng = m.rng;
    if (a.type === "fundamentalist") {
      const dev = (m.price - m.fundamental) / m.fundamental;
      return clip(-P.fundamentalist_alpha * dev + rng.normal(0, 0.06), -1.5, 1.5);
    }
    if (a.type === "chartist") {
      const w = P.chartist_momentum_window, h = m.history;
      if (h.length < w + 1) return rng.normal(0, 0.1);
      const past = h[h.length - (w + 1)];
      const momentum = (m.price - past) / (past + 1e-9);
      return clip(P.chartist_factor * momentum + rng.normal(0, 0.06), -1.5, 1.5);
    }
    // noise
    return clip(rng.normal(0, P.noise_sigma) + P.sentiment_sensitivity * m.sentiment, -1.5, 1.5);
  }

  function psychOverride(a, base, price, rng) {
    if (a.position <= 0 || a.entry <= 0) return base;
    const pnl = (price - a.entry) / a.entry;
    a.pnl = pnl;
    if (rng.random() > a.reactProb) return base;      // tidak semua bereaksi di tick ini
    a.inPain = pnl < -a.pain;

    if (a.psych === "disciplined") {
      if (pnl < -a.pain) return -1.5;                  // cut loss
      if (pnl > a.greed) return -1.5;                  // take profit
    } else if (a.psych === "bagholder") {             // denial
      if (pnl < -a.pain) {
        if (base < 0) return base * rng.uniform(0.05, 0.2); // tahan! jual sedikit saja
        return base;
      }
      if (pnl > 0) {                                   // serakah: menolak take profit selama masih untung
        if (base < 0) return base * rng.uniform(0.05, 0.2);
        return base;
      }
    } else { // averager
      if ((pnl < -a.pain || pnl > a.greed * AVERAGE_UP_FRACTION) && a.capital > 0.15) {
        const q = Math.min(a.capital * 0.55, 0.8);
        const total = a.position + q;
        a.entry = (a.entry * a.position + price * q) / total;
        a.position = Math.min(2.5, total);
        a.capital = Math.max(0, a.capital - q);
        return 1.5;                                    // averaging down (rugi) / averaging up (untung)
      }
      if (pnl > a.greed) return -1.5;                  // modal habis + untung besar → take profit
    }
    return base;
  }

  function updatePosition(a, order, price) {
    if (a.position <= 0) {
      if (order > 0.35) { a.position = 1; a.entry = price; a.capital = 1; a.pnl = 0; }
    } else if (order < -1.0) {
      a.position = 0; a.entry = 0; a.capital = 1; a.pnl = 0; a.inPain = false;
    }
  }

  function buildAgents(n, fRatio, cRatio, rng, dRatio, bRatio) {
    const nF = Math.round(n * fRatio), nC = Math.round(n * cRatio), nN = n - nF - nC;
    const pool = [];
    for (let i = 0; i < nF; i++) pool.push(makeAgent(i, "fundamentalist"));
    for (let i = 0; i < nC; i++) pool.push(makeAgent(nF + i, "chartist"));
    for (let i = 0; i < nN; i++) pool.push(makeAgent(nF + nC + i, "noise"));
    rng.shuffle(pool);

    const nD = Math.round(n * dRatio), nB = Math.round(n * bRatio), nA = n - nD - nB;
    const psych = [].concat(
      Array(nD).fill("disciplined"), Array(nB).fill("bagholder"), Array(Math.max(0, nA)).fill("averager")
    );
    rng.shuffle(psych);

    pool.forEach((a, i) => {
      a.id = i;
      a.psych = psych[i] || "averager";
      // Konteks retail IDX: tahan lama sebelum cut-loss (~25%), take-profit tinggi (~45%)
      a.pain = clip(rng.normal(0.25, 0.08), 0.1, 0.5);
      a.greed = clip(rng.normal(0.45, 0.1), 0.2, 0.8);
      a.reactProb = rng.uniform(0.5, 0.8);
    });
    return pool;
  }

  // ── Market ───────────────────────────────────────────────────────────
  class Market {
    constructor({ nAgents = 100, fundamental = 100, params = {}, seed = 42 } = {}) {
      this.nAgents = nAgents;
      this.fundamental = fundamental;
      this.params = Object.assign({}, DEFAULT_PARAMS, params);
      this.seed = seed;
      this.reset();
    }

    _makeAgents() {
      const P = this.params;
      return buildAgents(this.nAgents, P.fundamentalist_ratio, P.chartist_ratio, this.rng, P.disciplined_ratio, P.bagholder_ratio);
    }

    reset(seed) {
      if (seed != null) this.seed = seed;
      this.rng = new RNG(this.seed);
      this.price = this.fundamental;
      this.history = [this.fundamental];
      this.volumes = [0];          // total |order| per tick; tick 0 tanpa transaksi
      this.volume = 0;
      this.tick = 0;
      this.sentiment = 0;
      this.sentimentDecay = 0.72;
      this.paused = false;
      this.agents = this._makeAgents();
    }

    step() {
      if (this.paused) return this.getState();
      let sum = 0, vol = 0;
      for (const a of this.agents) {
        const base = baseSignal(a, this);
        const fin = psychOverride(a, base, this.price, this.rng);
        updatePosition(a, fin, this.price);
        a.order = fin;
        a.action = fin > 0.05 ? "buy" : fin < -0.05 ? "sell" : "hold";
        sum += fin;
        vol += Math.abs(fin);
      }
      const net = sum / this.agents.length;
      this.volume = vol;
      this.price = Math.max(0.5, this.price * Math.exp(this.params.lambda_price * net));
      this.history.push(this.price);
      this.volumes.push(vol);
      if (this.history.length > MAX_HISTORY) this.history.shift();
      if (this.volumes.length > MAX_HISTORY) this.volumes.shift();
      this.sentiment *= this.sentimentDecay;
      if (Math.abs(this.sentiment) < 1e-4) this.sentiment = 0;
      this.tick++;
      return this.getState();
    }

    injectRumor(s = 1) { this.sentiment = Math.min(3, this.sentiment + s); }
    injectPanic(s = 1) { this.sentiment = Math.max(-3, this.sentiment - s); }

    setPopulation(f, c, n) {
      const t = f + c + n; if (t <= 0) return;
      this.params.fundamentalist_ratio = f / t;
      this.params.chartist_ratio = c / t;
      this.params.noise_ratio = n / t;
      this.agents = this._makeAgents();
    }

    setPsych(d, b) {
      if (d + b <= 0 || d + b > 1) return;
      this.params.disciplined_ratio = d;
      this.params.bagholder_ratio = b;
      this.agents = this._makeAgents();
    }

    status() {
      const r = this.price / this.fundamental;
      return r > BUBBLE_THRESHOLD ? "Bubble" : r < CRASH_THRESHOLD ? "Panik-Crash" : "Normal";
    }

    getState() {
      const inPos = this.agents.filter((a) => a.position > 0);
      const inPain = inPos.filter((a) => a.inPain).length;
      const averaging = this.agents.filter((a) => a.psych === "averager" && a.action === "buy" && a.position > 1.05 && a.pnl < 0).length;   // nambah saat rugi
      const avgPnl = inPos.length ? inPos.reduce((s, a) => s + a.pnl, 0) / inPos.length : 0;
      let buy = 0, sell = 0, hold = 0;
      for (const a of this.agents) { if (a.action === "buy") buy++; else if (a.action === "sell") sell++; else hold++; }
      return {
        tick: this.tick, price: this.price, fundamental: this.fundamental,
        status: this.status(), sentiment: this.sentiment, paused: this.paused,
        agents: this.agents, history: this.history, volumes: this.volumes, volume: this.volume,
        stats: { inPosition: inPos.length, inPain, averaging, avgPnlPct: avgPnl * 100, buy, sell, hold },
      };
    }
  }

  window.SimPasar = { Market, DEFAULT_PARAMS, BUBBLE_THRESHOLD, CRASH_THRESHOLD };
})();

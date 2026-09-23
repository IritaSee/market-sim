/**
 * main.js — interaksi landing page: nav, scroll-reveal, matriks agen,
 * salin perintah, dan renderer untuk demo mini (SimPasar.Market dari sim.js).
 *
 * Demo mini memakai konvensi yang sama dengan simulator penuh:
 * 1 tick = 1 menit bursa simulasi (sesi BEI 09:00–12:00 dan 13:30–16:00),
 * candle bawaan 15 menit. Lihat tickToClock() di bawah.
 */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ───────────────────────────── NAV ─────────────────────────────
  const nav = $("#nav");
  const navToggle = $("#navToggle");
  const navLinks = $$("#navLinks a");

  if (nav) {
    const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 8);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }
  if (nav && navToggle) {
    navToggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", String(open));
      navToggle.setAttribute("aria-label", open ? "Tutup menu" : "Buka menu");
    });
    navLinks.forEach((a) => a.addEventListener("click", () => {
      nav.classList.remove("open");
      navToggle.setAttribute("aria-expanded", "false");
    }));
  }

  // Tandai link aktif berdasarkan section yang terlihat (hanya tautan anchor "#...")
  const sections = navLinks
    .map((a) => a.getAttribute("href") || "")
    .filter((h) => /^#[\w-]+$/.test(h))
    .map((h) => $(h))
    .filter(Boolean);
  if ("IntersectionObserver" in window && sections.length) {
    const spy = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        navLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + e.target.id));
      });
    }, { rootMargin: "-40% 0px -55% 0px" });
    sections.forEach((s) => spy.observe(s));
  }

  // ───────────────────────── SCROLL REVEAL ─────────────────────────
  const reveals = $$(".reveal");
  if (reduceMotion || !("IntersectionObserver" in window)) {
    reveals.forEach((el) => el.classList.add("in"));
  } else {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    reveals.forEach((el) => io.observe(el));

    // Fallback: bila observer belum menembak (tab latar, frame tertunda),
    // tampilkan elemen yang sudah berada di dalam viewport saat scroll/resize.
    const revealInView = () => {
      const vh = window.innerHeight;
      reveals.forEach((el) => {
        if (el.classList.contains("in")) return;
        const r = el.getBoundingClientRect();
        if (r.top < vh * 0.96 && r.bottom > 0) { el.classList.add("in"); io.unobserve(el); }
      });
    };
    window.addEventListener("scroll", revealInView, { passive: true });
    window.addEventListener("resize", revealInView);
    setTimeout(revealInView, 400);
  }

  // Sorotan kartu mengikuti kursor
  $$(".card").forEach((card) => {
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      card.style.setProperty("--mx", ((e.clientX - r.left) / r.width * 100) + "%");
      card.style.setProperty("--my", ((e.clientY - r.top) / r.height * 100) + "%");
    });
  });

  // ─────────────────────────── MATRIKS AGEN ───────────────────────────
  const matrix = $("#matrix");
  if (matrix) {
    const cells = $$(".mx-cell", matrix);
    const heads = $$(".mx-head", matrix);
    const rowheads = $$(".mx-rowhead", matrix);
    const clear = () => $$(".hl, .hl-strong", matrix).forEach((el) => el.classList.remove("hl", "hl-strong"));
    cells.forEach((cell) => {
      const r = cell.dataset.row, c = cell.dataset.col;
      cell.addEventListener("pointerenter", () => {
        clear();
        cell.classList.add("hl-strong");
        cells.forEach((x) => { if (x !== cell && (x.dataset.row === r || x.dataset.col === c)) x.classList.add("hl"); });
        heads.forEach((h) => { if (h.dataset.col === c) h.classList.add("hl"); });
        rowheads.forEach((h) => { if (h.dataset.row === r) h.classList.add("hl"); });
      });
    });
    matrix.addEventListener("pointerleave", clear);
  }

  const yearEl = $("#year");
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());

  // ───────────────────── PIL IHSG REAL-TIME (hero) ─────────────────────
  // Dipanggil sekali saat halaman dimuat (tidak ada polling → hemat kredit Sectors).
  // Di server statis (landing/serve.js) endpoint ini tidak ada → tampilkan keterangan.
  async function fetchLiveIHSG() {
    const valEl = $("#ihsg-val");
    const chgEl = $("#ihsg-change");
    const dateEl = $("#ihsg-date");
    if (!valEl) return;
    const unavailable = (why) => {
      valEl.textContent = "tidak tersedia";
      if (chgEl) chgEl.textContent = "";
      if (dateEl) dateEl.textContent = why ? `• ${why}` : "";
    };
    try {
      const res = await fetch("/api/ihsg");
      if (!res.ok) return unavailable("butuh server simulator");
      const data = await res.json();
      const price = Number(data.price);
      if (!Number.isFinite(price) || price <= 0) return unavailable("data belum ada");
      const nf2 = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
      const labelEl = $("#ihsg-label");
      if (data.status === "fallback") {
        // Angka contoh saat Sectors tidak terhubung: jangan tampil seolah data bursa terkini.
        if (labelEl) labelEl.textContent = "IHSG (angka contoh, data bursa offline)";
        valEl.textContent = price.toLocaleString("id-ID", nf2);
        if (chgEl) chgEl.textContent = "";
        if (dateEl) dateEl.textContent = "";
        return;
      }
      if (labelEl) labelEl.textContent = "IHSG penutupan terakhir";
      valEl.textContent = price.toLocaleString("id-ID", nf2);
      if (chgEl && Number.isFinite(Number(data.change_pct))) {
        const pct = Number(data.change_pct);
        const pts = Number(data.change_pts);
        const isPos = pct >= 0;
        const sign = isPos ? "+" : "";
        chgEl.style.color = isPos ? "#3ddc97" : "#ff6b61";
        chgEl.textContent = `${isPos ? "▲" : "▼"} ${sign}${pct.toLocaleString("id-ID", nf2)}%` +
          (Number.isFinite(pts) ? ` (${sign}${pts.toLocaleString("id-ID", nf2)})` : "");
      }
      if (dateEl && data.date) {
        const d = new Date(`${String(data.date).slice(0, 10)}T00:00:00`);
        const when = Number.isNaN(d.getTime()) ? String(data.date) : d.toLocaleDateString("id-ID", { day: "numeric", month: "short", year: "numeric" });
        dateEl.textContent = `• ${when} · data Sectors`;
      }
    } catch (err) {
      console.warn("IHSG ticker fetch error:", err);
      unavailable("butuh server simulator");
    }
  }
  fetchLiveIHSG();

  // ═════════════════════════ DEMO MINI (LIVE) ═════════════════════════
  if (!window.SimPasar) return;
  const { Market } = window.SimPasar;

  const el = {
    status: $("#simStatus"), price: $("#simPrice"), dev: $("#simDev"), tick: $("#simTick"),
    sentBar: $("#simSentBar"), sentVal: $("#simSentVal"),
    chart: $("#simChart"), grid: $("#simGrid"),
    stPos: $("#stPos"), stPain: $("#stPain"), stAvg: $("#stAvg"), stBSH: $("#stBSH"), stPnl: $("#stPnl"),
    log: $("#simLog"),
    btnRumor: $("#btnRumor"), btnPanic: $("#btnPanic"), btnPause: $("#btnPause"), btnReset: $("#btnReset"),
    selPop: $("#selPop"), selPsych: $("#selPsych"),
    day: $("#simDay"),
    panel: $("#demo"),
  };
  if (!el.chart || !el.grid || !el.panel) return;
  // Elemen teks opsional: bila tidak ada di HTML, pakai elemen dummy agar render() tidak error.
  const dummy = () => document.createElement("span");
  ["status", "price", "dev", "tick", "day", "sentBar", "sentVal", "stPos", "stPain", "stAvg", "stBSH", "stPnl", "log"]
    .forEach((k) => { if (!el[k]) el[k] = dummy(); });
  const on = (node, evt, fn) => { if (node) node.addEventListener(evt, fn); };

  // ── Waktu bursa simulasi: 1 tick = 1 menit ──
  // Identik dengan sim/simtime.py di server: Sesi 1 09:00–12:00 (180 menit),
  // Sesi 2 13:30–16:00 (150 menit) → 330 menit per hari bursa.
  const MINUTES_PER_DAY = 330;
  const pad2 = (n) => String(n).padStart(2, "0");
  function tickToClock(tick) {
    const t = Math.max(0, Math.trunc(Number(tick) || 0));
    const dayIndex = Math.floor(t / MINUTES_PER_DAY);
    const m = t % MINUTES_PER_DAY;
    let hh, mm, session;
    if (m < 180) { hh = 9 + Math.floor(m / 60); mm = m % 60; session = 1; }
    else { const m2 = m - 180; hh = 13 + Math.floor((30 + m2) / 60); mm = (30 + m2) % 60; session = 2; }
    return { day: dayIndex + 1, minuteOfDay: m, session, time: `${pad2(hh)}:${pad2(mm)}` };
  }
  function renderClock(tick) {
    const c = tickToClock(tick);
    el.tick.textContent = c.time;
    el.day.textContent = `hari ${c.day}`;
  }

  // Timeframe candle (menit per candle) → label & jarak label sumbu-X (tick)
  const TF = { 1: { label: "1m", every: 15 }, 5: { label: "5m", every: 30 }, 15: { label: "15m", every: 60 }, 30: { label: "30m", every: 60 }, 60: { label: "1H", every: 180 }, 330: { label: "1D", every: 330 } };
  const tfInfo = (p) => TF[p] || { label: `${p}m`, every: Math.max(15, p * 4) };

  const market = new Market({ nAgents: 100, fundamental: 100, seed: 42 });
  const TICK_MS = 125;               // ≈ 8 menit bursa per detik nyata (demo dipercepat)
  const COLORS = {
    text: "#eef1f6", dim: "#6f7a8c", line: "rgba(255,255,255,0.08)",
    amber: "#f6b73c", green: "#3ddc97", red: "#ff6b61", blue: "#6ea8ff", violet: "#b98cff", orange: "#ff9f43",
  };
  const ACTION_FILL = {
    fundamentalist: { buy: "#3ddc97", sell: "#ff6b61", hold: "#18211c" },
    chartist: { buy: "#5fe3a8", sell: "#ff847b", hold: "#1a1a2b" },
    noise: { buy: "#9ff0c9", sell: "#ffb3ad", hold: "#1b2230" },
  };
  const PSYCH_BORDER = { disciplined: COLORS.blue, bagholder: COLORS.violet, averager: COLORS.orange };
  const TYPE_DOT = { fundamentalist: COLORS.blue, chartist: COLORS.violet, noise: COLORS.orange };

  let lastStatus = "Normal";
  let sesMax = 100, sesMin = 100;
  let userPaused = false;
  let visible = true;
  let timer = null;

  // ── Chart candlestick + volume (TradingView style, landing/candlechart.js) ──
  // Opsi periodLabel/formatTickLabel/formatTickRange/labelEveryTicks/sessionBreaks
  // adalah opsi baru candlechart (label jam bursa). Bila versi candlechart belum
  // mengenalnya, opsi itu diabaikan dan chart memakai label tick bawaan + unitLabel "m".
  const DEFAULT_PERIOD = 15;
  const activeTf = $("#simTf button.active");
  const initialPeriod = Number(activeTf && activeTf.dataset.period) || DEFAULT_PERIOD;
  const chart = window.CandleChart
    ? window.CandleChart.create(el.chart, {
        period: initialPeriod,
        symbol: "DEMO",
        unitLabel: "m",
        tickLabel: "menit",
        periodLabel: tfInfo(initialPeriod).label,
        labelEveryTicks: tfInfo(initialPeriod).every,
        sessionBreaks: true,
        minutesPerDay: MINUTES_PER_DAY,
        formatTickLabel: (t0) => { const c = tickToClock(t0); return c.minuteOfDay === 0 ? `Hari ${c.day}` : c.time; },
        formatTickRange: (t0, t1) => { const a = tickToClock(t0), b = tickToClock(t1); return `Hari ${a.day} · ${a.time}` + (t1 > t0 ? `–${b.time}` : ""); },
        background: "#0c1119",
        fundamentalColor: COLORS.blue,
        bubbleColor: COLORS.amber,
        crashColor: COLORS.red,
        gridColor: "rgba(255,255,255,0.05)",
        formatPrice: (v) => v.toFixed(2),
        formatAxis: (v, d) => v.toFixed(d),
      })
    : null;
  function drawChart(state) {
    if (!chart) return;
    chart.setData({ tick: state.tick, prices: state.history, volumes: state.volumes, fundamental: state.fundamental });
  }
  // Lebar candle (menit bursa per candle)
  const tfButtons = $$("#simTf button");
  tfButtons.forEach((b) => b.addEventListener("click", () => {
    if (!chart) return;
    const p = Number(b.dataset.period) || DEFAULT_PERIOD;
    chart.setPeriod(p);
    if (typeof chart.setOptions === "function") chart.setOptions({ periodLabel: tfInfo(p).label, labelEveryTicks: tfInfo(p).every });
    tfButtons.forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-pressed", String(x === b)); });
  }));

  // ── Grid agen ──
  const gctx = el.grid.getContext("2d");
  const GRID_SIZE = 160, CELL = GRID_SIZE / 10;
  function drawGrid(agents) {
    gctx.clearRect(0, 0, GRID_SIZE, GRID_SIZE);
    agents.forEach((a, i) => {
      const col = i % 10, row = Math.floor(i / 10);
      const x0 = col * CELL, y0 = row * CELL;
      const fill = ACTION_FILL[a.type] || ACTION_FILL.noise;
      gctx.fillStyle = a.inPain ? "#4a0f0f" : fill[a.action];
      gctx.fillRect(x0 + 1, y0 + 1, CELL - 2, CELL - 2);
      gctx.strokeStyle = PSYCH_BORDER[a.psych] || "#444"; gctx.lineWidth = 0.8;
      gctx.strokeRect(x0 + 1.5, y0 + 1.5, CELL - 3, CELL - 3);
      gctx.fillStyle = TYPE_DOT[a.type]; gctx.beginPath(); gctx.arc(x0 + CELL / 2, y0 + CELL / 2, 1.6, 0, Math.PI * 2); gctx.fill();
      if (a.psych === "averager" && a.position > 1.05) {
        gctx.fillStyle = COLORS.orange; gctx.beginPath();
        gctx.moveTo(x0 + CELL - 4, y0 + CELL - 1.5); gctx.lineTo(x0 + CELL - 1.5, y0 + CELL - 1.5); gctx.lineTo(x0 + CELL - 2.75, y0 + CELL - 4.5); gctx.closePath(); gctx.fill();
      }
    });
  }

  // ── Log peristiwa ──
  const logs = [];
  function pushLog(html) {
    logs.unshift(html);
    if (logs.length > 3) logs.pop();
    el.log.innerHTML = logs.map((l) => `<li>${l}</li>`).join("");
  }

  // ── Render state ──
  function render(state) {
    const { price, fundamental, tick, status, sentiment, stats, history } = state;
    const prev = history.length >= 2 ? history[history.length - 2] : price;

    renderClock(tick);
    el.price.textContent = price.toFixed(2);
    el.price.style.color = price > prev ? COLORS.green : price < prev ? COLORS.red : COLORS.text;

    const dev = (price - fundamental) / fundamental * 100;
    el.dev.textContent = (dev >= 0 ? "+" : "") + dev.toFixed(1) + "%";
    el.dev.style.color = dev > 25 ? COLORS.amber : dev < -18 ? COLORS.red : dev > 0 ? COLORS.green : dev < 0 ? "#ff9d96" : COLORS.dim;

    const key = status === "Bubble" ? "bubble" : status === "Panik-Crash" ? "crash" : "normal";
    el.status.dataset.status = key;
    el.status.textContent = status === "Bubble" ? "▲ BUBBLE" : status === "Panik-Crash" ? "▼ PANIK-CRASH" : "● NORMAL";

    if (status !== lastStatus) {
      const color = key === "bubble" ? COLORS.amber : key === "crash" ? COLORS.red : COLORS.green;
      const label = key === "bubble" ? "BUBBLE: harga jauh di atas nilai wajar" : key === "crash" ? "PANIK-CRASH: harga jatuh" : "kembali NORMAL";
      pushLog(`<b style="color:${color}">${tickToClock(tick).time}</b> · ${label} @ ${price.toFixed(1)}`);
      lastStatus = status;
    }

    const spct = Math.min(50, Math.abs(sentiment) * 15);
    el.sentBar.style.width = spct + "%";
    el.sentBar.style.background = sentiment >= 0 ? COLORS.amber : COLORS.red;
    el.sentBar.style.transform = sentiment >= 0 ? "none" : "translateX(-100%)";
    el.sentVal.textContent = (sentiment >= 0 ? "+" : "") + sentiment.toFixed(3);

    el.stPos.textContent = stats.inPosition;
    el.stPain.textContent = stats.inPain;
    el.stPain.style.color = stats.inPain > 10 ? COLORS.red : stats.inPain > 5 ? COLORS.amber : COLORS.dim;
    el.stAvg.textContent = stats.averaging;
    el.stBSH.textContent = `${stats.buy} / ${stats.sell} / ${stats.hold}`;
    el.stPnl.textContent = (stats.avgPnlPct >= 0 ? "+" : "") + stats.avgPnlPct.toFixed(1) + "%";
    el.stPnl.style.color = stats.avgPnlPct > 0 ? COLORS.green : stats.avgPnlPct < -3 ? COLORS.red : COLORS.dim;

    if (price > sesMax) sesMax = price;
    if (price < sesMin) sesMin = price;

    drawChart(state);
    drawGrid(state.agents);
  }

  // ── Loop ──
  function loop() {
    if (userPaused || !visible || document.hidden) return;
    render(market.step());
  }
  function start() { if (!timer) timer = setInterval(loop, TICK_MS); }
  function stop() { if (timer) { clearInterval(timer); timer = null; } }

  // Hanya jalan saat panel terlihat (hemat CPU)
  if ("IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      visible = entries.some((e) => e.isIntersecting);
    }, { threshold: 0.05 }).observe(el.panel);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) render(market.getState()); });
  window.addEventListener("resize", () => render(market.getState()));

  // ── Kontrol ──
  const flash = (btn) => { if (!btn) return; btn.style.filter = "brightness(1.6)"; setTimeout(() => (btn.style.filter = ""), 180); };
  const now = () => tickToClock(market.tick).time;
  on(el.btnRumor, "click", () => {
    market.injectRumor(1.0); flash(el.btnRumor);
    pushLog(`<b style="color:${COLORS.amber}">${now()}</b> · 📢 rumor disebar, pemburu rumor condong beli`);
    render(market.getState());
  });
  on(el.btnPanic, "click", () => {
    market.injectPanic(1.0); flash(el.btnPanic);
    pushLog(`<b style="color:${COLORS.red}">${now()}</b> · 📉 kabar buruk, pemburu rumor condong jual`);
    render(market.getState());
  });
  on(el.btnPause, "click", () => {
    userPaused = !userPaused;
    el.btnPause.textContent = userPaused ? "▶" : "⏸";
    el.btnPause.setAttribute("aria-label", userPaused ? "Lanjutkan simulasi" : "Jeda simulasi");
  });
  on(el.btnReset, "click", () => {
    market.reset(); sesMax = sesMin = 100; lastStatus = "Normal"; logs.length = 0; el.log.innerHTML = "";
    pushLog(`<b>09:00</b> · mulai ulang dari harga awal 100`);
    render(market.getState());
  });
  on(el.selPop, "change", () => {
    const [f, c, n] = el.selPop.value.split(",").map(Number);
    market.setPopulation(f, c, n);
    pushLog(`<b style="color:${COLORS.blue}">${now()}</b> · nilai wajar ${Math.round(f * 100)}% · tren ${Math.round(c * 100)}% · rumor ${Math.round(n * 100)}%`);
    render(market.getState());
  });
  on(el.selPsych, "change", () => {
    const [d, b] = el.selPsych.value.split(",").map(Number);
    market.setPsych(d, b);
    pushLog(`<b style="color:${COLORS.violet}">${now()}</b> · disiplin ${Math.round(d * 100)}% · keras kepala ${Math.round(b * 100)}% · suka nambah ${Math.round((1 - d - b) * 100)}%`);
    render(market.getState());
  });

  // Mulai
  pushLog(`<b>09:00</b> · 100 investor tiruan siap · harga awal 100`);
  render(market.getState());
  start();
})();

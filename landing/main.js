/**
 * main.js — interaksi landing page: nav, scroll-reveal, matriks agen,
 * salin perintah, dan renderer untuk simulasi live (SimPasar.Market).
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

  function onScroll() { nav.classList.toggle("scrolled", window.scrollY > 8); }
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  navToggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", String(open));
    navToggle.setAttribute("aria-label", open ? "Tutup menu" : "Buka menu");
  });
  navLinks.forEach((a) => a.addEventListener("click", () => {
    nav.classList.remove("open");
    navToggle.setAttribute("aria-expanded", "false");
  }));

  // Tandai link aktif berdasarkan section yang terlihat
  const sections = navLinks.map((a) => $(a.getAttribute("href"))).filter(Boolean);
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

  // ───────────────────────────── SALIN ─────────────────────────────
  const copyBtn = $("#copyCmd");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      const text = $("#cmdBlock").innerText.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).map((l) => l.replace(/\s+#.*$/, "")).join("\n");
      try {
        await navigator.clipboard.writeText(text);
        copyBtn.textContent = "Tersalin ✓";
      } catch {
        copyBtn.textContent = "Gagal menyalin";
      }
      setTimeout(() => (copyBtn.textContent = "Salin"), 1600);
    });
  }
  const yearEl = $("#year");
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());

  // ═════════════════════════ LIVE SIMULATION ═════════════════════════
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
    panel: $("#demo"),
  };
  if (!el.chart || !el.grid) return;

  const market = new Market({ nAgents: 100, fundamental: 100, seed: 42 });
  const TICK_MS = 125;               // ≈ 8 tick/detik (server asli: 10/detik)
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
  const chart = window.CandleChart
    ? window.CandleChart.create(el.chart, {
        period: 5,
        symbol: "SIMPASAR",
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
  // Periode candle (tick per candle)
  const tfButtons = $$("#simTf button");
  tfButtons.forEach((b) => b.addEventListener("click", () => {
    if (!chart) return;
    chart.setPeriod(Number(b.dataset.period) || 5);
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

    el.tick.textContent = tick;
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
      pushLog(`<b style="color:${color}">tick ${tick}</b> · status → ${status} @ ${price.toFixed(1)}`);
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
  const flash = (btn) => { btn.style.filter = "brightness(1.6)"; setTimeout(() => (btn.style.filter = ""), 180); };
  el.btnRumor.addEventListener("click", () => {
    market.injectRumor(1.0); flash(el.btnRumor);
    pushLog(`<b style="color:${COLORS.amber}">tick ${market.tick}</b> · 📢 rumor disuntik (+1.0)`);
    render(market.getState());
  });
  el.btnPanic.addEventListener("click", () => {
    market.injectPanic(1.0); flash(el.btnPanic);
    pushLog(`<b style="color:${COLORS.red}">tick ${market.tick}</b> · 📉 bad news disuntik (−1.0)`);
    render(market.getState());
  });
  el.btnPause.addEventListener("click", () => {
    userPaused = !userPaused;
    el.btnPause.textContent = userPaused ? "▶" : "⏸";
    el.btnPause.setAttribute("aria-label", userPaused ? "Lanjutkan simulasi" : "Jeda simulasi");
  });
  el.btnReset.addEventListener("click", () => {
    market.reset(); sesMax = sesMin = 100; lastStatus = "Normal"; logs.length = 0; el.log.innerHTML = "";
    pushLog(`<b>tick 0</b> · reset · seed ${market.seed}`);
    render(market.getState());
  });
  el.selPop.addEventListener("change", () => {
    const [f, c, n] = el.selPop.value.split(",").map(Number);
    market.setPopulation(f, c, n);
    pushLog(`<b style="color:${COLORS.blue}">tick ${market.tick}</b> · populasi F${Math.round(f * 100)}/C${Math.round(c * 100)}/N${Math.round(n * 100)}`);
    render(market.getState());
  });
  el.selPsych.addEventListener("change", () => {
    const [d, b] = el.selPsych.value.split(",").map(Number);
    market.setPsych(d, b);
    pushLog(`<b style="color:${COLORS.violet}">tick ${market.tick}</b> · psikologi D${Math.round(d * 100)}/B${Math.round(b * 100)}/A${Math.round((1 - d - b) * 100)}`);
    render(market.getState());
  });

  // Mulai
  pushLog(`<b>tick 0</b> · 100 agen dibangun · seed ${market.seed}`);
  render(market.getState());
  start();

  // ── Fetch Real-time IHSG from Sectors MCP ──
  async function fetchLiveIHSG() {
    try {
      const res = await fetch("/api/ihsg");
      if (!res.ok) return;
      const data = await res.json();
      const valEl = $("#ihsg-val");
      const chgEl = $("#ihsg-change");
      const dateEl = $("#ihsg-date");
      if (valEl && data.price) {
        valEl.textContent = Number(data.price).toLocaleString("id-ID", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }
      if (chgEl && data.change_pct !== undefined) {
        const isPos = data.change_pct >= 0;
        chgEl.style.color = isPos ? "#3fb950" : "#f85149";
        chgEl.textContent = `${isPos ? "+" : ""}${data.change_pct.toFixed(2)}% (${isPos ? "+" : ""}${data.change_pts})`;
      }
      if (dateEl && data.date) {
        dateEl.textContent = `• ${data.date}`;
      }
    } catch (err) {
      console.warn("IHSG ticker fetch error:", err);
    }
  }
  fetchLiveIHSG();
})();

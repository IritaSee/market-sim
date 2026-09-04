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
  const CHART_N = 180;
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

  // ── Chart ──
  const cctx = el.chart.getContext("2d");
  function drawChart(state) {
    const c = el.chart;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = c.clientWidth || 300, H = c.clientHeight || 220;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
    cctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    cctx.clearRect(0, 0, W, H);

    const f = state.fundamental;
    const h = state.history.slice(-CHART_N);
    const padL = 8, padR = 46, padT = 12, padB = 18;
    let lo = Math.min(f * 0.85, ...h), hi = Math.max(f * 1.2, ...h);
    const span = (hi - lo) || 10; lo -= span * 0.08; hi += span * 0.08;
    const x = (i) => padL + ((i + (CHART_N - h.length)) / (CHART_N - 1)) * (W - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

    // gridlines + label
    cctx.font = "10px JetBrains Mono, ui-monospace, monospace";
    cctx.textAlign = "left"; cctx.textBaseline = "middle";
    const steps = 4;
    for (let i = 0; i <= steps; i++) {
      const v = lo + (hi - lo) * (i / steps);
      const yy = y(v);
      cctx.strokeStyle = COLORS.line; cctx.lineWidth = 1;
      cctx.beginPath(); cctx.moveTo(padL, yy); cctx.lineTo(W - padR, yy); cctx.stroke();
      cctx.fillStyle = COLORS.dim; cctx.fillText(v.toFixed(0), W - padR + 8, yy);
    }

    // ambang bubble / crash
    const band = (v, color, label) => {
      const yy = y(v);
      if (yy < padT || yy > H - padB) return;
      cctx.setLineDash([2, 4]); cctx.strokeStyle = color; cctx.globalAlpha = 0.45;
      cctx.beginPath(); cctx.moveTo(padL, yy); cctx.lineTo(W - padR, yy); cctx.stroke();
      cctx.globalAlpha = 1; cctx.setLineDash([]);
      cctx.fillStyle = color; cctx.globalAlpha = 0.8; cctx.textAlign = "left";
      cctx.fillText(label, padL + 4, yy - 7); cctx.globalAlpha = 1;
    };
    band(f * 1.25, COLORS.amber, "bubble 125%");
    band(f * 0.82, COLORS.red, "crash 82%");

    // fundamental
    cctx.setLineDash([6, 4]); cctx.strokeStyle = COLORS.blue; cctx.lineWidth = 1.2; cctx.globalAlpha = 0.9;
    cctx.beginPath(); cctx.moveTo(padL, y(f)); cctx.lineTo(W - padR, y(f)); cctx.stroke();
    cctx.setLineDash([]); cctx.globalAlpha = 1;
    cctx.fillStyle = COLORS.blue; cctx.textAlign = "left"; cctx.fillText("fundamental 100", padL + 4, y(f) + 9);

    if (h.length < 2) return;
    const lineColor = state.status === "Bubble" ? COLORS.amber : state.status === "Panik-Crash" ? COLORS.red : COLORS.text;

    // area antara harga dan fundamental
    cctx.beginPath();
    cctx.moveTo(x(0), y(h[0]));
    for (let i = 1; i < h.length; i++) cctx.lineTo(x(i), y(h[i]));
    cctx.lineTo(x(h.length - 1), y(f)); cctx.lineTo(x(0), y(f)); cctx.closePath();
    cctx.fillStyle = hexToRgba(lineColor, 0.12); cctx.fill();

    // garis harga
    cctx.beginPath(); cctx.moveTo(x(0), y(h[0]));
    for (let i = 1; i < h.length; i++) cctx.lineTo(x(i), y(h[i]));
    cctx.strokeStyle = lineColor; cctx.lineWidth = 2; cctx.lineJoin = "round"; cctx.stroke();

    // titik terakhir
    const lx = x(h.length - 1), ly = y(h[h.length - 1]);
    cctx.fillStyle = lineColor; cctx.beginPath(); cctx.arc(lx, ly, 3.2, 0, Math.PI * 2); cctx.fill();
    cctx.fillStyle = hexToRgba(lineColor, 0.25); cctx.beginPath(); cctx.arc(lx, ly, 8, 0, Math.PI * 2); cctx.fill();
  }

  function hexToRgba(hex, a) {
    const m = hex.replace("#", "");
    const n = parseInt(m.length === 3 ? m.split("").map((ch) => ch + ch).join("") : m, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

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
})();

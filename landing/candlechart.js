/**
 * candlechart.js — chart candlestick + volume bergaya TradingView, tanpa dependensi.
 *
 * Dipakai oleh landing page (landing/main.js) dan dashboard simulator
 * (frontend/index.html). File ini dilayani di "/candlechart.js" oleh
 * landing/serve.js maupun server FastAPI (folder landing/ di-mount di "/").
 *
 * API:
 *   const chart = CandleChart.create(canvas, { period: 5, symbol: "SIMPASAR", ... });
 *   chart.setData({ tick, prices, volumes, fundamental });
 *   chart.setPeriod(10);            // tick per candle
 *   chart.setOptions({ ... });      // ubah opsi lalu gambar ulang
 *   chart.getPeriod(); chart.getCandles(); chart.redraw(); chart.destroy();
 *
 * Data: prices[i] = harga penutupan pada tick (tick - prices.length + 1 + i),
 * volumes[i] = total |order| agen pada tick tersebut (intensitas transaksi).
 * Candle dibentuk dari `period` tick dengan batas tetap (kelipatan period);
 * open = close tick sebelumnya, sehingga candle tidak bergeser saat data baru
 * masuk dan harga bergerak kontinu tanpa celah.
 *
 * Tampilan: candle hijau/merah TradingView, panel volume, sumbu harga kanan,
 * garis dan pil harga terakhir, garis fundamental, ambang bubble/crash, legenda
 * OHLC, dan crosshair (mouse; sentuhan lewat tekan-geser). Geometri digambar di
 * ruang device pixel supaya tetap tajam pada skala layar 125%/150%.
 */
(function (global) {
  "use strict";

  const MONO = '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';

  const DEFAULTS = {
    period: 5,                      // tick per candle
    background: "#131722",          // null/"transparent" → hanya clearRect
    upColor: "#26a69a",
    downColor: "#ef5350",
    textColor: "#9598a1",           // label sumbu
    legendTextColor: "#d1d4dc",
    legendMutedColor: "#868993",
    gridColor: "rgba(255,255,255,0.06)",
    axisLineColor: "rgba(255,255,255,0.12)",
    crosshairColor: "#758696",
    crosshairLabelBg: "#363a45",
    fundamentalColor: "#2962ff",
    bubbleColor: "#f6b73c",
    crashColor: "#ef5350",
    bubbleRatio: 1.25,
    crashRatio: 0.82,
    showThresholds: true,
    showVolume: true,
    volumeHeightRatio: 0.2,
    volumeAlpha: 0.45,
    minBarSpacing: 4,
    maxBarSpacing: 28,
    minVisibleBars: 30,             // slot minimum; data sedikit tidak membuat candle raksasa
    rightOffsetBars: 2,
    font: "11px " + MONO,
    legendFont: "12px " + MONO,
    legendFontBold: "700 12px " + MONO,
    symbol: "SIMPASAR",
    unitLabel: "T",                 // "5T" = 5 tick per candle (notasi interval tick TradingView)
    tickLabel: "tick",
    formatPrice: (v) => v.toFixed(2),
    formatAxis: (v, decimals) => v.toFixed(decimals),
    formatPercent: (v) => v.toFixed(2) + "%",
    formatVolume: (v) => (v >= 1000 ? (v / 1000).toFixed(1) + "K" : v.toFixed(1)),
    crosshair: true,
    maxDpr: 3,
  };

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // Langkah grid "cantik" (1, 2, 2.5, 5 × 10^k) untuk sekitar targetCount garis.
  function niceStep(range, targetCount) {
    const rough = range / Math.max(1, targetCount);
    if (!(rough > 0) || !Number.isFinite(rough)) return 1;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    const norm = rough / mag;
    const nice = norm < 1.5 ? 1 : norm < 2.25 ? 2 : norm < 3.75 ? 2.5 : norm < 7.5 ? 5 : 10;
    return nice * mag;
  }

  // Jumlah desimal minimum agar kelipatan `step` tampil tepat (2.5 → 1, 0.25 → 2).
  function decimalsFor(step) {
    let d = 0;
    while (d < 6) {
      const scaled = step * Math.pow(10, d);
      if (Math.abs(Math.round(scaled) - scaled) <= 1e-6 * Math.max(1, scaled)) break;
      d++;
    }
    return d;
  }

  // Warna teks pil berdasar kecerahan latar (aturan yang sama dengan lightweight-charts).
  function textOn(bg) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(bg || ""));
    if (!m) return "#ffffff";
    const n = parseInt(m[1], 16);
    const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
    return 0.199 * r + 0.687 * g + 0.114 * b > 160 ? "#131722" : "#ffffff";
  }

  // Jarak antar candle dari kapasitas jendela yang stabil, bukan jumlah candle per frame,
  // supaya candle tidak "melompat" setiap kali candle pertama yang terpotong dibuang.
  function computeSpacing(plotW, nCandles, capacity, opt) {
    const slots = Math.max(Math.max(nCandles, capacity) + opt.rightOffsetBars, opt.minVisibleBars);
    return clamp(plotW / slots, opt.minBarSpacing, opt.maxBarSpacing);
  }

  /**
   * Agregasi deret tick menjadi candle OHLCV dengan batas tetap.
   * Candle pertama dibuang bila jendela history terpotong (startTick > 0),
   * supaya semua candle yang tersisa punya open yang benar (close tick sebelumnya).
   */
  function buildCandles(prices, volumes, tick, period) {
    const n = prices ? prices.length : 0;
    if (!n || period < 1) return [];
    const startTick = tick - n + 1;
    const out = [];
    let cur = null;
    for (let i = 0; i < n; i++) {
      const p = Number(prices[i]);
      if (!Number.isFinite(p)) continue;
      const t = startTick + i;
      const id = Math.floor(t / period);
      const vRaw = volumes ? Number(volumes[i]) : 0;
      const v = Number.isFinite(vRaw) ? vRaw : 0;
      if (!cur || cur.id !== id) {
        const prev = i > 0 ? Number(prices[i - 1]) : NaN;
        const open = Number.isFinite(prev) ? prev : p;
        cur = {
          id,
          t0: id * period,
          t1: id * period + period - 1,
          open,
          high: Math.max(open, p),
          low: Math.min(open, p),
          close: p,
          volume: v,
          ticks: 1,
        };
        out.push(cur);
      } else {
        if (p > cur.high) cur.high = p;
        if (p < cur.low) cur.low = p;
        cur.close = p;
        cur.volume += v;
        cur.ticks += 1;
      }
    }
    if (out.length > 1 && startTick > 0) out.shift();
    return out;
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.lineTo(x + w - rr, y);
    ctx.arcTo(x + w, y, x + w, y + rr, rr);
    ctx.lineTo(x + w, y + h - rr);
    ctx.arcTo(x + w, y + h, x + w - rr, y + h, rr);
    ctx.lineTo(x + rr, y + h);
    ctx.arcTo(x, y + h, x, y + h - rr, rr);
    ctx.lineTo(x, y + rr);
    ctx.arcTo(x, y, x + rr, y, rr);
    ctx.closePath();
  }

  function create(canvas, options) {
    if (!canvas || !canvas.getContext) throw new Error("CandleChart.create: canvas tidak valid");
    const opt = Object.assign({}, DEFAULTS, options || {});
    const ctx = canvas.getContext("2d");

    let data = { tick: 0, prices: [], volumes: [], fundamental: NaN };
    let candles = [];
    let hover = null;          // { x, y } dalam px CSS relatif ke area konten canvas
    let touchActive = false;
    let raf = 0;
    let destroyed = false;
    let deviceBox = null;      // ukuran bitmap tepat dari ResizeObserver (device px), bila tersedia
    let dprQuery = null;

    function schedule() {
      if (!raf && !destroyed) raf = global.requestAnimationFrame(draw);
    }

    function rebuild() {
      candles = buildCandles(data.prices, data.volumes, data.tick, opt.period);
    }

    function setData(d) {
      d = d || {};
      data = {
        tick: Number.isFinite(Number(d.tick)) ? Math.trunc(Number(d.tick)) : 0,
        prices: Array.isArray(d.prices) ? d.prices : [],
        volumes: Array.isArray(d.volumes) ? d.volumes : [],
        fundamental: Number(d.fundamental),
      };
      rebuild();
      schedule();
    }

    function setPeriod(p) {
      p = Math.max(1, Math.trunc(Number(p) || 1));
      if (p === opt.period) return;
      opt.period = p;
      rebuild();
      schedule();
    }

    function setOptions(partial) {
      Object.assign(opt, partial || {});
      rebuild();
      schedule();
    }

    function measureBitmap(dpr) {
      const rect = canvas.getBoundingClientRect();
      const borderX = Math.max(0, canvas.offsetWidth - canvas.clientWidth);
      const borderY = Math.max(0, canvas.offsetHeight - canvas.clientHeight);
      let bw = Math.round(Math.max(0, rect.width - borderX) * dpr);
      let bh = Math.round(Math.max(0, rect.height - borderY) * dpr);
      if (deviceBox && Math.abs(deviceBox.w - bw) <= 2 && Math.abs(deviceBox.h - bh) <= 2) {
        bw = deviceBox.w;
        bh = deviceBox.h;
      }
      return { bw, bh };
    }

    // ── Gambar ──────────────────────────────────────────────────────
    function draw() {
      raf = 0;
      if (destroyed) return;
      const dpr = Math.min(global.devicePixelRatio || 1, opt.maxDpr);
      const { bw, bh } = measureBitmap(dpr);
      if (bw < 40 || bh < 40) return;
      if (canvas.width !== bw || canvas.height !== bh) { canvas.width = bw; canvas.height = bh; }
      const W = bw / dpr, H = bh / dpr;
      const lw = Math.max(1, Math.floor(dpr));            // tebal garis dalam device px (bulat)
      const P = (v) => Math.round(v * dpr);                // px CSS → device px
      const cssMode = () => ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const devMode = () => ctx.setTransform(1, 0, 0, 1, 0, 0);

      function hLine(y, x0, x1, color, dash, alpha) {
        devMode();
        ctx.globalAlpha = alpha == null ? 1 : alpha;
        ctx.strokeStyle = color;
        ctx.lineWidth = lw;
        ctx.setLineDash(dash ? dash.map((d) => Math.max(1, Math.round(d * dpr))) : []);
        const yy = P(y) + (lw % 2 ? 0.5 : 0);
        ctx.beginPath(); ctx.moveTo(P(x0), yy); ctx.lineTo(P(x1), yy); ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
      function vLine(x, y0, y1, color, dash, alpha) {
        devMode();
        ctx.globalAlpha = alpha == null ? 1 : alpha;
        ctx.strokeStyle = color;
        ctx.lineWidth = lw;
        ctx.setLineDash(dash ? dash.map((d) => Math.max(1, Math.round(d * dpr))) : []);
        const xx = P(x) + (lw % 2 ? 0.5 : 0);
        ctx.beginPath(); ctx.moveTo(xx, P(y0)); ctx.lineTo(xx, P(y1)); ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
      }
      function text(str, x, y, color, font, align) {
        cssMode();
        ctx.font = font || opt.font;
        ctx.fillStyle = color;
        ctx.textAlign = align || "left";
        ctx.textBaseline = "middle";
        ctx.fillText(str, x, y);
      }

      devMode();
      ctx.globalAlpha = 1;
      ctx.clearRect(0, 0, bw, bh);
      if (opt.background && opt.background !== "transparent") {
        ctx.fillStyle = opt.background;
        ctx.fillRect(0, 0, bw, bh);
      }

      const f = data.fundamental;
      const n = candles.length;
      const last = n ? candles[n - 1] : null;

      // Lebar sumbu harga mengikuti label terlebar.
      cssMode();
      ctx.font = opt.font;
      let axisW = 48;
      const samples = [];
      if (last) samples.push(last.high, last.low, last.close);
      if (Number.isFinite(f)) samples.push(f);
      for (const v of samples) axisW = Math.max(axisW, ctx.measureText(opt.formatPrice(v)).width + 18);
      axisW = Math.min(Math.ceil(axisW), 110);

      const timeH = 22;
      const plotX0 = 0, plotX1 = W - axisW;
      const plotY0 = 0, plotY1 = H - timeH;
      const plotW = plotX1 - plotX0;
      const volH = opt.showVolume ? Math.round((plotY1 - plotY0) * opt.volumeHeightRatio) : 0;
      const priceY0 = plotY0, priceY1 = plotY1 - volH;
      const volY0 = priceY1, volY1 = plotY1;

      // Kapasitas jendela: opsi windowTicks (mis. 400 di simulator) membuat spasi stabil sejak koneksi
      // pertama; tanpa opsi itu, kapasitas mengikuti panjang data yang sudah ada (landing).
      const windowTicks = opt.windowTicks > 0 ? opt.windowTicks : (data.prices ? data.prices.length : 0);
      const capacity = Math.ceil(windowTicks / opt.period);
      const spacing = computeSpacing(plotW, n, capacity, opt);
      let bodyPx = Math.max(lw, Math.floor(spacing * dpr * 0.72));
      if ((bodyPx - lw) % 2 !== 0) bodyPx = Math.max(lw, bodyPx - 1);   // badan & sumbu wick satu paritas
      const xLast = plotX1 - opt.rightOffsetBars * spacing - spacing / 2;
      const maxVisible = Math.max(1, Math.floor((xLast - plotX0 + spacing / 2) / spacing));
      const startIdx = Math.max(0, n - maxVisible);
      const xOf = (i) => xLast - (n - 1 - i) * spacing;

      // ── Legenda: grup label+nilai diukur utuh supaya tidak terpotong saat dibungkus ──
      const LEG_X0 = plotX0 + 8, LEG_Y0 = plotY0 + 12, LEG_LH = 17, LEG_MAXX = plotX1 - 8;
      function legendGroups(lc, idx) {
        const col = lc.close >= lc.open ? opt.upColor : opt.downColor;
        const prevClose = idx > 0 ? candles[idx - 1].close : lc.open;
        const chg = lc.close - prevClose;
        const pct = prevClose ? (chg / prevClose) * 100 : 0;
        const sign = chg >= 0 ? "+" : "";
        const K = opt.legendMutedColor, F = opt.legendFont;
        const groups = [
          { parts: [[opt.symbol, opt.legendTextColor, opt.legendFontBold], [` · ${opt.period}${opt.unitLabel}`, K, F]], gap: 12 },
          { parts: [["O ", K, F], [opt.formatPrice(lc.open), col, F]], gap: 10 },
          { parts: [["H ", K, F], [opt.formatPrice(lc.high), col, F]], gap: 10 },
          { parts: [["L ", K, F], [opt.formatPrice(lc.low), col, F]], gap: 10 },
          { parts: [["C ", K, F], [opt.formatPrice(lc.close), col, F]], gap: 10 },
          { parts: [[`${sign}${opt.formatPrice(chg)} (${sign}${opt.formatPercent(pct)})`, col, F]], gap: 0 },
        ];
        if (opt.showVolume) {
          groups.push({ newline: true });
          groups.push({ parts: [["Vol ", K, F], [opt.formatVolume(lc.volume), col, F]], gap: 0 });
        }
        return groups;
      }
      function layoutLegend(groups) {
        const items = [];
        let x = LEG_X0, line = 0;
        for (const g of groups) {
          if (g.newline) { x = LEG_X0; line++; continue; }
          const widths = g.parts.map(([t, , font]) => { ctx.font = font; return ctx.measureText(t).width; });
          const total = widths.reduce((s, w) => s + w, 0);
          if (x + total > LEG_MAXX && x > LEG_X0) { x = LEG_X0; line++; }
          g.parts.forEach(([t, color, font], i) => { items.push({ t, color, font, x, line }); x += widths[i]; });
          x += g.gap;
        }
        return { items, lines: line + 1 };
      }
      const legendLast = last ? layoutLegend(legendGroups(last, n - 1)) : { items: [], lines: 1 };

      // ── Rentang harga: candle terlihat + fundamental + ruang legenda dalam piksel ──
      let lo = Infinity, hi = -Infinity;
      for (let i = startIdx; i < n; i++) {
        const c = candles[i];
        if (c.low < lo) lo = c.low;
        if (c.high > hi) hi = c.high;
      }
      if (Number.isFinite(f)) { lo = Math.min(lo, f); hi = Math.max(hi, f); }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
      if (hi - lo < 1e-9) { const d = Math.abs(hi) * 0.01 || 1; lo -= d; hi += d; }
      const range = hi - lo;
      lo -= range * 0.08;
      hi += range * 0.04;
      const legendPx = legendLast.lines * LEG_LH + 10;
      const topFrac = Math.min(0.45, legendPx / Math.max(1, priceY1 - priceY0));
      hi = (hi - topFrac * lo) / (1 - topFrac);
      const yOf = (v) => priceY0 + (1 - (v - lo) / (hi - lo)) * (priceY1 - priceY0);
      const priceAt = (y) => lo + (1 - (y - priceY0) / (priceY1 - priceY0)) * (hi - lo);

      // ── Crosshair: menempel ke candle terdekat, atau bebas di area kosong ──
      let hoverIdx = -1, hoverX = null;
      const inPlot = !!(hover && opt.crosshair && hover.x >= plotX0 && hover.x <= plotX1 && hover.y >= plotY0 && hover.y <= plotY1);
      if (inPlot) {
        hoverX = hover.x;
        if (n) {
          const idx = clamp(Math.round(n - 1 - (xLast - hover.x) / spacing), startIdx, n - 1);
          if (Math.abs(xOf(idx) - hover.x) <= spacing * 0.75) { hoverIdx = idx; hoverX = xOf(idx); }
        }
      }
      const hoverInPrice = inPlot && hover.y >= priceY0 && hover.y <= priceY1;

      // ── Pil sumbu kanan: posisi dihitung dulu (prioritas crosshair > harga terakhir > fundamental) ──
      const pillH = 18;
      const clampPill = (y) => clamp(y, priceY0 + pillH / 2, priceY1 - pillH / 2);
      const lastColor = last ? (last.close >= last.open ? opt.upColor : opt.downColor) : null;
      const pills = [];
      if (Number.isFinite(f)) pills.push({ prio: 1, y: yOf(f), text: opt.formatPrice(f), bg: opt.fundamentalColor });
      if (last) pills.push({ prio: 2, y: yOf(last.close), text: opt.formatPrice(last.close), bg: lastColor });
      if (hoverInPrice) pills.push({ prio: 3, y: hover.y, text: opt.formatPrice(priceAt(hover.y)), bg: opt.crosshairLabelBg });
      const placed = [];
      for (const p of pills.slice().sort((a, b) => b.prio - a.prio)) {
        let y = clampPill(p.y);
        for (let k = 0; k < 4; k++) {
          const hitIdx = placed.findIndex((q) => Math.abs(q - y) < pillH + 1);
          if (hitIdx < 0) break;
          const hit = placed[hitIdx];
          const above = clampPill(hit - pillH - 1), below = clampPill(hit + pillH + 1);
          const preferAbove = p.y <= hit;
          const first = preferAbove ? above : below, second = preferAbove ? below : above;
          y = Math.abs(first - hit) >= pillH + 1 ? first : second;
        }
        p.drawY = y;
        placed.push(y);
      }
      const hitsPill = (y) => placed.some((q) => Math.abs(q - y) < pillH / 2 + 7);

      // ── Grid horizontal + label sumbu harga ──
      const step = niceStep(hi - lo, Math.max(3, Math.floor((priceY1 - priceY0) / 36)));
      const dec = decimalsFor(step);
      for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
        const v = k * step;
        const y = yOf(v);
        if (y < priceY0 + 6 || y > priceY1 - 6) continue;
        hLine(y, plotX0, plotX1, opt.gridColor);
        if (!hitsPill(y)) text(opt.formatAxis(v, dec), plotX1 + 8, y, opt.textColor, opt.font, "left");
      }

      // ── Grid vertikal + label tick ──
      let kStep = 1;
      for (const k of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]) { kStep = k; if (k * spacing >= 72) break; }
      for (let i = startIdx; i < n; i++) {
        const c = candles[i];
        if (c.id % kStep !== 0) continue;
        const x = xOf(i);
        if (x < plotX0 + 2 || x > plotX1 - 2) continue;
        vLine(x, plotY0, plotY1, opt.gridColor);
        if (hoverIdx >= 0 && Math.abs(x - hoverX) < 52) continue;   // beri tempat label crosshair
        text(String(c.t0), x, plotY1 + timeH / 2 + 1, opt.textColor, opt.font, "center");
      }

      // ── Garis ambang & fundamental (di bawah candle; label ambang digambar belakangan) ──
      const thresholdLabels = [];
      if (Number.isFinite(f)) {
        if (opt.showThresholds) {
          const midY = (priceY0 + priceY1) / 2;
          const marks = [
            [f * opt.bubbleRatio, opt.bubbleColor, "Bubble ×" + opt.bubbleRatio],
            [f * opt.crashRatio, opt.crashColor, "Crash ×" + opt.crashRatio],
          ];
          for (const [v, color, label] of marks) {
            if (v <= lo || v >= hi) continue;
            const y = yOf(v);
            hLine(y, plotX0, plotX1, color, [2, 4], 0.55);
            thresholdLabels.push([label, color, y < midY ? y + 9 : y - 9]);
          }
        }
        hLine(yOf(f), plotX0, plotX1, opt.fundamentalColor, [5, 4], 0.9);
      }

      // ── Candle (device px: wick & badan tajam) ──
      devMode();
      for (let i = startIdx; i < n; i++) {
        const c = candles[i];
        ctx.fillStyle = c.close >= c.open ? opt.upColor : opt.downColor;
        const xc = P(xOf(i));
        const yH = P(yOf(c.high)), yL = P(yOf(c.low));
        const yO = P(yOf(c.open)), yC = P(yOf(c.close));
        ctx.fillRect(xc - Math.floor(lw / 2), yH, lw, Math.max(lw, yL - yH));
        ctx.fillRect(xc - Math.floor(bodyPx / 2), Math.min(yO, yC), bodyPx, Math.max(lw, Math.abs(yC - yO)));
      }

      // ── Volume ──
      if (opt.showVolume && n) {
        hLine(volY0, plotX0, plotX1, opt.gridColor);
        let maxV = 0;
        for (let i = startIdx; i < n; i++) if (candles[i].volume > maxV) maxV = candles[i].volume;
        if (maxV > 0) {
          devMode();
          ctx.globalAlpha = opt.volumeAlpha;
          const base = P(volY1);
          const usable = Math.max(1, P(volY1) - P(volY0) - Math.round(4 * dpr));
          for (let i = startIdx; i < n; i++) {
            const c = candles[i];
            const hgt = Math.max(lw, Math.round((c.volume / maxV) * usable));
            ctx.fillStyle = c.close >= c.open ? opt.upColor : opt.downColor;
            ctx.fillRect(P(xOf(i)) - Math.floor(bodyPx / 2), base - hgt, bodyPx, hgt);
          }
          ctx.globalAlpha = 1;
        }
      }

      // ── Garis pemisah sumbu ──
      hLine(plotY1, plotX0, W, opt.axisLineColor);
      vLine(plotX1, plotY0, plotY1, opt.axisLineColor);

      // ── Garis harga terakhir ──
      if (last) hLine(yOf(last.close), plotX0, plotX1, lastColor, [2, 2], 0.85);

      // ── Label ambang: di atas candle & garis harga, tetapi di sisi KIRI (menutupi candle lama,
      //    bukan candle terbaru) dan tidak pernah di dalam baris legenda ──
      if (thresholdLabels.length) {
        const legendForLabels = hoverIdx >= 0 ? layoutLegend(legendGroups(candles[hoverIdx], hoverIdx)) : legendLast;
        const legendBottom = legendForLabels.items.length ? LEG_Y0 + (legendForLabels.lines - 1) * LEG_LH + 10 : plotY0;
        cssMode();
        ctx.font = opt.font;
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const backing = (opt.background && opt.background !== "transparent") ? opt.background : "#000000";
        const usedY = [];
        for (const [label, color, lyRaw] of thresholdLabels) {
          let ly = Math.max(lyRaw, legendBottom + 9);
          while (usedY.some((u) => Math.abs(u - ly) < 17)) ly += 17;
          if (ly > priceY1 - 8) continue;
          usedY.push(ly);
          const labelW = ctx.measureText(label).width;
          ctx.globalAlpha = 0.85;
          ctx.fillStyle = backing;
          ctx.fillRect(plotX0 + 4, ly - 8, labelW + 8, 16);
          ctx.globalAlpha = 1;
          ctx.fillStyle = color;
          ctx.fillText(label, plotX0 + 8, ly);
        }
      }

      // ── Crosshair ──
      if (hoverX !== null) {
        vLine(hoverX, plotY0, plotY1, opt.crosshairColor, [4, 4]);
        if (hoverInPrice) hLine(hover.y, plotX0, plotX1, opt.crosshairColor, [4, 4]);
        if (hoverIdx >= 0) {
          const c = candles[hoverIdx];
          const tLabel = `${opt.tickLabel} ${c.t0}` + (c.t1 > c.t0 ? `–${c.t1}` : "");
          cssMode();
          ctx.font = opt.font;
          const tw = ctx.measureText(tLabel).width + 12;
          const tx = clamp(hoverX - tw / 2, plotX0, plotX1 - tw);
          ctx.fillStyle = opt.crosshairLabelBg;
          roundRectPath(ctx, tx, plotY1 + 2, tw, timeH - 4, 3);
          ctx.fill();
          text(tLabel, tx + tw / 2, plotY1 + timeH / 2 + 1, textOn(opt.crosshairLabelBg), opt.font, "center");
        }
      }

      // ── Pil sumbu kanan (prioritas terendah digambar lebih dulu) ──
      for (const p of pills.slice().sort((a, b) => a.prio - b.prio)) {
        cssMode();
        ctx.fillStyle = p.bg;
        roundRectPath(ctx, plotX1 + 2, p.drawY - pillH / 2, axisW - 4, pillH, 3);
        ctx.fill();
        text(p.text, plotX1 + 8, p.drawY + 0.5, textOn(p.bg), opt.font, "left");
      }

      // ── Legenda OHLC (kiri atas, paling atas) ──
      const legend = hoverIdx >= 0 ? layoutLegend(legendGroups(candles[hoverIdx], hoverIdx)) : legendLast;
      if (legend.items.length) {
        cssMode();
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        for (const it of legend.items) {
          ctx.font = it.font;
          ctx.fillStyle = it.color;
          ctx.fillText(it.t, it.x, LEG_Y0 + it.line * LEG_LH);
        }
      }
      ctx.font = opt.font;
      devMode();
    }

    // ── Interaksi: mouse hover, sentuh-dan-geser untuk layar sentuh ──
    function setHover(e) {
      const r = canvas.getBoundingClientRect();
      hover = { x: e.clientX - r.left - canvas.clientLeft, y: e.clientY - r.top - canvas.clientTop };
      schedule();
    }
    function onPointerMove(e) {
      if (e.pointerType === "touch" && !touchActive) return;
      setHover(e);
    }
    function onPointerDown(e) {
      if (e.pointerType !== "touch") return;
      touchActive = true;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* abaikan */ }
      setHover(e);
    }
    function onPointerEnd(e) {
      if (e.pointerType === "touch") {
        if (e.type === "pointerleave" && touchActive) return;   // capture aktif: tunggu pointerup/cancel
        touchActive = false;
      } else if (e.type !== "pointerleave") {
        return;                                                  // klik mouse tidak menyembunyikan crosshair
      }
      hover = null;
      schedule();
    }

    if (opt.crosshair) {
      canvas.addEventListener("pointermove", onPointerMove);
      canvas.addEventListener("pointerdown", onPointerDown);
      canvas.addEventListener("pointerup", onPointerEnd);
      canvas.addEventListener("pointercancel", onPointerEnd);
      canvas.addEventListener("pointerleave", onPointerEnd);
      canvas.style.cursor = "crosshair";
      canvas.style.touchAction = "pan-y";   // geser vertikal tetap scroll halaman, geser horizontal untuk inspeksi
    }

    let ro = null;
    if (global.ResizeObserver) {
      ro = new global.ResizeObserver((entries) => {
        const entry = entries[0];
        const box = entry && entry.devicePixelContentBoxSize && entry.devicePixelContentBoxSize[0];
        deviceBox = box ? { w: Math.round(box.inlineSize), h: Math.round(box.blockSize) } : null;
        schedule();
      });
      try { ro.observe(canvas, { box: "device-pixel-content-box" }); } catch (_) { ro.observe(canvas); }
    } else {
      global.addEventListener("resize", schedule);
    }

    function onDprChange() { watchDpr(); schedule(); }
    function watchDpr() {
      if (!global.matchMedia) return;
      if (dprQuery) {
        if (dprQuery.removeEventListener) dprQuery.removeEventListener("change", onDprChange);
        else if (dprQuery.removeListener) dprQuery.removeListener(onDprChange);
      }
      dprQuery = global.matchMedia(`(resolution: ${global.devicePixelRatio || 1}dppx)`);
      if (dprQuery.addEventListener) dprQuery.addEventListener("change", onDprChange);
      else if (dprQuery.addListener) dprQuery.addListener(onDprChange);
    }
    watchDpr();

    function destroy() {
      destroyed = true;
      if (raf) global.cancelAnimationFrame(raf);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointerup", onPointerEnd);
      canvas.removeEventListener("pointercancel", onPointerEnd);
      canvas.removeEventListener("pointerleave", onPointerEnd);
      if (ro) ro.disconnect(); else global.removeEventListener("resize", schedule);
      if (dprQuery) {
        if (dprQuery.removeEventListener) dprQuery.removeEventListener("change", onDprChange);
        else if (dprQuery.removeListener) dprQuery.removeListener(onDprChange);
      }
    }

    schedule();
    return {
      setData, setPeriod, setOptions, destroy,
      redraw: schedule,
      getPeriod: () => opt.period,
      getCandles: () => candles.slice(),
    };
  }

  global.CandleChart = {
    create,
    buildCandles,
    DEFAULTS,
    _internals: { niceStep, decimalsFor, textOn, computeSpacing },
  };
})(typeof window !== "undefined" ? window : globalThis);

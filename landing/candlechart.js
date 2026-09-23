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
 *   // dengan { navigation: true }: chart.zoomIn(); chart.zoomOut(); chart.panBy(bars); chart.panPrice(frac);
 *   //   chart.resetView(); chart.goToLatest(); chart.getView();  (+ opsi onViewChange)
 *
 *   CandleChart.buildCandles(prices, volumes, tick, period, opts?)   // opts = { minutesPerDay, sessionAligned }
 *
 * Opsi waktu simulasi (dipakai dashboard; landing memakai label tick default):
 *   formatTickLabel(t0, candle) → label sumbu-X ("09:15", "22 Sep", null = tidak digambar)
 *   formatTickRange(t0, t1, candle) → teks pil crosshair ("Sen 22 Sep · 09:15–09:29")
 *   labelEveryTicks → label pada t0 kelipatan nilai ini (dijarangkan otomatis ≥ 72px);
 *                     dengan sessionAligned, kelipatan dihitung dari awal hari bursa (09:00, 10:00, …)
 *   periodLabel → teks periode legenda ("15m"); sessionBreaks + minutesPerDay → garis awal hari bursa
 *   sessionAligned (default false) + minutesPerDay → candle tidak pernah melintasi batas hari bursa
 *                     (lihat "Batas candle" di bawah); landing tidak memakainya
 *   windowTicks → lebar jendela chart dalam tick: jarak candle dihitung dari kapasitas jendela
 *                     (ceil(windowTicks/period) slot), candle lama di luar jendela terpotong di kiri
 *   minStep (default 0) → langkah grid sumbu harga minimal (1 = tidak ada pecahan rupiah)
 *
 * Data: prices[i] = harga penutupan pada tick (tick - prices.length + 1 + i),
 * volumes[i] = total |order| agen pada tick tersebut (intensitas transaksi).
 * open = close tick sebelumnya, sehingga candle tidak bergeser saat data baru
 * masuk dan harga bergerak kontinu tanpa celah.
 *
 * Batas candle:
 *   - bawaan: batas tetap kelipatan `period` dari tick 0 (t0 = floor(t/period)*period);
 *   - sessionAligned: batas dihitung per hari bursa (minutesPerDay tick):
 *       day = floor(t/mpd), k = floor((t % mpd)/period), t0 = day*mpd + k*period,
 *       t1 = min(t0+period-1, day*mpd+mpd-1). Candle terakhir tiap hari boleh lebih pendek
 *       (1H: 09:00, 10:00, 11:00, 13:30, 14:30, 15:30–15:59), dan period ≥ mpd = 1 candle/hari.
 *     Bila period membagi habis mpd (1m/5m/15m/30m/1D) hasilnya sama persis dengan mode bawaan.
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
    // ── Opsi waktu simulasi (semua opsional; tanpa opsi ini label sumbu-X tetap nomor tick) ──
    formatTickLabel: null,          // (t0, candle) => string|null — label sumbu-X per candle (null = tidak digambar)
    formatTickRange: null,          // (t0, t1, candle) => string — teks pil crosshair di sumbu-X
    labelEveryTicks: null,          // label untuk candle dengan t0 % labelEveryTicks === 0 (dijarangkan otomatis ≥ 72px)
    periodLabel: null,              // teks periode di legenda (mis. "15m") menggantikan `${period}${unitLabel}`
    sessionBreaks: false,           // garis vertikal putus-putus di awal tiap hari bursa (t0 % minutesPerDay === 0)
    minutesPerDay: 330,             // panjang satu hari bursa dalam tick (Sesi 1 180 + Sesi 2 150 menit)
    sessionBreakColor: null,        // warna garis awal hari; null → axisLineColor (lebih terang dari gridColor)
    sessionAligned: false,          // true → candle dibentuk per hari bursa (tidak melintasi pergantian hari)
    windowTicks: 0,                 // > 0 → jarak candle dari kapasitas jendela ini (tick), bukan dari panjang histori
    minStep: 0,                     // langkah grid sumbu harga minimal (mis. 1 untuk saham: tanpa pecahan rupiah)
    // Garis harga tambahan (mis. batas ARA/ARB): [{ price, color, label, dash, alpha, fromTick, expand }].
    // fromTick → garis mulai dari candle pertama dengan t0 ≥ fromTick (mis. awal hari bursa);
    // expand → rentang sumbu ikut melebar bila garis sudah dekat; di luar rentang → tanda panah di tepi.
    priceLines: [],
    priceLineFont: null,            // null → font
    // Navigasi gaya TradingView (opsional; landing tidak memakainya): roda mouse = zoom di posisi kursor,
    // seret = geser ke segala arah (kiri/kanan = waktu, atas/bawah = harga), seret sumbu harga / roda di
    // sumbu = skala vertikal, cubit 2 jari = zoom, geser 2 jari = geser, klik ganda = reset.
    // Skala harga otomatis sampai pengguna menggeser/menskalakan harga (lalu manual sampai reset/Terbaru).
    navigation: false,
    onViewChange: null,             // (view) => void — { zoom, scroll, autoScale, atLatest, isDefault }
  };

  const NAV_MIN_SPACING = 2;        // px per candle saat zoom out maksimal
  const NAV_MAX_SPACING = 56;       // px per candle saat zoom in maksimal
  const NAV_DRAG_Y_THRESHOLD = 4;    // px vertikal sebelum seretan ikut menggeser harga (seret datar tetap datar)

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  // Apakah v kelipatan bulat dari m (toleran galat floating point)?
  function isMultipleOf(v, m) {
    const r = v / m;
    return Math.abs(Math.round(r) - r) <= 1e-9 * Math.max(1, Math.abs(r));
  }

  // Langkah grid "cantik" (1, 2, 2.5, 5 × 10^k) untuk sekitar targetCount garis.
  // minStep > 0 (opsional): langkah tidak pernah lebih kecil dari minStep dan selalu kelipatannya,
  // mis. minStep 1 untuk saham → 0,5 menjadi 1 dan 2,5 menjadi 2 (tidak ada label "3.502,5").
  function niceStep(range, targetCount, minStep) {
    const floorStep = minStep > 0 && Number.isFinite(minStep) ? minStep : 0;
    const rough = range / Math.max(1, targetCount);
    if (!(rough > 0) || !Number.isFinite(rough)) return floorStep || 1;
    const exp = Math.floor(Math.log10(rough));
    const mag = Math.pow(10, exp);
    const norm = rough / mag;
    const nice = norm < 1.5 ? 1 : norm < 2.25 ? 2 : norm < 3.75 ? 2.5 : norm < 7.5 ? 5 : 10;
    const step = nice * mag;
    if (!floorStep || (step >= floorStep && isMultipleOf(step, floorStep))) return step;
    if (rough <= floorStep) return floorStep;
    // Langkah cantik terdekat (skala log) yang ≥ minStep dan kelipatannya; seri → pilih yang lebih besar.
    let best = 0, bestScore = Infinity;
    for (let e = exp - 1; e <= exp + 2; e++) {
      for (const k of [1, 2, 2.5, 5]) {
        const c = k * Math.pow(10, e);
        if (c < floorStep || !isMultipleOf(c, floorStep)) continue;
        const score = Math.abs(Math.log(c / rough));
        if (score <= bestScore + 1e-12) { best = c; bestScore = score; }
      }
    }
    return best || Math.ceil(step / floorStep) * floorStep;
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
  // Dengan opt.windowTicks > 0 (dashboard) slot = kapasitas jendela saja: histori yang lebih panjang
  // dari jendela tidak membuat candle makin kurus, candle lama terpotong di kiri (startIdx).
  // Tanpa windowTicks (landing) perilaku lama: max(jumlah candle, kapasitas).
  function computeSpacing(plotW, nCandles, capacity, opt) {
    const base = opt.windowTicks > 0 ? capacity : Math.max(nCandles, capacity);
    const slots = Math.max(base + opt.rightOffsetBars, opt.minVisibleBars);
    return clamp(plotW / slots, opt.minBarSpacing, opt.maxBarSpacing);
  }

  // Panjang hari bursa dalam tick (bilangan bulat ≥ 1), atau 0 bila tidak valid.
  function dayLength(v) {
    const n = Math.trunc(Number(v));
    return Number.isFinite(n) && n >= 1 ? n : 0;
  }

  // Batas candle yang memuat tick t. Mode bawaan: kelipatan period dari tick 0.
  // Mode sadar hari (mpd > 0): kelipatan period dari awal hari bursa, dipotong di akhir hari.
  // id naik monoton dan unik per candle di kedua mode (sama persis bila period membagi habis mpd).
  function bucketOf(t, period, mpd) {
    if (!(mpd > 0)) {
      const id = Math.floor(t / period);
      return { id, t0: id * period, t1: id * period + period - 1 };
    }
    const day = Math.floor(t / mpd);
    const dayStart = day * mpd;
    const k = Math.floor((t - dayStart) / period);          // t - dayStart = t % mpd, aman untuk t negatif
    const t0 = dayStart + k * period;
    return { id: day * Math.ceil(mpd / period) + k, t0, t1: Math.min(t0 + period - 1, dayStart + mpd - 1) };
  }

  /**
   * Agregasi deret tick menjadi candle OHLCV.
   * opts (opsional) = { minutesPerDay, sessionAligned }: bila sessionAligned === true dan
   * minutesPerDay > 0, batas candle dihitung per hari bursa (lihat bucketOf); selain itu
   * batas tetap kelipatan period seperti versi lama (tanda tangan 4 argumen tetap berlaku).
   * Candle pertama dibuang bila jendela history terpotong (startTick > 0),
   * supaya semua candle yang tersisa punya open yang benar (close tick sebelumnya).
   */
  function buildCandles(prices, volumes, tick, period, opts) {
    const n = prices ? prices.length : 0;
    if (!n || period < 1) return [];
    const mpd = opts && opts.sessionAligned === true ? dayLength(opts.minutesPerDay) : 0;
    const startTick = tick - n + 1;
    const out = [];
    let cur = null;
    for (let i = 0; i < n; i++) {
      const p = Number(prices[i]);
      if (!Number.isFinite(p)) continue;
      const t = startTick + i;
      const b = bucketOf(t, period, mpd);
      const vRaw = volumes ? Number(volumes[i]) : 0;
      const v = Number.isFinite(vRaw) ? vRaw : 0;
      if (!cur || cur.id !== b.id) {
        const prev = i > 0 ? Number(prices[i - 1]) : NaN;
        const open = Number.isFinite(prev) ? prev : p;
        cur = {
          id: b.id,
          t0: b.t0,
          t1: b.t1,
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
    // Navigasi: zoom = pengali jarak candle, scroll = jumlah candle digeser ke kiri dari yang terbaru
    // (0 = mengikuti candle terbaru), manual = rentang harga { lo, hi } pilihan pengguna (null = skala otomatis).
    const view = { zoom: 1, scroll: 0, manual: null };
    let layout = null;         // geometri & rentang harga gambar terakhir (untuk event navigasi)
    let drag = null;           // { mode: "pan" | "yscale", x0, y0, scroll0, lo0, hi0, vMoved, id }
    const touches = new Map(); // pointerId → { x, y } (sentuhan aktif, untuk cubit 2 jari)
    let pinch = null;          // { dist0, zoom0, scroll0, mid0, midY0, s0, lo0, hi0, vMoved }

    function schedule() {
      if (!raf && !destroyed) raf = global.requestAnimationFrame(draw);
    }

    function viewInfo() {
      return {
        zoom: view.zoom, scroll: view.scroll, autoScale: !view.manual,
        atLatest: view.scroll <= 0.001,
        isDefault: Math.abs(view.zoom - 1) < 1e-6 && view.scroll <= 0.001 && !view.manual,
      };
    }
    let lastViewKey = "";
    function notifyView() {
      if (typeof opt.onViewChange !== "function") return;
      const v = viewInfo();
      const key = `${v.zoom.toFixed(3)}|${Math.round(v.scroll * 10)}|${v.autoScale}`;
      if (key === lastViewKey) return;
      lastViewKey = key;
      try { opt.onViewChange(v); } catch (_) { /* callback pemakai tidak boleh merusak chart */ }
    }
    function resetView() {
      view.zoom = 1; view.scroll = 0; view.manual = null;
      schedule(); notifyView();
    }

    // Candle sadar hari hanya bila diminta eksplisit (dashboard); landing tetap batas kelipatan period.
    const isAligned = () => opt.sessionAligned === true && dayLength(opt.minutesPerDay) > 0;

    function rebuild() {
      candles = isAligned()
        ? buildCandles(data.prices, data.volumes, data.tick, opt.period, { minutesPerDay: opt.minutesPerDay, sessionAligned: true })
        : buildCandles(data.prices, data.volumes, data.tick, opt.period);
    }

    function setData(d) {
      d = d || {};
      data = {
        tick: Number.isFinite(Number(d.tick)) ? Math.trunc(Number(d.tick)) : 0,
        prices: Array.isArray(d.prices) ? d.prices : [],
        volumes: Array.isArray(d.volumes) ? d.volumes : [],
        fundamental: Number(d.fundamental),
      };
      const prevLastId = candles.length ? candles[candles.length - 1].id : null;
      rebuild();
      if (opt.navigation && prevLastId !== null) {
        const newLastId = candles.length ? candles[candles.length - 1].id : null;
        if (newLastId === null || newLastId < prevLastId) {
          resetView();                                   // data baru (reset / ganti simbol): kembali ke bawaan
        } else if (view.scroll > 0 && newLastId > prevLastId) {
          // Sedang melihat histori: candle baru tidak boleh menggeser tampilan pengguna.
          let added = 0;
          for (let i = candles.length - 1; i >= 0 && candles[i].id > prevLastId; i--) added++;
          view.scroll += added;
        }
      }
      schedule();
    }

    function setPeriod(p) {
      p = Math.max(1, Math.trunc(Number(p) || 1));
      if (p === opt.period) return;
      opt.period = p;
      rebuild();
      view.zoom = 1; view.scroll = 0; view.manual = null;
      schedule(); notifyView();
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

      // Kapasitas jendela: opsi windowTicks (dashboard, ≈90 candle per timeframe) membuat spasi stabil
      // sejak koneksi pertama dan tetap berlaku setelah histori lebih panjang dari jendela (candle lama
      // terpotong di kiri); tanpa opsi itu, kapasitas mengikuti panjang data yang sudah ada (landing).
      const windowTicks = opt.windowTicks > 0 ? opt.windowTicks : (data.prices ? data.prices.length : 0);
      const capacity = Math.ceil(windowTicks / opt.period);
      const baseSpacing = computeSpacing(plotW, n, capacity, opt);
      const nav = opt.navigation === true;
      const spacing = nav ? clamp(baseSpacing * view.zoom, NAV_MIN_SPACING, NAV_MAX_SPACING) : baseSpacing;
      if (nav) view.scroll = clamp(view.scroll, 0, Math.max(0, n - 3));   // selalu ≥ 3 candle terlihat
      let bodyPx = Math.max(lw, Math.floor(spacing * dpr * 0.72));
      if ((bodyPx - lw) % 2 !== 0) bodyPx = Math.max(lw, bodyPx - 1);   // badan & sumbu wick satu paritas
      const xLast = plotX1 - opt.rightOffsetBars * spacing - spacing / 2 + (nav ? view.scroll * spacing : 0);
      const maxVisible = Math.max(1, Math.floor((xLast - plotX0 + spacing / 2) / spacing));
      const startIdx = Math.max(0, n - maxVisible);
      const xOf = (i) => xLast - (n - 1 - i) * spacing;
      // Candle terakhir yang terlihat (bila digeser ke histori, candle terbaru berada di kanan luar plot).
      let endIdx = n - 1;
      while (endIdx > startIdx && xOf(endIdx) > plotX1 - spacing * 0.5) endIdx--;
      layout = { plotX0, plotX1, plotY1, priceY0, priceY1, spacing, baseSpacing, n, W };

      // ── Legenda: grup label+nilai diukur utuh supaya tidak terpotong saat dibungkus ──
      const LEG_X0 = plotX0 + 8, LEG_Y0 = plotY0 + 12, LEG_LH = 17, LEG_MAXX = plotX1 - 8;
      function legendGroups(lc, idx) {
        const col = lc.close >= lc.open ? opt.upColor : opt.downColor;
        const prevClose = idx > 0 ? candles[idx - 1].close : lc.open;
        const chg = lc.close - prevClose;
        const pct = prevClose ? (chg / prevClose) * 100 : 0;
        const sign = chg >= 0 ? "+" : "";
        const K = opt.legendMutedColor, F = opt.legendFont;
        const periodText = (opt.periodLabel != null && opt.periodLabel !== "") ? String(opt.periodLabel) : `${opt.period}${opt.unitLabel}`;
        const groups = [
          { parts: [[opt.symbol, opt.legendTextColor, opt.legendFontBold], [` · ${periodText}`, K, F]], gap: 12 },
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
      for (let i = startIdx; i <= endIdx; i++) {
        const c = candles[i];
        if (c.low < lo) lo = c.low;
        if (c.high > hi) hi = c.high;
      }
      if (Number.isFinite(f)) { lo = Math.min(lo, f); hi = Math.max(hi, f); }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
      // Garis tambahan yang sudah dekat (≤ 35% rentang) ikut masuk rentang supaya tidak hanya jadi tanda tepi.
      const priceLines = Array.isArray(opt.priceLines) ? opt.priceLines.filter((pl) => pl && Number.isFinite(Number(pl.price))) : [];
      if (n && hi > lo) {
        const span = hi - lo;
        for (const pl of priceLines) {
          if (!pl.expand) continue;
          const v = Number(pl.price);
          if (v > hi && v - hi <= span * 0.35) hi = v;
          else if (v < lo && lo - v <= span * 0.35) lo = v;
        }
      }
      if (hi - lo < 1e-9) { const d = Math.abs(hi) * 0.01 || 1; lo -= d; hi += d; }
      // minStep (saham): rentang minimal 2 langkah supaya sumbu tetap punya ≥ 2 label bulat
      // walau harga emiten murah (mis. Rp50) hanya bergerak di bawah Rp1.
      const minStep = opt.minStep > 0 && Number.isFinite(opt.minStep) ? opt.minStep : 0;
      if (minStep && hi - lo < 2 * minStep) {
        const mid = (hi + lo) / 2;
        lo = lo >= 0 ? Math.max(0, mid - minStep) : mid - minStep;
        hi = lo + 2 * minStep;
      }
      const range = hi - lo;
      lo -= range * 0.08;
      hi += range * 0.04;
      const legendPx = legendLast.lines * LEG_LH + 10;
      const topFrac = Math.min(0.45, legendPx / Math.max(1, priceY1 - priceY0));
      hi = (hi - topFrac * lo) / (1 - topFrac);
      // Skala harga manual (pengguna menggeser atas/bawah atau menskalakan sumbu): pakai rentang pilihan pengguna.
      const m = nav ? view.manual : null;
      if (m && Number.isFinite(m.lo) && Number.isFinite(m.hi) && m.hi > m.lo) { lo = m.lo; hi = m.hi; }
      layout.lo = lo; layout.hi = hi;
      const yOf = (v) => priceY0 + (1 - (v - lo) / (hi - lo)) * (priceY1 - priceY0);
      const priceAt = (y) => lo + (1 - (y - priceY0) / (priceY1 - priceY0)) * (hi - lo);

      // ── Crosshair: menempel ke candle terdekat, atau bebas di area kosong ──
      let hoverIdx = -1, hoverX = null;
      const inPlot = !!(hover && opt.crosshair && hover.x >= plotX0 && hover.x <= plotX1 && hover.y >= plotY0 && hover.y <= plotY1);
      if (inPlot) {
        hoverX = hover.x;
        if (n) {
          const idx = clamp(Math.round(n - 1 - (xLast - hover.x) / spacing), startIdx, endIdx);
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
      for (const pl of priceLines) {
        const v = Number(pl.price);
        const atLast = last && Math.abs(v - last.close) < 1e-9;   // harga tepat di garis: pil harga terakhir sudah cukup
        if (v > lo && v < hi && !atLast) pills.push({ prio: 0, y: yOf(v), text: opt.formatPrice(v), bg: pl.color });
      }
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
      const step = niceStep(hi - lo, Math.max(3, Math.floor((priceY1 - priceY0) / 36)), minStep);
      const dec = decimalsFor(step);
      for (let k = Math.ceil(lo / step); k * step <= hi; k++) {
        const v = k * step;
        const y = yOf(v);
        if (y < priceY0 + 6 || y > priceY1 - 6) continue;
        hLine(y, plotX0, plotX1, opt.gridColor);
        if (!hitsPill(y)) text(opt.formatAxis(v, dec), plotX1 + 8, y, opt.textColor, opt.font, "left");
      }

      // ── Garis awal hari bursa (opsional): di bawah grid label supaya label hari tetap terbaca ──
      const minutesPerDay = opt.minutesPerDay > 0 ? Math.trunc(opt.minutesPerDay) : 330;
      // Mode sadar hari: setiap awal hari bursa pasti jatuh di t0 sebuah candle, jadi awal hari selalu
      // jadi jangkar label (garis putus-putus tetap hanya bila sessionBreaks).
      const aligned = isAligned();
      const markDays = opt.sessionBreaks || aligned;
      const dayStartX = [];
      if (markDays) {
        for (let i = startIdx; i <= endIdx; i++) {
          const c = candles[i];
          if (c.t0 % minutesPerDay !== 0) continue;
          const x = xOf(i);
          if (x < plotX0 + 2 || x > plotX1 - 2) continue;
          dayStartX.push(x);
          if (opt.sessionBreaks) vLine(x, plotY0, plotY1, opt.sessionBreakColor || opt.axisLineColor, [3, 3], 0.9);
        }
      }

      // ── Grid vertikal + label sumbu-X ──
      const LABEL_MIN_PX = 72;
      const labelY = plotY1 + timeH / 2 + 1;
      const drawTickLabel = (c, x) => {
        if (hoverIdx >= 0 && Math.abs(x - hoverX) < 52) return;      // beri tempat label crosshair
        const str = opt.formatTickLabel ? opt.formatTickLabel(c.t0, c) : String(c.t0);
        if (str == null || str === "") return;
        text(String(str), x, labelY, opt.textColor, opt.font, "center");
      };
      if (opt.labelEveryTicks > 0) {
        // Mode waktu simulasi: kandidat label = kelipatan labelEveryTicks (+ awal hari bursa bila sessionBreaks
        // atau sessionAligned). Penjarangan dilakukan di ruang tick (kelipatan `mult`) supaya label tidak
        // melompat saat data bergeser. Dengan sessionAligned kelipatan dihitung dari awal hari bursa
        // (t0 relatif hari), sehingga label selalu 09:00/10:00/… di setiap hari, bukan 09:30 di hari genap.
        const every = Math.max(1, Math.trunc(opt.labelEveryTicks));
        const pxPerLabel = (every / opt.period) * spacing;
        const mult = Math.max(1, Math.ceil(LABEL_MIN_PX / Math.max(1e-6, pxPerLabel)));
        // Label awal hari juga dijarangkan bila satu hari bursa lebih sempit dari LABEL_MIN_PX (mis. timeframe 1D).
        // Mode sadar hari: satu hari = ceil(mpd/period) candle (candle terakhir hari boleh lebih pendek).
        const candlesPerDay = aligned ? Math.ceil(minutesPerDay / opt.period) : minutesPerDay / opt.period;
        const pxPerDay = candlesPerDay * spacing;
        const dayMult = Math.max(1, Math.ceil(LABEL_MIN_PX / Math.max(1e-6, pxPerDay)));
        for (let i = startIdx; i <= endIdx; i++) {
          const c = candles[i];
          const rel = aligned ? c.t0 - Math.floor(c.t0 / minutesPerDay) * minutesPerDay : c.t0;
          const isDayStart = markDays && c.t0 % minutesPerDay === 0;
          const isRegular = rel % every === 0 && Math.round(rel / every) % mult === 0;
          if (!isDayStart && !isRegular) continue;
          const x = xOf(i);
          if (x < plotX0 + 2 || x > plotX1 - 2) continue;
          if (isDayStart) {
            if (!opt.sessionBreaks) vLine(x, plotY0, plotY1, opt.gridColor);   // tanpa garis putus-putus: grid biasa
            if (Math.round(c.t0 / minutesPerDay) % dayMult !== 0) continue;
          } else {
            vLine(x, plotY0, plotY1, opt.gridColor);
            // label reguler mengalah pada label awal hari yang terlalu dekat
            if (dayStartX.some((dx) => Math.abs(dx - x) < LABEL_MIN_PX * 0.8)) continue;
          }
          drawTickLabel(c, x);
        }
      } else {
        // Mode lama: label nomor tick dengan langkah kStep berdasarkan lebar candle.
        let kStep = 1;
        for (const k of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000]) { kStep = k; if (k * spacing >= LABEL_MIN_PX) break; }
        for (let i = startIdx; i <= endIdx; i++) {
          const c = candles[i];
          if (c.id % kStep !== 0) continue;
          const x = xOf(i);
          if (x < plotX0 + 2 || x > plotX1 - 2) continue;
          vLine(x, plotY0, plotY1, opt.gridColor);
          drawTickLabel(c, x);
        }
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
        if (f > lo && f < hi) hLine(yOf(f), plotX0, plotX1, opt.fundamentalColor, [5, 4], 0.9);
      }

      // ── Garis harga tambahan (ARA/ARB/acuan): dari awal hari (fromTick) sampai tepi kanan ──
      const lineLabels = [];
      for (const pl of priceLines) {
        const v = Number(pl.price);
        let x0 = plotX0;
        if (pl.fromTick != null && n) {
          let k = -1;
          for (let i = startIdx; i <= endIdx; i++) { if (candles[i].t1 >= pl.fromTick) { k = i; break; } }
          if (k < 0) continue;
          x0 = Math.max(plotX0, xOf(k) - spacing / 2);
        }
        if (v > lo && v < hi) {
          const y = yOf(v);
          hLine(y, x0, plotX1, pl.color, pl.dash || [6, 3], pl.alpha == null ? 0.9 : pl.alpha);
          if (pl.label) lineLabels.push({ text: String(pl.label), color: pl.color, y, x0, edge: 0 });
        } else if (pl.label && n) {
          lineLabels.push({ text: `${pl.label} ${v >= hi ? "↑" : "↓"}`, color: pl.color, y: v >= hi ? priceY0 + 10 : priceY1 - 10, edge: v >= hi ? 1 : -1 });
        }
      }

      // ── Candle (device px: wick & badan tajam) ──
      devMode();
      ctx.save();
      ctx.beginPath();
      ctx.rect(P(plotX0), P(priceY0), P(plotX1) - P(plotX0), P(priceY1) - P(priceY0));
      ctx.clip();
      for (let i = startIdx; i <= endIdx; i++) {
        const c = candles[i];
        ctx.fillStyle = c.close >= c.open ? opt.upColor : opt.downColor;
        const xc = P(xOf(i));
        const yH = P(yOf(c.high)), yL = P(yOf(c.low));
        const yO = P(yOf(c.open)), yC = P(yOf(c.close));
        ctx.fillRect(xc - Math.floor(lw / 2), yH, lw, Math.max(lw, yL - yH));
        ctx.fillRect(xc - Math.floor(bodyPx / 2), Math.min(yO, yC), bodyPx, Math.max(lw, Math.abs(yC - yO)));
      }
      ctx.restore();

      // ── Volume ──
      if (opt.showVolume && n) {
        hLine(volY0, plotX0, plotX1, opt.gridColor);
        let maxV = 0;
        for (let i = startIdx; i <= endIdx; i++) if (candles[i].volume > maxV) maxV = candles[i].volume;
        if (maxV > 0) {
          devMode();
          ctx.save();
          ctx.beginPath();
          ctx.rect(P(plotX0), P(volY0), P(plotX1) - P(plotX0), P(volY1) - P(volY0));
          ctx.clip();
          ctx.globalAlpha = opt.volumeAlpha;
          const base = P(volY1);
          const usable = Math.max(1, P(volY1) - P(volY0) - Math.round(4 * dpr));
          for (let i = startIdx; i <= endIdx; i++) {
            const c = candles[i];
            const hgt = Math.max(lw, Math.round((c.volume / maxV) * usable));
            ctx.fillStyle = c.close >= c.open ? opt.upColor : opt.downColor;
            ctx.fillRect(P(xOf(i)) - Math.floor(bodyPx / 2), base - hgt, bodyPx, hgt);
          }
          ctx.restore();
          ctx.globalAlpha = 1;
        }
      }

      // ── Garis pemisah sumbu ──
      hLine(plotY1, plotX0, W, opt.axisLineColor);
      vLine(plotX1, plotY0, plotY1, opt.axisLineColor);

      // ── Garis harga terakhir ──
      if (last && last.close > lo && last.close < hi) hLine(yOf(last.close), plotX0, plotX1, lastColor, [2, 2], 0.85);

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

      // ── Label garis tambahan: di dalam rentang → di awal garis (kiri), di luar → tanda di tepi kanan atas/bawah ──
      if (lineLabels.length) {
        cssMode();
        const lf = opt.priceLineFont || opt.font;
        ctx.font = lf;
        ctx.textBaseline = "middle";
        const backing = (opt.background && opt.background !== "transparent") ? opt.background : "#ffffff";
        const edgeUsed = { 1: 0, "-1": 0 };
        // Legenda OHLC (kiri atas) di layar sempit bisa selebar plot: tanda tepi atas turun ke bawahnya.
        const legNow = hoverIdx >= 0 ? layoutLegend(legendGroups(candles[hoverIdx], hoverIdx)) : legendLast;
        let legRight = plotX0;
        for (const it of legNow.items) { ctx.font = it.font; legRight = Math.max(legRight, it.x + ctx.measureText(it.t).width); }
        const legBottom = legNow.items.length ? LEG_Y0 + (legNow.lines - 1) * LEG_LH + 8 : plotY0;
        ctx.font = lf;
        for (const lb of lineLabels) {
          const w = ctx.measureText(lb.text).width;
          let x, y = lb.y;
          if (lb.edge) {
            x = plotX1 - 8 - w;
            if (lb.edge === 1 && x - 4 < legRight + 8) y = Math.max(y, legBottom + 10);
            y += lb.edge * -1 * edgeUsed[lb.edge] * 17;     // beberapa tanda di tepi yang sama → ditumpuk
            edgeUsed[lb.edge] += 1;
          } else {
            x = clamp(lb.x0 + 6, plotX0 + 6, plotX1 - w - 14);
            y = lb.y - 9 < priceY0 + 6 ? lb.y + 9 : lb.y - 9;   // di atas garis; di tepi atas → di bawahnya
            if (y - 8 < legBottom && x < legRight + 8) {          // jangan menimpa legenda OHLC
              x = Math.min(legRight + 12, plotX1 - w - 14);
              if (x < legRight + 8) y = lb.y + 9 > legBottom + 8 ? lb.y + 9 : legBottom + 10;
            }
          }
          ctx.globalAlpha = 0.92;
          ctx.fillStyle = backing;
          ctx.fillRect(x - 4, y - 8, w + 8, 16);
          ctx.globalAlpha = 1;
          ctx.fillStyle = lb.color;
          ctx.textAlign = "left";
          ctx.fillText(lb.text, x, y + 0.5);
        }
      }

      // ── Crosshair ──
      if (hoverX !== null) {
        vLine(hoverX, plotY0, plotY1, opt.crosshairColor, [4, 4]);
        if (hoverInPrice) hLine(hover.y, plotX0, plotX1, opt.crosshairColor, [4, 4]);
        if (hoverIdx >= 0) {
          const c = candles[hoverIdx];
          const tLabel = opt.formatTickRange
            ? String(opt.formatTickRange(c.t0, c.t1, c))
            : `${opt.tickLabel} ${c.t0}` + (c.t1 > c.t0 ? `–${c.t1}` : "");
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
      if (nav) notifyView();                            // scroll bisa ter-clamp saat histori berubah
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

    // ── Navigasi: zoom, geser, skala vertikal (hanya bila opt.navigation) ──
    function localXY(e) {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left - canvas.clientLeft, y: e.clientY - r.top - canvas.clientTop };
    }
    const inAxis = (p) => !!layout && p.x > layout.plotX1 && p.y < layout.plotY1;
    const inPlot = (p) => !!layout && p.x >= layout.plotX0 && p.x <= layout.plotX1 && p.y <= layout.plotY1;
    function zoomLimits() {
      // Zoom out paling jauh = seluruh histori muat di plot (tidak lebih kecil dari NAV_MIN_SPACING).
      const plotW = layout.plotX1 - layout.plotX0;
      const fitAll = plotW / Math.max(1, layout.n + opt.rightOffsetBars + 2);
      const zMin = Math.min(1, Math.max(NAV_MIN_SPACING, fitAll) / layout.baseSpacing);
      return { zMin, zMax: Math.max(1, NAV_MAX_SPACING / layout.baseSpacing) };
    }
    const spacingFor = (z) => clamp(layout.baseSpacing * z, NAV_MIN_SPACING, NAV_MAX_SPACING);
    function zoomAt(factor, x) {
      if (!layout || !(factor > 0)) return;
      const { zMin, zMax } = zoomLimits();
      const z1 = clamp(view.zoom * factor, zMin, zMax);
      const s0 = spacingFor(view.zoom), s1 = spacingFor(z1);
      if (Math.abs(z1 - view.zoom) < 1e-9) return;
      if (view.scroll > 0.001) {
        // Candle di bawah kursor (atau tengah plot) tetap di tempatnya: scroll' = scroll + (plotX1 − x)(1/s0 − 1/s1)
        const ax = clamp(x == null ? (layout.plotX0 + layout.plotX1) / 2 : x, layout.plotX0, layout.plotX1);
        view.scroll = Math.max(0, view.scroll + (layout.plotX1 - ax) * (1 / s0 - 1 / s1));
      }                                                  // mengikuti candle terbaru: jangkar tetap di kanan
      view.zoom = z1;
      schedule(); notifyView();
    }
    function panBy(bars) {
      if (!Number.isFinite(bars)) return;
      view.scroll = Math.max(0, view.scroll + bars);
      schedule(); notifyView();
    }
    // Rentang harga saat ini (manual, atau otomatis dari gambar terakhir).
    function curRange() {
      if (view.manual) return { lo: view.manual.lo, hi: view.manual.hi };
      return layout && Number.isFinite(layout.lo) && layout.hi > layout.lo ? { lo: layout.lo, hi: layout.hi } : null;
    }
    function setManual(lo, hi) {
      if (!Number.isFinite(lo) || !Number.isFinite(hi) || !(hi > lo)) return;
      view.manual = { lo, hi };
      schedule(); notifyView();
    }
    // Skala vertikal di sekitar tengah rentang: factor > 1 = rentang melebar (zoom out harga).
    function scalePrice(factor, base) {
      const r = base || curRange();
      if (!r || !(factor > 0)) return;
      const mid = (r.lo + r.hi) / 2;
      const half = Math.max(((r.hi - r.lo) / 2) * factor, Math.abs(mid) * 1e-6 + 1e-9);
      setManual(mid - half, mid + half);
    }
    // Geser harga: frac > 0 = tampilan naik (harga lebih tinggi terlihat), dalam pecahan tinggi rentang.
    function panPrice(frac) {
      const r = curRange();
      if (!r || !Number.isFinite(frac)) return;
      const d = (r.hi - r.lo) * frac;
      setManual(r.lo + d, r.hi + d);
    }
    const pricePerPx = (r) => (r.hi - r.lo) / Math.max(1, layout.priceY1 - layout.priceY0);
    function onWheel(e) {
      if (!layout) return;
      e.preventDefault();                               // roda di atas chart = navigasi chart, bukan scroll halaman
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
      const dx = e.deltaX * unit, dy = e.deltaY * unit;
      const p = localXY(e);
      if (inAxis(p)) { scalePrice(Math.exp(dy * 0.0015)); return; }
      if (e.shiftKey || Math.abs(dx) > Math.abs(dy)) {
        const d = Math.abs(dx) > Math.abs(dy) ? dx : dy;
        panBy(-d / layout.spacing);                      // geser ke kanan → candle lebih baru
        return;
      }
      zoomAt(Math.exp(-dy * 0.0015), p.x);               // roda ke atas = zoom in (juga cubit touchpad: ctrl+roda)
    }
    function touchPair() {
      const pts = Array.from(touches.values());
      return pts.length >= 2 ? [pts[0], pts[1]] : null;
    }
    function onNavDown(e) {
      if (!layout) return;
      if (e.pointerType === "touch") {
        touches.set(e.pointerId, localXY(e));
        const pair = touchPair();
        if (pair && !pinch) {
          const [a, b] = pair;
          const r0 = curRange();
          pinch = {
            dist0: Math.max(10, Math.hypot(a.x - b.x, a.y - b.y)), zoom0: view.zoom, scroll0: view.scroll,
            mid0: (a.x + b.x) / 2, midY0: (a.y + b.y) / 2, s0: layout.spacing,
            lo0: r0 ? r0.lo : NaN, hi0: r0 ? r0.hi : NaN, vMoved: false,
          };
          touchActive = false; hover = null;             // dua jari: sembunyikan crosshair
          try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* abaikan */ }
          schedule();
        }
        return;
      }
      if (e.button !== 0) return;
      const p = localXY(e);
      const r0 = curRange();
      if (!r0) return;
      if (inAxis(p)) drag = { mode: "yscale", y0: p.y, lo0: r0.lo, hi0: r0.hi, id: e.pointerId };
      else if (inPlot(p)) drag = { mode: "pan", x0: p.x, y0: p.y, scroll0: view.scroll, lo0: r0.lo, hi0: r0.hi, vMoved: false, id: e.pointerId };
      else return;
      try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* abaikan */ }
      canvas.style.cursor = drag.mode === "pan" ? "grabbing" : "ns-resize";
    }
    function onNavMove(e) {
      if (!layout) return;
      if (e.pointerType === "touch") {
        if (!touches.has(e.pointerId)) return;
        touches.set(e.pointerId, localXY(e));
        const pair = touchPair();
        if (pinch && pair) {
          const [a, b] = pair;
          const { zMin, zMax } = zoomLimits();
          const z1 = clamp(pinch.zoom0 * Math.hypot(a.x - b.x, a.y - b.y) / pinch.dist0, zMin, zMax);
          const s1 = spacingFor(z1), mid = (a.x + b.x) / 2;
          view.zoom = z1;
          view.scroll = Math.max(0, pinch.scroll0 + (layout.plotX1 - pinch.mid0) / pinch.s0 - (layout.plotX1 - mid) / s1);
          // Dua jari digeser naik/turun → harga ikut bergeser (geser ke segala arah).
          const dyMid = (a.y + b.y) / 2 - pinch.midY0;
          if (!pinch.vMoved && Math.abs(dyMid) >= NAV_DRAG_Y_THRESHOLD * 2) pinch.vMoved = true;
          if (pinch.vMoved && Number.isFinite(pinch.lo0)) {
            const shift = dyMid * pricePerPx({ lo: pinch.lo0, hi: pinch.hi0 });
            view.manual = { lo: pinch.lo0 + shift, hi: pinch.hi0 + shift };
          }
          touchActive = false; hover = null;
          schedule(); notifyView();
        }
        return;
      }
      const p = localXY(e);
      if (drag && drag.id === e.pointerId) {
        if (drag.mode === "pan") {
          // Kiri/kanan = waktu, atas/bawah = harga → seretan diagonal menggeser keduanya sekaligus.
          view.scroll = Math.max(0, drag.scroll0 + (p.x - drag.x0) / layout.spacing);
          const dy = p.y - drag.y0;
          if (!drag.vMoved && Math.abs(dy) >= NAV_DRAG_Y_THRESHOLD) drag.vMoved = true;
          if (drag.vMoved) {
            const shift = dy * pricePerPx({ lo: drag.lo0, hi: drag.hi0 });   // seret ke bawah → harga lebih tinggi terlihat
            view.manual = { lo: drag.lo0 + shift, hi: drag.hi0 + shift };
          }
        } else {
          scalePrice(Math.exp((p.y - drag.y0) * 0.006), { lo: drag.lo0, hi: drag.hi0 });   // seret sumbu ke bawah = rentang melebar
        }
        schedule(); notifyView();
        return;
      }
      canvas.style.cursor = inAxis(p) ? "ns-resize" : "crosshair";
    }
    function onNavUp(e) {
      if (e.pointerType === "touch") {
        touches.delete(e.pointerId);
        if (touches.size < 2) pinch = null;
        return;
      }
      if (drag && drag.id === e.pointerId) {
        drag = null;
        canvas.style.cursor = "crosshair";
      }
    }
    if (opt.navigation) {
      canvas.addEventListener("wheel", onWheel, { passive: false });
      canvas.addEventListener("pointerdown", onNavDown);
      canvas.addEventListener("pointermove", onNavMove);
      canvas.addEventListener("pointerup", onNavUp);
      canvas.addEventListener("pointercancel", onNavUp);
      canvas.addEventListener("dblclick", resetView);
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
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onNavDown);
      canvas.removeEventListener("pointermove", onNavMove);
      canvas.removeEventListener("pointerup", onNavUp);
      canvas.removeEventListener("pointercancel", onNavUp);
      canvas.removeEventListener("dblclick", resetView);
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
      // Navigasi (opt.navigation): tombol zoom/geser di luar canvas
      zoomIn: () => zoomAt(1.3, null),
      zoomOut: () => zoomAt(1 / 1.3, null),
      panBy,
      panPrice,
      resetView,
      // Kembali ke candle & harga terbaru: geser waktu ke kanan dan skala harga otomatis lagi (zoom dipertahankan).
      goToLatest: () => { view.scroll = 0; view.manual = null; schedule(); notifyView(); },
      getView: viewInfo,
    };
  }

  global.CandleChart = {
    create,
    buildCandles,
    DEFAULTS,
    _internals: { niceStep, decimalsFor, textOn, computeSpacing, bucketOf },
  };
})(typeof window !== "undefined" ? window : globalThis);

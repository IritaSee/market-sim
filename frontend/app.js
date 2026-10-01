/**
 * app.js — logika dashboard simulator SimPasar.
 *
 * Struktur halaman ada di index.html, gaya di styles.css. File ini:
 *   1. util & formatter angka id-ID (indeks 2 desimal, saham 0 desimal + "Rp" di kartu)
 *   2. waktu simulasi: tickToClock() identik dengan sim/simtime.py (1 tick = 1 menit bursa,
 *      Sesi 1 09:00–12:00, Sesi 2 13:30–16:00, 330 menit/hari, lompat Sabtu–Minggu)
 *   3. protokol WebSocket snapshot/delta (§3 kontrak) + resinkron via get_state saat tick loncat
 *      (batas waktu 1,5 s, maks 3× kirim ulang, lalu sambung ulang); kecepatan dari state.tick_interval /
 *      event speed_changed; slider dari params tiap state; asal harga dari symbol.source_kind
 *      (server lama tanpa field ini tetap didukung: kecepatan lokal & sumber ditebak dari teks)
 *   4. chart candlestick (landing/candlechart.js) dengan timeframe 1m…1D, label jam simulasi
 *   5. panel kiri (kartu emiten, grid 100 agen: Aksi / Tipe orang / Sifat orang + tooltip alasan Gemini, komposisi, P&L),
 *      statistik (harga vs acuan, batas harian ARA/ARB dari state.limits, suasana pasar)
 *   6. panel kanan (stream ritel fiktif, berita Sectors, Input berita), dock (aksi + pintasan R/K/Spasi, kecepatan, slider),
 *      panduan langkah demi langkah (tombol Panduan / ?)
 *   7. modal pencarian simbol → kartu konfirmasi harga Sectors → cmd set_symbol
 *
 * Semua teks yang datang dari server dimasukkan lewat textContent (tidak pernah innerHTML mentah).
 */
(function () {
  'use strict';

  // ═══════════════════════════ 1. Util & formatter ═══════════════════════════
  const $ = (id) => document.getElementById(id);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const pad2 = (n) => String(n).padStart(2, '0');
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const num = (v, dflt) => { const n = Number(v); return Number.isFinite(n) ? n : dflt; };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function setText(id, text) { const e = $(id); if (e) e.textContent = text; }
  function setTone(node, v) {
    if (!node) return;
    node.classList.remove('is-up', 'is-down', 'is-flat');
    node.classList.add(v > 0 ? 'is-up' : v < 0 ? 'is-down' : 'is-flat');
  }
  function storageGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  function storageSet(key, val) { try { localStorage.setItem(key, val); } catch (_) { /* privat / penuh: abaikan */ } }

  // Formatter dibuat sekali (Intl mahal bila dibuat per frame).
  const NF0 = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 0 });
  const NF1 = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 1 });
  const NF2 = new Intl.NumberFormat('id-ID', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const NF_UP2 = new Intl.NumberFormat('id-ID', { maximumFractionDigits: 2 });   // pecahan kecil saham: "0,35"
  const NF_AXIS = [0, 1, 2, 3].map((d) => new Intl.NumberFormat('id-ID', { minimumFractionDigits: d, maximumFractionDigits: d }));

  // Indeks → 2 desimal; saham → 0 desimal, awalan "Rp" hanya di kartu (bukan sumbu chart).
  // Nilai saham di bawah 1 (mis. perubahan per candle emiten puluhan rupiah) memakai ≤ 2 desimal
  // supaya tidak tampil "+0" berdampingan dengan persentase bukan nol.
  function fmtStockNum(v) { const a = Math.abs(v); return a > 0 && a < 1 ? NF_UP2.format(v) : NF0.format(v); }
  function fmtByKind(kind, v, withRp) {
    if (!isNum(v)) return '—';
    if (kind === 'index') return NF2.format(v);
    return (withRp ? 'Rp ' : '') + fmtStockNum(v);
  }
  const fmtPrice = (v) => fmtByKind(S.symbol.kind, v, false);
  function fmtSignedByKind(kind, v) {
    if (!isNum(v)) return '—';
    const sign = v > 0 ? '+' : v < 0 ? '−' : '';
    return sign + (kind === 'index' ? NF2.format(Math.abs(v)) : fmtStockNum(Math.abs(v)));
  }
  const fmtSigned1 = (v) => (v > 0 ? '+' : v < 0 ? '−' : '') + NF1.format(Math.abs(v));
  // Umur data dari epoch detik (fetched_at Sectors) → "12 menit", "3 jam", "2 hari".
  function fmtAge(epochSec) {
    const s = Date.now() / 1000 - Number(epochSec);
    if (!Number.isFinite(s) || s < 0) return '';
    if (s < 60) return 'kurang dari 1 menit';
    if (s < 3600) return `${Math.floor(s / 60)} menit`;
    if (s < 86400) return `${Math.floor(s / 3600)} jam`;
    return `${Math.floor(s / 86400)} hari`;
  }
  function fmtPct(v, digits) {
    if (!isNum(v)) return '—';
    const nf = digits === 1 ? NF1 : NF2;
    return (v > 0 ? '+' : v < 0 ? '−' : '') + nf.format(Math.abs(v)) + '%';
  }
  function fmtVol(v) {
    if (!isNum(v)) return '—';
    if (v >= 1e6) return NF1.format(v / 1e6) + ' jt';
    if (v >= 1000) return NF1.format(v / 1000) + ' rb';
    return NF1.format(v);
  }
  function fmtMcap(v) {
    if (!isNum(v) || v <= 0) return '—';
    if (v >= 1e12) return 'Rp ' + NF1.format(v / 1e12) + ' T';
    if (v >= 1e9) return 'Rp ' + NF1.format(v / 1e9) + ' M';
    if (v >= 1e6) return 'Rp ' + NF1.format(v / 1e6) + ' jt';
    return 'Rp ' + NF0.format(v);
  }

  const WEEKDAYS_SHORT = ['Min', 'Sen', 'Sel', 'Rab', 'Kam', 'Jum', 'Sab'];
  const WEEKDAYS_LONG = ['Minggu', 'Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu'];
  const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];

  function parseISODate(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(s || ''));
    return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
  }
  function fmtDateID(iso) {
    const d = parseISODate(iso);
    return d ? `${pad2(d.getDate())} ${MONTHS_SHORT[d.getMonth()]} ${d.getFullYear()}` : (iso ? String(iso) : '—');
  }

  // Sektor BEI → nama Indonesia (fallback bila server tidak mengirim sector_name).
  const SECTOR_NAMES = {
    index: 'Indeks', energy: 'Energi', 'basic-materials': 'Bahan Baku', industrials: 'Perindustrian',
    'consumer-non-cyclicals': 'Konsumen Primer', 'consumer-cyclicals': 'Konsumen Non-Primer', healthcare: 'Kesehatan',
    financials: 'Keuangan', 'properties-real-estate': 'Properti & Real Estat', technology: 'Teknologi',
    infrastructures: 'Infrastruktur', 'transportation-logistic': 'Transportasi & Logistik',
  };
  function setChip(node, sector, name) {
    if (!node) return;
    const slug = String(sector || 'other');
    node.dataset.sector = slug;
    node.textContent = name || SECTOR_NAMES[slug] || slug;
  }

  // ═══════════════════════════ 2. Waktu simulasi (identik sim/simtime.py) ═══════════════════════════
  const MINUTES_PER_DAY = 330;          // Sesi 1: 180 menit, Sesi 2: 150 menit
  const SESSION1_MINUTES = 180;

  function isWeekend(d) { const w = d.getDay(); return w === 0 || w === 6; }
  function nextWeekday(d) { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); while (isWeekend(x)) x.setDate(x.getDate() + 1); return x; }
  function addBusinessDays(d, n) {
    const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const step = n >= 0 ? 1 : -1;
    let left = Math.abs(n);
    while (left > 0) { x.setDate(x.getDate() + step); if (!isWeekend(x)) left--; }
    return x;
  }

  // Tanggal hari bursa ke-0. Default: hari ini (digeser ke hari kerja); disinkronkan dari
  // state.sim_time server ({date, day}) supaya label tanggal klien = label server.
  let simStartDate = nextWeekday(new Date());
  let simStartKey = '';
  const dayDateCache = new Map();
  function syncSimStart(simTime) {
    if (!simTime || !simTime.date) return;
    const key = `${simTime.date}|${simTime.day}`;
    if (key === simStartKey) return;
    const d = parseISODate(simTime.date);
    if (!d) return;
    simStartKey = key;
    simStartDate = addBusinessDays(d, -Math.max(0, (Number(simTime.day) || 1) - 1));
    dayDateCache.clear();
  }
  function dateForDay(dayIndex) {
    let d = dayDateCache.get(dayIndex);
    if (!d) { d = addBusinessDays(simStartDate, dayIndex); dayDateCache.set(dayIndex, d); }
    return d;
  }

  /** tick → jam bursa simulasi. Rumus sama persis dengan sim/simtime.py (§4 kontrak). */
  function tickToClock(tick) {
    tick = Math.max(0, Math.trunc(Number(tick) || 0));
    const dayIndex = Math.floor(tick / MINUTES_PER_DAY);
    const m = tick % MINUTES_PER_DAY;
    let hh, mm, session;
    if (m < SESSION1_MINUTES) { hh = 9 + Math.floor(m / 60); mm = m % 60; session = 1; }
    else { const m2 = m - SESSION1_MINUTES; hh = 13 + Math.floor((30 + m2) / 60); mm = (30 + m2) % 60; session = 2; }
    const date = dateForDay(dayIndex);
    return {
      tick, day: dayIndex + 1, dayIndex, minute: m, session,
      time: `${pad2(hh)}:${pad2(mm)}`,
      date, weekday: WEEKDAYS_LONG[date.getDay()], weekdayShort: WEEKDAYS_SHORT[date.getDay()],
      dateLabel: `${date.getDate()} ${MONTHS_SHORT[date.getMonth()]}`,
    };
  }
  // Label sumbu-X: "09:15"; pada candle pertama tiap hari bursa → "22 Sep".
  function formatTickLabel(t0) { const c = tickToClock(t0); return c.minute === 0 ? c.dateLabel : c.time; }
  // Pil crosshair: "Sen 22 Sep · 09:15–09:29" (bila candle melintasi hari: dua tanggal).
  function formatTickRange(t0, t1) {
    const a = tickToClock(t0), b = tickToClock(Math.max(t0, t1));
    if (a.dayIndex === b.dayIndex) return `${a.weekdayShort} ${a.dateLabel} · ${a.time}${b.tick > a.tick ? '–' + b.time : ''}`;
    return `${a.weekdayShort} ${a.dateLabel} ${a.time} – ${b.weekdayShort} ${b.dateLabel} ${b.time}`;
  }

  // ═══════════════════════════ 3. State klien ═══════════════════════════
  const MAX_HISTORY = 2000;             // = sim/market.py MAX_HISTORY
  // Tampilan awal = BBCA (sama dengan simbol awal server); snapshot pertama langsung menimpanya.
  const DEFAULT_SYMBOL = {
    symbol: 'BBCA', name: 'PT Bank Central Asia Tbk.', sector: 'financials', sector_name: 'Keuangan', kind: 'stock',
    source_price: null, source_date: null, source: null, source_kind: null,
  };
  const IHSG_ITEM = { symbol: 'IHSG', name: 'Indeks Harga Saham Gabungan', sector: 'index', sector_name: 'Indeks', kind: 'index' };
  const S = {
    symbol: Object.assign({}, DEFAULT_SYMBOL),
    prices: [], volumes: [],
    tick: -1, price: NaN, prevPrice: NaN, fundamental: NaN, status: 'Normal',
    ref: NaN, limits: null,             // harga acuan & batas ARA/ARB hari ini (state.limits dari server)
    sesHigh: null, sesLow: null,
    paused: false, connected: false, wasConnected: false,
    tf: '15m',
    awaitingSync: false, lastSyncReq: 0, syncAttempts: 0,
    lastAgents: [], llmReasons: new Map(), lastTickSeen: -1,
    priceInfo: {},                      // detail Sectors per simbol (change, market_cap…) dari modal, disimpan lokal
    paramsKey: '',                      // params populasi/psikologi terakhir dari server (deteksi perubahan)
  };
  try {
    const saved = JSON.parse(storageGet('simpasar:priceInfo') || '{}') || {};
    for (const k of Object.keys(saved)) if (saved[k] && Number(saved[k].price) > 0) S.priceInfo[k] = saved[k];   // buang entri tak valid
  } catch (_) { S.priceInfo = {}; }

  // ═══════════════════════════ Tema gelap / terang ═══════════════════════════
  // Canvas (chart, grid agen) tidak bisa membaca variabel CSS → palet per tema di sini, selaras dengan
  // :root / :root[data-theme="light"] di styles.css. Tema awal dipasang skrip kecil di <head> (tanpa kedip).
  const THEMES = {
    dark: {
      chart: {
        background: '#0a0d14', upColor: '#16c784', downColor: '#ea3943', fundamentalColor: '#3b82f6',
        bubbleColor: '#f59e0b', crashColor: '#ea3943', gridColor: 'rgba(28, 35, 51, 0.75)',
        axisLineColor: '#263042', sessionBreakColor: '#33405a', crosshairColor: '#8590a3',
        crosshairLabelBg: '#263042', textColor: '#8590a3',   // = --text-3 (≥ 5:1 di latar chart)
        legendTextColor: '#e6e9f0', legendMutedColor: '#9aa3b5',
      },
      up: '#16c784', down: '#ea3943',                        // beli/jual, garis ARA/ARB, sparkline
      holdFill: '#141a26', holdEdge: '#263042',              // kotak agen "diam"
      profitFill: '#0f6a45', lossFill: '#7a1f27',            // hijau tua sedang untung / merah tua sedang rugi (= landing page)
      halo: '#0a0d14', glyph: '#0a0d14',                     // halo cincin rugi & tanda ▲▼ di kotak berwarna
      pain: '#f87171', avg: '#f59e0b', muted: '#8590a3', text: '#e6e9f0',
      type: { fundamentalist: '#38bdf8', chartist: '#f472b6', noise: '#facc15' },     // = --type-*
      psych: { disciplined: '#3b82f6', bagholder: '#a78bfa', averager: '#f59e0b' },
      moodUp: '#f59e0b', moodDown: '#ea3943',
      metaColor: '#10151f',
    },
    light: {
      chart: {
        background: '#ffffff', upColor: '#087f50', downColor: '#dc2626', fundamentalColor: '#2962ff',
        bubbleColor: '#b45309', crashColor: '#c42b36', gridColor: 'rgba(15, 23, 42, 0.07)',
        axisLineColor: '#cfd6e2', sessionBreakColor: '#b6c0cf', crosshairColor: '#64748b',
        crosshairLabelBg: '#334155', textColor: '#5b6678',   // 5,8:1 di latar putih
        legendTextColor: '#0f172a', legendMutedColor: '#5b6678',
      },
      up: '#087f50', down: '#dc2626',                        // 5,05:1 / 4,83:1 di putih (label ARA/ARB)
      holdFill: '#edf1f6', holdEdge: '#cfd6e2',
      profitFill: '#14532d', lossFill: '#7f1d1d',
      halo: '#ffffff', glyph: '#ffffff',
      pain: '#dc2626', avg: '#d97706', muted: '#5b6678', text: '#0f172a',
      type: { fundamentalist: '#0369a1', chartist: '#be185d', noise: '#a16207' },
      psych: { disciplined: '#2563eb', bagholder: '#7c3aed', averager: '#b45309' },
      moodUp: '#d97706', moodDown: '#dc2626',
      metaColor: '#ffffff',
    },
  };
  let themeName = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  let T = THEMES[themeName];

  // ═══════════════════════════ 4. Chart ═══════════════════════════
  const TIMEFRAMES = {
    '1m': { period: 1, every: 15 }, '5m': { period: 5, every: 30 }, '15m': { period: 15, every: 60 },
    '30m': { period: 30, every: 60 }, '1H': { period: 60, every: 180 }, '1D': { period: 330, every: 330 },
  };
  // Timeframe yang bisa dipilih (= landing page): 1, 5, 15, 30 menit per candle; pilihan diingat per browser.
  const TF_CHOICES = ['1m', '5m', '15m', '30m'];
  { const saved = storageGet('simpasar:tf'); S.tf = TF_CHOICES.includes(saved) ? saved : '15m'; }
  // Kapasitas jendela chart mengikuti timeframe (≈90 candle), bukan seluruh histori 2000 tick:
  // dengan 2000 tick pada 15m kapasitasnya 134 candle sehingga candle terlalu kurus (~5 px).
  const windowTicksFor = (period) => Math.min(MAX_HISTORY, Math.max(120, period * 90));

  const chart = window.CandleChart ? window.CandleChart.create($('priceChart'), {
    period: TIMEFRAMES[S.tf].period,
    periodLabel: S.tf,
    labelEveryTicks: TIMEFRAMES[S.tf].every,
    sessionBreaks: true,
    minutesPerDay: MINUTES_PER_DAY,
    // Candle dibentuk per hari bursa (1H: 09:00, 10:00, 11:00, 13:30, 14:30, 15:30) — tidak melintasi
    // penutupan/pembukaan. Opsi baru candlechart; versi lama mengabaikannya.
    sessionAligned: true,
    minStep: 0,                         // disetel di applySymbolMeta: saham → grid sumbu ≥ Rp 1
    symbol: S.symbol.symbol,
    navigation: true,                   // scroll = zoom, seret = geser, seret sumbu harga = skala, klik ganda = reset
    windowTicks: windowTicksFor(TIMEFRAMES[S.tf].period),
    minVisibleBars: 40,
    maxBarSpacing: 22,
    ...T.chart,                         // warna chart mengikuti tema (THEMES)
    formatPrice: (v) => fmtPrice(v),
    // Saham IDR: sumbu 0 desimal (bersama minStep 1); indeks: desimal mengikuti langkah grid (maks 2).
    formatAxis: (v, d) => (S.symbol.kind === 'index' ? NF_AXIS[Math.min(d, 2)].format(v) : NF0.format(v)),
    formatPercent: (v) => NF2.format(v) + '%',
    formatVolume: fmtVol,
    formatTickLabel,
    formatTickRange,
  }) : null;
  if (!chart) console.error('candlechart.js gagal dimuat: chart harga tidak tersedia, kontrol lain tetap berjalan.');

  const tfButtons = Array.from(document.querySelectorAll('.tf-btn[data-tf]'));
  function setTimeframe(tf, persist) {
    const cfg = TIMEFRAMES[tf];
    if (!cfg) return;
    S.tf = tf;
    if (chart) {
      chart.setPeriod(cfg.period);
      chart.setOptions({ periodLabel: tf, labelEveryTicks: cfg.every, windowTicks: windowTicksFor(cfg.period) });
    }
    tfButtons.forEach((b) => { const on = b.dataset.tf === tf; b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on)); });
    if (persist) storageSet('simpasar:tf', tf);
    updateOhlcStrip();
  }
  tfButtons.forEach((b) => b.addEventListener('click', () => setTimeframe(b.dataset.tf, true)));
  setTimeframe(S.tf, false);

  const overlay = $('chartOverlay');
  function showOverlay(text, isError) {
    if (!overlay) return;
    overlay.hidden = false;
    overlay.classList.toggle('chart-overlay--error', !!isError);
    setText('chartOverlayText', text);
  }
  function hideOverlay() { if (overlay) overlay.hidden = true; }
  if (!chart) showOverlay('candlechart.js tidak termuat — chart tidak tersedia', true);

  // ═══════════════════════════ 5. WebSocket & protokol snapshot/delta ═══════════════════════════
  const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
  let ws = null, reconnTimer = null;

  function setConn(mode, label) {
    const c = $('conn');
    if (!c) return;
    c.classList.remove('conn--on', 'conn--off', 'conn--wait', 'conn--paused');
    c.classList.add(`conn--${mode}`);
    setText('connLabel', label);
  }
  function connect() {
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
    setConn('wait', 'Menghubungkan…');
    try { ws = new WebSocket(WS_URL); } catch (_) { scheduleReconnect(); return; }
    ws.onopen = () => {
      S.connected = true;
      updatePauseBtn();                 // "Live" atau "Dijeda" (snapshot pertama menyamakan status jeda)
      if (S.wasConnected) toast('ok', 'Tersambung kembali', 'Menyinkronkan state simulasi…', 0, 'conn');
      S.wasConnected = true;
      S.syncAttempts = 0;
      beginAwaitSync();                 // pesan pertama dari server adalah snapshot penuh
    };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg && typeof msg === 'object') handleMessage(msg);
    };
    ws.onclose = () => {
      const was = S.connected;
      S.connected = false;
      clearTimeout(syncTimer); syncTimer = null;
      setConn('off', 'Terputus');
      if (was) toast('warn', 'Koneksi terputus', 'Mencoba menyambung ulang setiap 2 detik…', 0, 'conn');
      if (!S.prices.length && chart) showOverlay('Terputus dari server simulasi — mencoba lagi…', true);
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* abaikan */ } };
  }
  function scheduleReconnect() {
    if (reconnTimer) return;
    reconnTimer = setTimeout(() => { reconnTimer = null; connect(); }, 2000);
  }
  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify(obj)); return true; }
    return false;
  }
  // Kirim perintah; bila tidak terhubung beri tahu pengguna (UI optimistis tidak boleh berubah).
  function sendOrWarn(obj) {
    if (send(obj)) return true;
    toast('error', 'Tidak terhubung', 'Server simulasi belum tersambung — perintah tidak terkirim.', 3000, 'offline');
    return false;
  }

  // ── Resinkron: menunggu snapshot penuh dengan batas waktu ──
  // get_state bisa hilang (koneksi tersendat / limiter server lama). Tanpa batas waktu klien membuang
  // semua delta selamanya; di sini: tunggu 1,5 s → kirim ulang get_state (maks 3×) → sambung ulang.
  const SYNC_TIMEOUT_MS = 1500, SYNC_MAX_RETRIES = 3;
  let syncTimer = null;
  function armSyncTimer() { clearTimeout(syncTimer); syncTimer = setTimeout(onSyncTimeout, SYNC_TIMEOUT_MS); }
  function beginAwaitSync() { S.awaitingSync = true; armSyncTimer(); }
  function endAwaitSync() { S.awaitingSync = false; S.syncAttempts = 0; clearTimeout(syncTimer); syncTimer = null; }
  function onSyncTimeout() {
    syncTimer = null;
    if (!S.awaitingSync || !S.connected) return;
    S.syncAttempts += 1;
    if (S.syncAttempts > SYNC_MAX_RETRIES) {
      S.syncAttempts = 0;
      try { ws.close(); } catch (_) { /* onclose → sambung ulang; server mengirim snapshot saat konek */ }
      return;
    }
    S.lastSyncReq = Date.now();
    send({ cmd: 'get_state' });
    armSyncTimer();
  }
  function requestSync() {
    const now = Date.now();
    if (S.awaitingSync && now - S.lastSyncReq < 400) return;   // jangan banjiri server saat beberapa delta loncat berturut-turut
    S.lastSyncReq = now;
    beginAwaitSync();
    send({ cmd: 'get_state' });
  }

  function handleMessage(msg) {
    if (msg.event) { handleEvent(msg); return; }
    if (typeof msg.tick !== 'number') return;
    handleState(msg);
  }

  function resetSession() { S.sesHigh = null; S.sesLow = null; S.prevPrice = NaN; }

  function applySnapshot(st) {
    let prices = Array.isArray(st.price_history) ? st.price_history.map(Number) : [];
    let vols = Array.isArray(st.volume_history) ? st.volume_history.map((v) => num(v, 0)) : [];
    if (prices.length > MAX_HISTORY) prices = prices.slice(-MAX_HISTORY);
    if (vols.length > prices.length) vols = vols.slice(-prices.length);
    while (vols.length < prices.length) vols.unshift(0);
    S.prices = prices;
    S.volumes = vols;
    if (st.tick === 0 || st.tick < S.tick) resetSession();
    // Tertinggi/Terendah memakai seluruh histori snapshot (buka halaman / sambung ulang di tengah sesi),
    // digabung dengan nilai sesi yang sudah tercatat klien.
    let hi = -Infinity, lo = Infinity;
    for (const p of prices) { if (Number.isFinite(p)) { if (p > hi) hi = p; if (p < lo) lo = p; } }
    if (hi >= lo) {
      S.sesHigh = S.sesHigh === null ? hi : Math.max(S.sesHigh, hi);
      S.sesLow = S.sesLow === null ? lo : Math.min(S.sesLow, lo);
    }
    endAwaitSync();
  }

  function handleState(st) {
    // Backend lama (tanpa field snapshot) selalu mengirim histori penuh → perlakukan sebagai snapshot.
    const isSnapshot = st.snapshot === true || (st.snapshot === undefined && Array.isArray(st.price_history));
    if (isSnapshot) {
      applySnapshot(st);
    } else {
      if (S.awaitingSync) return;                          // tunggu snapshot penuh (ada batas waktu, lihat onSyncTimeout)
      if (S.tick < 0 || !S.prices.length) { requestSync(); return; }
      if (st.tick === S.tick) return;                      // duplikat
      if (st.tick !== S.tick + 1) { requestSync(); return; } // tick loncat / putus → sinkron ulang
      S.prices.push(num(st.price, S.price));
      S.volumes.push(num(st.volume, 0));
      if (S.prices.length > MAX_HISTORY) { S.prices.shift(); S.volumes.shift(); }
    }
    S.prevPrice = isSnapshot && S.prices.length >= 2 ? S.prices[S.prices.length - 2] : S.price;
    S.tick = st.tick;
    S.price = num(st.price, NaN);
    S.fundamental = num(st.fundamental, NaN);
    S.status = st.status || 'Normal';
    S.limits = st.limits && typeof st.limits === 'object' ? st.limits : null;   // server lama: tidak ada
    S.ref = S.limits && isNum(S.limits.ref) ? S.limits.ref : NaN;
    if (st.symbol && typeof st.symbol === 'object') applySymbolMeta(st.symbol);
    if (st.sim_time) syncSimStart(st.sim_time);
    if (typeof st.paused === 'boolean' && st.paused !== S.paused) { S.paused = st.paused; updatePauseBtn(); }
    if (st.params && typeof st.params === 'object') syncParams(st.params);
    if (st.tick_interval != null) syncSpeed(Number(st.tick_interval), false);   // server baru; server lama tidak mengirim
    render(st);
    initStreamMessages();                                    // sekali, setelah simbol server diketahui (pesan menyebut ticker aktif)
    streamReact(st);
  }

  // Nama perintah WS → istilah tombol di UI (untuk pesan error yang ramah).
  const CMD_LABEL = {
    inject_rumor: 'Sebar rumor', inject_panic: 'Kabar buruk', inject_news_sentiment: 'Suntik berita',
    pause: 'Jeda', resume: 'Lanjut', reset: 'Reset', set_speed: 'Kecepatan', set_population: 'Tipe orang',
    set_psych: 'Sifat orang', set_fundamental: 'Terapkan IHSG riil', set_symbol: 'Ganti emiten', get_state: 'Sinkronisasi',
  };
  function handleEvent(msg) {
    switch (msg.event) {
      case 'paused': S.paused = true; updatePauseBtn(); break;
      case 'resumed': S.paused = false; updatePauseBtn(); break;
      case 'speed_changed': syncSpeed(Number(msg.tick_interval), true); break;
      case 'news_injected': {
        const strength = num(msg.strength, 0);
        const fp = num(msg.fundamental_pct, 0);
        const effect = [`suasana pasar ${fmtSigned1(strength)}`];
        if (Math.abs(fp) >= 0.05) effect.push(`nilai wajar ${fmtPct(fp, 1)}`);
        const own = msg.origin === 'user' && Date.now() - (S.ownNewsAt || 0) < 5000;
        if (own) S.ownNewsAt = 0;
        const title = own ? 'Berita yang kamu input disuntik ke pasar'
          : msg.origin === 'user' ? 'Berita dari penonton lain disuntik ke pasar' : 'Berita Sectors disuntik ke pasar';
        toast(strength >= 0 ? 'ok' : 'error', title,
          `${String(msg.title || 'Berita')} · ${effect.join(' · ')}`, 5200, 'news');
        pulseMood();
        break;
      }
      case 'symbol_changed': onSymbolChanged(msg); break;
      case 'symbol_error': onSymbolError(msg); break;
      case 'error': {
        const cmd = String(msg.cmd || '');
        const burstKey = cmd === 'inject_rumor' ? 'rumor' : cmd === 'inject_panic' ? 'panic' : null;
        if (burstKey) untoast(burstKey);
        toast('error', `${CMD_LABEL[cmd] || 'Perintah'} tidak diproses`, String(msg.message || 'Server menolak perintah.'), 3500, 'err');
        // UI yang sudah berubah secara optimistis (jeda, slider, kecepatan) disamakan lagi dengan server.
        if (/^(pause|resume|set_population|set_psych|set_speed|reset)$/.test(cmd)) requestSync();
        break;
      }
      default: break;
    }
  }

  // ═══════════════════════════ 6. Simbol aktif ═══════════════════════════
  // Asal harga awal. Server baru mengirim symbol.source_kind; untuk server lama ditebak dari teks sumber.
  const SOURCE_KINDS = ['sectors', 'cache', 'stale', 'manual', 'fallback'];
  function inferSourceKind(m) {
    if (!isNum(m.source_price)) return null;
    const src = String(m.source || '');
    if (/fallback|cadangan/i.test(src)) return 'fallback';
    if (/klien|manual/i.test(src) || !m.source_date) return 'manual';
    return 'sectors';
  }
  function applySymbolMeta(meta) {
    const prev = S.symbol;
    const next = {
      symbol: String(meta.symbol || prev.symbol || 'IHSG').toUpperCase(),
      name: String(meta.name || prev.name || ''),
      sector: String(meta.sector || 'index'),
      sector_name: meta.sector_name ? String(meta.sector_name) : (SECTOR_NAMES[meta.sector] || ''),
      kind: meta.kind === 'index' || String(meta.symbol || '').toUpperCase() === 'IHSG' ? 'index' : 'stock',
      source_price: isNum(meta.source_price) ? meta.source_price : null,
      source_date: meta.source_date || null,
      source: meta.source || null,
      source_kind: null,
    };
    next.source_kind = 'source_kind' in meta
      ? (SOURCE_KINDS.includes(meta.source_kind) ? meta.source_kind : (next.source_price == null ? null : inferSourceKind(next)))
      : inferSourceKind(next);
    const changed = ['symbol', 'name', 'sector', 'kind', 'source_price', 'source_date', 'source', 'source_kind'].some((k) => prev[k] !== next[k]);
    if (!changed) return;
    S.symbol = next;
    renderSymbol();
    // Saham: grid sumbu harga minimal Rp 1 (label 0 desimal); indeks: bebas.
    if (chart) chart.setOptions({ symbol: next.symbol, minStep: next.kind === 'index' ? 0 : 1, showThresholds: next.kind === 'index' });
  }

  // Label kartu emiten per asal harga — hanya harga dari Sectors yang boleh disebut "Sectors".
  const SOURCE_TEXT = {
    sectors:  { label: 'Harga awal Sectors', note: 'Sumber: data bursa harian dari Sectors' },
    cache:    { label: 'Harga awal Sectors', note: 'Sumber: data bursa dari Sectors (tersimpan ≤ 6 jam)' },
    stale:    { label: 'Harga awal (cache lama)', note: 'Cache Sectors lama — Sectors sedang tidak bisa dihubungi' },
    manual:   { label: 'Harga awal (diisi manual)', note: 'Diisi manual — bukan data bursa' },
    fallback: { label: 'Nilai cadangan', note: 'Sectors offline — nilai cadangan, bukan harga riil' },
    none:     { label: 'Harga awal', note: 'Harga awal bawaan — belum memakai data Sectors' },
  };
  const isRealSource = (k) => k === 'sectors' || k === 'cache' || k === 'stale';
  function setRow(rowId, valueId, text) {
    setText(valueId, text);
    const row = $(rowId);
    if (row) row.hidden = !text || text === '—';
  }

  function renderSymbol() {
    const sym = S.symbol;
    const shortName = sym.name.replace(/^PT\s+/i, '').replace(/\s+Tbk\.?$/i, '').replace(/\s*\(Persero\)\s*/i, ' ').trim();
    setText('hdrTicker', sym.symbol);
    setText('hdrName', shortName);
    setChip($('hdrSector'), sym.sector, sym.sector_name);
    setText('cardTicker', sym.symbol);
    setText('cardName', sym.name);
    setChip($('cardSector'), sym.sector, sym.sector_name);
    setText('ohlcSymbol', sym.symbol);

    const kind = sym.source_kind || 'none';
    const txt = SOURCE_TEXT[kind] || SOURCE_TEXT.none;
    setText('cardSourcePriceLabel', txt.label);
    setRow('rowSourcePrice', 'cardSourcePrice', sym.source_price != null ? fmtByKind(sym.kind, sym.source_price, true) : '—');
    setRow('rowSourceDate', 'cardSourceDate', isRealSource(kind) && sym.source_date ? fmtDateID(sym.source_date) : '—');
    // Detail Sectors (perubahan harian, market cap) dari modal hanya ditampilkan bila memang milik
    // harga awal yang sedang dipakai server (klien lain bisa saja mengisi harga manual).
    const saved = S.priceInfo[sym.symbol];
    const info = saved && isRealSource(kind) && isNum(saved.price) && sym.source_price != null &&
      Math.abs(saved.price - sym.source_price) < 1e-6 && (!saved.date || !sym.source_date || saved.date === sym.source_date) ? saved : null;
    const chgEl = $('cardChange');
    if (info && isNum(info.change) && isNum(info.change_pct)) {
      setRow('rowChange', 'cardChange', `${fmtSignedByKind(sym.kind, info.change)} (${fmtPct(info.change_pct)})`);
      setTone(chgEl, info.change);
    } else { setRow('rowChange', 'cardChange', '—'); setTone(chgEl, 0); }
    setRow('rowMcap', 'cardMcap', sym.kind === 'index' ? '—' : fmtMcap(info && info.market_cap));
    const srcEl = $('cardSource');
    if (srcEl) {
      srcEl.textContent = txt.note;
      srcEl.title = sym.source ? `Sumber server: ${sym.source}` : '';
      srcEl.classList.toggle('is-warn', kind === 'fallback' || kind === 'stale');
    }
    const useIhsg = $('btnUseIhsg');
    if (useIhsg) useIhsg.hidden = sym.kind !== 'index';
    updateTitle();
  }

  function updateTitle() {
    if (!isNum(S.price)) { document.title = `${S.symbol.symbol} · SimPasar`; return; }
    const base = isNum(S.ref) ? S.ref : S.prevPrice;       // arah terhadap harga acuan (seperti aplikasi sekuritas)
    const arrow = isNum(base) && S.price < base ? '▼' : '▲';
    document.title = `${S.symbol.symbol} ${fmtPrice(S.price)} ${arrow} · SimPasar`;
  }

  function onSymbolChanged(msg) {
    const meta = msg.symbol && typeof msg.symbol === 'object' ? msg.symbol : { symbol: msg.symbol };
    S.llmReasons.clear();
    S.prices = []; S.volumes = []; S.tick = -1;
    beginAwaitSync();                                       // snapshot penuh menyusul dari server
    resetSession();
    resetChatReactions(0);
    if (isNum(msg.fundamental)) { S.fundamental = msg.fundamental; S.price = msg.fundamental; S.ref = msg.fundamental; }
    limitAnnounced.clear();  // judul tab tidak sempat memakai harga simbol lama
    applySymbolMeta(meta);
    renderSymbol();
    Search.onSymbolChanged(S.symbol.symbol);
    MyNews.onSymbolChanged(S.symbol.symbol);
    const priceText = fmtByKind(S.symbol.kind, isNum(msg.fundamental) ? msg.fundamental : S.symbol.source_price, true);
    toast('ok', `Simbol diganti: ${S.symbol.symbol}`, `${S.symbol.name} · harga awal ${priceText}`);
    addChatMessage('DokterSaham', 'ANALIS 📊', 'badge-fomo', `Ganti fokus ke {T} (${S.symbol.sector_name || 'BEI'}) di harga ${priceText}. Pantau reaksi kerumunan ya 📈`);
  }
  function onSymbolError(msg) {
    const text = String(msg.message || 'Simbol tidak dikenal');
    if (MyNews.onSymbolError(msg)) { toast('error', `Gagal mengganti simbol ${String(msg.symbol || '')}`, text); return; }
    if (!Search.onSymbolError(msg)) toast('error', `Gagal mengganti simbol ${String(msg.symbol || '')}`, text);
  }

  // ═══════════════════════════ 7. Render state ═══════════════════════════
  function render(st) {
    const { tick, price, fundamental } = S;

    // Header: badge status, jam simulasi
    const badge = $('statusBadge');
    if (S.status === 'Bubble') { badge.className = 'status status--bubble'; badge.textContent = '▲ BUBBLE'; }
    else if (S.status === 'Panik-Crash') { badge.className = 'status status--crash'; badge.textContent = '▼ PANIK-CRASH'; }
    else { badge.className = 'status status--normal'; badge.textContent = '● NORMAL'; }
    // Jam simulasi: pakai sim_time dari server bila ada (sumber kebenaran), fallback tickToClock lokal.
    const clock = tickToClock(tick);
    const stTime = st.sim_time || {};
    const serverDate = parseISODate(stTime.date);
    const dateLabel = serverDate ? `${WEEKDAYS_SHORT[serverDate.getDay()]} ${serverDate.getDate()} ${MONTHS_SHORT[serverDate.getMonth()]}` : `${clock.weekdayShort} ${clock.dateLabel}`;
    setText('clockDate', dateLabel);
    setText('clockTime', stTime.time || clock.time);
    setText('clockSession', `Sesi ${stTime.session || clock.session}`);
    setText('clockDay', `Hari ${stTime.day || clock.day}`);

    // Statistik
    // Harga diwarnai terhadap harga acuan (bukan tick sebelumnya) supaya tidak berkedip hijau-merah tiap detik.
    const ref = S.ref;
    const chg = isNum(price) && isNum(ref) ? price - ref : NaN;
    const priceEl = $('statPrice');
    priceEl.textContent = fmtPrice(price);
    setTone(priceEl, isNum(chg) ? chg : (isNum(S.prevPrice) ? price - S.prevPrice : 0));
    const chgEl = $('statChange');
    if (chgEl) {
      chgEl.textContent = isNum(chg) && ref ? `${fmtSignedByKind(S.symbol.kind, chg)} (${fmtPct(chg / ref * 100)})` : '—';
      chgEl.title = isNum(ref) ? `Terhadap harga acuan ${fmtPrice(ref)} (penutupan hari bursa sebelumnya)` : '';
      setTone(chgEl, isNum(chg) ? chg : 0);
    }
    setText('statFundamental', fmtPrice(fundamental));
    const dev = isNum(price) && isNum(fundamental) && fundamental ? (price - fundamental) / fundamental * 100 : NaN;
    const devEl = $('statDev');
    devEl.textContent = fmtPct(dev);
    setTone(devEl, isNum(dev) ? (Math.abs(dev) < 0.005 ? 0 : dev) : 0);
    const day = dayRange();
    setText('statHigh', fmtPrice(day ? day.hi : NaN));
    setText('statLow', fmtPrice(day ? day.lo : NaN));
    renderLimits(S.limits, price, tick);

    // Chart
    if (chart && S.prices.length) {
      chart.setData({ tick, prices: S.prices, volumes: S.volumes, fundamental });
      hideOverlay();
    }
    updateOhlcStrip();
    updateTitle();

    // Agen
    if (Array.isArray(st.agents)) renderAgents(st.agents, tick);
    if (st.psych_stats) renderPnl(st.psych_stats);
    if (st.llm) renderLlm(st.llm);
    renderSentiment(num(st.sentiment, 0));
  }

  function updateOhlcStrip() {
    if (!chart) return;
    const candles = chart.getCandles();
    const n = candles.length;
    if (!n) {
      ['ohlcO', 'ohlcH', 'ohlcL', 'ohlcC', 'ohlcVol'].forEach((id) => setText(id, '—'));
      setText('ohlcDelta', '—'); setText('ohlcRange', '');
      return;
    }
    const c = candles[n - 1];
    const prevClose = n > 1 ? candles[n - 2].close : c.open;
    const chg = c.close - prevClose;
    const pct = prevClose ? chg / prevClose * 100 : 0;
    setText('ohlcO', fmtPrice(c.open)); setText('ohlcH', fmtPrice(c.high));
    setText('ohlcL', fmtPrice(c.low)); setText('ohlcC', fmtPrice(c.close));
    setText('ohlcVol', fmtVol(c.volume));
    const d = $('ohlcDelta');
    d.textContent = `${fmtSignedByKind(S.symbol.kind, chg)} (${fmtPct(pct)})`;
    setTone(d, chg);
    ['ohlcO', 'ohlcH', 'ohlcL', 'ohlcC'].forEach((id) => setTone($(id), c.close - c.open));
    setText('ohlcRange', formatTickRange(c.t0, c.t1));
  }

  // ── Tertinggi/terendah HARI INI (hari bursa simulasi yang sedang berjalan) dari histori klien ──
  function dayRange() {
    const n = S.prices.length;
    if (!n || S.tick < 0) return null;
    const dayStart = Math.floor(S.tick / MINUTES_PER_DAY) * MINUTES_PER_DAY;
    const count = Math.min(n, S.tick - dayStart + 1);
    let hi = -Infinity, lo = Infinity;
    for (let i = n - count; i < n; i++) { const p = S.prices[i]; if (Number.isFinite(p)) { if (p > hi) hi = p; if (p < lo) lo = p; } }
    return hi >= lo ? { hi, lo } : null;
  }

  // ── Batas harian ARA / ARB (state.limits dari server, aturan BEI) ──
  // Saham: "ARB – ARA" + rel (tanda acuan & posisi harga) di baris statistik, garis putus-putus di chart
  // sejak awal hari bursa, kotak berwarna + toast + obrolan saat harga terkunci. Indeks (IHSG): tidak ada batas.
  const limitAnnounced = new Set();                       // "hari|ara" yang sudah diumumkan
  let limitLinesKey = '', lastLimitTick = -1;
  const pctText = (v) => `${NF0.format(Math.round(Number(v) * 100))}%`;
  function setChartLimitLines(lim) {
    if (!chart) return;
    const key = lim && lim.applies ? `${lim.ara}|${lim.arb}|${lim.day}` : '';
    if (key === limitLinesKey) return;
    limitLinesKey = key;
    if (!key) { chart.setOptions({ priceLines: [] }); return; }
    const fromTick = (Math.max(1, Number(lim.day) || 1) - 1) * MINUTES_PER_DAY;
    chart.setOptions({
      priceLines: [
        { price: lim.ara, color: T.up, label: `ARA ${fmtPrice(lim.ara)}`, dash: [6, 3], alpha: 0.8, fromTick, expand: true },
        { price: lim.arb, color: T.down, label: `ARB ${fmtPrice(lim.arb)}`, dash: [6, 3], alpha: 0.8, fromTick, expand: true },
      ],
    });
  }
  function renderLimits(lim, price, tick) {
    const box = $('limitsBox');
    if (!box) return;
    if (tick < lastLimitTick) limitAnnounced.clear();     // reset / ganti simbol: boleh diumumkan lagi
    lastLimitTick = tick;
    if (!lim) {
      box.dataset.state = 'none';
      setText('limLabel', 'Batas harian'); setText('limArb', '—'); setText('limAra', '—');
      setChartLimitLines(null);
      return;
    }
    if (!lim.applies) {
      box.dataset.state = 'index';
      setText('limLabel', 'Batas harian');
      setText('limArb', 'tidak ada'); setText('limAra', '');
      box.title = 'IHSG adalah indeks, bukan saham, jadi tidak punya batas ARA/ARB. Batas berlaku untuk tiap saham.';
      setChartLimitLines(null);
      return;
    }
    const ara = Number(lim.ara), arb = Number(lim.arb), ref = Number(lim.ref);
    const span = ara - arb;
    const pos = (v) => (span > 0 && isNum(v) ? clamp((v - arb) / span * 100, 0, 100) : 50);
    const pRef = pos(ref), pNow = pos(price);
    const refMark = $('limRefMark'), pin = $('limPin'), fill = $('limFill');
    if (refMark) refMark.style.left = pRef + '%';
    if (pin) pin.style.left = pNow + '%';
    if (fill) {
      fill.style.left = Math.min(pRef, pNow) + '%';
      fill.style.width = Math.abs(pNow - pRef) + '%';
      fill.classList.toggle('is-down', isNum(price) && price < ref);
    }
    setText('limArb', fmtPrice(arb));
    setText('limAra', fmtPrice(ara));
    const rule = lim.rule === 'nominal'
      ? 'harga acuan Rp1–Rp10: naik/turun maks. Rp1'
      : `harga ${String(lim.band || '')}: ARA +${pctText(lim.ara_pct)}, ARB −${pctText(lim.arb_pct)}`;
    box.title = `Batas harian BEI hari ini. Acuan ${fmtPrice(ref)} (penutupan hari sebelumnya) · ${rule}. ` +
      'ARB = Auto Rejection Bawah, ARA = Auto Rejection Atas: harga tidak bisa keluar dari rentang ini.';

    const hit = lim.hit === 'ara' || lim.hit === 'arb' ? lim.hit : null;
    box.dataset.state = hit || 'open';
    setText('limLabel', hit ? `Terkunci ${hit.toUpperCase()}` : 'Batas harian');
    setChartLimitLines(lim);

    if (hit) {
      const key = `${lim.day}|${hit}`;
      if (!limitAnnounced.has(key)) {
        limitAnnounced.add(key);
        const p = fmtByKind(S.symbol.kind, hit === 'ara' ? ara : arb, true);
        if (hit === 'ara') {
          toast('ok', `${S.symbol.symbol} menyentuh ARA`, `Harga terkunci di batas atas ${p} untuk hari ini.`, 4200, 'limit');
          addChatMessage('ScalperGarisKeras', 'ARA 🚀', 'badge-tp', pick(CHAT_POOLS.ara));
        } else {
          toast('error', `${S.symbol.symbol} menyentuh ARB`, `Harga terkunci di batas bawah ${p} untuk hari ini.`, 4200, 'limit');
          addChatMessage('PrajuritCutloss', 'ARB 🔻', 'badge-cl', pick(CHAT_POOLS.arb));
        }
      }
    }
  }

  // ── Grid 100 agen: satu dimensi warna per mode (lebih mudah dibaca daripada 8 legenda sekaligus) ──
  //   Aksi        : hijau beli / hijau tua sedang untung / merah jual / merah tua sedang rugi / gelap diam
  //                 (beli/jual: pekat = keyakinan order besar; untung/rugi = pegang saham, P&L di luar ±2%)
  //   Tipe orang  : fundamentalist / chartist / noise (▲ ▼ – di dalam kotak = beli/jual/diam)
  //   Sifat orang : discipline / denial / averager
  // Di semua mode: cincin merah = sedang rugi berat, ▲ amber di pojok = sedang nambah posisi (average up/down).
  const agentCanvas = $('agentCanvas');
  const actx = agentCanvas.getContext('2d');
  const GRID_PX = 240, CELL = GRID_PX / 10;
  // Warna kotak dari tema aktif (T): beli/jual T.up/T.down, untung/rugi T.profitFill/T.lossFill,
  // diam T.holdFill, tipe orang T.type, sifat orang T.psych.
  const ACTION_COLOR_KEYS = { buy: 'up', sell: 'down', profit: 'profitFill', loss: 'lossFill' };
  function crowdColor(mode, key) {
    if (mode === 'action') return ACTION_COLOR_KEYS[key] ? T[ACTION_COLOR_KEYS[key]] : null;
    return (mode === 'type' ? T.type : T.psych)[key] || T.muted;
  }
  // Kategori mode Aksi: order beli/jual yang tegas (|order| ≥ 0,35, ambang masuk posisi di sim/agents.py)
  // tampil hijau/merah; investor yang sedang pegang saham dengan order lemah tampil sebagai sedang
  // untung (hijau tua) / sedang rugi (merah tua); sisanya order lemah atau diam.
  const PNL_STATE_PCT = 2;                                // = sim/market.py PNL_STATE_EPS (dalam persen)
  const FIRM_ORDER = 0.35;
  function actionKey(a) {
    const o = num(a.order, 0);
    if (Math.abs(o) >= FIRM_ORDER) return o > 0 ? 'buy' : 'sell';
    if (num(a.pos, 0) > 0) {
      const p = num(a.pnl, 0);
      if (p > PNL_STATE_PCT) return 'profit';
      if (p < -PNL_STATE_PCT) return 'loss';
    }
    return a.action === 'buy' || a.action === 'sell' ? a.action : 'hold';
  }
  const crowdKey = (mode, a) => (mode === 'action' ? actionKey(a) : mode === 'type' ? a.type : a.psych);
  // Istilah sama dengan landing page: tipe orang & sifat orang.
  const TYPE_LABEL = { fundamentalist: 'Fundamentalist', chartist: 'Chartist', noise: 'Noise' };
  const PSYCH_LABEL = { disciplined: 'Discipline', bagholder: 'Denial', averager: 'Averager' };
  const ACTION_LABEL = { buy: 'BELI', sell: 'JUAL', hold: 'DIAM' };
  const MAX_ORDER = 1.5;                                  // |order| agen maksimal (sim/agents.py)
  const CROWD_MODES = {
    action: { items: [
      { key: 'buy', label: 'Beli' },
      { key: 'profit', label: 'Sedang untung' },
      { key: 'sell', label: 'Jual' },
      { key: 'loss', label: 'Sedang rugi' },
      { key: 'hold', label: 'Diam' },
    ] },
    type: { items: [
      { key: 'fundamentalist', label: 'Fundamentalist' },
      { key: 'chartist', label: 'Chartist' },
      { key: 'noise', label: 'Noise' },
    ] },
    psych: { items: [
      { key: 'disciplined', label: 'Discipline' },
      { key: 'bagholder', label: 'Denial' },
      { key: 'averager', label: 'Averager' },
    ] },
  };
  let crowdMode = storageGet('simpasar:crowdMode');
  if (!CROWD_MODES[crowdMode]) crowdMode = 'action';
  let legendRefs = [];

  function buildCrowdLegend() {
    const ul = $('crowdLegend');
    if (!ul) return;
    ul.replaceChildren();
    legendRefs = CROWD_MODES[crowdMode].items.map((m) => {
      const color = crowdColor(crowdMode, m.key);
      const li = el('li');
      const sw = el('span', 'sw' + (color ? '' : ' sw--hold'));
      if (color) sw.style.background = color;
      const cnt = el('b', null, '—');
      const bar = el('span', 'bar');
      const fillEl = el('i');
      fillEl.style.background = color || T.muted;
      bar.appendChild(fillEl);
      li.append(sw, el('span', null, m.label), cnt, bar);
      ul.appendChild(li);
      return { key: m.key, cnt, fill: fillEl };
    });
    ul.appendChild(el('li', 'crowd-legend__note', crowdMode === 'action'
      ? 'Hijau/merah tua = pegang saham, untung/rugi lebih dari 2%, dan tidak sedang beli/jual tegas. Jumlah semua yang untung ada di Untung / rugi.'
      : 'Tanda di dalam kotak: ▲ beli · ▼ jual · – diam'));
  }
  function setCrowdMode(mode) {
    if (!CROWD_MODES[mode]) return;
    crowdMode = mode;
    storageSet('simpasar:crowdMode', mode);
    document.querySelectorAll('#crowdMode .seg__btn[data-mode]').forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on));
    });
    buildCrowdLegend();
    if (S.lastAgents.length) { drawAgents(S.lastAgents); updateCrowdCounts(S.lastAgents); }
  }
  document.querySelectorAll('#crowdMode .seg__btn[data-mode]').forEach((b) => b.addEventListener('click', () => setCrowdMode(b.dataset.mode)));

  let agentScale = 1;
  function fitAgentCanvas() {
    const css = agentCanvas.clientWidth || GRID_PX;
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const px = Math.max(1, Math.round(css * dpr));
    if (agentCanvas.width !== px) { agentCanvas.width = px; agentCanvas.height = px; }
    agentScale = px / GRID_PX;
    if (S.lastAgents.length) drawAgents(S.lastAgents);
  }
  if (window.ResizeObserver) new ResizeObserver(fitAgentCanvas).observe(agentCanvas); else window.addEventListener('resize', fitAgentCanvas);

  function drawAgents(agents) {
    actx.setTransform(agentScale, 0, 0, agentScale, 0, 0);
    actx.clearRect(0, 0, GRID_PX, GRID_PX);
    const s = CELL - 3;
    agents.forEach((a, i) => {
      const col = i % 10, row = Math.floor(i / 10);
      const x = col * CELL + 1.5, y = row * CELL + 1.5;
      let color = null, alpha = 1;
      if (crowdMode === 'action') {
        const key = actionKey(a);
        color = crowdColor('action', key);
        if (key === 'buy' || key === 'sell') alpha = 0.8 + 0.2 * clamp(Math.abs(num(a.order, 0)) / MAX_ORDER, 0, 1);   // pekat = order besar (tetap beda dari hijau/merah tua)
      } else {
        color = crowdColor(crowdMode, crowdMode === 'type' ? a.type : a.psych);
      }
      if (color) {
        actx.globalAlpha = alpha; actx.fillStyle = color; actx.fillRect(x, y, s, s); actx.globalAlpha = 1;
      } else {
        actx.fillStyle = T.holdFill; actx.fillRect(x, y, s, s);
        actx.strokeStyle = T.holdEdge; actx.lineWidth = 1; actx.strokeRect(x + 0.5, y + 0.5, s - 1, s - 1);
      }
      if (crowdMode !== 'action') {
        // Tanda aksi di dalam kotak berwarna: ▲ beli, ▼ jual, – diam
        const cx = x + s / 2, cy = y + s / 2;
        actx.fillStyle = T.glyph; actx.strokeStyle = T.glyph; actx.lineWidth = 1.8;
        actx.beginPath();
        if (a.action === 'buy') { actx.moveTo(cx - 4.5, cy + 3); actx.lineTo(cx + 4.5, cy + 3); actx.lineTo(cx, cy - 4); actx.closePath(); actx.fill(); }
        else if (a.action === 'sell') { actx.moveTo(cx - 4.5, cy - 3); actx.lineTo(cx + 4.5, cy - 3); actx.lineTo(cx, cy + 4); actx.closePath(); actx.fill(); }
        else { actx.moveTo(cx - 3.5, cy); actx.lineTo(cx + 3.5, cy); actx.stroke(); }
      }
      if (a.pain) {
        actx.strokeStyle = T.halo; actx.lineWidth = 4;
        actx.strokeRect(x + 5, y + 5, s - 10, s - 10);
        actx.strokeStyle = T.pain; actx.lineWidth = 2;
        actx.strokeRect(x + 5, y + 5, s - 10, s - 10);
      }
      if ('add' in a ? a.add === true : (a.psych === 'averager' && a.action === 'buy' && num(a.pos, 0) > 1.05)) {
        // ▲ amber = sedang nambah posisi (average up/down), dengan tepi gelap agar terlihat di isi terang
        actx.fillStyle = T.avg; actx.strokeStyle = T.halo; actx.lineWidth = 1;
        actx.beginPath();
        actx.moveTo(x + s - 7.5, y + s - 1.5); actx.lineTo(x + s - 1.5, y + s - 1.5); actx.lineTo(x + s - 4.5, y + s - 7);
        actx.closePath(); actx.fill(); actx.stroke();
      }
    });
  }
  function updateCrowdCounts(agents) {
    const tot = agents.length || 1;
    const cnt = {};
    agents.forEach((a) => { const k = crowdKey(crowdMode, a); cnt[k] = (cnt[k] || 0) + 1; });
    legendRefs.forEach((r) => {
      const v = cnt[r.key] || 0;
      r.cnt.textContent = String(v);
      r.fill.style.width = (v / tot * 100).toFixed(1) + '%';
    });
  }
  function renderAgents(agents, tick) {
    S.lastAgents = agents;
    if (tick < S.lastTickSeen) S.llmReasons.clear();      // reset / set_symbol (populasi baru: lihat syncParams)
    S.lastTickSeen = tick;
    agents.forEach((a) => { if (a.reason) S.llmReasons.set(a.id, { reason: String(a.reason), tick }); });
    drawAgents(agents);
    updateCrowdCounts(agents);

    const tot = agents.length || 1;
    const cnt = { fundamentalist: 0, chartist: 0, noise: 0, disciplined: 0, bagholder: 0, averager: 0 };
    agents.forEach((a) => { cnt[a.type] = (cnt[a.type] || 0) + 1; cnt[a.psych] = (cnt[a.psych] || 0) + 1; });
    const setBar = (barId, pctId, v) => { const pct = Math.round(v / tot * 100); $(barId).style.width = pct + '%'; setText(pctId, pct + '%'); };
    setBar('barF', 'pctF', cnt.fundamentalist); setBar('barC', 'pctC', cnt.chartist); setBar('barN', 'pctN', cnt.noise);
    setBar('barD', 'pctD', cnt.disciplined); setBar('barB', 'pctB', cnt.bagholder); setBar('barA', 'pctA', cnt.averager);
  }

  // Tooltip agen (termasuk alasan Gemini terakhir yang dipakai agen itu). Mouse: sorot; sentuh: ketuk.
  const tooltip = $('agentTooltip');
  let tooltipTouchTimer = null;
  function showAgentTooltip(clientX, clientY) {
    if (!S.lastAgents.length) return false;
    const rect = agentCanvas.getBoundingClientRect();
    const col = Math.floor((clientX - rect.left) / rect.width * 10);
    const row = Math.floor((clientY - rect.top) / rect.height * 10);
    const idx = row * 10 + col;
    if (col < 0 || col > 9 || idx < 0 || idx >= S.lastAgents.length) { tooltip.hidden = true; return false; }
    const a = S.lastAgents[idx];
    tooltip.replaceChildren();
    const title = el('div', 'tooltip__title', `Investor #${a.id} · `);
    const type = el('span', null, TYPE_LABEL[a.type] || String(a.type)); type.style.color = T.type[a.type] || T.text;
    const psych = el('span', null, PSYCH_LABEL[a.psych] || String(a.psych)); psych.style.color = T.psych[a.psych] || T.text;
    title.append(type, ' / ', psych);
    tooltip.appendChild(title);
    const act = el('div', 'tooltip__row');
    const order = num(a.order, 0);
    const force = Math.round(clamp(Math.abs(order) / MAX_ORDER, 0, 1) * 100);
    act.append('Aksi: ', el('b', null, ACTION_LABEL[a.action] || String(a.action || '').toUpperCase()),
      a.action === 'hold' || !force ? '' : ` · kekuatan ${force}%`);
    tooltip.appendChild(act);
    const pnl = el('div', 'tooltip__row');
    const pnlNum = num(a.pnl, 0);
    const pnlV = el('b', null, fmtPct(pnlNum, 1));
    pnlV.className = pnlNum > 0 ? 'is-up' : pnlNum < 0 ? 'is-down' : 'is-flat';
    pnl.append('Untung/rugi: ', pnlV, a.pain ? ' · sedang rugi berat' : '', num(a.pos, 0) > 1.05 ? ` · ▲ posisi ×${NF1.format(Number(a.pos))}` : '');
    tooltip.appendChild(pnl);
    const r = S.llmReasons.get(a.id);
    if (r) tooltip.appendChild(el('div', 'tooltip__llm', `Alasan Gemini (menit ke-${r.tick}): ${r.reason}`));
    tooltip.hidden = false;
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    tooltip.style.left = clamp(clientX + 14, 8, window.innerWidth - tw - 8) + 'px';
    tooltip.style.top = clamp(clientY - 20, 8, window.innerHeight - th - 8) + 'px';
    return true;
  }
  agentCanvas.addEventListener('mousemove', (e) => { showAgentTooltip(e.clientX, e.clientY); });
  agentCanvas.addEventListener('mouseleave', () => { if (!tooltipTouchTimer) tooltip.hidden = true; });
  // Sentuhan / pena: ketuk kotak → tooltip tampil 4 detik (mousemove tidak ada di layar sentuh).
  agentCanvas.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    if (!showAgentTooltip(e.clientX, e.clientY)) return;
    clearTimeout(tooltipTouchTimer);
    tooltipTouchTimer = setTimeout(() => { tooltipTouchTimer = null; tooltip.hidden = true; }, 4000);
  });
  if (window.matchMedia && window.matchMedia('(hover: none)').matches) setText('agentHint', 'ketuk kotak');
  setCrowdMode(crowdMode);

  function renderPnl(ps) {
    setText('pnlInPos', String(ps.in_position));
    const painEl = $('pnlInPain');
    painEl.textContent = String(ps.in_pain);
    painEl.className = 'mono ' + (ps.in_pain > 10 ? 'is-down' : ps.in_pain > 5 ? 'is-warn' : 'is-flat');
    const profitEl = $('pnlInProfit');
    const inProfit = num(ps.in_profit, NaN);                // server lama tidak mengirim
    profitEl.textContent = isNum(inProfit) ? String(inProfit) : '—';
    profitEl.className = 'mono ' + (inProfit > 0 ? 'is-up' : 'is-flat');
    const up = num(ps.averaging_up, NaN), down = isNum(num(ps.averaging_down, NaN)) ? num(ps.averaging_down, 0) : num(ps.averaging, 0);
    const upEl = $('pnlAvgUp'), downEl = $('pnlAvgDown');
    upEl.textContent = isNum(up) ? String(up) : '—';
    upEl.className = 'mono ' + (up > 0 ? 'is-warn' : 'is-flat');
    downEl.textContent = String(down);
    downEl.className = 'mono ' + (down > 0 ? 'is-warn' : 'is-flat');
    const pnlEl = $('pnlAvg');
    pnlEl.textContent = fmtPct(num(ps.avg_pnl_pct, 0), 1);
    pnlEl.className = 'mono ' + (ps.avg_pnl_pct > 0 ? 'is-up' : ps.avg_pnl_pct < -3 ? 'is-down' : 'is-flat');
    const frac = ps.in_position > 0 ? ps.in_pain / ps.in_position : 0;
    $('painBar').style.width = (frac * 100).toFixed(1) + '%';
  }

  function renderLlm(l) {
    const llmEl = $('pnlLlm');
    const row = $('pnlLlmRow');
    if (l.enabled) {
      llmEl.textContent = `${l.applied} dipakai`;
      llmEl.className = 'mono ' + (l.backoff_s > 0 ? 'is-warn' : 'is-up');
      row.title = `Gemini aktif · ${l.model} · ${l.inflight} sedang berjalan · ${l.requested}${l.max_requests ? '/' + l.max_requests : ''} request (maks ${l.rpm}/menit)` +
        ` · ${l.discarded} dibuang (situasi berubah) · ${l.errors} error · ${l.input_tokens || 0} token input` + (l.last_error ? ` · ${l.last_error}` : '');
    } else {
      const labels = { disabled: 'mati', no_key: 'tanpa kunci', auth_error: 'kunci ditolak', budget_exhausted: 'batas sesi habis', invalid_config: 'konfigurasi salah', closed: 'berhenti' };
      llmEl.textContent = labels[l.reason] || 'tidak aktif';
      llmEl.className = 'mono ' + ((l.reason === 'auth_error' || l.reason === 'invalid_config') ? 'is-down' : 'is-flat');
      row.title = 'Agen memakai aturan, Gemini tidak aktif' + (l.reason ? ` (${l.reason})` : '') +
        (l.requested ? ` · ${l.applied} jawaban dipakai dari ${l.requested} request` : '');
    }
  }

  // Suasana pasar (sentimen server, −3…+3, meluruh sendiri tiap tick) → kata awam + angka 1 desimal + meter.
  const MOOD_MAX = 3;
  function moodWord(s) {
    if (Math.abs(s) < 0.05) return 'Netral';
    if (s > 0) return s >= 1 ? 'Euforia' : 'Positif';
    return s <= -1 ? 'Panik' : 'Negatif';
  }
  let lastMoodKey = '';
  function renderSentiment(sent) {
    S.sentiment = sent;                                     // disimpan untuk menggambar ulang saat tema berganti
    const neutral = Math.abs(sent) < 0.05;
    const key = neutral ? '0' : NF1.format(sent);
    if (key === lastMoodKey) return;
    lastMoodKey = key;
    const bar = $('sentimentBar');
    bar.style.width = Math.min(50, Math.abs(sent) / MOOD_MAX * 50) + '%';
    bar.style.background = sent > 0 ? T.moodUp : T.moodDown;
    bar.style.transform = sent >= 0 ? 'none' : 'translateX(-100%)';
    const word = $('moodWord');
    word.textContent = moodWord(sent);
    word.className = 'mood__word ' + (neutral ? 'is-flat' : sent > 0 ? 'is-warn' : 'is-down');
    setText('sentimentVal', neutral ? '' : fmtSigned1(sent));
  }
  function pulseMood() {
    const box = $('statMoodBox');
    if (!box) return;
    box.classList.add('is-pulse');
    setTimeout(() => box.classList.remove('is-pulse'), 600);
  }

  // ═══════════════════════════ 8. Dock: aksi, kecepatan, slider ═══════════════════════════
  function flash(btn) { if (!btn) return; btn.classList.add('is-flash'); setTimeout(() => btn.classList.remove('is-flash'), 180); }

  // Rumor / kabar buruk: umpan balik langsung (toast di atas dock + kotak Suasana pasar berkedip),
  // karena harga baru bereaksi beberapa tick kemudian.
  $('btnRumor').addEventListener('click', (e) => {
    if (!sendOrWarn({ cmd: 'inject_rumor', strength: 1.0 })) return;
    flash(e.currentTarget); pulseMood();
    toast('warn', 'Rumor disebar', 'Suasana pasar +1, orang noise condong beli. Lihat reaksinya di chart.', 2600, 'rumor');
  });
  $('btnPanic').addEventListener('click', (e) => {
    if (!sendOrWarn({ cmd: 'inject_panic', strength: 1.0 })) return;
    flash(e.currentTarget); pulseMood();
    toast('error', 'Kabar buruk disebar', 'Suasana pasar −1, orang noise condong jual. Lihat reaksinya di chart.', 2600, 'panic');
  });
  $('btnPause').addEventListener('click', () => {
    const want = !S.paused;
    if (!sendOrWarn({ cmd: want ? 'pause' : 'resume' })) return;   // terputus: jangan ubah label (server tetap berjalan)
    S.paused = want;
    updatePauseBtn();
  });
  $('btnReset').addEventListener('click', (e) => {
    if (!sendOrWarn({ cmd: 'reset' })) return;
    resetSession(); flash(e.currentTarget);
  });
  // Jeda berlaku global: tombol, titik koneksi ("Dijeda") dan pil di chart ikut berubah.
  function updatePauseBtn() {
    const b = $('btnPause');
    setText('btnPauseLabel', S.paused ? '▶ Lanjut' : '⏸ Jeda');
    b.setAttribute('aria-pressed', String(S.paused));
    const pill = $('chartPaused');
    if (pill) pill.hidden = !S.paused;
    if (S.connected) setConn(S.paused ? 'paused' : 'on', S.paused ? 'Dijeda' : 'Live');
  }

  // "Terapkan IHSG riil": hanya harga yang benar-benar dari Sectors (bukan nilai cadangan / isian manual).
  // Bila state belum punya, ambil saat diklik (aksi pengguna, hemat kredit).
  $('btnUseIhsg').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const sym = S.symbol;
    let price = sym.kind === 'index' && isRealSource(sym.source_kind) ? sym.source_price : null;
    let stale = sym.source_kind === 'stale';
    if (!isNum(price)) {
      btn.disabled = true;
      let data = null;
      try {
        const res = await fetch('/api/ihsg');
        data = res.ok ? await res.json() : null;
      } catch (_) { /* jatuh ke pesan di bawah */ }
      btn.disabled = false;
      const p = data ? Number(data.price) : NaN;
      if (data && (data.status === 'success' || data.status === 'cache') && isNum(p) && p > 0) {
        price = p; stale = !!data.stale;
      } else {
        toast('warn', 'IHSG riil tidak tersedia', data && data.status === 'fallback'
          ? 'Sectors sedang offline — nilai cadangan tidak dipakai sebagai harga riil.'
          : 'Sectors offline atau kuota habis.', 0, 'ihsg');
        return;
      }
    }
    if (!sendOrWarn({ cmd: 'set_fundamental', fundamental: price })) return;
    resetSession();
    flash(btn);
    toast('info', 'Nilai wajar disamakan dengan IHSG riil', `${NF2.format(price)}${stale ? ' (cache lama Sectors)' : ''} · simulasi dimulai ulang`, 0, 'ihsg');
  });

  // Kecepatan: interval detik/tick, rasio nyata terhadap 1x = 0,25 s dalam batas server 0,05–1,5 s.
  // Tombol aktif mengikuti tick_interval dari server (klien lain / reload), bukan hanya klik lokal.
  const SPEED_VALUES = { '0.5': 0.5, '1.0': 0.25, '2.0': 0.125, '3.0': 0.0833, '5.0': 0.05 };
  const speedButtons = Array.from(document.querySelectorAll('.seg__btn[data-speed]'));
  let speedKey = '1.0', speedLocalKey = null, speedLocalAt = 0;
  function markSpeed(key) {
    if (!SPEED_VALUES[key]) return;
    speedKey = key;
    speedButtons.forEach((b) => {
      const on = b.dataset.speed === key;
      b.classList.toggle('is-active', on); b.setAttribute('aria-pressed', String(on));
      if (on) setText('speedLabel', b.textContent.trim());
    });
  }
  function nearestSpeedKey(interval) {
    let best = '1.0', bestD = Infinity;
    for (const [k, v] of Object.entries(SPEED_VALUES)) { const d = Math.abs(Math.log(interval / v)); if (d < bestD) { bestD = d; best = k; } }
    return best;
  }
  function syncSpeed(interval, authoritative) {
    if (!isNum(interval) || interval <= 0) return;
    const key = nearestSpeedKey(interval);
    // Delta yang sudah di jalan sebelum set_speed kita diproses masih membawa interval lama → abaikan sebentar.
    if (!authoritative && key !== speedLocalKey && Date.now() - speedLocalAt < 1500) return;
    if (authoritative) speedLocalAt = 0;
    if (key !== speedKey) markSpeed(key);
  }
  speedButtons.forEach((btn) => btn.addEventListener('click', () => {
    const key = btn.dataset.speed;
    if (!sendOrWarn({ cmd: 'set_speed', interval: SPEED_VALUES[key] || 0.25 })) return;
    speedLocalKey = key; speedLocalAt = Date.now();
    markSpeed(key);
  }));

  // Slider komposisi (debounce 400ms; populasi dibangun ulang di server → alasan Gemini lama dibuang).
  // Dua slider per kelompok; sisanya otomatis ke kelompok ketiga (noise / averager).
  // Jumlah dua slider maksimal 100%: slider pasangannya diturunkan, sehingga angka = posisi slider.
  const slF = $('slF'), slC = $('slC'), slD = $('slD'), slB = $('slB');
  const SLIDER_GROUPS = {
    pop:   { a: slF, b: slC, lblA: 'lblF', lblB: 'lblC', lblRest: 'lblN', timer: null, activeAt: 0, pressed: false, key: '' },
    psych: { a: slD, b: slB, lblA: 'lblD', lblB: 'lblB', lblRest: 'lblA', timer: null, activeAt: 0, pressed: false, key: '' },
  };
  // Kelompok "sibuk" = sedang ditekan, menunggu debounce, atau baru saja dikirim (server belum menjawab).
  const groupBusy = (g) => g.pressed || g.timer !== null || Date.now() - g.activeAt < 1500;
  function readGroup(g, moved) {
    let x = parseInt(g.a.value, 10) || 0, y = parseInt(g.b.value, 10) || 0;
    if (x + y > 100) {
      if (moved === g.b) { x = 100 - y; g.a.value = String(x); } else { y = 100 - x; g.b.value = String(y); }
    }
    return { x, y, rest: Math.max(0, 100 - x - y) };
  }
  function showGroup(g, x, y, rest) { setText(g.lblA, x + '%'); setText(g.lblB, y + '%'); setText(g.lblRest, rest + '%'); }
  function scheduleGroupSend(g, build) {
    g.activeAt = Date.now();
    g.key = '';
    clearTimeout(g.timer);
    g.timer = setTimeout(() => {
      g.timer = null; g.activeAt = Date.now();
      S.llmReasons.clear();
      sendOrWarn(build());
    }, 400);
  }
  function onPopChange(e) {
    const g = SLIDER_GROUPS.pop;
    const { x: f, y: c, rest: n } = readGroup(g, e && e.target);
    showGroup(g, f, c, n);
    scheduleGroupSend(g, () => ({ cmd: 'set_population', fundamentalist: f / 100, chartist: c / 100, noise: n / 100 }));
  }
  function onPsychChange(e) {
    const g = SLIDER_GROUPS.psych;
    const moved = e && e.target;
    let r = readGroup(g, moved);
    if (r.x + r.y === 0) {                    // server butuh Discipline + Denial > 0
      (moved === g.a ? g.b : g.a).value = '5';
      r = readGroup(g, moved);
    }
    showGroup(g, r.x, r.y, r.rest);
    const d = r.x, b = r.y;
    scheduleGroupSend(g, () => ({ cmd: 'set_psych', disciplined: d / 100, bagholder: b / 100 }));
  }
  [slF, slC].forEach((s) => s.addEventListener('input', onPopChange));
  [slD, slB].forEach((s) => s.addEventListener('input', onPsychChange));
  Object.values(SLIDER_GROUPS).forEach((g) => [g.a, g.b].forEach((s) => s.addEventListener('pointerdown', () => { g.pressed = true; g.activeAt = Date.now(); })));
  const releaseSliders = () => Object.values(SLIDER_GROUPS).forEach((g) => { if (g.pressed) { g.pressed = false; g.activeAt = Date.now(); } });
  window.addEventListener('pointerup', releaseSliders);
  window.addEventListener('pointercancel', releaseSliders);

  // Params dari server (snapshot maupun delta): samakan slider kelompok yang tidak sedang disentuh,
  // dan buang alasan Gemini bila populasi berubah (id agen sama, orangnya baru — mis. diubah penonton lain).
  // g.key = nilai server yang terakhir dipasang ke slider; dikosongkan saat pengguna menggeser, sehingga
  // perintah yang ditolak/terbuang server otomatis dikembalikan ke nilai server setelah kelompok tidak sibuk.
  function syncParams(p) {
    const pct = (v) => Math.round(clamp(v, 0, 1) * 100);
    const all = ['f_ratio', 'c_ratio', 'n_ratio', 'd_ratio', 'b_ratio'].map((k) => (isNum(p[k]) ? p[k].toFixed(2) : '-')).join('|');
    if (S.paramsKey && all !== S.paramsKey) S.llmReasons.clear();
    S.paramsKey = all;
    const pop = SLIDER_GROUPS.pop, psy = SLIDER_GROUPS.psych;
    if (isNum(p.f_ratio) && isNum(p.c_ratio) && !groupBusy(pop)) {
      const f = pct(p.f_ratio), c = pct(p.c_ratio);
      const n = isNum(p.n_ratio) ? pct(p.n_ratio) : Math.max(0, 100 - f - c);
      const key = `${f}|${c}|${n}`;
      if (key !== pop.key) { pop.key = key; slF.value = String(f); slC.value = String(c); showGroup(pop, f, c, n); }
    }
    if (isNum(p.d_ratio) && isNum(p.b_ratio) && !groupBusy(psy)) {
      const d = pct(p.d_ratio), b = pct(p.b_ratio);
      const key = `${d}|${b}`;
      if (key !== psy.key) { psy.key = key; slD.value = String(d); slB.value = String(b); showGroup(psy, d, b, Math.max(0, 100 - d - b)); }
    }
  }

  // ═══════════════════════════ 9. Toast ═══════════════════════════
  // Di atas dock, tidak menangkap klik. `key` menggabungkan toast sejenis (mis. klik Rumor beruntun → "×3").
  const toastsEl = $('toasts');
  const toastByKey = new Map();
  // Kurangi hitungan toast gabungan (mis. klik Rumor yang ditolak server); hapus bila jadi 0.
  function untoast(key) {
    const t = toastByKey.get(key);
    if (!t || !t.isConnected) return;
    t._count -= 1;
    if (t._count <= 0) { clearTimeout(t._timer); t.remove(); toastByKey.delete(key); return; }
    const cnt = t.querySelector('.toast__count');
    if (t._count > 1 && cnt) cnt.textContent = `×${t._count}`;
    else if (cnt) cnt.remove();
  }
  function toast(type, title, body, ms, key) {
    if (!toastsEl) return;
    let t = key ? toastByKey.get(key) : null;
    if (t && (!t.isConnected || t.classList.contains('is-leaving'))) t = null;
    if (t) { t._count = t._title === title ? t._count + 1 : 1; clearTimeout(t._timer); }
    else { t = el('div', 'toast'); t._count = 1; toastsEl.appendChild(t); if (key) toastByKey.set(key, t); }
    t._title = title;
    t.className = `toast toast--${type || 'info'}`;
    const head = el('div', 'toast__title', title);
    if (t._count > 1) head.appendChild(el('span', 'toast__count', `×${t._count}`));
    t.replaceChildren(head);
    if (body) t.appendChild(el('div', 'toast__body', body));
    while (toastsEl.children.length > 3) toastsEl.removeChild(toastsEl.firstChild);
    t._timer = setTimeout(() => {
      t.classList.add('is-leaving');
      setTimeout(() => { t.remove(); if (key && toastByKey.get(key) === t) toastByKey.delete(key); }, 220);
    }, ms || 4200);
  }

  // ═══════════════════════════ 10. Panel kanan: tab, stream ritel, berita ═══════════════════════════
  const tabs = Array.from(document.querySelectorAll('.tab[data-tab]'));
  let newsLoaded = false;
  const PANES = { stream: 'paneStream', news: 'paneNews', mynews: 'paneMyNews' };
  const railTabs = Array.from(document.querySelectorAll('.rail__btn[data-open-tab]'));
  function activateTab(name) {
    tabs.forEach((t) => { const on = t.dataset.tab === name; t.classList.toggle('is-active', on); t.setAttribute('aria-selected', String(on)); });
    railTabs.forEach((b) => {
      const on = b.dataset.openTab === name;
      b.classList.toggle('is-active', on);
      if (on) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current');   // tab yang akan dibuka strip
    });
    Object.entries(PANES).forEach(([key, id]) => { const p = $(id); if (p) { p.classList.toggle('is-active', key === name); p.hidden = key !== name; } });
    if (name === 'news' && !newsLoaded) loadNews();        // berita dimuat saat tab dibuka (aksi pengguna, hemat kredit)
  }
  tabs.forEach((t) => t.addEventListener('click', () => {
    activateTab(t.dataset.tab);
    if (!RightPanel.isOpen()) RightPanel.setOpen(true);   // ponsel: panel tertutup tetap menampilkan baris tab
  }));
  $('newsReload').addEventListener('click', () => loadNews());

  // ── Buka / tutup panel kanan ──
  // Tertutup = body.right-collapsed: di layar lebar panel menyusut jadi strip tipis (tombol buka + pintasan
  // tab) dan chart melebar; di ponsel/tablet tegak (< 1024 px, panel di bawah) hanya baris tab yang tersisa.
  // Selalu terbuka saat aplikasi dibuka: pilihan buka/tutup sengaja tidak disimpan.
  const RightPanel = (() => {
    const body = document.body;
    const collapseBtn = $('rightCollapse'), expandBtn = $('rightExpand');
    const isOpen = () => !body.classList.contains('right-collapsed');
    const shown = (n) => !!n && n.getClientRects().length > 0;
    function sync() {
      const open = isOpen(), label = open ? 'Tutup panel kanan' : 'Buka panel kanan';
      collapseBtn.setAttribute('aria-expanded', String(open)); collapseBtn.title = label; collapseBtn.setAttribute('aria-label', label);
      expandBtn.setAttribute('aria-expanded', String(open));
    }
    // instant: tanpa animasi lebar (dipakai panduan supaya sorotannya langsung pas).
    function setOpen(open, opts) {
      if (open === isOpen()) return;
      const instant = opts && opts.instant;
      if (instant) body.classList.add('right-instant');
      body.classList.toggle('right-collapsed', !open);
      sync();
      if (instant) { void body.offsetWidth; requestAnimationFrame(() => body.classList.remove('right-instant')); }
    }
    function activeTab() { return tabs.find((t) => t.classList.contains('is-active')) || tabs[0]; }
    collapseBtn.addEventListener('click', () => {
      const open = !isOpen();
      setOpen(open);
      // Tombol ini hilang saat panel tertutup di layar lebar → fokus pindah ke tombol buka di strip.
      if (!open && shown(expandBtn)) expandBtn.focus({ preventScroll: true });
    });
    expandBtn.addEventListener('click', () => { setOpen(true); activeTab().focus({ preventScroll: true }); });
    railTabs.forEach((b) => b.addEventListener('click', () => {
      activateTab(b.dataset.openTab);
      setOpen(true);
      activeTab().focus({ preventScroll: true });
    }));
    sync();
    return { isOpen, setOpen };
  })();

  // ── Stream ritel (komunitas fiktif; pool kalimat dipertahankan, {T} = ticker aktif) ──
  const USER_PROFILES = [
    { name: 'ScalperGarisKeras', badge: 'HAKA 🚀', badgeClass: 'badge-tp', color: '#16c784' },
    { name: 'NyangkutDiPucuk',   badge: 'NYANGKUT 😭', badgeClass: 'badge-nyangkut', color: '#f59e0b' },
    { name: 'PrajuritCutloss',   badge: 'CUT LOSS ✂️', badgeClass: 'badge-cl', color: '#ea3943' },
    { name: 'SultanSenopati',    badge: 'WHALE 🐋', badgeClass: 'badge-bandar', color: '#8b5cf6' },
    { name: 'PejuangDividen',    badge: 'HOLD 💎', badgeClass: 'badge-fomo', color: '#3b82f6' },
    { name: 'BandarGhaib',       badge: 'BANDAR 😈', badgeClass: 'badge-bandar', color: '#ec4899' },
    { name: 'RitelPasrah',       badge: 'FOMO 🤡', badgeClass: 'badge-fomo', color: '#f97316' },
    { name: 'DokterSaham',       badge: 'ANALIS 📊', badgeClass: 'badge-fomo', color: '#06b6d4' },
    { name: 'BocilCrypto',       badge: 'FOMO 🚀', badgeClass: 'badge-fomo', color: '#f43f5e' },
    { name: 'TuruBerjamaah',     badge: 'PASRAH 🗿', badgeClass: 'badge-cl', color: '#64748b' },
    { name: 'KangCopetIDX',      badge: 'SCALPER ⚡', badgeClass: 'badge-tp', color: '#14b8a6' },
  ];
  const CHAT_POOLS = {
    crash: [
      'Waduh cut loss massal {T} nih guys! Stop loss jebol parah 💀',
      'Nyangkut di pucuk {T} jemput woyy, dingin banget di atas 😭😭',
      'Siapa yang jualan barbar woy?! Bandar buang barang ini mah!',
      'Porto kebakaran gaes, menu makan sebulan fix Indomie polos 🍜',
      '{T} ARB berjilid-jilid ini mah, pasrah hapus aplikasi dulu 🗿',
      'Titip sendal dulu nunggu pantulan di dasar palung mariana 🩴',
      'Halo OJK tolong gembok {T} please, gak kuat liatnya 😭',
      'Niat scalping 5 menit malah jadi investor jangka panjang 10 tahun 🧓',
      'Serok bawah {T} apa nangkep pisau jatuh nih gan? 🔪',
      'Cut loss adalah koentji keselamatan porto... hiks ✂️',
    ],
    bubble: [
      '{T} TO THE MOON CAPT! ARA-kan jangan kasih kendorrr 🚀🚀',
      'HAKA (Hajar Kanan) {T} full margin! Jangan sampai ketinggalan kereta! 🔥',
      'Malam ini makan enak resto bintang 5 all in Wagyu A5 🥩',
      'Gokil bandar {T} lagi baik hati bagi-bagi THR lebaran awal! 😎',
      'Terbanggg tinggi! Hold terus jangan goyang jempol lemas! 💎🙌',
      'TP (Take Profit) bertahap gaes, amankan cuan bungkus dulu! 💰',
      'Yang kemarin cut loss di bawah pasti lagi nangis di pojokan wkwk 😂',
      'Pucuk {T} masih jauh, target multi-bagger gas pol terus! 📈',
      'Candle-nya hijau pekat mantap, bandar akumulasi besar-besaran! 🟩',
    ],
    averaging: [
      'Malah averaging down {T}, peluru udah abis tinggal modal doa wkwk 💸',
      'Averaging down terus sampai jadi pemegang saham pengendali {T} 🗿',
      'Tenang gaes, fundamentalnya bagus kok... kata influencer di X wkwk',
      'Turun terus serok terus, dompet yang boncos wkwk 😂',
    ],
    averagingUp: [
      'Average up {T} terus mumpung naik, semoga bukan beli di pucuk 😅',
      'Udah cuan malah nambah muatan {T}, harga rata-rata ikut naik nih 📈',
      'Pyramiding {T} dulu capt, selama tren masih hijau gas terus 🔥',
    ],
    ara: [
      'ARA! {T} dikunci di pucuk, offer kosong, antrean beli numpuk 🚀',
      'Mentok ARA {T}, yang belum kebagian cuma bisa antre bid sampai besok 😂',
      'Auto reject atas {T} coy, besok lanjut ARA lagi gak nih? 👀',
    ],
    arb: [
      '{T} ARB, bid kosong melompong, antre jual numpuk 😭',
      'Mentok ARB {T}, mau cut loss aja gak bisa keluar, bid-nya hilang 🗿',
      'Auto reject bawah {T}... besok pagi pre-opening deg-degan nih 💀',
    ],
    normal: [
      'Pasar lagi adem ayem nih, cocok buat scalping santai {T} ☕',
      'Volume transaksi {T} mulai rame, ada tarikan halus dari broker asing.',
      '{T} sideways dulu ya, mantau bid-offer sambil ngopi santai.',
      'Chart {T} di timeframe 15 menit ada sinyal reversal nih capt 📊',
      'Titip sendal dulu di support kuat 🩴',
      'Asing mulai net buy tipis-tipis di {T} nih 👀',
      'Sabar itu subur, jangan fomo ngejar candle pucuk ya gaes.',
    ],
  };
  const feedListEl = $('feedList');
  let lastChatState = 'Normal', lastChatTick = 0;
  const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

  // Teks pesan dirakit sebagai node: "{T}" → <span class="cashtag">$BBCA</span>; sisanya textContent.
  function renderChatText(container, text) {
    const parts = String(text).split('{T}');
    parts.forEach((p, i) => {
      if (p) container.appendChild(document.createTextNode(p));
      if (i < parts.length - 1) container.appendChild(el('span', 'cashtag', `$${S.symbol.symbol}`));
    });
  }
  // Auto-scroll hanya bila pengguna memang sedang di bawah; bila sedang membaca pesan lama,
  // posisinya dipertahankan dan muncul pil "Pesan baru ↓".
  const feedNewBtn = $('feedNew');
  const feedAtBottom = () => feedListEl.scrollHeight - feedListEl.scrollTop - feedListEl.clientHeight < 40;
  // feedStick = pengguna sedang di bawah (dicatat saat menggulir), sehingga perubahan ukuran panel
  // (resize jendela, panel kanan dibuka) tidak membuat obrolan "tertinggal" dengan pil "Pesan baru".
  let feedStick = true;
  function feedToBottom() { feedListEl.scrollTop = feedListEl.scrollHeight; feedStick = true; if (feedNewBtn) feedNewBtn.hidden = true; }
  if (feedNewBtn) feedNewBtn.addEventListener('click', feedToBottom);
  if (feedListEl) {
    feedListEl.addEventListener('scroll', () => {
      feedStick = feedAtBottom();
      if (feedStick && feedNewBtn && !feedNewBtn.hidden) feedNewBtn.hidden = true;
    }, { passive: true });
    if (window.ResizeObserver) new ResizeObserver(() => { if (feedStick) feedToBottom(); }).observe(feedListEl);
  }

  function addChatMessage(userName, badgeText, badgeClass, text) {
    if (!feedListEl) return;
    const stick = feedStick || feedListEl.clientHeight === 0;   // panel tersembunyi: anggap di bawah
    const user = USER_PROFILES.find((u) => u.name === userName) || pick(USER_PROFILES);
    const item = el('div', 'feed-item');
    const top = el('div', 'feed-item__top');
    const avatar = el('span', 'feed-item__avatar', user.name.charAt(0).toUpperCase());
    avatar.style.background = user.color;
    top.appendChild(avatar);
    top.appendChild(el('span', 'feed-item__user', `@${user.name}`));
    top.appendChild(el('span', `feed-item__badge ${badgeClass || user.badgeClass}`, badgeText || user.badge));
    const now = new Date();
    top.appendChild(el('span', 'feed-item__time mono', `${pad2(now.getHours())}:${pad2(now.getMinutes())}`));
    item.appendChild(top);
    const body = el('div', 'feed-item__text');
    renderChatText(body, text);
    item.appendChild(body);
    feedListEl.appendChild(item);
    while (feedListEl.children.length > 35) {
      const first = feedListEl.firstChild;
      const h = stick ? 0 : first.offsetHeight + 6;          // 6 = gap .feed
      feedListEl.removeChild(first);
      if (h) feedListEl.scrollTop = Math.max(0, feedListEl.scrollTop - h);   // pesan yang sedang dibaca tidak bergeser
    }
    if (stick) feedToBottom();
    else if (feedNewBtn) feedNewBtn.hidden = false;
  }
  let streamInitDone = false;
  function initStreamMessages() {
    if (streamInitDone) return;
    streamInitDone = true;
    const initial = [
      { u: 'DokterSaham', text: 'Selamat pagi trader IDX! Pantau pergerakan harga vs fundamental {T} real-time hari ini 📈' },
      { u: 'ScalperGarisKeras', text: 'Pagi capt! Siap HAKA saham yang punya momentum kenceng hari ini 🔥' },
      { u: 'NyangkutDiPucuk', text: 'Semoga hari ini {T} hijau royo-royo biar jemputan datang 😭' },
      { u: 'BandarGhaib', text: 'Barang sudah terkumpul rapi di bawah, siap-siap ya ritel tersayang 😈' },
    ];
    initial.forEach((m, i) => setTimeout(() => addChatMessage(m.u, null, null, m.text), i * 250));
  }
  setInterval(() => {
    if (Math.random() > 0.45) { const u = pick(USER_PROFILES); addChatMessage(u.name, u.badge, u.badgeClass, pick(CHAT_POOLS.normal)); }
  }, 5000);
  setInterval(() => {
    // jumlah "online" fiktif bergoyang pelan supaya panel terasa hidup
    const base = 14820 + Math.round((Math.random() - 0.5) * 240);
    setText('streamOnline', `${NF0.format(base)} online`);
  }, 7000);

  // Tick mundur (reset / ganti simbol / nilai wajar baru): jeda antarreaksi dihitung ulang dari tick sekarang,
  // supaya reaksi BUBBLE/PANIK tidak bungkam ribuan tick.
  function resetChatReactions(tick) { lastChatTick = tick - 16; lastChatState = 'Normal'; }
  function streamReact(st) {
    const { status, tick, psych_stats } = st;
    if (tick < lastChatTick) resetChatReactions(tick);
    if (status !== lastChatState && tick - lastChatTick > 15) {
      lastChatState = status; lastChatTick = tick;
      if (status === 'Panik-Crash') {
        addChatMessage('PrajuritCutloss', 'PANIK CRASH 🔴', 'badge-cl', pick(CHAT_POOLS.crash));
        setTimeout(() => addChatMessage('NyangkutDiPucuk', 'NYANGKUT 😭', 'badge-nyangkut', pick(CHAT_POOLS.crash)), 600);
      } else if (status === 'Bubble') {
        addChatMessage('ScalperGarisKeras', 'BUBBLE RALLY 🚀', 'badge-tp', pick(CHAT_POOLS.bubble));
        setTimeout(() => addChatMessage('BocilCrypto', 'TO THE MOON 🌕', 'badge-fomo', pick(CHAT_POOLS.bubble)), 600);
      }
    }
    if (psych_stats && psych_stats.averaging > 0 && Math.random() < 0.12 && tick - lastChatTick > 10) {
      lastChatTick = tick;
      const upMore = num(psych_stats.averaging_up, 0) > num(psych_stats.averaging_down, psych_stats.averaging);
      addChatMessage('PejuangDividen', upMore ? 'AVG UP 📈' : 'AVG DOWN ↘️', 'badge-nyangkut', pick(upMore ? CHAT_POOLS.averagingUp : CHAT_POOLS.averaging));
    }
  }

  // ── Berita Sectors (kartu + tombol Suntik) ──
  const newsListEl = $('newsList');
  let newsBusy = false;
  function newsEmpty(text, isError) {
    const box = el('div', 'empty' + (isError ? ' empty--error' : ''));
    box.appendChild(el('p', null, text));
    return box;
  }
  async function loadNews() {
    if (newsBusy) return;
    newsBusy = true;
    newsListEl.replaceChildren(el('div', 'skeleton'), el('div', 'skeleton'), el('div', 'skeleton'));
    try {
      const res = await fetch('/api/news');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const items = await res.json();
      newsLoaded = true;
      renderNews(Array.isArray(items) ? items : []);
    } catch (e) {
      newsListEl.replaceChildren(newsEmpty('Berita tidak bisa dimuat. Server simulasi belum jalan atau Sectors offline.', true));
    } finally { newsBusy = false; }
  }
  const SENTI_LABEL = { BULLISH: 'Positif', POSITIVE: 'Positif', POSITIF: 'Positif', BEARISH: 'Negatif', NEGATIVE: 'Negatif', NEGATIF: 'Negatif', NEUTRAL: 'Netral', NETRAL: 'Netral' };
  function renderNews(items) {
    newsListEl.replaceChildren();
    if (!items.length) { newsListEl.appendChild(newsEmpty('Tidak ada berita baru dari Sectors.')); return; }
    items.forEach((item) => {
      const impact = num(item.sentiment_impact, 0);
      const card = el('article', 'news-card');
      const meta = el('div', 'news-card__meta');
      meta.appendChild(el('span', 'news-card__type', item.type === 'filing' ? 'Filing BEI' : 'Berita'));
      if (item.date) meta.appendChild(el('span', null, `· ${fmtDateID(item.date)}`));
      card.appendChild(meta);
      card.appendChild(el('div', 'news-card__title', String(item.title || '(tanpa judul)')));
      if (item.body) card.appendChild(el('div', 'news-card__body', String(item.body)));
      const foot = el('div', 'news-card__foot');
      const cls = impact > 0.2 ? 'senti--bull' : impact < -0.2 ? 'senti--bear' : 'senti--neu';
      const rawLabel = String(item.sentiment_label || '').toUpperCase().replace(/[^A-Z]/g, '');
      const label = SENTI_LABEL[rawLabel] || (impact > 0.2 ? 'Positif' : impact < -0.2 ? 'Negatif' : 'Netral');
      const senti = el('span', `senti ${cls}`, `${label} · ${fmtSigned1(impact)}`);
      senti.title = 'Perkiraan dampak ke suasana pasar (−3 sampai +3)';
      foot.appendChild(senti);
      const btn = el('button', 'btn btn--xs ' + (impact >= 0 ? 'btn--teal' : 'btn--red'), 'Suntik ke pasar');
      btn.type = 'button';
      btn.title = 'Suntik sentimen berita ini ke pasar simulasi';
      btn.addEventListener('click', () => injectNews(item, btn));
      foot.appendChild(btn);
      card.appendChild(foot);
      newsListEl.appendChild(card);
    });
  }
  function injectNews(item, btn) {
    const strength = num(item.sentiment_impact, 1.0);
    const title = String(item.title || 'Berita');
    if (!send({ cmd: 'inject_news_sentiment', title, strength })) { toast('error', 'Tidak terhubung', 'Server simulasi belum tersambung.'); return; }
    flash(btn);
    const isBull = strength >= 0;
    const short = title.length > 65 ? title.substring(0, 65) + '…' : title;
    addChatMessage(isBull ? 'ScalperGarisKeras' : 'PrajuritCutloss', isBull ? 'HAKA 🚀' : 'HAKI 🚨', isBull ? 'badge-tp' : 'badge-cl',
      isBull ? `🔥 BREAKING NEWS: "${short}" HAKA {T} berjamaah capt!!` : `🚨 BAD NEWS ALERT: "${short}" Buang kiri {T} woy sebelum ARB!`);
  }

  // ═══════════════════════════ 10b. Input berita ═══════════════════════════
  // Pengguna menempelkan link (atau isi) berita → POST /api/news/analyze (server membaca judul &
  // ringkasan, menebak saham yang dibahas, menilai sentimen + saran pergeseran nilai wajar) →
  // pratinjau yang bisa disetel → cmd inject_news_sentiment. Agen bereaksi lewat suasana pasar
  // (orang noise), nilai wajar (orang fundamentalist), dan judul berita di prompt agen Gemini.
  // Bila berita membahas saham lain, simulasi bisa diganti ke saham itu dulu (set_symbol) lalu disuntik.
  const MyNews = (() => {
    const form = $('myNewsForm'), urlIn = $('myNewsUrl'), textIn = $('myNewsText');
    const btn = $('myNewsAnalyze'), statusEl = $('myNewsStatus'), preview = $('myNewsPreview');
    const listEl = $('myNewsList'), clearBtn = $('myNewsClear');
    const STORE_KEY = 'simpasar:myNews';
    const MAX_HISTORY_ITEMS = 20;
    const round1 = (v) => Math.round(v * 10) / 10;
    const validItem = (h) => h && typeof h.title === 'string' && h.title && isNum(h.strength) && isNum(h.fpct);
    let history = [];
    try {
      const saved = JSON.parse(storageGet(STORE_KEY) || '[]');
      if (Array.isArray(saved)) history = saved.filter(validItem).slice(0, MAX_HISTORY_ITEMS);
    } catch (_) { history = []; }
    let current = null;                                  // hasil analisis yang sedang dipratinjau
    let pending = null;                                  // {item, symbol}: tunggu simbol diganti, lalu suntik
    let busy = false;

    function setStatus(text, tone) {
      statusEl.textContent = text || '';
      statusEl.className = 'mynews__status' + (tone ? ` is-${tone}` : '');
    }
    const safeUrl = (u) => (/^https?:\/\//i.test(String(u || '')) ? String(u) : '');
    const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (_) { return ''; } };
    const sentiClass = (v) => (v > 0.2 ? 'senti--bull' : v < -0.2 ? 'senti--bear' : 'senti--neu');
    const sentiLabel = (v) => (v > 0.2 ? 'Positif' : v < -0.2 ? 'Negatif' : 'Netral');

    async function analyze(e) {
      if (e) e.preventDefault();
      if (busy) return;
      const url = urlIn.value.trim(), text = textIn.value.trim();
      if (!url && !text) { setStatus('Tempel link berita atau tulis isi beritanya dulu.', 'warn'); urlIn.focus(); return; }
      busy = true; btn.disabled = true; btn.textContent = 'Membaca berita…';
      setStatus('Membaca dan menilai berita…', 'info');
      preview.hidden = true;
      try {
        const res = await fetch('/api/news/analyze', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, text }),
        });
        let data = null;
        try { data = await res.json(); } catch (_) { /* bukan JSON */ }
        if (!res.ok || !data || !data.ok) {
          setStatus((data && data.message) || `Berita tidak bisa dianalisis (HTTP ${res.status}).`, 'error');
          return;
        }
        current = {
          title: String(data.title || 'Berita'), url: safeUrl(data.url), source: String(data.source || hostOf(data.url) || 'Berita'),
          summary: String(data.summary || ''), reason: String(data.reason || ''), method: data.method === 'gemini' ? 'gemini' : 'aturan',
          category: String(data.category || 'lainnya'), warning: data.warning ? String(data.warning) : '',
          tickers: Array.isArray(data.tickers) ? data.tickers.filter((t) => t && /^[A-Z]{4}$/.test(String(t.symbol))) : [],
          primary: /^[A-Z]{4}$/.test(String(data.primary || '')) ? String(data.primary) : null,
          strength: round1(clamp(num(data.sentiment, 0), -3, 3)),
          fpct: round1(clamp(num(data.fundamental_pct, 0), -5, 5)),
        };
        setStatus(current.warning, current.warning ? 'warn' : '');
        renderPreview();
      } catch (_) {
        setStatus('Server simulasi tidak bisa dihubungi. Coba lagi setelah tersambung.', 'error');
      } finally {
        busy = false; btn.disabled = false; btn.textContent = 'Analisis berita';
      }
    }

    function effectText(c) {
      const parts = [];
      if (Math.abs(c.strength) >= 0.05) {
        parts.push(`Suasana pasar ${fmtSigned1(c.strength)}: orang noise condong ${c.strength > 0 ? 'beli' : 'jual'}${Math.abs(c.strength) >= 1.5 ? ' kuat' : ''}`);
      } else {
        parts.push('Suasana pasar tidak berubah');
      }
      if (Math.abs(c.fpct) >= 0.05) {
        const after = isNum(S.fundamental) ? S.fundamental * (1 + c.fpct / 100) : NaN;
        parts.push(`nilai wajar ${fmtPct(c.fpct, 1)}${isNum(after) ? ` (${fmtPrice(S.fundamental)} → ${fmtPrice(after)})` : ''}: orang fundamentalist menilai ulang harga yang pantas`);
      }
      return parts.join(' · ') + '.';
    }

    function sliderRow(label, min, max, step, value, fmt, onInput) {
      const wrap = el('label', 'mynews-card__ctl');
      const head = el('span', 'mynews-card__ctl-head');
      const out = el('b', 'mono', fmt(value));
      head.append(el('span', null, label), out);
      const input = el('input');
      input.type = 'range'; input.min = String(min); input.max = String(max); input.step = String(step); input.value = String(value);
      input.addEventListener('input', () => { const v = round1(Number(input.value)); out.textContent = fmt(v); onInput(v); });
      wrap.append(head, input);
      return wrap;
    }

    function renderPreview() {
      const c = current;
      preview.replaceChildren();
      const meta = el('div', 'news-card__meta');
      meta.appendChild(el('span', 'news-card__type', c.source));
      meta.appendChild(el('span', 'mynews-card__method', c.method === 'gemini' ? '· dinilai Gemini' : '· dinilai otomatis'));
      if (c.category && c.category !== 'lainnya') meta.appendChild(el('span', null, `· ${c.category}`));
      preview.appendChild(meta);
      if (c.url) {
        const a = el('a', 'news-card__title mynews-card__title', c.title);
        a.href = c.url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        preview.appendChild(a);
      } else {
        preview.appendChild(el('div', 'news-card__title mynews-card__title', c.title));
      }
      if (c.summary && c.summary !== c.title) preview.appendChild(el('div', 'news-card__body', c.summary));

      const tick = el('div', 'mynews-card__tickers');
      tick.appendChild(el('span', 'mynews-card__k', 'Saham dibahas'));
      if (c.tickers.length) {
        c.tickers.forEach((t) => {
          const chip = el('span', 'mynews-card__tick mono' + (t.symbol === c.primary ? ' is-primary' : ''), t.symbol);
          if (t.name) chip.title = t.name;
          tick.appendChild(chip);
        });
      } else {
        tick.appendChild(el('span', 'mynews-card__none', 'tidak terdeteksi, dianggap berita pasar umum'));
      }
      preview.appendChild(tick);

      const senti = el('span', `senti ${sentiClass(c.strength)}`, `${sentiLabel(c.strength)} · ${fmtSigned1(c.strength)}`);
      senti.title = 'Perkiraan dampak ke suasana pasar (−3 sampai +3)';
      const sentiRow = el('div', 'mynews-card__senti');
      sentiRow.appendChild(senti);
      if (c.reason) sentiRow.appendChild(el('span', 'mynews-card__reason', c.reason));
      preview.appendChild(sentiRow);

      const effect = el('p', 'mynews-card__effect', effectText(c));
      const ctl = el('div', 'mynews-card__ctls');
      ctl.appendChild(sliderRow('Kekuatan ke suasana pasar', -3, 3, 0.1, c.strength, (v) => fmtSigned1(v), (v) => {
        c.strength = v; senti.className = `senti ${sentiClass(v)}`; senti.textContent = `${sentiLabel(v)} · ${fmtSigned1(v)}`; effect.textContent = effectText(c);
      }));
      ctl.appendChild(sliderRow('Geser nilai wajar', -5, 5, 0.5, c.fpct, (v) => fmtPct(v, 1), (v) => { c.fpct = v; effect.textContent = effectText(c); }));
      preview.appendChild(ctl);
      preview.appendChild(effect);

      const actions = el('div', 'mynews-card__actions');
      const sym = S.symbol.symbol;
      if (c.primary && c.primary !== sym) {
        preview.appendChild(el('p', 'mynews-card__note', `Berita ini membahas ${c.primary}, sedangkan simulasi sedang memakai ${sym}.`));
        const go = el('button', 'btn btn--primary btn--sm', `Simulasikan ${c.primary} + suntik`);
        go.type = 'button';
        go.title = `Ganti saham simulasi ke ${c.primary} (harga penutupan terakhir dari Sectors), lalu suntik berita ini`;
        go.disabled = !!pending;
        go.addEventListener('click', () => switchAndInject(Object.assign({}, c), go));
        const here = el('button', 'btn btn--outline btn--sm', `Suntik ke ${sym}`);
        here.type = 'button';
        here.addEventListener('click', () => inject(Object.assign({}, c), here));
        actions.append(go, here);
      } else {
        const b = el('button', 'btn btn--primary btn--sm', 'Suntik ke pasar');
        b.type = 'button';
        b.addEventListener('click', () => inject(Object.assign({}, c), b));
        actions.appendChild(b);
      }
      preview.appendChild(actions);
      preview.hidden = false;
    }

    function inject(item, button) {
      const payload = {
        cmd: 'inject_news_sentiment', title: item.title, strength: round1(item.strength), fundamental_pct: round1(item.fpct),
        origin: 'user', url: item.url || '', ticker: item.primary || '',
      };
      if (!sendOrWarn(payload)) return false;
      S.ownNewsAt = Date.now();                            // toast "Berita yang kamu input" hanya untuk pengirim
      if (button) flash(button);
      pulseMood();
      const short = item.title.length > 65 ? item.title.substring(0, 65) + '…' : item.title;
      if (item.strength >= 0.2) addChatMessage('ScalperGarisKeras', 'HAKA 🚀', 'badge-tp', `Ada yang share berita: "${short}" Gas HAKA {T} capt!! 🔥`);
      else if (item.strength <= -0.2) addChatMessage('PrajuritCutloss', 'HAKI 🚨', 'badge-cl', `Baru baca: "${short}" Buang kiri {T} dulu woy sebelum ARB!`);
      else addChatMessage('DokterSaham', 'ANALIS 📊', 'badge-fomo', `Berita "${short}" netral sih, {T} kayaknya sideways dulu.`);
      remember(item);
      setStatus(`Disuntik ke ${S.symbol.symbol}. Lihat reaksinya di chart dan grid investor.`, 'ok');
      return true;
    }

    function clearPending() { if (pending) { clearTimeout(pending.timer); pending = null; } }
    function switchAndInject(item, btn) {
      if (pending) return;
      if (!sendOrWarn({ cmd: 'set_symbol', symbol: item.primary })) return;
      pending = {
        item, symbol: item.primary,
        timer: setTimeout(() => {                          // koneksi putus / server diam: jangan menunggu selamanya
          if (!pending) return;
          const sym = pending.symbol; pending = null;
          setStatus(`Server belum menjawab penggantian ke ${sym}. Coba lagi.`, 'warn');
          if (current && !preview.hidden) renderPreview();
        }, 15000),
      };
      if (btn) btn.disabled = true;
      setStatus(`Mengganti simulasi ke ${item.primary}…`, 'info');
    }
    // Dipanggil dari onSymbolChanged: snapshot simbol baru menyusul, beri jeda sebentar lalu suntik.
    function onSymbolChanged(sym) {
      const job = pending && pending.symbol === sym ? pending : null;
      clearPending();
      if (preview && !preview.hidden && current) renderPreview();          // tombol & teks efek mengikuti simbol aktif
      if (job) setTimeout(() => { if (inject(job.item, null) && current) renderPreview(); }, 700);
    }
    function onSymbolError(msg) {
      if (!pending || (msg.symbol && String(msg.symbol).toUpperCase() !== pending.symbol)) return false;
      const sym = pending.symbol;
      clearPending();
      setStatus(`${sym} tidak bisa disimulasikan: ${String(msg.message || 'harga tidak tersedia')}. Berita tetap bisa disuntik ke ${S.symbol.symbol}.`, 'error');
      if (current && !preview.hidden) renderPreview();
      return true;
    }

    function remember(item) {
      const entry = {
        title: item.title.slice(0, 300), url: item.url || '', source: item.source || '', primary: item.primary || '',
        strength: round1(item.strength), fpct: round1(item.fpct), symbol: S.symbol.symbol, at: Date.now(),
      };
      history = [entry].concat(history.filter((h) => !(h.title === entry.title && h.url === entry.url))).slice(0, MAX_HISTORY_ITEMS);
      storageSet(STORE_KEY, JSON.stringify(history));
      renderList();
    }
    function renderList() {
      listEl.replaceChildren();
      clearBtn.hidden = !history.length;
      if (!history.length) {
        listEl.appendChild(el('p', 'mynews__empty', 'Belum ada berita yang kamu suntik. Tempel link berita saham di atas, atau tulis isi beritanya.'));
        return;
      }
      history.forEach((h) => {
        const row = el('div', 'mynews-item');
        const top = el('div', 'mynews-item__top');
        const when = new Date(h.at);
        top.append(
          el('span', `senti ${sentiClass(h.strength)}`, fmtSigned1(h.strength)),
          el('span', 'mynews-item__meta', `${h.source || 'Teks kamu'} · ${pad2(when.getHours())}:${pad2(when.getMinutes())} · ${h.symbol}` +
            (Math.abs(h.fpct) >= 0.05 ? ` · nilai wajar ${fmtPct(h.fpct, 1)}` : '')),
        );
        row.appendChild(top);
        const url = safeUrl(h.url);
        if (url) {
          const a = el('a', 'mynews-item__title', h.title);
          a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
          row.appendChild(a);
        } else {
          row.appendChild(el('div', 'mynews-item__title', h.title));
        }
        const again = el('button', 'btn btn--ghost btn--xs', 'Suntik lagi');
        again.type = 'button';
        again.title = 'Suntik ulang berita ini ke saham yang sedang disimulasikan';
        again.addEventListener('click', () => inject({ title: h.title, url, source: h.source, primary: h.primary, strength: h.strength, fpct: h.fpct }, again));
        row.appendChild(again);
        listEl.appendChild(row);
      });
    }

    form.addEventListener('submit', analyze);
    clearBtn.addEventListener('click', () => { history = []; storageSet(STORE_KEY, '[]'); renderList(); });
    renderList();
    return { onSymbolChanged, onSymbolError, refresh: renderList };
  })();

  // ═══════════════════════════ 10c. Panduan (tur langkah demi langkah) ═══════════════════════════
  // Kartu bernomor yang menyorot satu fitur per langkah. Muncul otomatis sekali (localStorage), bisa
  // dibuka lagi lewat tombol "Panduan" atau tombol ?. Langkah yang targetnya tidak terlihat tetap
  // ditampilkan di tengah layar tanpa sorotan; panel kanan yang sedang ditutup dibuka sebentar.
  const Tour = (() => {
    const DONE_KEY = 'simpasar:tourDone';
    const STEPS = [
      { sel: '#symbolButton', title: 'Pilih saham',
        text: 'Klik di sini (atau tekan /) untuk memilih saham BEI mana saja, misalnya BBCA atau GOTO. Harga awalnya diambil dari harga penutupan terakhir di bursa lewat Sectors API.' },
      { sel: '#statusBadge', title: 'Status pasar',
        text: 'Normal, Bubble (harga jauh di atas nilai wajar), atau Panik-Crash (jatuh jauh di bawahnya). Status ini muncul sendiri dari reaksi 100 investor tiruan.' },
      { sel: '#simClock', title: 'Jam bursa simulasi',
        text: '1 langkah simulasi = 1 menit jam bursa: Sesi 1 pukul 09.00–12.00, Sesi 2 pukul 13.30–16.00. Akhir pekan dilompati.' },
      { sel: '#statsRow', title: 'Angka penting',
        text: 'Harga sekarang, nilai wajar, selisih keduanya, tertinggi dan terendah hari ini, suasana pasar (−3 panik sampai +3 euforia), serta batas harian ARA/ARB.' },
      { sel: ['#tfGroup', '#chartWrap'], title: 'Chart dan timeframe',
        text: 'Pilih lebar candle 1, 5, 15, atau 30 menit. Di chart: scroll untuk zoom, seret untuk menggeser, klik ganda untuk kembali ke tampilan awal.' },
      { sel: '#dockActions', title: 'Atur simulasinya',
        text: 'Sebar rumor (R) menaikkan suasana pasar, Kabar buruk (K) menurunkannya; orang noise bereaksi lebih dulu. Jeda (Spasi) dan Reset juga ada di sini.' },
      { sel: '#speedGroup', title: 'Kecepatan',
        text: 'Percepat atau perlambat simulasi dari 0,5x sampai 5x.' },
      { sel: '#dockMix', title: 'Komposisi investor',
        text: 'Geser porsi tipe orang (fundamentalist dan chartist, sisanya noise) dan sifat orang (discipline dan denial, sisanya averager). Ke-100 investor langsung dibentuk ulang dan posisinya mulai dari nol, sedangkan harga dan chart tetap berjalan.' },
      { sel: '#crowdBlock', title: '100 investor tiruan',
        text: 'Tiap kotak satu investor. Mode Aksi: hijau beli, hijau tua sedang untung, merah jual, merah tua sedang rugi, gelap diam. Ganti ke Tipe orang atau Sifat orang, lalu sorot atau ketuk kotak untuk melihat detailnya.' },
      { sel: '#pnlBlock', title: 'Untung / rugi',
        text: 'Berapa investor yang pegang saham, sedang untung, rugi berat, dan yang nambah posisi saat harga naik atau turun.' },
      { sel: '#rightHead', title: 'Stream ritel, Berita Sectors, dan Input berita', openRight: true,
        // Teks mengikuti tata letak: layar lebar (panel di samping chart) vs ponsel/tablet tegak (panel di bawah).
        text: () => 'Stream ritel berisi komunitas fiktif yang ikut bereaksi. Berita Sectors berisi berita BEI asli. Di Input berita, tempel link berita sendiri, lalu lihat agen bereaksi. ' +
          (window.matchMedia('(max-width: 1023px)').matches
            ? 'Tombol panah di ujung baris tab melipat panel ini sampai tinggal baris tabnya; ketuk tombol itu atau salah satu tab untuk membukanya lagi.'
            : 'Tombol panah › di ujung baris tab menutup panel ini menjadi strip tipis supaya chart lebih lebar; buka lagi lewat tombol ‹ di strip itu atau klik nama tabnya.') },
      { sel: '#helpBtn', title: 'Selesai, selamat bereksperimen',
        text: 'Buka panduan ini lagi kapan saja lewat tombol Panduan atau tombol ?. Mode gelap/terang ada di sebelahnya.' },
    ];
    let root = null, spot = null, card = null, idx = 0, isOpen = false, lastFocus = null, openedRight = false;
    let savedScroll = null;                              // posisi gulir sebelum panduan (dikembalikan saat ditutup)

    function build() {
      root = el('div', 'tour');
      root.hidden = true;
      root.setAttribute('role', 'dialog');
      root.setAttribute('aria-modal', 'true');
      root.setAttribute('aria-labelledby', 'tourTitle');
      root.tabIndex = -1;                                  // klik di luar tombol → fokus tetap di panduan (Esc/panah jalan)
      spot = el('div', 'tour__spot');
      card = el('div', 'tour__card');
      root.append(spot, card);
      document.body.appendChild(root);
      root.addEventListener('keydown', onKey);
      window.addEventListener('resize', () => { if (isOpen) place(); });
      window.addEventListener('scroll', () => { if (isOpen) place(); }, true);
    }
    function visible(node) {
      if (!node) return false;
      const r = node.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(node).visibility !== 'hidden';
    }
    function target() {
      for (const sel of [].concat(STEPS[idx].sel)) { const n = document.querySelector(sel); if (visible(n)) return n; }
      return null;
    }
    function button(text, cls, fn) { const b = el('button', `btn ${cls}`, text); b.type = 'button'; b.addEventListener('click', fn); return b; }
    function render() {
      const step = STEPS[idx];
      // Panel kanan sedang ditutup: buka sebentar untuk langkah itu, lalu tutup lagi setelahnya.
      if (step.openRight && !RightPanel.isOpen()) { RightPanel.setOpen(true, { instant: true }); openedRight = true; }
      card.replaceChildren();
      card.appendChild(el('div', 'tour__step mono', `Langkah ${idx + 1} dari ${STEPS.length}`));
      const h = el('h2', 'tour__title', step.title);
      h.id = 'tourTitle';
      card.appendChild(h);
      card.appendChild(el('p', 'tour__text', typeof step.text === 'function' ? step.text() : step.text));
      const dots = el('div', 'tour__dots');
      STEPS.forEach((_, i) => dots.appendChild(el('i', i === idx ? 'is-on' : null)));
      card.appendChild(dots);
      const nav = el('div', 'tour__nav');
      nav.appendChild(button('Lewati', 'btn--ghost btn--sm', () => close()));
      const right = el('div', 'tour__nav-r');
      if (idx > 0) right.appendChild(button('Kembali', 'btn--outline btn--sm', () => go(idx - 1)));
      const next = button(idx === STEPS.length - 1 ? 'Selesai' : 'Lanjut', 'btn--primary btn--sm', () => (idx === STEPS.length - 1 ? close() : go(idx + 1)));
      right.appendChild(next);
      nav.appendChild(right);
      card.appendChild(nav);
      const n = target();
      if (n) {
        n.scrollIntoView({ block: 'nearest', inline: 'nearest' });   // juga di dalam panel kiri yang bergulir
        const r = n.getBoundingClientRect(), room = card.offsetHeight + 30, vh = window.innerHeight;
        if (r.bottom + room > vh && r.top - room < 0) n.scrollIntoView({ block: 'start' });   // target tinggi (ponsel)
      }
      place();
      next.focus({ preventScroll: true });
    }
    function place() {
      const n = target();
      const vw = window.innerWidth, vh = window.innerHeight, pad = 6, gap = 12, m = 12;
      const cw = card.offsetWidth, ch = card.offsetHeight;
      if (!n) {
        spot.classList.add('is-none');
        card.style.left = `${Math.max(m, (vw - cw) / 2)}px`;
        card.style.top = `${Math.max(m, (vh - ch) / 2)}px`;
        return;
      }
      spot.classList.remove('is-none');
      const r = n.getBoundingClientRect();
      const x = Math.max(4, r.left - pad), y = Math.max(4, r.top - pad);
      spot.style.left = `${x}px`; spot.style.top = `${y}px`;
      spot.style.width = `${Math.min(vw - 4, r.right + pad) - x}px`;
      spot.style.height = `${Math.min(vh - 4, r.bottom + pad) - y}px`;
      let top = r.bottom + pad + gap;
      if (top + ch > vh - m) top = r.top - pad - gap - ch;          // tidak muat di bawah → di atas
      if (top < m) {                                                // tidak muat juga → di samping
        let left = r.right + pad + gap;
        if (left + cw > vw - m) left = r.left - pad - gap - cw;
        if (left < m) {                                             // tidak ada ruang di samping (ponsel) → dasar layar
          card.style.left = `${Math.max(m, (vw - cw) / 2)}px`;
          card.style.top = `${Math.max(m, vh - ch - m)}px`;
          return;
        }
        card.style.left = `${clamp(left, m, Math.max(m, vw - cw - m))}px`;
        card.style.top = `${clamp(r.top, m, Math.max(m, vh - ch - m))}px`;
        return;
      }
      card.style.top = `${top}px`;
      card.style.left = `${clamp(r.left + r.width / 2 - cw / 2, m, Math.max(m, vw - cw - m))}px`;
    }
    function go(i) {
      if (openedRight && !STEPS[i].openRight) { RightPanel.setOpen(false, { instant: true }); openedRight = false; }
      idx = clamp(i, 0, STEPS.length - 1);
      render();
    }
    function open(start) {
      if (!root) build();
      if (Search.isOpen()) Search.close();
      if (!tooltip.hidden) tooltip.hidden = true;
      lastFocus = document.activeElement;
      const lp = $('leftPanel');
      savedScroll = { left: lp ? lp.scrollTop : 0, page: window.scrollY };
      isOpen = true;
      root.hidden = false;
      document.body.classList.add('tour-open');
      go(start || 0);
    }
    function close() {
      if (!isOpen) return;
      isOpen = false;
      root.hidden = true;
      document.body.classList.remove('tour-open');
      if (openedRight) { RightPanel.setOpen(false, { instant: true }); openedRight = false; }
      storageSet(DONE_KEY, '1');
      if (savedScroll) { const lp = $('leftPanel'); if (lp) lp.scrollTop = savedScroll.left; window.scrollTo(0, savedScroll.page); savedScroll = null; }
      if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); if (idx < STEPS.length - 1) go(idx + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); if (idx > 0) go(idx - 1); }
      else if (e.key === 'Tab') {                                   // fokus tetap di dalam kartu
        const f = Array.from(card.querySelectorAll('button'));
        if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (!f.includes(document.activeElement)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
        else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    function maybeAutoStart() { if (storageGet(DONE_KEY) !== '1' && !isOpen) open(0); }
    return { open, close, isOpen: () => isOpen, maybeAutoStart };
  })();
  $('helpBtn').addEventListener('click', () => Tour.open(0));

  // ═══════════════════════════ 11. Modal pencarian simbol ═══════════════════════════
  const POPULAR_FALLBACK = [
    Object.assign({}, IHSG_ITEM),
    { symbol: 'BBCA', name: 'PT Bank Central Asia Tbk.', sector: 'financials' },
    { symbol: 'BBRI', name: 'PT Bank Rakyat Indonesia (Persero) Tbk.', sector: 'financials' },
    { symbol: 'BMRI', name: 'PT Bank Mandiri (Persero) Tbk.', sector: 'financials' },
    { symbol: 'BBNI', name: 'PT Bank Negara Indonesia (Persero) Tbk.', sector: 'financials' },
    { symbol: 'TLKM', name: 'PT Telkom Indonesia (Persero) Tbk.', sector: 'infrastructures' },
    { symbol: 'ASII', name: 'PT Astra International Tbk.', sector: 'industrials' },
    { symbol: 'GOTO', name: 'PT GoTo Gojek Tokopedia Tbk.', sector: 'technology' },
    { symbol: 'TPIA', name: 'PT Chandra Asri Pacific Tbk.', sector: 'basic-materials' },
    { symbol: 'BREN', name: 'PT Barito Renewables Energy Tbk.', sector: 'infrastructures' },
    { symbol: 'AMMN', name: 'PT Amman Mineral Internasional Tbk.', sector: 'basic-materials' },
    { symbol: 'UNVR', name: 'PT Unilever Indonesia Tbk.', sector: 'consumer-non-cyclicals' },
  ].map((c) => Object.assign({ kind: c.kind || 'stock', sector_name: SECTOR_NAMES[c.sector] || '' }, c));

  const Search = (() => {
    const modal = $('searchModal'), input = $('searchInput'), list = $('searchResults'), stateEl = $('searchState');
    const resultsView = $('searchResultsView'), confirmView = $('searchConfirmView');
    const sectionTitle = $('searchSectionTitle'), sectionHint = $('searchSectionHint');
    // qSeq = urutan query pencarian, cSeq = urutan pengambilan harga (dipisah supaya membuka kartu
    // konfirmasi tidak membuang hasil query yang masih berjalan). resultsQuery = query milik `results`.
    let isOpen = false, results = [], resultsQuery = null, active = -1, qSeq = 0, cSeq = 0, debounceT = null, ctrl = null;
    let enterWaiting = false;
    let companies = null, companiesPromise = null;     // prefetch /api/stocks (sekali) untuk hitungan & fallback lokal
    let current = null;                                  // {item, data, manual} pada kartu konfirmasi
    let lastSpark = null;                                // {hist, chg} sparkline terakhir (digambar ulang saat tema berganti)
    let pendingTimer = null, lastFocus = null;

    function open() {
      if (isOpen) return;
      isOpen = true;
      lastFocus = document.activeElement;
      modal.hidden = false;
      showResultsView();
      input.value = '';
      input.focus();
      query('');
      ensureCompanies();
    }
    function close() {
      if (!isOpen) return;
      isOpen = false;
      modal.hidden = true;
      if (ctrl) { ctrl.abort(); ctrl = null; }
      clearTimeout(debounceT); debounceT = null;
      if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
    }
    function toggle() { if (isOpen) close(); else open(); }
    function showResultsView() { resultsView.hidden = false; confirmView.hidden = true; current = null; }

    function ensureCompanies() {
      if (companies || companiesPromise) return companiesPromise;
      companiesPromise = fetch('/api/stocks').then((r) => (r.ok ? r.json() : null)).then((d) => {
        if (d && Array.isArray(d.companies)) {
          companies = d.companies;
          setText('searchCount', `${NF0.format(d.count || companies.length)} emiten BEI · sumber Sectors`);
        }
        return companies;
      }).catch(() => null);
      return companiesPromise;
    }

    // Fallback lokal bila endpoint pencarian belum tersedia: ticker/nama mengandung query.
    function localSearch(q) {
      const qq = q.trim().toLowerCase();
      if (!qq) return POPULAR_FALLBACK.slice();
      const out = [];
      if (/^(ihsg|composite|indeks|index|gabungan)/.test(qq)) out.push(Object.assign({}, IHSG_ITEM));
      const src = companies || POPULAR_FALLBACK.filter((c) => c.kind !== 'index');
      const tokens = qq.split(/\s+/).filter(Boolean);
      const scored = [];
      for (const c of src) {
        const sym = c.symbol.toLowerCase(), name = c.name.toLowerCase();
        let score = 0;
        if (sym === qq) score = 1; else if (sym.startsWith(qq)) score = 0.9;
        else if (tokens.every((t) => name.includes(t))) score = 0.7;
        if (score) scored.push(Object.assign({ score, match: score >= 0.9 ? 'ticker' : 'name', kind: 'stock', sector_name: SECTOR_NAMES[c.sector] || '' }, c));
      }
      scored.sort((a, b) => b.score - a.score || a.symbol.localeCompare(b.symbol));
      return out.concat(scored).slice(0, 12);
    }

    function setState(text, isError) {
      list.replaceChildren();
      stateEl.hidden = false;
      stateEl.className = 'results-state' + (isError ? ' results-state--error' : '');
      stateEl.textContent = text;
    }

    async function query(q) {
      const mySeq = ++qSeq;
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      const trimmed = q.trim();
      sectionTitle.textContent = trimmed ? 'Hasil pencarian' : 'Populer';
      sectionHint.textContent = trimmed ? 'Mencari…' : 'IHSG + emiten yang sering dicari';
      let items = null, err = null;
      try {
        const res = await fetch(`/api/stocks/search?q=${encodeURIComponent(trimmed)}&limit=12`, { signal: ctrl.signal });
        if (res.ok) { const d = await res.json(); items = Array.isArray(d.results) ? d.results : []; }
        else err = `HTTP ${res.status}`;
      } catch (e) {
        if (e && e.name === 'AbortError') return;
        err = 'server tidak merespons';
      }
      if (mySeq !== qSeq) return;                          // ada query lebih baru
      if (!items) {
        await ensureCompanies();
        if (mySeq !== qSeq) return;
        items = localSearch(trimmed);
        sectionHint.textContent = companies ? `pencarian lokal (${err})` : `daftar cadangan (${err})`;
      } else {
        sectionHint.textContent = trimmed ? `${items.length} hasil` : 'IHSG + emiten yang sering dicari';
      }
      renderResults(items, trimmed);
    }

    function renderResults(items, q) {
      results = items;
      resultsQuery = q;
      active = items.length ? 0 : -1;
      list.replaceChildren();
      if (!items.length) { setState(`Tidak ada emiten yang cocok untuk “${q}”. Coba ticker (BBCA), nama (bank bca), atau sektor (batu bara).`); return; }
      stateEl.hidden = true;
      items.forEach((it, i) => {
        const li = el('li', 'result' + (i === active ? ' is-active' : ''));
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', String(i === active));
        li.id = `result-${i}`;
        li.appendChild(el('span', 'result__ticker', String(it.symbol || '')));
        li.appendChild(el('span', 'result__name', String(it.name || '')));
        const right = el('span', 'result__right');
        if (it.match === 'fuzzy') right.appendChild(el('span', 'result__match', 'mirip'));
        else if (it.match === 'alias') right.appendChild(el('span', 'result__match', 'alias'));
        else if (it.match === 'sector') right.appendChild(el('span', 'result__match', 'sektor'));
        const chip = el('span', 'chip chip--sm');
        if (it.kind === 'index') setChip(chip, 'index', 'Indeks'); else setChip(chip, it.sector, it.sector_name);
        right.appendChild(chip);
        right.appendChild(el('kbd', 'result__enter', '↵'));
        li.appendChild(right);
        li.addEventListener('mousemove', () => setActive(i));
        li.addEventListener('click', () => select(i));
        list.appendChild(li);
      });
      input.setAttribute('aria-activedescendant', active >= 0 ? `result-${active}` : '');
    }
    function setActive(i) {
      if (i === active || i < 0 || i >= results.length) return;
      active = i;
      Array.from(list.children).forEach((li, k) => { li.classList.toggle('is-active', k === i); li.setAttribute('aria-selected', String(k === i)); });
      const li = list.children[i];
      if (li && li.scrollIntoView) li.scrollIntoView({ block: 'nearest' });
      input.setAttribute('aria-activedescendant', `result-${i}`);
    }
    function move(delta) { if (results.length) setActive((active + delta + results.length) % results.length); }
    function select(i) { const it = results[i]; if (it) showConfirm(it); }
    // Enter di daftar hasil: bila query yang diketik belum selesai dicari (debounce/fetch masih berjalan),
    // tunggu hasil query SEKARANG dulu — jangan memilih hasil lama (mis. "tlkm" → IHSG dari daftar Populer).
    async function selectCurrent() {
      if (debounceT !== null || resultsQuery !== input.value.trim()) {
        clearTimeout(debounceT); debounceT = null;
        if (enterWaiting) return;
        enterWaiting = true;
        try { await query(input.value); } finally { enterWaiting = false; }
        if (!isOpen || !confirmView.hidden || resultsQuery !== input.value.trim()) return;
      }
      select(active >= 0 ? active : 0);
    }

    // ── Kartu konfirmasi harga Sectors ──
    const cTicker = $('confirmTicker'), cName = $('confirmName'), cSector = $('confirmSector');
    const cPrice = $('confirmPrice'), cChange = $('confirmChange'), cDate = $('confirmDate'), cStatus = $('confirmStatus');
    const cManual = $('confirmManual'), cManualInput = $('manualPrice'), cStart = $('confirmStart'), cSpark = $('confirmSpark');
    const cPriceLabel = $('confirmPriceLabel'), cCredit = $('confirmCredit');
    const CREDIT_DEFAULT = '1 kredit Sectors per emiten (di-cache 6 jam)';

    function setStatus(text, tone) { cStatus.textContent = text || ''; cStatus.className = 'confirm__status' + (tone ? ` confirm__status--${tone}` : ''); }
    function setCredit(text) { if (!cCredit) return; cCredit.textContent = text || ''; cCredit.hidden = !text; }
    function showManual(kind, value, label) {
      cManual.hidden = false;
      cManualInput.value = value == null ? '' : String(value);
      cManualInput.placeholder = kind === 'index' ? 'mis. 7000' : 'mis. 1000';
      cManualInput.step = kind === 'index' ? '0.01' : '1';
      cManualInput.min = '1';
      const lbl = cManual.querySelector('label');
      if (lbl) lbl.textContent = label || 'Masukkan harga awal manual';
      const prefix = cManual.querySelector('.confirm__manual-prefix');
      if (prefix) prefix.hidden = kind === 'index';           // IHSG dalam poin, bukan rupiah
      current.manual = true;
      cStart.disabled = false; cStart.textContent = 'Mulai';
      cManualInput.focus(); cManualInput.select();
    }

    async function showConfirm(item) {
      const kind = item.kind === 'index' || item.symbol === 'IHSG' ? 'index' : 'stock';
      current = { item: Object.assign({}, item, { kind }), data: null, manual: false, sending: false };
      resultsView.hidden = true; confirmView.hidden = false;
      cTicker.textContent = item.symbol;
      cName.textContent = String(item.name || '');
      if (kind === 'index') setChip(cSector, 'index', 'Indeks'); else setChip(cSector, item.sector, item.sector_name);
      cPrice.textContent = '—'; cPrice.classList.add('is-muted');
      cChange.textContent = ''; cChange.className = ''; cDate.textContent = '';
      cSpark.replaceChildren();
      lastSpark = null;
      cManual.hidden = true;
      cPriceLabel.textContent = 'Harga terakhir Sectors';
      setCredit(CREDIT_DEFAULT);
      cStart.disabled = true; cStart.textContent = 'Mulai simulasi di harga ini';
      const loading = el('span', 'spinner'); loading.setAttribute('aria-hidden', 'true');
      cStatus.replaceChildren(loading, document.createTextNode(' Mengambil harga terakhir dari Sectors…'));
      cStatus.className = 'confirm__status';
      const mySeq = ++cSeq;
      let data = null, httpErr = null;
      try {
        const res = await fetch(`/api/stocks/${encodeURIComponent(item.symbol)}/price`);
        data = await res.json().catch(() => null);
        if (!res.ok && !(data && data.status)) httpErr = `HTTP ${res.status}`;
      } catch (e) { httpErr = 'server tidak merespons'; }
      if (mySeq !== cSeq || !current || current.item.symbol !== item.symbol || confirmView.hidden) return;
      current.data = data;
      const price = data ? Number(data.price) : NaN;
      const status = data ? String(data.status || '') : '';
      if (status === 'unknown') {
        setStatus(`Simbol ${item.symbol} tidak dikenal server.`, 'error');
        setCredit('');
        cStart.disabled = true;
        return;
      }
      if (isNum(price) && price > 0 && status === 'fallback') {
        // Nilai cadangan server (mis. IHSG saat offline): BUKAN data bursa → tanpa perubahan harian/sparkline,
        // diisikan sebagai harga manual yang bisa diubah, dan tidak disimpan sebagai detail Sectors.
        cPriceLabel.textContent = 'Nilai cadangan (bukan data Sectors)';
        cPrice.textContent = fmtByKind(kind, price, true); cPrice.classList.add('is-muted');
        setStatus('Sectors sedang offline — ini nilai cadangan, bukan harga hari ini. Ubah bila perlu:', 'warn');
        setCredit('');
        showManual(kind, price, 'Harga awal (boleh diubah)');
        return;
      }
      if (isNum(price) && price > 0) {
        const stale = status === 'cache' && !!data.stale;
        cPriceLabel.textContent = stale ? 'Harga Sectors (cache lama)' : 'Harga terakhir Sectors';
        cPrice.textContent = fmtByKind(kind, price, true); cPrice.classList.remove('is-muted');
        const chg = Number(data.change), pct = Number(data.change_pct);
        if (isNum(chg) && isNum(pct)) { cChange.textContent = `${fmtSignedByKind(kind, chg)} (${fmtPct(pct)})`; setTone(cChange, chg); }
        cDate.textContent = data.date ? `· ${fmtDateID(data.date)}` : '';
        const hist = Array.isArray(data.history) ? data.history : [];
        if (hist.length >= 3) { lastSpark = { hist, chg }; drawSparkline(cSpark, hist, chg); }   // 2 titik bukan tren
        const age = data.fetched_at != null ? fmtAge(data.fetched_at) : '';
        if (stale) {
          const msg = String(data.message || 'Memakai cache lama karena Sectors tidak dapat dihubungi').replace(/[.!]?\s*$/, '');
          setStatus(`${msg}${age ? ` — data diambil ${age} lalu` : ''}. Harga bisa sudah berubah.`, 'warn');
          setCredit('Tanpa kredit — memakai cache lama');
        } else if (status === 'cache') {
          setStatus(`Dari cache Sectors${age ? ` (diambil ${age} lalu)` : ' (maks. 6 jam)'} — tidak memakai kredit.`, 'ok');
          setCredit('Tanpa kredit — data tersimpan maks. 6 jam');
        } else if (status === 'success') {
          setStatus('Harga terbaru dari Sectors (memakai 1 kredit).', 'ok');
          setCredit(CREDIT_DEFAULT);
        } else {
          setStatus('Harga terakhir dari server simulasi.', 'ok');
          setCredit('');
        }
        current.manual = false;
        cStart.disabled = false;
      } else {
        cPrice.textContent = 'Tidak tersedia'; cPrice.classList.add('is-muted');
        cPriceLabel.textContent = 'Harga terakhir Sectors';
        const msg = (data && data.message) ? String(data.message) : (httpErr ? `Harga Sectors tidak tersedia (${httpErr}).` : 'Harga Sectors tidak tersedia (mode offline).');
        setStatus(`${msg.replace(/[.!]?\s*$/, '.')} Masukkan harga awal manual:`, 'warn');
        setCredit('');
        showManual(kind, '');
      }
    }

    function drawSparkline(svg, history, chg) {
      svg.replaceChildren();
      const pts = (Array.isArray(history) ? history : []).map((h) => Number(h && (h.close != null ? h.close : h.price))).filter(Number.isFinite).slice(-30);
      if (pts.length < 3) return;
      const W = 180, H = 56, padY = 4;
      let lo = Math.min(...pts), hi = Math.max(...pts);
      if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
      const x = (i) => (i / (pts.length - 1)) * W;
      const y = (v) => padY + (1 - (v - lo) / (hi - lo)) * (H - padY * 2);
      const color = chg < 0 ? T.down : T.up;
      const d = pts.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
      const NS = 'http://www.w3.org/2000/svg';
      const area = document.createElementNS(NS, 'path');
      area.setAttribute('d', `${d} L${W},${H} L0,${H} Z`);
      area.setAttribute('class', 'spark__area'); area.setAttribute('fill', color);
      const line = document.createElementNS(NS, 'path');
      line.setAttribute('d', d); line.setAttribute('class', 'spark__line'); line.setAttribute('stroke', color);
      const dot = document.createElementNS(NS, 'circle');
      dot.setAttribute('cx', x(pts.length - 1).toFixed(1)); dot.setAttribute('cy', y(pts[pts.length - 1]).toFixed(1));
      dot.setAttribute('class', 'spark__dot'); dot.setAttribute('fill', color);
      svg.append(area, line, dot);
    }

    const MIN_PRICE = 1, MAX_PRICE = 1e9;                   // = batas server (kontrak §3)
    function start() {
      if (!current || current.sending || cStart.disabled) return;   // harga masih dimuat / simbol tidak dikenal
      const { item, data, manual } = current;
      const fundamental = manual ? Number(cManualInput.value) : Number(data && data.price);
      if (!isNum(fundamental) || fundamental < MIN_PRICE || fundamental > MAX_PRICE) {
        setStatus('Harga awal harus angka antara 1 dan 1.000.000.000.', 'error');
        if (manual) cManualInput.focus();
        return;
      }
      if (!send({ cmd: 'set_symbol', symbol: item.symbol, fundamental })) { setStatus('Tidak terhubung ke server simulasi. Coba lagi setelah tersambung.', 'error'); return; }
      current.sending = true;
      cStart.disabled = true; cStart.textContent = 'Mengirim…';
      const optNum = (v) => (v == null || v === '' ? null : (Number.isFinite(Number(v)) ? Number(v) : null));
      // Hanya data Sectors asli (baru / cache) yang disimpan sebagai detail kartu emiten — bukan nilai cadangan.
      const real = data && (data.status === 'success' || data.status === 'cache');
      if (!manual && real && optNum(data.price) > 0) {
        S.priceInfo[item.symbol] = { price: optNum(data.price), prev_close: optNum(data.prev_close), change: optNum(data.change), change_pct: optNum(data.change_pct), market_cap: optNum(data.market_cap), date: data.date || null, stale: !!data.stale, saved_at: Date.now() };
      } else {
        delete S.priceInfo[item.symbol];                    // harga manual / cadangan: detail Sectors lama tidak relevan
      }
      storageSet('simpasar:priceInfo', JSON.stringify(S.priceInfo));
      clearTimeout(pendingTimer);
      pendingTimer = setTimeout(() => {
        if (current && current.sending) { current.sending = false; cStart.disabled = false; cStart.textContent = manual ? 'Mulai' : 'Mulai simulasi di harga ini'; setStatus('Server belum menjawab. Coba lagi.', 'warn'); }
      }, 8000);
    }
    function onSymbolChanged(sym) {
      clearTimeout(pendingTimer);
      if (current && current.sending && current.item.symbol === sym) { current.sending = false; close(); }
      else if (isOpen && current && current.sending) { current.sending = false; close(); }
    }
    function onSymbolError(msg) {
      clearTimeout(pendingTimer);
      if (!isOpen || !current) return false;
      current.sending = false;
      cStart.disabled = false; cStart.textContent = 'Mulai';
      setStatus(String(msg.message || 'Simbol tidak dikenal'), 'error');
      if (msg.price_status === 'offline' || msg.price_status === 'fallback') {
        showManual(current.item.kind, '');
        setStatus(String(msg.message || 'Harga Sectors tidak tersedia'), 'error');
      }
      return true;
    }

    // Event
    input.addEventListener('input', () => {
      clearTimeout(debounceT);
      debounceT = setTimeout(() => { debounceT = null; if (!confirmView.hidden) showResultsView(); query(input.value); }, 80);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        if (e.repeat) return;                               // Enter ditahan tidak boleh langsung mengirim set_symbol
        if (!confirmView.hidden) start(); else selectCurrent();
      }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    modal.addEventListener('click', (e) => { if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-close')) close(); });
    modal.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } });
    $('confirmBack').addEventListener('click', () => {
      showResultsView();
      input.focus();
      if (resultsQuery !== input.value.trim()) { clearTimeout(debounceT); debounceT = null; query(input.value); }
    });
    cStart.addEventListener('click', start);
    cManualInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); if (!e.repeat) start(); } });

    function refreshTheme() { if (isOpen && !confirmView.hidden && lastSpark) drawSparkline(cSpark, lastSpark.hist, lastSpark.chg); }

    return { open, close, toggle, isOpen: () => isOpen, onSymbolChanged, onSymbolError, refreshTheme };
  })();

  $('symbolButton').addEventListener('click', () => Search.open());
  if ($('cardChangeBtn')) $('cardChangeBtn').addEventListener('click', () => Search.open());

  // Pintasan global: "/" atau Ctrl+K membuka pencarian (saat tidak mengetik di input lain).
  document.addEventListener('keydown', (e) => {
    if (Tour.isOpen()) return;                              // panduan menangani tombolnya sendiri
    const t = e.target;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); Search.toggle(); return; }
    if (typing || Search.isOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === '/') { e.preventDefault(); Search.open(); return; }
    if (e.key === '?') { e.preventDefault(); Tour.open(0); return; }
    if (e.repeat) return;                                   // tombol ditahan tidak menyebar rumor berkali-kali
    const k = e.key.toLowerCase();
    if (chart && chart.zoomIn) {
      if (e.key === '+' || e.key === '=') { e.preventDefault(); chart.zoomIn(); return; }
      if (e.key === '-' || e.key === '_') { e.preventDefault(); chart.zoomOut(); return; }
      if (e.key === '0') { e.preventDefault(); chart.resetView(); return; }
      if (e.key === 'ArrowLeft') { e.preventDefault(); chart.panBy(5); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); chart.panBy(-5); return; }
      if (e.key === 'ArrowUp' && chart.panPrice) { e.preventDefault(); chart.panPrice(0.1); return; }
      if (e.key === 'ArrowDown' && chart.panPrice) { e.preventDefault(); chart.panPrice(-0.1); return; }
      if (e.key === 'End') { e.preventDefault(); chart.goToLatest(); return; }
    }
    if (k === 'r') { e.preventDefault(); $('btnRumor').click(); }
    else if (k === 'k') { e.preventDefault(); $('btnPanic').click(); }
    else if (e.key === ' ' || e.code === 'Space') {
      // Spasi pada tombol/tautan yang sedang fokus tetap milik elemen itu (aksesibilitas).
      if (t && t.closest && t.closest('button, a, summary, [role="tab"], select')) return;
      e.preventDefault(); $('btnPause').click();
    }
  });

  // ═══════════════════════════ Tombol mode gelap / terang ═══════════════════════════
  const themeBtn = $('themeToggle');
  function updateThemeBtn() {
    if (!themeBtn) return;
    const label = themeName === 'dark' ? 'Ganti ke mode terang' : 'Ganti ke mode gelap';
    themeBtn.title = label;
    themeBtn.setAttribute('aria-label', label);
  }
  function applyTheme(name, persist) {
    themeName = name === 'light' ? 'light' : 'dark';
    T = THEMES[themeName];
    const root = document.documentElement;
    // Transisi warna singkat hanya saat pengguna mengganti tema (bukan saat memuat halaman).
    root.classList.add('theme-anim');
    clearTimeout(applyTheme._t);
    applyTheme._t = setTimeout(() => root.classList.remove('theme-anim'), 260);
    root.setAttribute('data-theme', themeName);
    if (persist) storageSet('simpasar:theme', themeName);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', T.metaColor);
    updateThemeBtn();
    if (!tooltip.hidden) { clearTimeout(tooltipTouchTimer); tooltipTouchTimer = null; tooltip.hidden = true; }
    Search.refreshTheme();
    // Warna canvas: chart, garis ARA/ARB, grid agen + legenda, meter suasana.
    if (chart) chart.setOptions(Object.assign({}, T.chart));
    limitLinesKey = '';
    setChartLimitLines(S.limits);
    buildCrowdLegend();
    if (S.lastAgents.length) { drawAgents(S.lastAgents); updateCrowdCounts(S.lastAgents); }
    lastMoodKey = '';
    renderSentiment(num(S.sentiment, 0));
  }
  if (themeBtn) themeBtn.addEventListener('click', () => applyTheme(themeName === 'dark' ? 'light' : 'dark', true));
  // Tab lain mengganti tema → ikut (localStorage bersama satu origin).
  window.addEventListener('storage', (e) => {
    if (e.key === 'simpasar:theme' && (e.newValue === 'light' || e.newValue === 'dark') && e.newValue !== themeName) applyTheme(e.newValue, false);
  });
  updateThemeBtn();
  { const meta = document.querySelector('meta[name="theme-color"]'); if (meta) meta.setAttribute('content', T.metaColor); }

  // ═══════════════════════════ 12. Init ═══════════════════════════
  renderSymbol();
  updatePauseBtn();
  fitAgentCanvas();
  setTimeout(initStreamMessages, 2500);                     // fallback bila server belum tersambung: stream tetap hidup
  if (chart) showOverlay('Menghubungkan ke simulator…', false);   // tanpa chart, pesan "candlechart.js tidak termuat" dipertahankan
  connect();
  setTimeout(() => Tour.maybeAutoStart(), 1200);           // pengunjung baru: panduan muncul sekali
})();

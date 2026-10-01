# SimPasar IDX 📈🏛️

> **Simulasi Pasar Modal Indonesia Berbasis Agen Hibrida (Rule-Based & Large Language Model)**  
> Platform simulasi interaktif dinamika pasar modal Indonesia yang memodelkan perilaku *emergent* seperti *bubble*, *panic selling*, dan *herding behavior* dari interaksi 100 agen otonom.

---

## 📑 Daftar Isi

- [Arsitektur & Komponen](#-arsitektur--komponen)
- [Prasyarat Sistem](#-prasyarat-sistem)
- [Konfigurasi Environment (.env)](#-konfigurasi-environment-env)
- [Cara Menjalankan Aplikasi](#-cara-menjalankan-aplikasi)
  - [Opsi 1: Menggunakan Docker Compose (Direkomendasikan)](#opsi-1-menggunakan-docker-compose-direkomendasikan)
  - [Opsi 2: Menjalankan Secara Lokal (Python Virtual Environment)](#opsi-2-menjalankan-secara-lokal-python-virtual-environment)
- [Akses Aplikasi](#-akses-aplikasi)
- [Fitur Utama & Kontrol Interaktif](#-fitur-utama--kontrol-interaktif)
- [API & WebSocket Reference](#-api--websocket-reference)
- [Struktur Proyek](#-struktur-proyek)
- [Troubleshooting & FAQ](#-troubleshooting--faq)

---

## 🏗️ Arsitektur & Komponen

SimPasar IDX dibangun dengan arsitektur hibrida terpisah antara Frontend dan Backend:

```
┌─────────────────────────────────────────────────────────────┐
│                 Browser / Klien Klien                       │
│    (Landing Page, Dashboard Visualisasi Candlestick & UI)   │
└──────────────────────────────┬──────────────────────────────┘
                               │ HTTP / WebSocket (:80 / :8000)
                               ▼
┌──────────────────────────────┴──────────────────────────────┐
│                  Nginx (Frontend Proxy)                      │
│            Melayani static assets & proxy /api, /ws         │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                 FastAPI Server (Backend)                    │
│   ┌─────────────────────────────────────────────────────┐   │
│   │ Market Engine (100 Agents: Fundamentalist,          │   │
│   │ Chartist/Teknikal, Noise Trader)                    │   │
│   └──────────┬──────────────────────────────┬───────────┘   │
│              ▼                              ▼               │
│   ┌────────────────────┐         ┌──────────────────────┐   │
│   │  LLM Advisor       │         │  Sectors API / MCP   │   │
│   │  (Google Gemini)   │         │  (Realtime IHSG &    │   │
│   │  Asinkron Decision │         │   News Sentiment)    │   │
│   └────────────────────┘         └──────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 📋 Prasyarat Sistem

Pilih salah satu metode deployment:
- **Untuk Docker Deployment:** Docker Engine & Docker Compose (v2.0+)
- **Untuk Local Python Deployment:** 
  - Python 3.9, 3.10, atau 3.11
  - `pip` dan `virtualenv`

---

## ⚙️ Konfigurasi Environment (`.env`)

Salin file template `.env.example` ke `.env`:

```bash
cp .env.example .env
```

Isi konfigurasi pada file `.env` sesuai kebutuhan:

| Variabel | Tipe | Default | Keterangan |
|---|---|---|---|
| `GEMINI_API_KEY` | String | *Kosong* | Kunci API Google Gemini untuk agen berbasis LLM. *(Jika kosong, simulasi otomatis beralih ke mode rule-based)*. |
| `SIMPASAR_LLM` | String | `on` | Set ke `off` untuk menonaktifkan LLM sepenuhnya. |
| `SIMPASAR_LLM_MODEL` | String | `gemini-3.1-flash-lite` | Model Gemini yang digunakan untuk penalaran agen. |
| `SIMPASAR_LLM_RPM` | Number | `3` | Laju awal maksimum request Gemini per menit; otomatis turun setelah 429. |
| `SIMPASAR_LLM_CONCURRENCY` | Integer | `1` | Jumlah request paralel ke Gemini. |
| `SIMPASAR_LLM_BATCH_SIZE` | Integer | `11` | Maksimum agen sekepribadian yang dinilai dalam satu request Gemini. |
| `SIMPASAR_LLM_MAX_REQUESTS` | Integer | `1000` | Batas total request per proses server; `0` berarti tanpa batas. |
| `SIMPASAR_LLM_TIMEOUT_MS` | Integer | `25000` | Batas waktu satu request Gemini dalam milidetik. |
| `SIMPASAR_LLM_SENTIMENT_WEIGHT` | Number | `0.25` | Bobot sentimen LLM pada keputusan fallback; `0` menonaktifkannya. |
| `SIMPASAR_LLM_SENTIMENT_HALFLIFE_S` | Number | `60` | Waktu paruh (detik) sentimen LLM sebelum pengaruhnya meluruh. |
| `SIMPASAR_AGENT_LOG` | Integer | `0` | Set `1` untuk mencetak log keputusan agen setiap tick. |
| `SECTORS_API_KEY` | String | *(kunci default tim)* | Opsional. Kunci Bearer Sectors MCP untuk harga IHSG/emiten & berita. |
| `SECTORS_MCP_URL` | String | `https://sectors-mcp.supertype.ai/mcp` | Opsional. Set `offline://x` untuk mematikan semua panggilan Sectors (hemat kredit); `scripts/dev_server.py` melakukannya otomatis kecuali `--online`. |
| `SECTORS_PRICE_MAX_PER_HOUR` | Integer | `120` | Batas panggilan `fetch-daily-price` (harga emiten) per jam per proses server, supaya kredit tidak terkuras. `0` = tanpa batas. Request paralel untuk simbol yang sama hanya memotong 1 kredit. |
| `SIMPASAR_START_SYMBOL` | String | `BBCA` | Opsional. Simbol yang tampil saat server dinyalakan (ticker emiten BEI atau `IHSG`). Nilai tidak dikenal → `BBCA`. Harga awal diambil lewat cache Sectors 6 jam (maks. 1 kredit per start); bila tidak tersedia, simulasi memakai nilai cadangan BBCA Rp6.200 (`source_kind: "fallback"`). |
| `SIMPASAR_ALLOWED_ORIGINS` | String | *Kosong* | Opsional. Origin tambahan (pisahkan koma) yang boleh membuka WebSocket, mis. `https://simpasar.example`. Secara default hanya halaman dari host yang sama (dan localhost) yang diizinkan. |

---

## 🚀 Cara Menjalankan Aplikasi

### Opsi 1: Menggunakan Docker Compose (Direkomendasikan)

Metode ini menjalankan **Frontend (Nginx)** dan **Backend (FastAPI)** dalam kontainer terisolasi.

1. **Build dan Jalankan:**
   ```bash
   docker compose up -d --build
   ```

2. **Cek Status Kontainer:**
   ```bash
   docker compose ps
   ```

3. **Melihat Log Real-time:**
   ```bash
   docker compose logs -f
   ```

4. **Menghentikan Kontainer:**
   ```bash
   docker compose down
   ```

---

### Opsi 2: Menjalankan Secara Lokal (Python Virtual Environment)

1. **Buat & Aktifkan Virtual Environment:**
   ```bash
   # Buat environment
   python3 -m venv .venv

   # Aktifkan di macOS / Linux:
   source .venv/bin/activate

   # Aktifkan di Windows (PowerShell):
   # .venv\Scripts\Activate.ps1
   ```

2. **Install Dependensi:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Jalankan Server:**

   - **Mode Pengembangan (Dev Server - Offline Sectors data):**
     ```bash
     python scripts/dev_server.py
     ```
   
   - **Mode Dev Server tanpa LLM (Rule-based only):**
     ```bash
     python scripts/dev_server.py --no-llm
     ```

   - **Mode Standar FastAPI / Uvicorn:**
     ```bash
     python server.py
     # atau
     uvicorn server:app --reload --host 0.0.0.0 --port 8000
     ```

---

## 🌐 Akses Aplikasi

Setelah server berjalan, buka URL berikut di browser Anda:

| Halaman | Docker (Port 80) | Local / Backend (Port 8000) |
|---|---|---|
| **Landing Page & SandBox** | [http://localhost](http://localhost) | [http://localhost:8000](http://localhost:8000) |
| **Simulator Dashboard** | [http://localhost/simulator](http://localhost/simulator) | [http://localhost:8000/simulator](http://localhost:8000/simulator) |
| **API Docs (Swagger)** | [http://localhost:8000/docs](http://localhost:8000/docs) | [http://localhost:8000/docs](http://localhost:8000/docs) |

---

## 🎮 Fitur Utama & Kontrol Interaktif

Pada halaman Simulator Dashboard (`/simulator`), Anda dapat berinteraksi secara real-time:

- **Pilih saham BEI apa saja (962 emiten):** cari lewat ticker (`BBCA`), nama sehari-hari (`bank bca`, `mandiri`, `telkom`, `gojek`), kata kunci sektor (`bank`, `batu bara`, `rumah sakit`) atau dengan typo (`mandri` → BMRI). Simulator dibuka di **BBCA** (ganti lewat `SIMPASAR_START_SYMBOL`); IHSG tetap bisa dipilih sebagai simbol indeks. Daftar emiten & sektor: `sim/data/idx_companies.json`; nama sehari-hari: `sim/data/idx_aliases.json`.
- **Harga awal dari Sectors:** saat simbol dipilih, harga penutupan terakhir diambil dari Sectors MCP (`fetch-daily-price`, 1 kredit per emiten) dan menjadi fundamental + harga awal simulasi. Hasil di-cache di memori dan disk (`.cache/sectors/`, TTL 6 jam). IHSG memakai cache sendiri (15 menit) dan berita 10 menit; setelah API gagal, panggilan baru ditahan 60 detik. Dalam mode offline pengguna memasukkan harga awal manual (minimal 1). Asal harga selalu terlihat di `symbol.source_kind` (Sectors / cache / cache kedaluwarsa / manual / nilai cadangan).
- **Batas ARA/ARB (Auto Rejection BEI):** harga saham tidak bisa naik/turun melewati batas harian dari **harga acuan** (hari pertama = harga awal dari Sectors, hari berikutnya = penutupan hari bursa sebelumnya, berganti tiap 330 tick). IHSG (indeks) tidak dibatasi. Aturan di `sim/idx_rules.py`:

  | Harga acuan | ARA (batas atas) | ARB (batas bawah) |
  |---|---|---|
  | Rp1–Rp10 | acuan + Rp1 (nominal) | acuan − Rp1 (nominal, minimal Rp1) |
  | Rp11–Rp200 | +35% | −15% |
  | >Rp200–Rp5.000 | +25% | −15% |
  | >Rp5.000 | +20% | −15% |

  Contoh: acuan Rp5 → ARA Rp6, ARB Rp4 (bukan dikali persen). BBCA acuan Rp6.200 → ARA Rp7.440, ARB Rp5.270. Acuan dibulatkan ke rupiah terdekat dan batas dihitung dengan bilangan bulat (ARA dibulatkan ke bawah, ARB ke atas) supaya tidak ada galat pembulatan; fraksi harga (tick size) tidak diterapkan.
- **Waktu bursa simulasi:** 1 tick = 1 menit bursa. Sesi mengikuti BEI (09:00–12:00 & 13:30–16:00 = 330 menit/hari), kalender melewati Sabtu/Minggu. Chart candlestick dengan timeframe 1m / 5m / 15m / 30m (pilihan diingat per browser) dan label jam simulasi, histori hingga 2000 tick (≈6 hari bursa). Navigasi chart: roda mouse = zoom, seret = geser ke segala arah (kiri/kanan = waktu, atas/bawah/diagonal = harga), seret sumbu harga = skala vertikal, dua jari = cubit/geser (layar sentuh), klik ganda = reset; pintasan `+` `−` (zoom), `←` `→` `↑` `↓` (geser), `0` (reset), `End` (kembali ke candle & harga terbaru).
- **Chart Candlestick Real-time:** Menampilkan pergerakan harga saham simulasi secara live melalui WebSocket (protokol snapshot + delta, lihat referensi di bawah).
- **Suntik Sentimen & Berita:**
  - *Berita Sectors*: berita atau corporate filing nyata dari Sectors MCP, tombol *Suntik ke pasar*.
  - *Input berita*: tempel link berita (atau isinya) → server membaca judul & ringkasan (`sim/news.py`), menebak saham yang dibahas, menilai sentimen −3…+3 dan saran pergeseran nilai wajar (Gemini bila aktif, cadangan kamus kata Indonesia/Inggris). Pengguna bisa menyetel kekuatannya, lalu menyuntik ke saham aktif atau langsung mengganti simulasi ke saham yang dibahas. Agen bereaksi lewat suasana pasar (orang noise), nilai wajar (orang fundamentalist), dan judul berita di prompt agen Gemini.
  - *Sebar rumor (+)* / *Kabar buruk (−)*: sentimen dadakan untuk menguji *bubble* dan *panic selling*.
- **Tipe orang (cara membaca pasar):**
  - **Fundamentalist:** melihat nilai wajar atau harga yang pantas menurut kondisi perusahaan.
  - **Chartist:** memprediksi pergerakan dari chart masa lalu (momentum 8 menit).
  - **Noise:** percaya berita dan rumor yang beredar.
- **Sifat orang (saat untung & saat rugi):**
  - *Discipline* (`disciplined`): take profit sesuai rencana, berani cut loss.
  - *Denial* (`bagholder`): menolak jual selama masih untung karena serakah (baru bisa keluar setelah untungnya habis); saat rugi menahan sampai nyangkut (sesekali kapitulasi).
  - *Averager* (`averager`): menambah posisi saat harga naik (average up, mulai di 0,5 × ambang serakah) maupun turun (average down) dari sisa modal yang sama; take profit setelah modal habis.
- **Panduan di aplikasi:** tur 12 langkah muncul sekali untuk pengunjung baru; buka lagi lewat tombol *Panduan* atau tombol `?`.
- **Panel kanan bisa ditutup:** panel *Stream ritel / Berita Sectors / Input berita* selalu terbuka saat aplikasi dibuka. Tombol panah di ujung baris tab menutupnya menjadi strip tipis di tepi kanan supaya chart lebih lebar; strip itu berisi tombol buka dan pintasan ke tiap tab. Di ponsel dan tablet tegak (lebar < 1024px) semua bagian disusun satu kolom: chart selebar layar, panel ini di bagian bawah, dan saat ditutup hanya baris tabnya yang tersisa.

---

## 📡 API & WebSocket Reference

### REST Endpoints

- `GET /api/ihsg`  
  Mengambil data harga baseline dan indikator real-time IHSG (cache 15 menit). `status`: `success` (data Sectors) ·
  `cache` + `stale: true` + `message` (cache kedaluwarsa karena API gagal) · `fallback` (nilai cadangan, bukan harga bursa).
  `source_kind` ∈ `sectors | cache | stale | fallback`, `fetched_at` = epoch detik pengambilan dari API.
- `GET /api/news`  
  Mengambil daftar berita dan company filings teranyar beserta skor sentimen (cache 10 menit — setiap pengambilan memotong 2 kredit).
- `POST /api/news/analyze` (body JSON `{"url"?: str, "text"?: str}`)  
  Membaca & menilai berita dari tab "Input berita" tanpa mengubah pasar. Balasan `{ok, url, source, title, summary, published, tickers: [{symbol, name}], primary, sentiment (−3…+3), label, category (fundamental | rumor | pasar | lainnya), fundamental_pct (−5…+5), reason, method (gemini | aturan), fetched, warning}`.
  Hanya URL http/https publik (port 80/443) yang diambil; host lokal/privat ditolak (dicek saat resolve, di tiap redirect, dan pada alamat yang benar-benar tersambung, sehingga tahan DNS rebinding; proxy sistem diabaikan); batas 8 detik per operasi, 15 detik total, 1,5 MB, maks. 3 unduhan bersamaan.
  Wajib `Content-Type: application/json` dan Origin yang sama (atau `SIMPASAR_ALLOWED_ORIGINS`), body maks. 64 KB. Gemini dipakai hanya bila agen LLM aktif dan tidak sedang backoff (maks. 4 analisis/menit, 200/proses), selain itu kamus kata.
  Halaman yang gagal dibaca → judul ditebak dari URL (`warning` diisi). Dibatasi 20 analisis/menit dan 3 bersamaan.
  Galat: 400 (body kosong/rusak/bukan JSON), 403 (Origin lain), 413 (body > 64 KB), 422 (URL tidak bisa dipakai, pesan ramah di `message`), 429 (terlalu sering).
- `GET /api/stocks/search?q=<teks>&limit=<1..30, default 12>`  
  Cari emiten BEI. Balasan `{"query", "count", "results": [{symbol, name, sector, sector_name, kind, score, match}]}`.
  `match` ∈ `ticker | alias | name | sector | fuzzy | default`; `kind` ∈ `stock | index` (IHSG). Query kosong → IHSG + emiten populer.
  Urutan skor: ticker persis (1.0) > alias persis (0.95) > ticker prefix / frasa alias + kata sisa (0.9) > alias prefix (0.85) > nama lengkap persis (0.8) > token nama (0.7) > sektor (0.5) > fuzzy (0.3–0.6). Bonus popularitas (≤ 0,04) tidak pernah membuat tingkat bertukar. Teks dinormalisasi NFKC (`ＢＢＣＡ` → BBCA) dan awalan bursa `IDX:`/`BEI:`/`JK:` dibuang (`IDX:BBCA` → BBCA).
- `GET /api/stocks/{symbol}/price`  
  Harga penutupan terakhir dari Sectors (`fetch-daily-price`, di-cache 6 jam). Selalu 200 dengan `status` di body:
  `success` (baru dari API) · `cache` (dari cache; `stale: true` + `message` bila cache kedaluwarsa dipakai karena API gagal) ·
  `offline` (API gagal & tanpa cache → `price: null`, `history: []`, `message`) · `fallback` (hanya IHSG saat offline, dengan `message`).
  `fetched_at` (epoch detik) ada bila data dari API/cache; `source_kind` ∈ `sectors | cache | stale | fallback | null`.
  IHSG tidak memakai cache 6 jam: selalu lewat cache IHSG 15 menit (`/api/ihsg`).
  Simbol yang bukan emiten BEI dalam daftar dan bukan IHSG → **404** dengan `status: "unknown"`.
  ```json
  { "symbol": "BBCA", "name": "PT Bank Central Asia Tbk.", "sector": "financials", "sector_name": "Keuangan",
    "price": 6625.0, "date": "2026-09-07", "prev_close": 6700.0, "change": -75.0, "change_pct": -1.12,
    "open": 6750.0, "high": 6750.0, "low": 6600.0, "volume": 65118000, "market_cap": 817683406650000,
    "history": [{"date": "2026-08-24", "open": 6450, "high": 6450, "low": 6350, "close": 6400, "volume": 81295600}],
    "source": "Sectors Financial API (fetch-daily-price)", "status": "success", "source_kind": "sectors",
    "fetched_at": 1790000000.0 }
  ```
- `GET /api/stocks`  
  Seluruh daftar emiten `{"count", "snapshot_date", "sectors": {...}, "companies": [{symbol, name, sector}]}` untuk prefetch klien.
- `GET /api/state`  
  Snapshot penuh state simulasi (sama dengan pesan pertama di WebSocket, termasuk `tick_interval`).
- `GET /paper.pdf`  
  Mengunduh dokumen PDF publikasi penelitian.

### WebSocket (`ws://localhost/ws` atau `ws://localhost:8000/ws`)

**Pesan dari server** — dua jenis:

1. **State** (tanpa field `event`). Selalu memuat `tick, price, fundamental, status, sentiment, paused, llm, volume, agents, params, psych_stats`, plus:
   - `agents[]`: `{id, type, psych, action, order, pnl, pos, pain, reason}` + `add: true` hanya pada tick agen benar-benar menambah posisi (average up/down).
   - `psych_stats`: `{in_position, in_pain, in_profit, averaging, averaging_up, averaging_down, avg_pnl_pct}` — `in_profit` = pegang saham dengan untung > 2%, `averaging_up`/`averaging_down` = agen yang menambah posisi di tick ini saat untung/rugi (`averaging` = jumlah keduanya).
   - `tick_interval`: kecepatan server saat ini (detik per tick, 0,05–1,5; 1x = 0,25). Klien menandai tombol kecepatan dengan interval terdekat (`0.5x=0.5, 1x=0.25, 2x=0.125, 3x=0.0833, 5x=0.05`).
   - `symbol`: `{symbol, name, sector, sector_name, kind, source_price, source_date, source, source_kind}` — simbol yang sedang disimulasikan.
     `source_kind` = asal harga awal: `"sectors"` (harga baru dari API), `"cache"` (cache Sectors ≤ TTL), `"stale"` (cache kedaluwarsa karena API gagal),
     `"manual"` (diisi pengguna), `"fallback"` (nilai cadangan saat offline: IHSG, atau BBCA Rp6.200 saat server baru menyala), atau `null` (belum ada data).
   - `limits`: batas ARA/ARB hari bursa berjalan (ada di snapshot **dan** delta):
     `{applies, ref, ara, arb, ara_pct, arb_pct, rule, band, hit, day}`. `ref` = harga acuan (rupiah bulat untuk saham),
     `rule` ∈ `nominal | percent | index`, `band` ∈ `Rp1–Rp10 | Rp11–Rp200 | >Rp200–Rp5.000 | >Rp5.000 | Indeks`,
     `ara_pct`/`arb_pct` = 0,35/0,25/0,20 dan 0,15 (`null` untuk band nominal & indeks), `hit` = `"ara"` / `"arb"` bila harga
     sedang terkunci di batas (selain itu `null`), `day` = hari bursa ke-n (mulai 1). Indeks: `applies: false`, `ara`/`arb` `null`.
     Contoh BBCA: `{"applies": true, "ref": 6200.0, "ara": 7440.0, "arb": 5270.0, "ara_pct": 0.2, "arb_pct": 0.15, "rule": "percent", "band": ">Rp5.000", "hit": null, "day": 1}`.
   - `sim_time`: `{tick, day, date, weekday, weekday_short, date_label, time, session, minute_of_day, minutes_per_day, label}` — waktu bursa simulasi (1 tick = 1 menit; Sesi 1 09:00–12:00, Sesi 2 13:30–16:00; 330 menit/hari; kalender melewati akhir pekan).
   - `snapshot: true` → **snapshot penuh** dengan `price_history` & `volume_history` (hingga 2000 tick). Dikirim saat konek, `get_state` (ke pengirim), dan di-broadcast ke **semua** klien setelah `reset`, `set_fundamental`, `set_population`, `set_psych`, `set_symbol`. Klien mengganti seluruh histori (dan menyinkronkan slider dari `params`).
   - `snapshot: false` → **delta per tick** tanpa histori. Klien melakukan `push(price)`, `push(volume)`; bila `tick !== lastTick + 1` (loncat/putus) kirim `{"cmd":"get_state"}` untuk sinkron ulang; potong array ke 2000.
2. **Event** (`{"event": ...}`): `paused`, `resumed`, `news_injected {title, strength, sentiment, origin ("sectors" | "user"), url, ticker, fundamental_pct, fundamental}`,
   `symbol_changed {symbol, fundamental}` (selalu sebelum snapshot-nya), `symbol_error {symbol, message, price_status?}`,
   `speed_changed {tick_interval}` (ke semua klien setelah `set_speed`), `error {cmd, message}` (perintah rusak atau dibuang limiter; koneksi tetap hidup).

**Klien lambat:** server tidak pernah menunggu klien. Tiap koneksi punya antrean sendiri; bila klien tertinggal (antrean > 16 pesan) delta lama dibuang dan klien menerima **snapshot penuh terbaru** begitu antreannya lega (event yang tertunda tetap dikirim lebih dulu, berurutan). Klien yang tidak membaca sama sekali selama > 10 detik diputus; browser cukup menyambung ulang. Snapshot atas permintaan (`get_state`/tertinggal) dibatasi 5 per detik per koneksi — permintaan digabung, tidak dibuang.

**Perintah dari client:**
```json
// Ganti emiten yang disimulasikan. `fundamental` opsional: bila klien sudah memanggil
// GET /api/stocks/BBCA/price kirim harganya di sini (tidak memotong kredit lagi);
// bila tidak ada, server mengambil harga dari Sectors (cache dulu). Sukses → broadcast
// {"event":"symbol_changed"} lalu snapshot penuh; gagal → {"event":"symbol_error"} ke pengirim.
{ "cmd": "set_symbol", "symbol": "BBCA", "fundamental": 6625.0 }

// Mengatur kecepatan simulasi (detik per tick; 0.05–1.5). Balasan ke semua klien:
// {"event":"speed_changed","tick_interval":0.25}; setiap state berikutnya memuat tick_interval.
{ "cmd": "set_speed", "interval": 0.25 }

// Menyuntikkan sentimen rumor / kepanikan / berita
{ "cmd": "inject_rumor", "strength": 1.5 }
{ "cmd": "inject_panic", "strength": 1.0 }
{ "cmd": "inject_news_sentiment", "title": "Judul berita", "strength": -0.8 }
// Opsional: geser nilai wajar (persen, ±10 per berita, total ±50% dari harga awal; reset membatalkan),
// asal berita, tautan (hanya http/https yang diteruskan) dan ticker yang dibahas.
{ "cmd": "inject_news_sentiment", "title": "Saham GOTO di bawah gocap", "strength": -2.5,
  "fundamental_pct": -3, "origin": "user", "url": "https://…", "ticker": "GOTO" }

// Komposisi & psikologi agen (balasan: snapshot penuh ke SEMUA klien)
{ "cmd": "set_population", "fundamentalist": 0.3, "chartist": 0.5, "noise": 0.2 }
{ "cmd": "set_psych", "disciplined": 0.34, "bagholder": 0.33 }

// Pause / Resume / Reset / fundamental manual (source_kind → "manual") / minta snapshot (tidak kena limiter)
{ "cmd": "pause" }
{ "cmd": "resume" }
{ "cmd": "reset" }
{ "cmd": "set_fundamental", "fundamental": 7000 }
{ "cmd": "get_state" }
```

**Validasi & batas:** setiap angka harus finite. `strength` di-clamp ke ±3, `interval` ke 0,05–1,5, rasio ke 0–1, dan harga awal (`fundamental`) harus 1–1.000.000.000 (pesan: "harga awal harus angka antara 1 dan 1.000.000.000"); nilai di luar itu (termasuk `NaN`/`Infinity`) ditolak dengan event `error` / `symbol_error` tanpa mengubah state. Lantai harga simulasi relatif: 0,5% dari fundamental. Server tidak pernah mengirim `NaN`/`Infinity` (JSON ketat). Koneksi dari Origin domain lain ditolak (kode 1008; lihat `SIMPASAR_ALLOWED_ORIGINS`), frame WebSocket > 64 KB ditolak uvicorn dengan menutup koneksi (kode 1009; `ws_max_size` di `server.py`, `scripts/dev_server.py`, dan `Dockerfile.backend`), dan tiap koneksi dibatasi 20 perintah/detik (kecuali `get_state`); perintah yang dibuang dibalas `{"event":"error","cmd":...,"message":"Terlalu banyak perintah, coba lagi sebentar"}` paling banyak sekali per detik.

### Menjalankan tes

Tes berupa skrip Python biasa (tanpa pytest), dijalankan dari root repo dalam mode offline:
```bash
set PYTHONIOENCODING=utf-8   # Windows; di bash: export PYTHONIOENCODING=utf-8
python tests/test_simtime.py
python tests/test_stocks.py
python tests/test_market_protocol.py
python tests/test_price_limits.py    # batas ARA/ARB per band harga, clamp & pergantian hari
python tests/test_sectors_price.py
python tests/test_api.py
python tests/test_robustness.py
python tests/test_ws_broadcast.py   # klien macet tidak membekukan klien lain (uvicorn sungguhan, port acak)
```

---

## 📁 Struktur Proyek

```
market-sim/
├── Dockerfile.backend      # Kontainer Backend FastAPI
├── Dockerfile.frontend     # Kontainer Frontend Nginx Reverse Proxy
├── docker-compose.yml      # Konfigurasi orkestrasi Multi-Container
├── nginx.conf              # Konfigurasi Nginx reverse proxy
├── requirements.txt        # Dependensi Python
├── server.py               # Entrypoint FastAPI, REST (/api/stocks/*, /api/state) & WebSocket
├── .env.example            # Template environment variable
├── .cache/sectors/         # (dibuat otomatis, di-gitignore) cache harga Sectors per emiten, TTL 6 jam
│
├── frontend/               # Dashboard simulator
│   ├── index.html          # Kerangka halaman dashboard
│   ├── styles.css          # Gaya "terminal bursa"
│   └── app.js              # Logika klien: pencarian simbol, protokol snapshot/delta, jam simulasi, chart 15m
├── landing/                # Halaman landing page & demo ringan
│   ├── index.html
│   ├── styles.css
│   ├── main.js
│   ├── sim.js
│   └── candlechart.js      # Chart candlestick canvas (dipakai landing & dashboard)
│
├── sim/                    # Inti Engine Simulasi
│   ├── market.py           # Agregasi order → harga; state snapshot/delta; set_symbol; histori 2000 tick
│   ├── agents.py           # Definisi agen & pengambilan keputusan
│   ├── llm_advisor.py      # Integrasi asinkron LLM Google Gemini
│   ├── sectors.py          # Konektor Sectors MCP: IHSG, berita, harga emiten (fetch-daily-price) + cache
│   ├── stocks.py           # Daftar emiten BEI & pencarian (ticker/alias/nama/sektor/fuzzy)
│   ├── simtime.py          # Waktu bursa simulasi (1 tick = 1 menit, sesi BEI, kalender hari bursa)
│   ├── idx_rules.py        # Aturan BEI: batas ARA/ARB dari harga acuan
│   ├── env.py              # Utility helper loader file .env
│   └── data/
│       ├── idx_companies.json  # 962 emiten BEI + 11 sektor (snapshot Sectors)
│       └── idx_aliases.json    # Nama sehari-hari → ticker (bca → BBCA, telkom → TLKM, ...)
│
├── tests/                  # Skrip uji (python tests/<file>.py, mode offline)
│   ├── test_simtime.py
│   ├── test_stocks.py
│   ├── test_market_protocol.py
│   ├── test_price_limits.py
│   ├── test_sectors_price.py
│   ├── test_api.py         # REST + WebSocket lewat fastapi.testclient
│   ├── test_robustness.py  # masukan rusak (NaN/inf), dedup kredit Sectors, cek Origin WS
│   └── test_ws_broadcast.py # antrean per klien: klien lambat/macet tidak membekukan simulasi
├── scripts/
│   └── dev_server.py       # Server launcher khusus pengembangan
├── tuning/
│   └── grid_search.py      # Skrip tuning parameter agen
└── docs/                   # Dokumentasi & referensi tambahan
```

---

## ❓ Troubleshooting & FAQ

1. **Port `80` atau `8000` sudah digunakan oleh aplikasi lain:**
   Ubah pemetaan port pada file `docker-compose.yml`:
   ```yaml
   ports:
     - "8080:80"   # Frontend dapat diakses di port 8080
   ```

2. **Mode Offline vs Online Sectors:**
   Secara default, skrip `scripts/dev_server.py` berjalan dalam mode offline agar menghemat kuota API. Untuk mengaktifkan koneksi langsung ke Sectors API, gunakan flag `--online`:
   ```bash
   python scripts/dev_server.py --online
   ```

3. **Simulasi berjalan tanpa Gemini / LLM:**
   Jika `GEMINI_API_KEY` tidak diisi di `.env`, sistem secara otomatis tetap berjalan normal menggunakan model logika *rule-based*.

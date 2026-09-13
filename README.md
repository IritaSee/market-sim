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
| `SIMPASAR_LLM_RPM` | Integer | `12` | Batas maksimum request Gemini per menit. |
| `SIMPASAR_LLM_CONCURRENCY` | Integer | `4` | Jumlah request paralel ke Gemini. |
| `SIMPASAR_AGENT_LOG` | Integer | `0` | Set `1` untuk mencetak log keputusan agen setiap tick. |

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

- **Chart Candlestick Real-time:** Menampilkan pergerakan harga saham simulasi secara live melalui WebSocket.
- **Suntik Sentimen & Berita:**
  - *⚡ SUNTIK*: Memilih berita atau corporate filing nyata dari Sectors MCP untuk disuntikkan ke pasar.
  - *🚀 Rumor (+)*: Menyuntikkan sentimen positif dadakan untuk menguji pembentukan *bubble*.
  - *📉 Bad News (-)*: Menyuntikkan sentimen kepanikan (*panic selling*).
- **Komposisi Agen (Population Mix):**
  - **Fundamentalis:** Menilai harga berdasarkan nilai intrinsik.
  - **Chartist (Teknikal):** Mengikuti tren momentum & moving averages.
  - **Noise Trader:** Bertindak berdasarkan sentimen emosional dan rumor.
- **Profil Psikologis Agen:**
  - *Disciplined*: Disiplin stop-loss dan profit taking.
  - *Bagholder / FOMO*: Cenderung menahan rugi (*loss aversion*) dan mudah panik saat pasar rontok.

---

## 📡 API & WebSocket Reference

### REST Endpoints

- `GET /api/ihsg`  
  Mengambil data harga baseline dan indikator real-time IHSG.
- `GET /api/news`  
  Mengambil daftar berita dan company filings teranyar beserta skor sentimen.
- `GET /paper.pdf`  
  Mengunduh dokumen PDF publikasi penelitian.

### WebSocket (`ws://localhost/ws` atau `ws://localhost:8000/ws`)

Format payload pesan kontrol dari client:
```json
// Contoh: Mengatur kecepatan simulasi (detik per tick)
{ "cmd": "set_speed", "interval": 0.25 }

// Contoh: Menyuntikkan sentimen rumor
{ "cmd": "inject_rumor", "strength": 1.5 }

// Contoh: Pause / Resume / Reset
{ "cmd": "pause" }
{ "cmd": "resume" }
{ "cmd": "reset" }
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
├── server.py               # Entrypoint FastAPI & WebSocket server
├── .env.example            # Template environment variable
│
├── frontend/               # Dashboard simulator
│   └── index.html          # Web UI dashboard interaktif & visualisasi
├── landing/                # Halaman landing page & demo ringan
│   ├── index.html
│   ├── styles.css
│   ├── main.js
│   ├── sim.js
│   └── candlechart.js
│
├── sim/                    # Inti Engine Simulasi
│   ├── market.py           # Order book, matching engine, & kalkulasi harga
│   ├── agents.py           # Definisi agen & pengambilan keputusan
│   ├── llm_advisor.py      # Integrasi asinkron LLM Google Gemini
│   ├── sectors.py          # Konektor data Sectors MCP (IHSG & News)
│   └── env.py              # Utility helper loader file .env
│
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

"""
Jalankan server SimPasar IDX secara lokal untuk pengembangan.

Sectors: default MODE OFFLINE. URL Sectors MCP diganti skema yang tidak dikenal
sehingga httpx langsung menolaknya dan kode memakai data fallback. Tujuannya agar
sesi pengembangan tidak menghabiskan kredit API hackathon (maks. 1 kredit per start
server untuk harga BBCA yang di-cache 6 jam, 1 per landing page, 3 per halaman simulator).
Simulator dibuka di BBCA (SIMPASAR_START_SYMBOL untuk mengganti); dalam mode offline
harga awal dibaca dari cache disk .cache/sectors/ bila ada, selain itu nilai cadangan Rp6.200.

Gemini: kunci dibaca dari environment atau file .env di root repo. Bila ada,
sebagian kecil keputusan agen dibantu LLM secara asinkron dengan batas request per
menit (lihat sim/llm_advisor.py). Pakai --no-llm untuk mematikannya.

    python scripts/dev_server.py              # Sectors offline, LLM aktif bila ada kunci
    python scripts/dev_server.py --no-llm     # Sectors offline, tanpa Gemini
    python scripts/dev_server.py --online     # panggil Sectors sungguhan
    PORT=8080 python scripts/dev_server.py

Selalu berjalan dari root repo, karena server.py memakai path relatif
(frontend/, landing/, *.pdf).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Console Windows sering berkode cp1252; paksa UTF-8 supaya log (termasuk
# emoji atau alasan LLM dari agen) tidak memicu UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from sim.env import load_env_file  # noqa: E402

KNOWN_FLAGS = {"--online", "--no-llm"}
flags = set(sys.argv[1:])
if flags - KNOWN_FLAGS:
    print(f"[dev_server] opsi tidak dikenal: {' '.join(sorted(flags - KNOWN_FLAGS))}")
    print("[dev_server] pemakaian: python scripts/dev_server.py [--online] [--no-llm]")
    sys.exit(2)

loaded = load_env_file(ROOT / ".env")
if loaded:
    print(f"[dev_server] .env dimuat: {', '.join(sorted(loaded))}")

if "--online" not in flags:
    # Skema "offline://" ditolak httpx sebelum ada koneksi jaringan (UnsupportedProtocol)
    # dan ditangkap oleh except di sim/sectors.py, jadi fallback langsung dipakai.
    os.environ["SECTORS_MCP_URL"] = "offline://sectors-disabled"
    os.environ["SECTORS_API_KEY"] = "offline"
    print("[dev_server] MODE OFFLINE: Sectors MCP dimatikan, data fallback dipakai (kredit tidak terpotong).")
    print("[dev_server] Gunakan --online untuk memanggil Sectors sungguhan.")
else:
    if not os.environ.get("SECTORS_API_KEY"):
        print("[dev_server] --online tanpa SECTORS_API_KEY di environment; sim/sectors.py akan memakai nilai default-nya.")
    print("[dev_server] MODE ONLINE: setiap start/landing/simulator memotong kredit Sectors.")

if "--no-llm" in flags:
    os.environ["SIMPASAR_LLM"] = "off"

# Status dibaca dari sim.agents (nilai efektif setelah validasi), bukan string env mentah.
from sim.agents import llm_status  # noqa: E402

gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
status = llm_status()
if status.get("enabled"):
    masked = f"{gemini_key[:4]}...{gemini_key[-4:]}" if gemini_key and len(gemini_key) > 12 else "(tidak terbaca)"
    budget = f"batas {status['max_requests']} request per sesi" if status.get("max_requests") else "tanpa batas request per sesi"
    print(f"[dev_server] Gemini aktif: kunci {masked}, model {status['model']}, maks {status['rpm']:g} request/menit, "
          f"{budget}, {status['concurrency']} paralel, asinkron.")
else:
    why = {
        "disabled": "LLM dimatikan (SIMPASAR_LLM=off atau batas request 0)",
        "no_key": "Tanpa GEMINI_API_KEY yang valid",
        "invalid_config": "Konfigurasi SIMPASAR_LLM_* tidak valid",
    }.get(status.get("reason"), f"LLM tidak aktif ({status.get('reason')})")
    print(f"[dev_server] {why}: semua agen memakai aturan.")
if os.environ.get("GOOGLE_GENAI_DEBUG") and gemini_key:
    print("[dev_server] PERINGATAN: GOOGLE_GENAI_DEBUG aktif; SDK google-genai akan mencetak header kunci API ke log.")

import uvicorn  # noqa: E402  (impor setelah env diset)

host = os.environ.get("HOST", "127.0.0.1")
port = int(os.environ.get("PORT", "8000"))
print(f"[dev_server] landing   -> http://{host}:{port}/")
print(f"[dev_server] simulator -> http://{host}:{port}/simulator")
# ws_max_size: perintah klien < 1 KB; frame raksasa ditolak sebelum di-parse (lihat server.WS_MAX_MESSAGE_BYTES).
uvicorn.run("server:app", host=host, port=port, reload=False, ws_max_size=64 * 1024)

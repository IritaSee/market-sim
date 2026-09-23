"""
Uji pengiriman WebSocket tidak memblok (F0) — jalankan: python tests/test_ws_broadcast.py

Regresi: dulu broadcast() melakukan `await ws.send_text()` per klien secara berurutan, sehingga
SATU klien yang berhenti membaca membekukan loop simulasi untuk semua orang. Sekarang tiap
koneksi punya antrean + task penulis sendiri (server.WSClient):
  - broadcast() tidak pernah menunggu I/O klien;
  - urutan pesan per klien tetap (event symbol_changed sebelum snapshot-nya);
  - antrean penuh → delta dibuang, snapshot penuh terbaru dikirim begitu antrean lega;
  - klien yang macet > WS_STALL_TIMEOUT diputus (transport di-abort) dan tidak ada task bocor.

Bagian 1 memakai WebSocket palsu (cepat, deterministik). Bagian 2 menyalakan uvicorn sungguhan
di port acak 127.0.0.1 (bukan 8000) dengan satu klien websockets sehat dan satu socket mentah
yang tidak pernah membaca. Mode offline: tidak ada kredit Sectors yang terpakai.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SECTORS_API_KEY"] = "offline"
os.environ["SIMPASAR_LLM"] = "off"

import uvicorn  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

import server  # noqa: E402


# ------------------------------------------------------------------ #
#  Bagian 1: WebSocket palsu                                          #
# ------------------------------------------------------------------ #

class RecordingWS:
    """Klien sehat: setiap pesan langsung 'terkirim'."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


class HangingWS:
    """Klien yang berhenti membaca: send_text tidak pernah selesai (seperti buffer TCP penuh)."""

    def __init__(self) -> None:
        self.attempts = 0
        self._never = asyncio.Event()

    async def send_text(self, text: str) -> None:
        self.attempts += 1
        await self._never.wait()


class GateWS:
    """Klien lambat yang pulih: send_text tertahan sampai gerbang dibuka."""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        await self.gate.wait()
        self.sent.append(json.loads(text))


def _tick_state() -> dict:
    return server._with_speed(server.market.step())


async def _let_writers_run(rounds: int = 3) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def _states(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if "event" not in m]


async def _scenario_slow_client_does_not_block() -> None:
    server.clients.clear()
    server.market.resume()
    fast = server.WSClient(RecordingWS())
    fast.send(server.current_state(full=True))
    stuck = server.WSClient(HangingWS())
    stuck.send(server.current_state(full=True))
    for c in (fast.start(), stuck.start()):
        server.clients.add(c)
    await _let_writers_run()
    stuck_since = time.monotonic()                 # send pertama klien macet mulai menggantung
    start_tick = server.market.tick

    n_ticks = 100
    worst = 0.0
    for _ in range(n_ticks):
        t0 = time.perf_counter()
        server.broadcast(_tick_state())            # sinkron: tidak ada yang bisa ditunggu
        worst = max(worst, time.perf_counter() - t0)
        await _let_writers_run()
    assert worst < 0.1, f"broadcast terlalu lama ({worst:.3f} s) — ada yang menunggu I/O?"

    # Klien sehat menerima SEMUA tick berurutan walaupun ada klien macet.
    ticks = [m["tick"] for m in _states(fast.ws.sent) if m.get("snapshot") is False]
    assert ticks == list(range(start_tick + 1, start_tick + n_ticks + 1)), ticks[:5]
    assert fast.dropped == 0 and not fast.needs_snapshot

    # Klien macet: antrean terbatas, delta dibuang, ditandai butuh snapshot. Hanya 1 send menggantung.
    assert stuck.pending <= server.WS_QUEUE_MAX and stuck.needs_snapshot, (stuck.pending, stuck.needs_snapshot)
    assert stuck.dropped >= n_ticks - server.WS_QUEUE_MAX - 1 and stuck.ws.attempts == 1
    assert not stuck.closed

    # Setelah WS_STALL_TIMEOUT klien macet diputus; broadcast berikutnya membuangnya dari daftar.
    await asyncio.sleep(max(0.0, stuck_since + server.WS_STALL_TIMEOUT + 0.3 - time.monotonic()))
    assert stuck.closed and stuck.stalled and stuck.writer.done()
    server.broadcast(_tick_state())
    assert stuck not in server.clients and fast in server.clients
    await _let_writers_run()
    assert fast.ws.sent[-1]["tick"] == server.market.tick

    for c in (fast, stuck):
        await c.aclose()
        assert c.writer.done() and c.closed
    server.clients.clear()
    # Tidak ada task penulis yang bocor.
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert not leftover, leftover


async def _scenario_backlog_then_fresh_snapshot() -> None:
    server.clients.clear()
    server.market.resume()
    slow = server.WSClient(GateWS())
    slow.send(server.current_state(full=True))    # snapshot awal (penulis tertahan di gerbang)
    server.clients.add(slow.start())
    await _let_writers_run()

    for _ in range(server.WS_QUEUE_MAX * 3):       # antrean meluap → delta dibuang
        server.broadcast(_tick_state())
    assert slow.needs_snapshot and slow.pending < server.WS_QUEUE_MAX

    # Event yang datang saat tertinggal tetap dikirim (berurutan), snapshot eksplisit digantikan
    # snapshot segar yang dibuat saat dikirim — tetap SESUDAH event-nya.
    server.broadcast({"event": "symbol_changed", "symbol": dict(server.market.symbol), "fundamental": server.market.fundamental})
    server.broadcast(server.current_state(full=True))
    for _ in range(3):
        server.broadcast(_tick_state())            # masih tertinggal → dibuang
    tick_at_release = server.market.tick

    slow.ws.gate.set()
    await _let_writers_run(10)
    sent = slow.ws.sent
    assert sent[0]["snapshot"] is True                              # snapshot awal
    assert sent[1]["event"] == "symbol_changed", sent[1]
    assert sent[2]["snapshot"] is True and sent[2]["tick"] == tick_at_release, (sent[2].get("tick"), tick_at_release)
    assert "price_history" in sent[2] and "tick_interval" in sent[2]
    assert len(sent) == 3 and not slow.needs_snapshot and slow.pending == 0

    # Setelah pulih: delta lanjut tanpa celah dari tick snapshot.
    for _ in range(5):
        server.broadcast(_tick_state())
        await _let_writers_run()
    ticks = [m["tick"] for m in slow.ws.sent[3:]]
    assert ticks == list(range(tick_at_release + 1, tick_at_release + 6)), ticks

    # get_state beruntun digabung menjadi satu snapshot.
    before = len(slow.ws.sent)
    for _ in range(10):
        slow.request_snapshot()
    await asyncio.sleep(server.WS_SNAPSHOT_MIN_INTERVAL + 0.1)
    assert len(slow.ws.sent) == before + 1 and slow.ws.sent[-1]["snapshot"] is True

    await slow.aclose()
    server.clients.clear()
    assert slow.writer.done()


async def _scenario_get_state_flood_is_throttled() -> None:
    """Banjir get_state dari klien yang membaca cepat: snapshot dibatasi (±5/detik), tidak pernah hilang."""
    server.clients.clear()
    c = server.WSClient(RecordingWS()).start()
    server.clients.add(c)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.0:
        c.request_snapshot()
        await asyncio.sleep(0.001)
    await asyncio.sleep(server.WS_SNAPSHOT_MIN_INTERVAL + 0.1)
    snaps = sum(1 for m in c.ws.sent if m.get("snapshot") is True)
    limit = 1.0 / server.WS_SNAPSHOT_MIN_INTERVAL + 2
    assert 3 <= snaps <= limit, snaps
    assert not c.needs_snapshot                       # permintaan terakhir tetap dilayani
    await c.aclose()
    server.clients.clear()


async def _scenario_event_flood_disconnects() -> None:
    """Klien macet yang terus menerima event (bukan state) diputus saat antrean event penuh."""
    server.clients.clear()
    c = server.WSClient(HangingWS()).start()
    server.clients.add(c)
    await _let_writers_run()
    for _ in range(server.WS_QUEUE_HARD_MAX + 5):
        server.broadcast({"event": "news_injected", "title": "x", "strength": 0.1, "sentiment": 0.1})
    assert c.closed and c.stalled
    await _let_writers_run()
    assert c.writer.done()
    await c.aclose()
    server.broadcast({"event": "paused"})
    assert c not in server.clients


def test_fake_clients() -> None:
    saved = server.WS_STALL_TIMEOUT
    server.WS_STALL_TIMEOUT = 3.0                  # > durasi 100 tick (± 5 ms/tick) di mesin lambat
    try:
        asyncio.run(_scenario_slow_client_does_not_block())
        server.WS_STALL_TIMEOUT = 0.5
        asyncio.run(_scenario_backlog_then_fresh_snapshot())
        asyncio.run(_scenario_get_state_flood_is_throttled())
        asyncio.run(_scenario_event_flood_disconnects())
    finally:
        server.WS_STALL_TIMEOUT = saved
        server.clients.clear()
    print("ok  test_fake_clients")


# ------------------------------------------------------------------ #
#  Bagian 2: uvicorn sungguhan + socket yang tidak pernah membaca    #
# ------------------------------------------------------------------ #

class _RecordingClient(server.WSClient):
    instances: list["_RecordingClient"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        _RecordingClient.instances.append(self)


def _start_uvicorn() -> tuple[uvicorn.Server, threading.Thread, int]:
    config = uvicorn.Config(server.app, host="127.0.0.1", port=0, log_level="warning",
                            ws_max_size=server.WS_MAX_MESSAGE_BYTES, lifespan="on")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started:
        if time.time() > deadline or not thread.is_alive():
            raise AssertionError("uvicorn uji tidak menyala")
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    assert port != 8000
    return srv, thread, port


def _raw_ws_that_never_reads(port: int) -> socket.socket:
    """Handshake WebSocket lewat socket mentah (buffer terima kecil) lalu TIDAK pernah membaca lagi."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    sock.settimeout(5)
    sock.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                  f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(1)                        # baca byte demi byte: jangan ikut menyedot data frame
        if not chunk:
            raise AssertionError("handshake socket mentah gagal")
        head += chunk
    assert head.startswith(b"HTTP/1.1 101"), head[:40]
    return sock


class _HealthyReader(threading.Thread):
    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.ws = ws_connect(f"ws://127.0.0.1:{port}/ws", max_size=2 ** 24, open_timeout=10)
        self.ticks: list[tuple[float, int]] = []
        self.snapshots = 0
        self.stop = threading.Event()

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    raw = self.ws.recv(timeout=0.5)
                except TimeoutError:
                    continue
                msg = json.loads(raw)
                if "event" in msg:
                    continue
                if msg.get("snapshot"):
                    self.snapshots += 1
                else:
                    self.ticks.append((time.monotonic(), msg["tick"]))
        except Exception:
            pass

    def count_between(self, t0: float, t1: float) -> int:
        return sum(1 for t, _ in list(self.ticks) if t0 <= t < t1)


def test_real_server_stalled_socket() -> None:
    saved = (server.WSClient, server.WS_STALL_TIMEOUT, server.tick_rate)
    server.WSClient = _RecordingClient
    server.WS_STALL_TIMEOUT = 4.0
    server.tick_rate = 0.05                          # 5x: ±15 KB × 20 tick/detik, buffer cepat penuh
    server.market.resume()
    srv, thread, port = _start_uvicorn()
    healthy = raw = None
    try:
        healthy = _HealthyReader(port)
        healthy.start()
        time.sleep(1.0)
        t0 = time.monotonic()
        time.sleep(1.0)
        baseline = healthy.count_between(t0, time.monotonic())
        assert baseline >= 8, f"laju awal terlalu rendah: {baseline} tick/detik"

        raw = _raw_ws_that_never_reads(port)
        t_stall = time.monotonic()
        time.sleep(2.5)                              # buffer penuh < 1 s; klien masih terdaftar (timeout 4 s)
        during = healthy.count_between(t_stall + 0.5, time.monotonic())
        per_sec = during / 2.0
        assert len(_RecordingClient.instances) == 2
        stuck = _RecordingClient.instances[1]
        assert stuck in server.clients and not stuck.closed, "klien macet diputus terlalu cepat"
        assert stuck.dropped > 0 or stuck.needs_snapshot, "socket mentah belum macet — uji tidak bermakna"
        # Dulu: 0 tick sejak buffer klien macet penuh. Sekarang laju tetap dekat laju awal.
        assert per_sec >= baseline * 0.6, f"klien sehat melambat: {per_sec:.1f}/s vs awal {baseline}/s"
        ticks = [tk for _, tk in healthy.ticks]
        assert all(b == a + 1 for a, b in zip(ticks, ticks[1:])), "klien sehat melihat celah tick"
        assert _RecordingClient.instances[0].dropped == 0

        # Klien macet diputus setelah WS_STALL_TIMEOUT: dikeluarkan dari daftar & transport di-abort.
        deadline = time.monotonic() + server.WS_STALL_TIMEOUT + 6
        while stuck in server.clients and time.monotonic() < deadline:
            time.sleep(0.1)
        assert stuck not in server.clients and stuck.stalled, "klien macet tidak diputus"
        assert stuck.writer.done()
        assert server._find_transport(stuck.ws) is not None, "transport uvicorn tidak ditemukan untuk di-abort"
        # Putus paksa (RST, SO_LINGER 0), bukan FIN sopan yang antre di belakang data tak terbaca
        # dan menahan koneksi yatim di kernel (FIN_WAIT_1) selama peer tidak membaca.
        raw.settimeout(5)
        outcome = "timeout"
        try:
            while True:
                if not raw.recv(65536):
                    outcome = "EOF"
                    break
        except (ConnectionResetError, ConnectionAbortedError):
            outcome = "reset"
        except socket.timeout:
            pass
        assert outcome == "reset", f"socket klien macet tidak di-reset server ({outcome})"
        after = healthy.count_between(time.monotonic() - 1.0, time.monotonic())
        assert after >= baseline * 0.6, f"laju setelah pemutusan: {after}/s"
        print(f"ok  test_real_server_stalled_socket (awal {baseline}/s, saat ada klien macet {per_sec:.1f}/s)")
    finally:
        if healthy is not None:
            healthy.stop.set()
            try:
                healthy.ws.close()
            except Exception:
                pass
            healthy.join(timeout=5)
        if raw is not None:
            raw.close()
        # Semua koneksi selesai → tidak ada klien atau task penulis yang tertinggal.
        deadline = time.monotonic() + 5
        while server.clients and time.monotonic() < deadline:
            time.sleep(0.05)
        leftover = list(server.clients)
        writers_done = all(c.writer is None or c.writer.done() for c in _RecordingClient.instances)
        srv.should_exit = True
        thread.join(timeout=15)
        server.WSClient, server.WS_STALL_TIMEOUT, server.tick_rate = saved
        server.clients.clear()
    assert not leftover, leftover
    assert writers_done, "task penulis bocor setelah klien putus"
    assert not thread.is_alive(), "uvicorn uji tidak berhenti"


if __name__ == "__main__":
    test_fake_clients()
    test_real_server_stalled_socket()
    print("SEMUA TES ws_broadcast LULUS")

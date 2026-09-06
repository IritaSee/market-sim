"""
SimPasar IDX — FastAPI server dengan WebSocket real-time dan integrasi Sectors MCP.

Jalankan:
    python server.py
    atau:
    uvicorn server:app --reload --port 8000

Buka di browser:
    Landing Page : http://localhost:8000
    Simulator    : http://localhost:8000/simulator
"""
import os
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from sim.agents import get_gemini_client
from sim.market import Market
from sim.sectors import get_realtime_ihsg, get_latest_news_and_filings

app = FastAPI(title="SimPasar IDX")

# ------------------------------------------------------------------ #
#  State global                                                       #
# ------------------------------------------------------------------ #

market     = Market(n_agents=100, seed=42)
clients:   set[WebSocket] = set()
tick_rate  = 0.25   # detik antar tick (≈4 tick/detik default 1x, realistis)


# ------------------------------------------------------------------ #
#  Simulation loop                                                    #
# ------------------------------------------------------------------ #

async def broadcast(payload: dict) -> None:
    dead: set[WebSocket] = set()
    for ws in list(clients):
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


async def simulation_loop() -> None:
    while True:
        if not market.is_paused and clients:
            state = market.step()
            await broadcast(state)
        await asyncio.sleep(tick_rate)


def _silence_genai_aclose_bug(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    exc = context.get("exception")
    if isinstance(exc, AttributeError) and "_async_httpx_client" in str(exc):
        return
    loop.default_exception_handler(context)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.get_running_loop().set_exception_handler(_silence_genai_aclose_bug)
    # Inisialisasi baseline fundamental pasar dengan data IHSG real-time dari Sectors MCP
    try:
        ihsg_data = await get_realtime_ihsg()
        if ihsg_data and ihsg_data.get("price"):
            market.set_fundamental(float(ihsg_data["price"]))
    except Exception:
        pass
    asyncio.create_task(simulation_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    client = get_gemini_client()
    if client is not None:
        try:
            await client.aio.aclose()
        except Exception:
            pass


# ------------------------------------------------------------------ #
#  REST Endpoints                                                     #
# ------------------------------------------------------------------ #

@app.get("/simulator")
async def serve_simulator() -> FileResponse:
    """Halaman Dashboard Simulator Interaktif."""
    return FileResponse("frontend/index.html")


@app.get("/api/ihsg")
async def api_ihsg():
    """Ambil data real-time IHSG dari Sectors MCP."""
    data = await get_realtime_ihsg()
    return JSONResponse(data)


@app.get("/api/news")
async def api_news():
    """Ambil berita & company filings terkini dari Sectors MCP beserta skor sentimen."""
    items = await get_latest_news_and_filings()
    return JSONResponse(items)


@app.get("/paper.pdf")
async def serve_paper():
    """Download/baca draf paper PDF jika ada."""
    for f in os.listdir("."):
        if f.lower().endswith(".pdf"):
            return FileResponse(f)
    return JSONResponse({"message": "Draf paper PDF belum tersedia di root folder."}, status_code=404)


# ------------------------------------------------------------------ #
#  WebSocket endpoint                                                 #
# ------------------------------------------------------------------ #

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    global tick_rate

    await websocket.accept()
    clients.add(websocket)
    # Kirim state awal langsung ke client baru
    await websocket.send_text(json.dumps(market.get_state()))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            cmd = msg.get("cmd", "")

            if cmd == "inject_rumor":
                market.inject_rumor(float(msg.get("strength", 1.0)))

            elif cmd == "inject_news_sentiment":
                # Injeksi sentimen berdasarkan berita nyata
                strength = float(msg.get("strength", 1.0))
                title = msg.get("title", "Berita IDX")
                if strength >= 0:
                    market.inject_rumor(strength)
                else:
                    market.inject_panic(abs(strength))
                await broadcast({
                    "event": "news_injected",
                    "title": title,
                    "strength": strength,
                    "sentiment": market.sentiment
                })

            elif cmd == "inject_panic":
                market.inject_panic(float(msg.get("strength", 1.0)))

            elif cmd == "set_population":
                market.set_population(
                    float(msg.get("fundamentalist", 0.30)),
                    float(msg.get("chartist",      0.50)),
                    float(msg.get("noise",         0.20)),
                )
                await websocket.send_text(json.dumps(market.get_state()))

            elif cmd == "pause":
                market.pause()
                await broadcast({"event": "paused"})

            elif cmd == "resume":
                market.resume()
                await broadcast({"event": "resumed"})

            elif cmd == "reset":
                market.reset()
                await broadcast(market.get_state())

            elif cmd == "set_fundamental":
                fund_val = float(msg.get("fundamental", 100.0))
                market.set_fundamental(fund_val)
                await broadcast(market.get_state())

            elif cmd == "set_psych":
                market.set_psych(
                    float(msg.get("disciplined", 0.34)),
                    float(msg.get("bagholder",   0.33)),
                )
                await websocket.send_text(json.dumps(market.get_state()))

            elif cmd == "set_speed":
                # interval antara tick: 0.05s (sangat cepat) – 1.0s (lambat)
                tick_rate = max(0.05, min(1.5, float(msg.get("interval", 0.18))))

            elif cmd == "get_state":
                await websocket.send_text(json.dumps(market.get_state()))

    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)


# ------------------------------------------------------------------ #
#  Static Routes & Landing Page                                       #
# ------------------------------------------------------------------ #

app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")
app.mount("/", StaticFiles(directory="landing", html=True), name="landing")


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)


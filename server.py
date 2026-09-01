"""
SimPasar IDX — FastAPI server dengan WebSocket real-time.

Jalankan:
    cd market-sim
    uvicorn server:app --reload --port 8000
    buka http://localhost:8000
"""
import asyncio
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import uvicorn

from sim.agents import get_gemini_client
from sim.market import Market

app = FastAPI(title="SimPasar IDX")

# ------------------------------------------------------------------ #
#  State global                                                       #
# ------------------------------------------------------------------ #

market     = Market(n_agents=100, seed=42)
clients:   set[WebSocket] = set()
tick_rate  = 0.10   # detik antar tick (≈10 tick/detik default)


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


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(simulation_loop())


@app.on_event("shutdown")
async def shutdown_event() -> None:
    # Close the Gemini client explicitly while the loop is still healthy —
    # otherwise its __del__ schedules cleanup as a background task that can
    # fire after --reload has already torn down module state, logging a
    # spurious "Task exception was never retrieved" AttributeError.
    client = get_gemini_client()
    if client is not None:
        try:
            await client.aio.aclose()
        except Exception:
            pass


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
#  Static frontend                                                    #
# ------------------------------------------------------------------ #

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)

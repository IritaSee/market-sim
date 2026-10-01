"""
Uji "Input berita" (sim/news.py + POST /api/news/analyze + inject_news_sentiment dengan fundamental_pct)
dan perilaku sifat orang saat untung (Denial menahan, Averager menambah) — jalankan: python tests/test_news.py

Tanpa jaringan: analisis memakai teks / judul dari URL, pengambilan halaman dipalsukan, Gemini mati.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ["SECTORS_MCP_URL"] = "offline://x"
os.environ["SECTORS_API_KEY"] = "offline"
os.environ["SIMPASAR_LLM"] = "off"

import numpy as np  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from sim import agents, news  # noqa: E402
from sim.agents import Agent, NoiseAgent  # noqa: E402
from sim.market import Market  # noqa: E402

GOTO_URL = "https://www.emitennews.com/news/tiga-hari-beruntun-saham-goto-bawah-gocap-apa-kabar-fundamentalnya"
GOTO_HTML = """<html><head><meta charset="utf-8">
<title>Situs</title>
<meta property="og:title" content="Tiga Hari Beruntun Saham GOTO Bawah Gocap, Apa Kabar Fundamentalnya?">
<meta property="og:description" content="Harga saham GoTo Gojek Tokopedia (GOTO) tiga hari beruntun diperdagangkan di bawah Rp50.">
<meta property="og:site_name" content="EmitenNews">
</head><body><script>var x = "laba naik";</script>
<p>Harga saham GOTO masih berada dalam tekanan setelah tiga hari beruntun ditutup di bawah gocap.</p>
</body></html>"""


def test_url_validation() -> None:
    for bad in ("", "ftp://x.com/a", "javascript:alert(1)", "http://user:pw@example.com/", "http://example.com:8080/",
                "http://" + "a" * 2100 + ".com"):
        try:
            news.validate_url(bad)
        except news.NewsError:
            continue
        raise AssertionError(f"URL harus ditolak: {bad[:40]!r}")
    assert news.validate_url("emitennews.com/news/x") == "https://emitennews.com/news/x"
    # Host lokal/privat ditolak sebelum koneksi dibuat (cegah SSRF)
    for host in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "[::1]"):
        try:
            news._check_public_host(host.strip("[]"), 80)
        except news.NewsError:
            continue
        raise AssertionError(f"host lokal lolos: {host}")


def test_slug_and_tickers() -> None:
    title = news.title_from_slug(GOTO_URL)
    assert title == "Tiga hari beruntun saham GOTO bawah gocap apa kabar fundamentalnya", title
    assert news.detect_tickers(title, "", GOTO_URL) == ["GOTO"]
    # ticker yang hanya disebut sekali di isi (mis. tautan berita terkait) tidak ikut
    assert news.detect_tickers("Laba BBRI naik", "Sementara itu TLKM stagnan.", "") == ["BBRI"]
    assert news.detect_tickers("IHSG dan YANG lain", "", "") == []


def test_rules_sentiment() -> None:
    neg = news.analyze_rules("Tiga Hari Beruntun Saham GOTO Bawah Gocap", "", "")
    assert neg["sentiment"] < -1.0 and neg["label"] == "Negatif", neg
    pos = news.analyze_rules("Laba bersih BBRI naik 12%, dividen jumbo", "", "")
    assert pos["sentiment"] > 1.0 and pos["category"] == "fundamental" and pos["fundamental_pct"] > 0, pos
    flat = news.analyze_rules("Direksi baru diperkenalkan dalam acara tahunan", "", "")
    assert flat["label"] == "Netral" and flat["fundamental_pct"] == 0.0, flat
    assert -3.0 <= news.analyze_rules("anjlok " * 50, "", "")["sentiment"] <= 3.0


def test_analyze_with_fake_fetch() -> None:
    original = news._fetch_html
    try:
        news._fetch_html = lambda url: (GOTO_HTML, url)
        r = asyncio.run(news.analyze_news(GOTO_URL, None, "BBCA"))
        assert r["ok"] and r["fetched"] and r["source"] == "EmitenNews", r
        assert r["title"].startswith("Tiga Hari Beruntun Saham GOTO"), r["title"]
        assert r["primary"] == "GOTO" and r["label"] == "Negatif" and r["fundamental_pct"] < 0, r
        assert "laba" not in r["reason"], "isi <script> tidak boleh ikut dinilai"

        def fail(url):
            raise news.NewsError("Situs menolak dibaca (HTTP 403).")
        news._fetch_html = fail
        r = asyncio.run(news.analyze_news(GOTO_URL, None, "BBCA"))
        assert r["ok"] and not r["fetched"] and r["warning"] and r["primary"] == "GOTO", r
    finally:
        news._fetch_html = original
    r = asyncio.run(news.analyze_news(None, "Saham BBRI melonjak setelah laba naik. Analis optimis.", "BBCA"))
    assert r["title"] == "Saham BBRI melonjak setelah laba naik." and r["primary"] == "BBRI" and r["sentiment"] > 0, r


def test_endpoint_and_injection(client: TestClient) -> None:
    r = client.post("/api/news/analyze", json={"text": "Saham GOTO anjlok, rugi bersih membengkak."})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["primary"] == "GOTO" and body["sentiment"] < 0 and body["method"] == "aturan", body
    assert client.post("/api/news/analyze", json={}).status_code == 400
    assert client.post("/api/news/analyze", content=b"bukan json").status_code == 400
    assert client.post("/api/news/analyze", content=b"bukan json", headers={"Content-Type": "application/json"}).status_code == 400
    # Halaman dari domain lain ditolak; body raksasa ditolak sebelum diurai
    r = client.post("/api/news/analyze", json={"text": "x"}, headers={"Origin": "https://jahat.example"})
    assert r.status_code == 403, r.text
    r = client.post("/api/news/analyze", content=b'{"text": "' + b"a" * 70000 + b'"}', headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    r = client.post("/api/news/analyze", json={"url": "http://127.0.0.1/secret"})
    assert r.status_code == 422 and "lokal" in r.json()["message"], r.text

    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                                   # snapshot awal
        server.market.pause()                               # nilai wajar tidak bergerak oleh tick
        before = server.market.fundamental
        ws.send_json({"cmd": "inject_news_sentiment", "title": "Saham GOTO anjlok\n<b>x</b>", "strength": -1.5,
                      "fundamental_pct": -3, "origin": "user", "url": "javascript:alert(1)", "ticker": "goto"})
        ev = None
        for _ in range(50):
            msg = ws.receive_json()
            if msg.get("event") == "news_injected":
                ev = msg
                break
        assert ev, "event news_injected tidak diterima"
        assert ev["origin"] == "user" and ev["url"] == "" and ev["ticker"] == "GOTO", ev
        assert abs(ev["fundamental_pct"] + 3.0) < 0.01 and abs(server.market.fundamental - before * 0.97) < 1e-6, ev
        assert "\n" not in ev["title"]
        # Batas: satu berita maks. ±10%
        ws.send_json({"cmd": "inject_news_sentiment", "title": "x", "strength": 0, "fundamental_pct": 999})
        for _ in range(50):
            msg = ws.receive_json()
            if msg.get("event") == "news_injected":
                assert abs(msg["fundamental_pct"] - 10.0) < 0.01, msg
                break
        # Reset membatalkan pergeseran nilai wajar dari berita
        server.market.reset()
        assert server.market.fundamental == server.market.base_fundamental
        server.market.resume()
    agents.set_news_context("", 0)


def test_shift_fundamental_bounds() -> None:
    m = Market(n_agents=10, seed=1, fundamental=1000.0)
    for _ in range(20):
        m.shift_fundamental(10)
    assert abs(m.fundamental - 1500.0) < 1e-6, m.fundamental          # total maks. +50% dari nilai awal
    assert m.shift_fundamental(float("nan")) is None
    m.set_fundamental(200.0)
    assert m.base_fundamental == 200.0 and m.fundamental == 200.0


def _agent(psych: str) -> Agent:
    a = NoiseAgent(0)
    a.psych_profile = psych
    a.position, a.entry_price, a.capital_remaining = 1.0, 100.0, 1.0
    a.pain_threshold, a.greed_threshold, a.reaction_delay_prob = 0.25, 0.40, 1.0
    return a


def test_profit_side_traits() -> None:
    rng = np.random.default_rng(0)
    # Discipline: take profit di atas ambang serakah
    assert _agent("disciplined")._psych_override(0.2, 150.0, rng) < -0.4
    # Denial: serakah, tidak take profit selama masih untung (berapa pun untungnya); sinyal jual
    # hanya dijalankan sedikit sekali — juga saat agen sedang "tidak bereaksi" (reaction delay)
    denial = _agent("bagholder")
    out = denial._psych_override(-1.4, 150.0, rng)
    assert -0.3 < out <= 0, out
    assert denial._psych_override(0.3, 150.0, rng) == 0.3
    assert -0.3 < denial._psych_override(-1.4, 110.0, rng) <= 0          # untung kecil pun ditahan
    assert denial._psych_override(-1.4, 95.0, rng) == -1.4               # untung habis → bisa keluar
    # last_added hanya benar pada tick averager benar-benar menambah posisi
    # Averager: nambah posisi saat harga naik (average up) selama modal ada, lalu baru take profit
    avg = _agent("averager")
    out = avg._psych_override(0.0, 125.0, rng)               # untung 25% > 0,5 × ambang serakah
    assert out > 0 and avg.position > 1.0 and avg.entry_price > 100.0 and avg.capital_remaining < 1.0, (out, avg)
    assert avg.last_added and avg.to_dict().get("add") is True
    assert "add" not in _agent("disciplined").to_dict()
    avg.capital_remaining = 0.1
    assert avg._psych_override(0.0, 200.0, rng) < -0.4      # modal habis + untung besar → take profit
    # Averager tetap averaging down saat rugi
    down = _agent("averager")
    assert down._psych_override(-0.5, 70.0, rng) > 0 and down.entry_price < 100.0


def test_state_stats() -> None:
    m = Market(n_agents=100, seed=3, fundamental=1000.0)
    for _ in range(400):
        m.step()
    ps = m.get_state()["psych_stats"]
    for k in ("in_position", "in_pain", "in_profit", "averaging", "averaging_up", "averaging_down", "avg_pnl_pct"):
        assert k in ps, k
    assert ps["averaging_up"] + ps["averaging_down"] == ps["averaging"]
    assert 0 <= ps["in_profit"] <= ps["in_position"]


if __name__ == "__main__":
    test_url_validation()
    print("ok  test_url_validation")
    test_slug_and_tickers()
    print("ok  test_slug_and_tickers")
    test_rules_sentiment()
    print("ok  test_rules_sentiment")
    test_analyze_with_fake_fetch()
    print("ok  test_analyze_with_fake_fetch")
    test_shift_fundamental_bounds()
    print("ok  test_shift_fundamental_bounds")
    test_profit_side_traits()
    print("ok  test_profit_side_traits")
    test_state_stats()
    print("ok  test_state_stats")
    with TestClient(server.app) as client:
        test_endpoint_and_injection(client)
        print("ok  test_endpoint_and_injection")
    print("SEMUA TES news LULUS")

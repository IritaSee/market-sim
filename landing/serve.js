/**
 * SimPasar — landing page static server (tanpa dependensi, Node >= 18).
 *
 * Jalankan:
 *   node landing/serve.js          # → http://localhost:5173
 *   PORT=3000 node landing/serve.js
 *
 * Melayani folder ini di "/", plus alias "/paper.pdf" → PDF draf paper di root proyek.
 * Permintaan "/api/..." diteruskan ke server simulator (bawaan http://127.0.0.1:8000, ubah lewat
 * SIMPASAR_API) seperti nginx di produksi, supaya pil IHSG di hero ikut tampil saat server jalan.
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const PROJECT = path.resolve(__dirname, "..");
const PORT = Number(process.env.PORT) || 5173;
const API = process.env.SIMPASAR_API || "http://127.0.0.1:8000";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".json": "application/json; charset=utf-8",
  ".pdf": "application/pdf",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
};

function findPaper() {
  try {
    return fs.readdirSync(PROJECT).find((f) => f.toLowerCase().endsWith(".pdf")) || null;
  } catch {
    return null;
  }
}

function send404(res) {
  res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
  res.end("404 — tidak ditemukan");
}

function stream(res, file) {
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, {
    "Content-Type": MIME[ext] || "application/octet-stream",
    "Cache-Control": "no-cache",
  });
  fs.createReadStream(file).pipe(res);
}

function proxyApi(req, res) {
  let target;
  try {
    target = new URL(req.url, API);
  } catch {
    return send404(res);
  }
  const upstream = http.request(target, { method: req.method, headers: { ...req.headers, host: target.host } }, (r) => {
    r.on("error", () => res.destroy());
    res.writeHead(r.statusCode || 502, r.headers);
    r.pipe(res);
  });
  upstream.on("error", () => {
    if (res.headersSent) return res.destroy();       // respons sudah setengah jalan: putus saja
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("server simulator tidak berjalan");
  });
  res.on("close", () => upstream.destroy());
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  if ((req.url || "").startsWith("/api/")) return proxyApi(req, res);

  let url;
  try {
    url = decodeURIComponent((req.url || "/").split("?")[0]);
  } catch {
    return send404(res);
  }

  if (url === "/paper.pdf") {
    const paper = findPaper();
    if (!paper) return send404(res);
    return stream(res, path.join(PROJECT, paper));
  }

  if (url === "/") url = "/index.html";
  const file = path.normalize(path.join(ROOT, url));
  if (!file.startsWith(ROOT)) return send404(res);

  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) return send404(res);
    stream(res, file);
  });
});

server.listen(PORT, () => {
  console.log(`SimPasar landing → http://localhost:${PORT}`);
  console.log(`paper: ${findPaper() ? "/paper.pdf tersedia" : "PDF tidak ditemukan di root proyek"}`);
});

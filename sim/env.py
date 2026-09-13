"""
Pemuat file .env minimal tanpa dependensi (pengganti python-dotenv).

Dipakai server.py dan scripts/dev_server.py supaya kunci lokal seperti
GEMINI_API_KEY cukup ditaruh di file .env (sudah ada di .gitignore) tanpa perlu
diekspor manual di terminal.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: str | os.PathLike[str] = ".env", override: bool = False) -> list[str]:
    """
    Muat pasangan KEY=VALUE dari `path` ke os.environ.

    - Baris kosong dan komentar (#) diabaikan; awalan `export ` diperbolehkan.
    - Nilai boleh diapit tanda kutip tunggal atau ganda.
    - Untuk nilai tanpa kutip, komentar di akhir baris (" #...") dibuang.
    - Variabel yang sudah ada di environment TIDAK ditimpa kecuali override=True,
      sehingga nilai dari shell atau CI selalu menang atas .env.

    Mengembalikan daftar nama variabel yang dimuat (tanpa nilainya).
    """
    p = Path(path)
    if not p.is_file():
        return []
    loaded: list[str] = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        else:
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded

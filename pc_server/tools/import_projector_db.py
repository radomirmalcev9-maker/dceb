"""Convert assets/projector.db (from Projector Remote Control_2.3.apk)
into Flipper-style .ir files under irdb/Projectors/<Brand>/<Model>.ir.

remote table: _id, fragment (model), buttonFragment (button), brand,
frequency, pattern (comma-separated microseconds).
"""
import re
import sqlite3
import sys
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/proj/assets/projector.db")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "irdb" / "Projectors")

SAFE = re.compile(r"[^A-Za-z0-9 _\-.()]+")

def safe(s: str) -> str:
    return SAFE.sub("_", s).strip("._")[:80] or "unknown"

def main() -> int:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT fragment, buttonFragment, brand, frequency, pattern FROM remote").fetchall()
    files = 0
    signals = 0
    # group rows by (brand, model)
    grouped: dict[tuple[str, str], list[tuple[str, int, str]]] = {}
    for model, button, brand, freq, pattern in rows:
        grouped.setdefault((safe(brand), safe(model)), []).append(
            (safe(button), int(freq or 38000), pattern))
    for (brand, model), entries in grouped.items():
        d = OUT / brand
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{model}.ir"
        n = 0
        while f.exists():
            n += 1
            f = d / f"{model}_{n}.ir"
        with f.open("w", encoding="utf-8") as fh:
            fh.write("Filetype: IR signals file\nVersion: 1\n")
            fh.write(f"# source: Projector Remote Control_2.3.apk projector.db\n")
            for button, freq, pattern in entries:
                data = " ".join(p.strip() for p in pattern.split(",") if p.strip())
                fh.write(f"name: {button}\ntype: raw\nfrequency: {freq}\n"
                         f"duty_cycle: 0.33\ndata: {data}\n#\n")
                signals += 1
        files += 1
    print(f"wrote {files} .ir files, {signals} signals -> {OUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())

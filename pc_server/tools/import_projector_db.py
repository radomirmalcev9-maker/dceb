"""Convert ``assets/projector.db`` from ``Projector Remote Control_2.3.apk``
into Flipper-style raw ``.ir`` files under ``irdb/Projectors/<Brand>/<Model>.ir``.

The donor database's ``remote`` table looks like this::

    _id | fragment (model) | buttonFragment (button) | brand |
    frequency | pattern (comma-separated microseconds)

One model repeats the same button across several rows (different codes the
donor app offers for that model), so rows are grouped by ``(brand, model)``
and each distinct ``(button, frequency, pattern)`` is written exactly once.
Duplicate buttons in a Flipper file are legal but they make the phone UI
show the same chip twice and they bloat the index for nothing.

Usage::

    python tools/import_projector_db.py [PROJECTOR.DB] [OUT_DIR]
    python tools/import_projector_db.py --from-apk ../Projector\\ Remote\\ Control_2.3.apk
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
APK_DEFAULT = REPO_ROOT / "Projector Remote Control_2.3.apk"
OUT_DEFAULT = HERE.parent / "irdb" / "Projectors"
DB_IN_APK = "assets/projector.db"

SAFE = re.compile(r"[^A-Za-z0-9 _\-.()]+")
FILE_TYPE = "Filetype: IR signals file"

# Sanity window for one mark/space, in microseconds. Anything outside is donor
# noise, not IR:
#   * "< 20"  — rounding debris and zeros (a 0-length mark is meaningless)
#   * "> 5 s" — the donor ships "Delay" rows whose single 260000 us value is a
#               UI wait, and a couple of rows where a 32-bit overflow left
#               80 085 408 us (80 s) in the pulse array
MIN_PULSE_US = 20
MAX_PULSE_US = 5_000_000


def safe(value: str | None) -> str:
    """Filesystem-safe name, tolerant of NULLs and unicode junk in the DB."""
    cleaned = SAFE.sub("_", (value or "")).strip("._ ")
    return cleaned[:80] or "unknown"


@dataclass
class ConvertReport:
    source: Path
    out: Path
    rows: int = 0
    files: int = 0
    signals: int = 0
    duplicates: int = 0
    rows_repaired: int = 0
    rows_dropped: int = 0
    dropped_buttons: list[str] = field(default_factory=list)
    brands: set[str] = field(default_factory=set)

    def summary(self) -> str:
        lines = [
            f"source: {self.source}",
            f"target: {self.out}",
            f"rows read: {self.rows}",
            f"files written: {self.files}",
            f"signals written: {self.signals}",
            f"duplicate rows dropped: {self.duplicates}",
            f"brands: {len(self.brands)}",
        ]
        if self.rows_repaired:
            lines.append(f"rows with salvageable garbage: {self.rows_repaired}")
        if self.rows_dropped:
            lines.append(f"rows dropped as unusable: {self.rows_dropped}")
            lines += [f"  - {name}" for name in self.dropped_buttons[:10]]
        return "\n".join(lines)


def extract_db_from_apk(apk: Path) -> Path:
    """Pull assets/projector.db out of the donor APK into a temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="dceb-projdb-"))
    with zipfile.ZipFile(apk) as zf:
        try:
            payload = zf.read(DB_IN_APK)
        except KeyError as exc:
            raise FileNotFoundError(f"{DB_IN_APK} missing inside {apk}") from exc
    target = tmp / "projector.db"
    target.write_bytes(payload)
    return target


def _normalise_pattern(raw: str | None) -> tuple[list[int], bool]:
    """Parse one donor ``pattern`` string into pulses. Returns ``(pulses, repaired)``.

    Only genuine damage is touched:

    * non-numeric tokens are cut out — the donor's own Java class constants
      (``ConnectionResult.DRIVE_EXTERNAL_STORAGE_REQUIRED``,
      ``TypedValues.TransitionType.TYPE_DURATION``) were spliced into two
      Epson T1 pulse arrays by an unpacking bug in the original app;
    * values outside the plausible IR window are cut out — that catches the
      32-bit overflow that left ``80085408`` us (80 s) in two 3M rows and the
      single ``260000`` "Delay" rows, which are UI waits, not marks.

    What is deliberately *not* touched: odd-length patterns (109 rows in the
    donor legitimately trail off on a mark with no closing space — real
    receivers accept that) and the ``200, 3600, ...`` preamble that a whole
    family of donor rows carries. Reshaping those would be inventing a frame
    the donor never recorded.
    """
    pulses: list[int] = []
    repaired = False
    for chunk in (raw or "").split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            repaired = True
            continue
        if MIN_PULSE_US <= value <= MAX_PULSE_US:
            pulses.append(value)
        else:
            repaired = True
    return pulses, repaired


def convert(db: Path = DB_IN_APK, out: Path = OUT_DEFAULT) -> ConvertReport:
    db = Path(db)
    if not db.is_file():
        raise FileNotFoundError(f"projector.db not found: {db}")
    report = ConvertReport(source=db, out=Path(out))
    Path(out).mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT fragment, buttonFragment, brand, frequency, pattern "
            "FROM remote ORDER BY brand, fragment, _id").fetchall()
    finally:
        con.close()
    report.rows = len(rows)

    grouped: dict[tuple[str, str], dict[tuple[str, int, tuple[int, ...]], None]] = {}
    for model_raw, button_raw, brand_raw, freq_raw, pattern_raw in rows:
        label = f"{safe(brand_raw)}/{safe(model_raw)}/{safe(button_raw)}"
        pulses, repaired = _normalise_pattern(pattern_raw)
        if repaired:
            report.rows_repaired += 1
        if not pulses:
            report.rows_dropped += 1
            report.dropped_buttons.append(label)
            continue
        brand, model = safe(brand_raw), safe(model_raw)
        key = (brand, model)
        signal = (safe(button_raw), int(freq_raw or 38000), tuple(pulses))
        bucket = grouped.setdefault(key, {})
        if signal in bucket:
            report.duplicates += 1
            continue
        bucket[signal] = None

    for (brand, model), bucket in grouped.items():
        target_dir = Path(out) / brand
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{model}.ir"
        suffix = 0
        while target.exists():
            suffix += 1
            target = target_dir / f"{model}_{suffix}.ir"
        with target.open("w", encoding="utf-8") as fh:
            fh.write(f"{FILE_TYPE}\nVersion: 1\n")
            fh.write("# source: Projector Remote Control_2.3.apk projector.db\n")
            for (button, freq, pattern), _ in bucket.items():
                data = " ".join(str(v) for v in pattern)
                fh.write(f"name: {button}\ntype: raw\nfrequency: {freq}\n"
                         f"duty_cycle: 0.33\ndata: {data}\n#\n")
                report.signals += 1
        report.files += 1
        report.brands.add(brand)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert the projector donor DB into Flipper .ir files")
    parser.add_argument("db", nargs="?", default=None,
                        help=f"projector.db path (default: {DB_IN_APK} inside the donor APK)")
    parser.add_argument("out", nargs="?", default=str(OUT_DEFAULT),
                        help=f"output directory (default: {OUT_DEFAULT})")
    parser.add_argument("--from-apk", default=str(APK_DEFAULT),
                        help=f"donor APK to extract the DB from (default: {APK_DEFAULT})")
    args = parser.parse_args(argv)

    source = Path(args.db) if args.db else None
    try:
        if source is None:
            source = extract_db_from_apk(Path(args.from_apk))
        report = convert(source, Path(args.out))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Extract the bundled Flipper-IRDB from ``app-release.apk`` into ``irdb/``.

The donor APK in the repository root ships the full Flipper-IRDB as
``assets/Flipper-IRDB-main/<Category>/<Brand>/<Model>.ir``. This script pulls
every ``.ir`` file out into ``pc_server/irdb/`` so the FastAPI server
(``GET /api/v1/irdb/*``) and the phone client have something to search.

Usage::

    python tools/import_flipper_irdb.py [APK] [OUT_DIR]

Two things make the extraction less trivial than unzip:

* **Zip entry names are cp437-mangled.** The donor APK stores non-ASCII
  folder names (the Russian "ИК глушилка" jammer category) as raw UTF-8
  bytes that ``zipfile`` decodes with cp437. Names are re-decoded here,
  otherwise the same category shows up as ``╨ÿ╨Ü ╨│╨╗...``.
* **One category is a deliberate drop.** ``ИК глушилка`` (IR jammer) is
  skipped by default: the project is a personal lab tool, and shipping a
  "block every TV in the room" signal set contradicts its own stated
  purpose. ``--keep-jammer`` exists for anyone who really means it; the
  default path stays clean.

Every written file is checked for the ``Filetype: IR signals file`` header
before it lands, so a half-broken donor asset fails loudly instead of
silently poisoning the index. Two malformed headers in the donor are
*repaired* rather than dropped (see ``normalise_header``), because the signal
bodies underneath them are perfectly good:

* ``Filetype: \\nIR signals file``  — a stray newline inside the keyword
* ``Filetype: IR signals file Version: 1 `` — the whole header on one line

Flipper-IRDB nests a few models one level deeper than ``Category/Brand/
Model.ir`` (``Blu-Ray/Panasonic/Universal_Region_Unlock/....ir``). Those are
flattened into ``Category/Brand/<subdir>_<Model>.ir`` instead of being
thrown away, and depth-1 files (``Fans/Generic_Fan_HS95104SK.ir``) land in an
``Unbranded`` brand folder.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APK_DEFAULT = Path(__file__).resolve().parents[2] / "app-release.apk"
OUT_DEFAULT = Path(__file__).resolve().parent.parent / "irdb"

ASSET_PREFIX = "assets/Flipper-IRDB-main/"
JAMMER_DIR = "ИК глушилка"
FILE_TYPE = "Filetype: IR signals file"
UNBRANDED = "Unbranded"

# Flipper-IRDB dirs are already filesystem-safe; this is belt-and-braces for
# donor variants that add ":" or newlines to brand names.
_SAFE = re.compile(r"[^A-Za-z0-9 _\-.()\u0400-\u04FF]+")

# "Filetype:\nIR signals file" and "Filetype: IR signals file Version: 1"
_HEADER_SPLIT = re.compile(r"^Filetype:\s*\r?\n\s*IR signals file", re.MULTILINE)
_HEADER_ONELINE = re.compile(r"^Filetype:\s*IR signals file[^\n]*$", re.MULTILINE)


@dataclass
class ImportReport:
    apk: Path
    out: Path
    files: int = 0
    signals: int = 0
    skipped_jammer: int = 0
    repaired_headers: int = 0
    flattened_nested: int = 0
    skipped_bad_header: list[str] = field(default_factory=list)
    categories: set[str] = field(default_factory=set)

    def summary(self) -> str:
        lines = [
            f"source: {self.apk}",
            f"target: {self.out}",
            f"files written: {self.files}",
            f"signals counted: {self.signals}",
            f"categories: {len(self.categories)}",
        ]
        if self.repaired_headers:
            lines.append(f"headers repaired: {self.repaired_headers}")
        if self.flattened_nested:
            lines.append(f"nested models flattened: {self.flattened_nested}")
        if self.skipped_jammer:
            lines.append(f"jammer files skipped: {self.skipped_jammer}")
        if self.skipped_bad_header:
            lines.append(f"rejected (unusable header): {len(self.skipped_bad_header)}")
            lines += [f"  - {name}" for name in self.skipped_bad_header[:10]]
        return "\n".join(lines)


def decode_entry_name(name: str) -> str:
    """Undo cp437 mangling of UTF-8 zip entry names (the jammer dir)."""
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def safe_segment(segment: str) -> str:
    cleaned = _SAFE.sub("_", segment).strip("._ ")
    return cleaned[:80] or "unknown"


def count_signals(text: str) -> int:
    """Number of entries in a .ir file — one ``name:`` line per signal."""
    return sum(1 for line in text.splitlines() if line.startswith("name:"))


def normalise_header(text: str) -> str | None:
    """Force the first line to the canonical magic, or None if hopeless.

    The signal body is never touched — only the broken ``Filetype`` line —
    because rebuilding the header is what lets those files survive the
    ``FILE_TYPE in text[:200]`` gate downstream.
    """
    if text.startswith(FILE_TYPE):
        return text
    if _HEADER_SPLIT.match(text):
        return FILE_TYPE + _HEADER_SPLIT.sub("", text, count=1)
    if _HEADER_ONELINE.match(text):
        body = _HEADER_ONELINE.sub(FILE_TYPE, text, count=1)
        return body
    return None


def iter_ir_entries(apk: Path):
    """Yield (decoded_path, category, brand, model, bytes) for every .ir.

    ``brand`` is ``UNBRANDED`` for depth-1 files; ``model`` carries the nested
    subdirectory as a prefix so nothing collides once flattened.
    """
    with zipfile.ZipFile(apk) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(ASSET_PREFIX):
                continue
            if not info.filename.endswith(".ir"):
                continue
            decoded = decode_entry_name(info.filename)
            parts = decoded[len(ASSET_PREFIX):].split("/")
            if len(parts) == 2:                      # Category/Model.ir
                category, model_file = parts
                brand = UNBRANDED
                model = Path(model_file).stem
            elif len(parts) == 3:                    # Category/Brand/Model.ir
                category, brand, model_file = parts
                model = Path(model_file).stem
            elif len(parts) == 4:                    # Category/Brand/Sub/Model.ir
                category, brand, subdir, model_file = parts
                model = f"{subdir}_{Path(model_file).stem}"
            else:
                continue
            yield decoded, category, brand, model, zf.read(info)


def import_irdb(apk: Path = APK_DEFAULT, out: Path = OUT_DEFAULT,
                keep_jammer: bool = False, clean: bool = False) -> ImportReport:
    if not Path(apk).is_file():
        raise FileNotFoundError(f"donor APK not found: {apk}")
    report = ImportReport(apk=Path(apk), out=Path(out))
    if clean and report.out.exists():
        for child in report.out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            elif child.name != ".gitkeep":
                child.unlink()
    report.out.mkdir(parents=True, exist_ok=True)

    for decoded, category, brand, model, raw in iter_ir_entries(Path(apk)):
        if category == JAMMER_DIR and not keep_jammer:
            report.skipped_jammer += 1
            continue
        if decoded[len(ASSET_PREFIX):].count("/") == 3:
            report.flattened_nested += 1
        text = raw.decode("utf-8", errors="replace")
        repaired = normalise_header(text)
        if repaired is None:
            report.skipped_bad_header.append(decoded)
            continue
        if repaired != text:
            report.repaired_headers += 1
        text = repaired
        target_dir = report.out / safe_segment(category) / safe_segment(brand)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_segment(model)}.ir"
        suffix = 0
        while target.exists():
            suffix += 1
            target = target_dir / f"{safe_segment(model)}_{suffix}.ir"
        target.write_text(text, encoding="utf-8")
        report.files += 1
        report.signals += count_signals(text)
        report.categories.add(safe_segment(category))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("apk", nargs="?", default=str(APK_DEFAULT),
                        help=f"donor APK (default: {APK_DEFAULT})")
    parser.add_argument("out", nargs="?", default=str(OUT_DEFAULT),
                        help=f"output dir (default: {OUT_DEFAULT})")
    parser.add_argument("--keep-jammer", action="store_true",
                        help=f'also import the "{JAMMER_DIR}" category (off by default)')
    parser.add_argument("--clean", action="store_true",
                        help="wipe existing category dirs under OUT before writing")
    args = parser.parse_args(argv)

    try:
        report = import_irdb(Path(args.apk), Path(args.out),
                             keep_jammer=args.keep_jammer, clean=args.clean)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())

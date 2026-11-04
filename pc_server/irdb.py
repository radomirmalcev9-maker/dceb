"""Server-side index over the Flipper-style IR database stored in irdb/.

Layout mirrors the Flipper-IRDB repo:
    irdb/TVs/<Brand>/<Model>.ir
    irdb/ACs/<Brand>/<Model>.ir
    irdb/Audio/...
Files use the Flipper `.ir` format ("parsed" NECext/Samsung32/... entries or
"raw" frequency+duty_cycle+data entries).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

IRDB_ROOT = Path(os.environ.get("DCEB_IRDB_ROOT", "irdb")).resolve()
DUMPS_ROOT = Path(os.environ.get("DCEB_DUMPS_ROOT", "dumps")).resolve()

_SAFE_PATH = re.compile(r"^[A-Za-z0-9 _\-.()]+$")


def _allowed_category(name: str) -> bool:
    """Any existing top-level dir is a valid category (TVs, Projectors, ...),
    as long as the name is path-safe. The IR-jammer category from the donor
    APK is deliberately excluded at import time (see tools/)."""
    return bool(_SAFE_PATH.match(name))


@dataclass
class IrFile:
    category: str
    brand: str
    model: str
    path: Path


def _safe(segment: str) -> bool:
    return bool(segment) and bool(_SAFE_PATH.match(segment))


def list_entries() -> List[IrFile]:
    entries: List[IrFile] = []
    if not IRDB_ROOT.is_dir():
        return entries
    for category_dir in sorted(IRDB_ROOT.iterdir()):
        if not category_dir.is_dir() or not _allowed_category(category_dir.name):
            continue
        for brand_dir in sorted(category_dir.iterdir()):
            if not brand_dir.is_dir():
                continue
            for ir_file in sorted(brand_dir.glob("*.ir")):
                entries.append(IrFile(category_dir.name, brand_dir.name,
                                      ir_file.stem, ir_file))
    return entries


def search(query: str, limit: int = 50, category: str | None = None) -> List[IrFile]:
    entries = list_entries()
    if category:
        entries = [e for e in entries if e.category == category]
    q = query.strip().lower()
    if not q:
        return entries[:limit]
    return [e for e in entries
            if q in e.brand.lower() or q in e.model.lower()
            or q in f"{e.category}/{e.brand}/{e.model}".lower()][:limit]


def categories() -> List[str]:
    if not IRDB_ROOT.is_dir():
        return []
    return sorted(d.name for d in IRDB_ROOT.iterdir()
                  if d.is_dir() and _allowed_category(d.name))


def read_entry(category: str, brand: str, model: str) -> str | None:
    """Return the .ir file text; path traversal is blocked by _safe()."""
    if not (_safe(category) and _safe(brand) and _safe(model)):
        return None
    if not _allowed_category(category):
        return None
    candidate = IRDB_ROOT / category / brand / f"{model}.ir"
    if not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8", errors="replace")


def save_dump(phone_name: str, content: str) -> Path:
    """Persist a raw dump uploaded from the phone into dumps/."""
    DUMPS_ROOT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_\-.]", "_", phone_name)[:80] or "dump"
    path = DUMPS_ROOT / f"{safe}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def dumps_count() -> int:
    if not DUMPS_ROOT.is_dir():
        return 0
    return sum(1 for p in DUMPS_ROOT.iterdir() if p.is_file())

"""Chat archiving, modelled on the ``arenaaispofandpars`` behaviour.

That project archives an Arena conversation as soon as the answer is fully
read (``AutoOptions.archive_after_answer``). Its hard-won details are copied
here on purpose:

* **Archive after the answer, not before it.** The response is already on its
  way to the phone when the filing happens.
* **Never stand between two turns.** If the next request for the same session
  is already in flight, the archive is *skipped* (``archive.skipped``) and the
  session becomes a debt in :data:`ChatArchive.pending`.
* **Debts are paid.** The pending set is replayed on the next turn, so a
  skipped session is still filed even though another turn has since run.
* **Archive by id**, never "whatever session is current" — that is why a
  postponed chat can still be filed later.
* **Failure is a phase, not an exception.** ``archive.missed`` /
  ``archive.error`` are logged and the session stays in the debt list.

On disk:

    archives/open/<session_id>.jsonl   # live transcript, appended per turn
    archives/<YYYY-MM-DD>.jsonl        # closed sessions, one file per day
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

log = logging.getLogger("dceb.archive")

ARCHIVE_ROOT = Path(os.environ.get("DCEB_ARCHIVE_ROOT", "archives")).resolve()
RETENTION_DAYS = int(os.environ.get("DCEB_ARCHIVE_RETENTION_DAYS", "30"))
MAX_PENDING = 64


def _now() -> float:
    return time.time()


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


@dataclass
class Turn:
    ts: float
    role: str                      # "user" | "assistant"
    content: str = ""
    mode: str = ""
    action: str = ""
    confidence: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_line(self, session: str) -> str:
        return json.dumps({
            "ts": _stamp(self.ts), "epoch": round(self.ts, 3), "session": session,
            "role": self.role, "content": self.content, "mode": self.mode,
            "action": self.action, "confidence": self.confidence, "meta": self.meta,
        }, ensure_ascii=False)


class ChatArchive:
    """Append-only chat archive with background finalization."""

    def __init__(self, root: Path = ARCHIVE_ROOT, retention_days: int = RETENTION_DAYS,
                 enabled: bool = True) -> None:
        self.root = Path(root)
        self.open_dir = self.root / "open"
        self.retention_days = retention_days
        self.enabled = enabled
        self.pending: List[str] = []            # sessions whose archive was skipped
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._queue: List[str] = []
        self._cond = threading.Condition()
        self._worker.start()

    # ---------------------------------------------------------------- setup
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.open_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- recording
    def open_session(self, session_id: str, meta: Optional[dict] = None) -> None:
        """Write a session header the first time a session is seen."""
        if not self.enabled:
            return
        self.ensure()
        path = self.open_dir / f"{_safe(session_id)}.jsonl"
        if path.exists():
            return
        with self._lock:
            path.write_text(json.dumps({
                "ts": _stamp(_now()), "session": session_id,
                "event": "session_start", "meta": meta or {}}, ensure_ascii=False) + "\n",
                encoding="utf-8")

    def record_turn(self, session_id: str, turn: Turn) -> None:
        if not self.enabled:
            return
        self.ensure()
        with self._lock:
            with (self.open_dir / f"{_safe(session_id)}.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(turn.to_line(session_id) + "\n")

    # ---------------------------------------------------------- finalization
    def finalize(self, session_id: str,
                 idle_fn: Optional[Callable[[str], bool]] = None) -> bool:
        """File the session into the daily archive. Returns True on success."""
        if not self.enabled:
            return False
        log.debug("archive.start session=%s", session_id)
        if idle_fn is not None and not idle_fn(session_id):
            self._mark_pending(session_id)
            log.info("archive.skipped session=%s (next turn already waiting)", session_id)
            return False
        try:
            self.ensure()
            src = self.open_dir / f"{_safe(session_id)}.jsonl"
            if not src.exists():
                log.info("archive.missed session=%s (no transcript)", session_id)
                return False
            turns = [ln for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
            turns.append(json.dumps({
                "ts": _stamp(_now()), "session": session_id,
                "event": "session_end", "turns": len(turns)}, ensure_ascii=False))
            day = time.strftime("%Y-%m-%d")
            with self._lock:
                with (self.root / f"{day}.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(turns) + "\n")
                src.unlink(missing_ok=True)
            self._unmark_pending(session_id)
            log.info("archive.done session=%s turns=%d", session_id, len(turns))
            return True
        except Exception as exc:                                # noqa: BLE001
            log.warning("archive.error session=%s: %s", session_id, exc)
            self._mark_pending(session_id)
            return False

    def finalize_async(self, session_id: str,
                       idle_fn: Optional[Callable[[str], bool]] = None) -> None:
        """Queue finalization on the background worker (never blocks a reply)."""
        with self._cond:
            self._queue.append(session_id)
            self._cond.notify()
        self._idle_hook = idle_fn

    # Background worker ----------------------------------------------------
    _idle_hook: Optional[Callable[[str], bool]] = None

    def _drain(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                session_id = self._queue.pop(0)
            try:
                # Debts first: a skipped session is older than the current one.
                for owed in list(self.pending):
                    if owed != session_id:
                        self.finalize(owed, self._idle_hook)
                self.finalize(session_id, self._idle_hook)
            except Exception as exc:                            # noqa: BLE001
                log.warning("archive.worker error: %s", exc)

    # ------------------------------------------------------------- debt list
    def _mark_pending(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self.pending:
                self.pending.append(session_id)
                del self.pending[:-MAX_PENDING]

    def _unmark_pending(self, session_id: str) -> None:
        with self._lock:
            if session_id in self.pending:
                self.pending.remove(session_id)

    # ----------------------------------------------------------------- reads
    def list_archives(self) -> List[dict]:
        self.ensure()
        out = []
        for path in sorted(self.root.glob("*.jsonl"), reverse=True):
            out.append({"name": path.name, "size": path.stat().st_size,
                        "modified": path.stat().st_mtime})
        for path in sorted(self.open_dir.glob("*.jsonl")):
            out.append({"name": f"open/{path.stem}", "size": path.stat().st_size,
                        "modified": path.stat().st_mtime, "open": True})
        return out

    def read_archive(self, name: str, limit: int = 500) -> List[dict]:
        path = self._resolve(name)
        if path is None or not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def delete_archive(self, name: str) -> bool:
        path = self._resolve(name)
        if path is None or not path.is_file():
            return False
        with self._lock:
            path.unlink()
        return True

    def stats(self) -> dict:
        self.purge()
        files = list(self.root.glob("*.jsonl"))
        return {"enabled": self.enabled, "root": str(self.root),
                "days": len(files), "open_sessions": len(list(self.open_dir.glob("*.jsonl"))),
                "pending": list(self.pending),
                "retention_days": self.retention_days,
                "total_bytes": sum(p.stat().st_size for p in files)}

    def purge(self) -> int:
        """Drop daily files older than the retention window."""
        self.ensure()
        cutoff = _now() - self.retention_days * 86400
        removed = 0
        for path in self.root.glob("*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        return removed

    # ---------------------------------------------------------------- utils
    def _resolve(self, name: str) -> Optional[Path]:
        candidate = (self.root / name).resolve()
        if self.root in candidate.parents and candidate.is_file():
            return candidate
        return None


def _safe(session_id: str) -> str:
    keep = [c for c in (session_id or "default") if c.isalnum() or c in "._-"]
    return "".join(keep)[:64] or "default"


archive = ChatArchive()

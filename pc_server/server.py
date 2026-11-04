"""Do Cats Eat Bats — PC AI backend (FastAPI).

Endpoints:
  GET  /api/v1/health          ping + live AI probe + IRDB stats
  POST /api/v1/process         generic task from the phone (mode-aware, with memory)
  POST /api/v1/parse_dump      analyze raw IR/Sub-GHz/dmesg/NFC/Bluetooth dump
  POST /api/v1/netscan         AUTHORIZED LAN inventory (explicit owner consent)
  POST /api/v1/dumps/upload    store a raw dump sent from the phone
  GET  /api/v1/irdb/search?q=  search the local .ir database
  GET  /api/v1/irdb/file       fetch one .ir file by category/brand/model
  GET  /api/v1/knowledge/search?q=  peek at the local grounding notes
  POST /api/v1/session/reset   drop the server-side transcript for a session
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, Dict, List

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import netscan as netscan_mod
from ai_bridge import MODES, bridge
from knowledge import retrieve
from schemas import (ActionKind, AiActionResponse, ChatTurn, HealthResponse,
                     NetHost, NetScanRequest, NetScanResponse, ParseDumpRequest,
                     ProcessRequest)
from irdb import dumps_count, list_entries, read_entry, save_dump, search

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dceb.server")

VERSION = "0.2.0"
STARTED = time.time()

# Per-session short-term memory (session_id -> recent turns).
MAX_TURNS = 20
_SESSIONS: Dict[str, Deque[ChatTurn]] = {}

# Cache the live model probe so /health stays cheap.
_PROBE_CACHE_SECONDS = 30.0
_probe_cache: dict = {"at": 0.0, "ok": False, "error": None}

app = FastAPI(title="Do Cats Eat Bats — Capricorn AI Bridge", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # lab tool on a private network
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _session(session_id: str) -> Deque[ChatTurn]:
    return _SESSIONS.setdefault(session_id or "default", deque(maxlen=MAX_TURNS))


def _history_for(req_history: List[ChatTurn], session_id: str) -> List[ChatTurn]:
    """Client transcript wins when supplied; otherwise use server memory."""
    if req_history:
        return list(req_history)
    return list(_session(session_id))


def _remember(session_id: str, user_text: str, assistant_text: str) -> None:
    store = _session(session_id)
    store.append(ChatTurn(role="user", content=user_text[:2000]))
    store.append(ChatTurn(role="assistant", content=assistant_text[:2000]))


def _probe_ai() -> tuple:
    now = time.time()
    if now - _probe_cache["at"] < _PROBE_CACHE_SECONDS:
        return _probe_cache["ok"], _probe_cache["error"]
    ok, error = bridge.probe()
    _probe_cache.update(at=now, ok=ok, error=error)
    return ok, error


def _sanitize(resp: AiActionResponse, context: str) -> AiActionResponse:
    """Downgrade anything that must never fire from an analysis path.

    * Dump analysis may not emit HID payloads (the owner's phone is not a
      keyboard for an unverified host).
    * `run_shell` has no approval path on the client at all, so it is always
      reduced to advice.
    """
    if resp.action == ActionKind.run_shell:
        return resp.model_copy(update={
            "action": ActionKind.info_only,
            "explanation": "[safety] shell action downgraded to advice "
                           "(no silent shell execution path). " + resp.explanation})
    if context != "hid" and resp.action == ActionKind.execute_hid:
        return resp.model_copy(update={
            "action": ActionKind.info_only,
            "explanation": "[safety] HID action downgraded to info_only "
                           "(dump analysis may not emit HID). " + resp.explanation})
    return resp


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/api/v1/health", response_model=HealthResponse)
def health(probe: bool = Query(True, description="run a live model probe (cached 30s)")) -> HealthResponse:
    if probe:
        ok, error = _probe_ai()
    else:
        ok, error = bridge.available, bridge.last_error
    return HealthResponse(
        status="ok",
        ai_backend=f"arenaaispofandpars/{bridge.available and 'capricorn' or 'offline'}",
        ai_session_ok=bridge.available,
        ai_probe_ok=bool(ok),
        ai_last_error=error,
        irdb_entries=len(list_entries()),
        dumps_stored=dumps_count(),
        version=VERSION,
        modes=list(MODES),
    )


# --------------------------------------------------------------------------- #
# AI endpoints
# --------------------------------------------------------------------------- #

@app.post("/api/v1/process", response_model=AiActionResponse)
def process(req: ProcessRequest) -> AiActionResponse:
    payload = (f"MODE: {req.mode}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW_DATA:\n{req.raw_data}")
    history = _history_for(req.history, req.session_id)
    try:
        result = bridge.ask(payload, mode=req.mode, history=history)
    except Exception as exc:
        log.exception("process failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    result = _sanitize(result, req.mode)
    _remember(req.session_id, f"[{req.mode}] {req.raw_data}",
              f"{result.action}: {result.explanation}")
    return result


@app.post("/api/v1/parse_dump", response_model=AiActionResponse)
def parse_dump(req: ParseDumpRequest) -> AiActionResponse:
    payload = (f"DUMP_TYPE: {req.dump_type}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW:\n{req.raw_data}")
    history = _history_for(req.history, req.session_id)
    try:
        result = bridge.ask(payload, mode=req.dump_type, history=history)
    except Exception as exc:
        log.exception("parse_dump failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    result = _sanitize(result, req.dump_type)
    _remember(req.session_id, f"[dump:{req.dump_type}] {req.raw_data}",
              f"{result.action}: {result.explanation}")
    return result


@app.post("/api/v1/netscan", response_model=NetScanResponse)
def net_scan(req: NetScanRequest) -> NetScanResponse:
    """Authorized LAN inventory.

    Requires ``confirm_owner=True``; non-private ranges additionally require
    ``allow_public=True``. Only ordinary connect() probes are used.
    """
    if not req.confirm_owner:
        raise HTTPException(
            status_code=403,
            detail="confirm_owner=True is required: scan only networks you own "
                   "or have written permission to test")
    try:
        raw = netscan_mod.scan(
            target=req.target,
            ports=req.ports,
            confirm_owner=True,
            allow_public=req.allow_public,
            max_hosts=req.max_hosts,
            timeout=req.timeout,
            grab_banner=req.grab_banner,
            do_ping=req.do_ping)
    except netscan_mod.ScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        log.exception("netscan failed")
        raise HTTPException(status_code=500, detail=f"scan error: {exc}")

    response = NetScanResponse(
        target=raw["target"],
        scanned_hosts=raw["scanned_hosts"],
        hosts_with_open_ports=raw["hosts_with_open_ports"],
        open_port_total=raw["open_port_total"],
        ports_probed=raw["ports_probed"],
        banner_grabbing=raw["banner_grabbing"],
        ping_sweep=raw["ping_sweep"],
        results=[NetHost(**h) for h in raw["results"]],
        note=raw["note"])

    # Ask Capri for an authorized read of the inventory (advisory only).
    digest = (f"Authorized inventory of {raw['target']}: "
              f"{raw['scanned_hosts']} host(s) probed on ports {raw['ports_probed']}.\n"
              + "\n".join(f"- {h['host']}: open {h['open_ports']}"
                          + (f" banners={h['banners']}" if h["banners"] else "")
                          for h in raw["results"] if h["open_ports"]))
    try:
        response.advice = bridge.ask(digest, mode="netscan",
                                     history=_history_for(req.history, req.session_id))
    except Exception as exc:                       # advice is optional
        log.warning("netscan advice failed: %s", exc)
    _remember(req.session_id, f"[netscan] {req.target}",
              f"{raw['hosts_with_open_ports']} host(s) with open ports")
    return response


@app.post("/api/v1/dumps/upload")
def upload_dump(name: str = Query("dump"), body: str = ""):
    if not body.strip():
        raise HTTPException(status_code=400, detail="empty body")
    path = save_dump(name, body)
    return {"status": "ok", "stored": str(path)}


@app.post("/api/v1/session/reset")
def session_reset(session_id: str = Query("default")):
    _SESSIONS.pop(session_id or "default", None)
    return {"status": "ok", "session_id": session_id, "turns": 0}


@app.get("/api/v1/knowledge/search")
def knowledge_search(q: str = Query(""), k: int = Query(3, ge=1, le=10)):
    notes = retrieve(q, k=k)
    return {"status": "ok",
            "results": [{"topic": n.topic, "text": n.text} for n in notes]}


# --------------------------------------------------------------------------- #
# IRDB
# --------------------------------------------------------------------------- #

@app.get("/api/v1/irdb/categories")
def irdb_categories():
    from irdb import categories
    return {"status": "ok", "categories": categories()}


@app.get("/api/v1/irdb/search")
def irdb_search(q: str = Query(""), category: str | None = Query(None)):
    return {"status": "ok",
            "results": [{"category": e.category, "brand": e.brand,
                         "model": e.model} for e in search(q, category=category)]}


@app.get("/api/v1/irdb/file")
def irdb_file(category: str, brand: str, model: str):
    content = read_entry(category, brand, model)
    if content is None:
        raise HTTPException(status_code=404, detail="not found or unsafe path")
    return {"status": "ok", "category": category, "brand": brand,
            "model": model, "content": content}


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #

@app.websocket("/ws/v1/ai")
async def ai_ws(websocket: WebSocket):
    """Streaming AI channel: phone sends {"mode","raw_data","device_context",
    "session_id","history"}, server replies with the strict AiActionResponse."""
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_json()
            mode = msg.get("mode", "")
            session_id = msg.get("session_id", "default")
            history = [ChatTurn(**t) for t in msg.get("history", [])
                       if isinstance(t, dict)] or list(_session(session_id))
            try:
                resp = bridge.ask(
                    f"MODE: {mode}\n"
                    f"DEVICE_CONTEXT: {msg.get('device_context', {})}\n"
                    f"RAW_DATA:\n{msg.get('raw_data', '')}",
                    mode=mode, history=history)
                resp = _sanitize(resp, mode)
                _remember(session_id, f"[{mode}] {msg.get('raw_data', '')}",
                          f"{resp.action}: {resp.explanation}")
                await websocket.send_json(resp.model_dump())
            except Exception as exc:
                await websocket.send_json({"status": "error", "detail": str(exc)})
    except WebSocketDisconnect:
        log.info("ws client disconnected")


@app.get("/")
def root():
    return {"name": "Do Cats Eat Bats PC Server", "docs": "/docs",
            "ws": "/ws/v1/ai", "modes": list(MODES),
            "uptime_s": round(time.time() - STARTED, 1)}

"""Do Cats Eat Bats — PC AI backend (FastAPI).

Endpoints:
  GET  /api/v1/health            ping + live AI probe + IRDB stats
  POST /api/v1/process           mode-aware task (memory, persona, archive)
  POST /api/v1/parse_dump        analyze IR/Sub-GHz/dmesg/NFC/Bluetooth dump
  POST /api/v1/netscan           AUTHORIZED LAN inventory (explicit consent)
  GET  /api/v1/network/scan      quick LAN inventory (private ranges only)
  GET  /api/v1/bluetooth/scan    BLE inventory of what is advertising nearby
  GET  /api/v1/bluetooth/gatt    GATT services of ONE device you name
  GET  /api/v1/irdb/resolve      phrase -> transmittable signal, no model
  POST /api/v1/dumps/upload      store a raw dump sent from the phone
  GET  /api/v1/irdb/search?q=    search the local .ir database
  GET  /api/v1/irdb/file         fetch one .ir file by category/brand/model
  GET  /api/v1/knowledge/search?q=   peek at the local grounding notes
  GET  /api/v1/archives*         chat archive: stats, list, read, delete
  POST /api/v1/session/reset     drop the server-side transcript
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import btscan
import irdb_resolver
import netscan
from ai_bridge import MODES, bridge
from archive import Turn, archive
from knowledge import retrieve
from schemas import (ActionKind, AiActionResponse, ChatTurn, HealthResponse,
                     NetHost, NetScanRequest, NetScanResponse, ParseDumpRequest,
                     ProcessRequest)
from irdb import dumps_count, list_entries, read_entry, save_dump, search

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dceb.server")

VERSION = "0.3.0"
STARTED = time.time()

# Per-session short-term memory (session_id -> recent turns).
MAX_TURNS = 20
_SESSIONS: Dict[str, Deque[ChatTurn]] = {}

# Sessions currently being answered: archiving must not race a live turn.
_IN_FLIGHT: Dict[str, int] = {}

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

# Modes that mean "look this device+button up in the database", not "ask the
# model". Bare "ir" is deliberately absent: that is a dump-analysis mode.
RESOLVE_MODES = {"resolve_ir", "irdb", "ir_request", "ir_lookup"}
SCAN_MODES = {"network_scan", "scan_network", "lan_scan"}
BLE_MODES = {"ble_scan", "bluetooth_scan", "scan_ble"}


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


def _archive_turn(session_id: str, mode: str, user_text: str,
                  resp: AiActionResponse, meta: Optional[dict] = None) -> None:
    """Record one exchange and file it, the way arenaaispofandpars does.

    The filing runs on the archive worker and is skipped (becoming a debt) when
    the next turn for this session is already in flight.
    """
    try:
        archive.open_session(session_id, meta)
        archive.record_turn(session_id, Turn(
            ts=time.time(), role="user", content=user_text[:4000], mode=mode,
            meta=meta or {}))
        archive.record_turn(session_id, Turn(
            ts=time.time(), role="assistant", content=resp.explanation[:4000],
            mode=mode, action=resp.action.value, confidence=resp.confidence))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("archive.record failed: %s", exc)
        return
    archive.finalize_async(session_id, lambda sid: _IN_FLIGHT.get(sid, 0) == 0)


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
# Local, model-free resolution (from main): database first, always honest
# --------------------------------------------------------------------------- #

def _scan_action() -> AiActionResponse:
    """Network inventory, shaped as an action the phone can display."""
    try:
        hosts = netscan.scan()
    except netscan.ScopeError as exc:
        return AiActionResponse(action=ActionKind.info_only,
                                explanation=f"scan refused: {exc}", confidence=0.0)
    return AiActionResponse(
        action=ActionKind.network_scan,
        params={"devices": [{"ip": h.ip, "mac": h.mac, "name": h.name,
                             "open_ports": h.open_ports, "hints": h.hints()}
                            for h in hosts]},
        explanation=netscan.describe(hosts),
        confidence=0.9)


def _ble_action() -> AiActionResponse:
    """Bluetooth LE inventory, shaped as an action the phone can display."""
    try:
        devices = btscan.scan()
    except btscan.BluetoothUnavailable as exc:
        return AiActionResponse(action=ActionKind.info_only,
                                explanation=f"Bluetooth unavailable: {exc}",
                                confidence=0.0)
    return AiActionResponse(
        action=ActionKind.ble_scan,
        params={"devices": [{"address": d.address, "name": d.name,
                             "rssi": d.rssi, "services": d.services}
                            for d in devices]},
        explanation=btscan.describe(devices),
        confidence=0.9)


def resolve_query(query: str) -> AiActionResponse:
    """Look ``query`` up in the IR database and answer with a real waveform."""
    resolution = irdb_resolver.resolve(query)
    if resolution is None:
        near = irdb_resolver.suggestions(query)
        hint = (" Closest entries: " + "; ".join(near)) if near else ""
        return AiActionResponse(
            action=ActionKind.info_only,
            explanation=f"No confident match for {query!r} in the IR database.{hint}",
            confidence=0.0)

    if not resolution.pattern:
        # Device found, button not (or the file only holds protocols the encoder
        # cannot express). Say so and hand over the button list — never guess.
        buttons = ", ".join(resolution.available_buttons[:25]) or "none"
        return AiActionResponse(
            action=ActionKind.info_only,
            params={"device": f"{resolution.brand} {resolution.model}",
                    "buttons": resolution.available_buttons[:60]},
            explanation=(f"Found {resolution.brand} {resolution.model} in "
                         f"{resolution.category}, but no button matching that "
                         f"request. Available: {buttons}"),
            confidence=resolution.confidence)

    return AiActionResponse(
        action=ActionKind.transmit_ir,
        params=resolution.params(),
        explanation=f"Ready to send {resolution.describe()}",
        confidence=resolution.confidence)


def _vet_model_ir(result: AiActionResponse) -> AiActionResponse:
    """Cross-check a model-proposed IR pattern against the database.

    The model is allowed to *name* a device and a button; it is not allowed to
    be the source of the timings. Any ``transmit_ir`` the model wrote whose
    pattern does not appear verbatim in a database entry is downgraded to
    ``info_only`` rather than transmitted on faith.

    Deterministic answers (offline analyzer, database resolution) are not
    vetted: a waveform measured off the owner's own remote legitimately does
    not exist in the Flipper database.
    """
    if getattr(result, "_provenance", "offline") != "capricorn":
        return result
    if result.action != ActionKind.transmit_ir:
        return result
    pattern = result.params.get("pattern") or []
    if not pattern:
        return result
    needle = " ".join(str(value) for value in pattern)
    for entry in list_entries():
        text = read_entry(entry.category, entry.brand, entry.model) or ""
        if needle in text:
            return result
    log.info("model-proposed IR pattern not found in database; downgrading")
    result.action = ActionKind.info_only
    result.explanation = ("[verified] proposed waveform is not in the local IR "
                          "database, refusing to transmit it. " + result.explanation)
    return result


# --------------------------------------------------------------------------- #
# AI endpoints
# --------------------------------------------------------------------------- #

@app.post("/api/v1/process", response_model=AiActionResponse)
def process(req: ProcessRequest) -> AiActionResponse:
    """Answer a phone request.

    Fast paths that never wake the model:

    * ``irdb_only`` or a resolve mode — the query names a device and a button;
      the answer is looked up in the local database.
    * scan modes — read-only inventory of the owner's LAN or the BLE ether.

    Everything else goes to Capricorn with the session transcript, the persona
    and the generation settings; a proposed ``transmit_ir`` is then vetted
    against the database, and the exchange is archived.
    """
    mode = req.mode.strip().lower()
    if req.irdb_only or mode in RESOLVE_MODES:
        return resolve_query(req.raw_data or req.mode)
    if mode in SCAN_MODES:
        return _scan_action()
    if mode in BLE_MODES:
        return _ble_action()

    payload = (f"MODE: {req.mode}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW_DATA:\n{req.raw_data}")
    history = _history_for(req.history, req.session_id)
    _IN_FLIGHT[req.session_id] = _IN_FLIGHT.get(req.session_id, 0) + 1
    try:
        result = bridge.ask(payload, mode=req.mode, history=history,
                            persona=req.persona, max_tokens=req.max_tokens,
                            temperature=req.temperature)
    except Exception as exc:
        _IN_FLIGHT[req.session_id] = max(0, _IN_FLIGHT.get(req.session_id, 1) - 1)
        log.exception("process failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    result = _sanitize(result, req.mode)
    result = _vet_model_ir(result)
    _IN_FLIGHT[req.session_id] = max(0, _IN_FLIGHT.get(req.session_id, 1) - 1)
    _remember(req.session_id, f"[{req.mode}] {req.raw_data}",
              f"{result.action}: {result.explanation}")
    _archive_turn(req.session_id, req.mode, req.raw_data, result,
                  meta={"device_context": req.device_context,
                        "persona": bool(req.persona)})
    return result


@app.post("/api/v1/parse_dump", response_model=AiActionResponse)
def parse_dump(req: ParseDumpRequest) -> AiActionResponse:
    payload = (f"DUMP_TYPE: {req.dump_type}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW:\n{req.raw_data}")
    history = _history_for(req.history, req.session_id)
    _IN_FLIGHT[req.session_id] = _IN_FLIGHT.get(req.session_id, 0) + 1
    try:
        result = bridge.ask(payload, mode=req.dump_type, history=history)
    except Exception as exc:
        _IN_FLIGHT[req.session_id] = max(0, _IN_FLIGHT.get(req.session_id, 1) - 1)
        log.exception("parse_dump failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    result = _sanitize(result, req.dump_type)
    _IN_FLIGHT[req.session_id] = max(0, _IN_FLIGHT.get(req.session_id, 1) - 1)
    _remember(req.session_id, f"[dump:{req.dump_type}] {req.raw_data}",
              f"{result.action}: {result.explanation}")
    _archive_turn(req.session_id, req.dump_type, req.raw_data, result,
                  meta={"device_context": req.device_context, "dump": True})
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
        raw = netscan.scan_report(
            target=req.target,
            ports=req.ports,
            confirm_owner=True,
            allow_public=req.allow_public,
            max_hosts=req.max_hosts,
            timeout=req.timeout,
            grab_banner=req.grab_banner,
            do_ping=req.do_ping)
    except netscan.ScopeError as exc:
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
    _archive_turn(req.session_id, "netscan", f"[netscan] {req.target}",
                  AiActionResponse(action=ActionKind.net_inventory,
                                   explanation=f"{raw['hosts_with_open_ports']} host(s) "
                                               "with open ports", confidence=0.9),
                  meta={"target": req.target, "ports": req.ports})
    return response


# --------------------------------------------------------------------------- #
# Inventory endpoints (read-only)
# --------------------------------------------------------------------------- #

@app.get("/api/v1/network/scan")
def network_scan(target: str | None = Query(None, description="private CIDR, default: local /24"),
                 ports: str | None = Query(None, description="comma-separated ports")):
    """Inventory the owner's own LAN: who is up, what they expose.

    Read-only and deliberately loud. Private address space only — anything
    else is a 400, not a scan.
    """
    wanted = None
    if ports:
        try:
            wanted = [int(part) for part in ports.split(",") if part.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="ports must be integers")
    try:
        hosts = netscan.scan(target=target, ports=wanted or netscan.DEFAULT_PORTS)
    except netscan.ScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "ok", "count": len(hosts),
            "devices": [{"ip": h.ip, "mac": h.mac, "name": h.name,
                         "open_ports": h.open_ports, "hints": h.hints()}
                        for h in hosts],
            "report": netscan.describe(hosts)}


@app.get("/api/v1/bluetooth/scan")
def bluetooth_scan(timeout: float = Query(6.0, ge=1.0, le=30.0)):
    """List Bluetooth LE devices currently advertising nearby."""
    try:
        devices = btscan.scan(timeout=timeout)
    except btscan.BluetoothUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok", "count": len(devices), "backends": btscan.backends(),
            "devices": [{"address": d.address, "name": d.name, "rssi": d.rssi,
                         "services": d.services} for d in devices],
            "report": btscan.describe(devices)}


@app.get("/api/v1/bluetooth/gatt")
def bluetooth_gatt(address: str = Query(..., description="MAC of YOUR device")):
    """Enumerate GATT services on one explicitly named device."""
    try:
        services = btscan.gatt_services(address)
    except btscan.ScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except btscan.BluetoothUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"status": "ok", "address": address.upper(),
            "services": [{"uuid": uuid,
                          "name": btscan.KNOWN_SERVICES.get(uuid.lower())}
                         for uuid in services]}


@app.get("/api/v1/irdb/resolve")
def irdb_resolve(q: str = Query(..., description="device + button, e.g. 'epson projector power'")):
    """Resolve a phrase to a transmittable signal. Read-only, no AI involved."""
    return resolve_query(q).model_dump()


# --------------------------------------------------------------------------- #
# Dumps, knowledge, sessions, archives
# --------------------------------------------------------------------------- #

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


@app.get("/api/v1/archives/stats")
def archive_stats():
    return {"status": "ok", "stats": archive.stats()}


@app.get("/api/v1/archives")
def archives_list():
    return {"status": "ok", "archives": archive.list_archives()}


@app.get("/api/v1/archives/{name}")
def archives_read(name: str, limit: int = Query(500, ge=1, le=5000)):
    rows = archive.read_archive(name, limit=limit)
    if not rows:
        raise HTTPException(status_code=404, detail="archive not found")
    return {"status": "ok", "name": name, "rows": rows}


@app.delete("/api/v1/archives/{name}")
def archives_delete(name: str):
    if not archive.delete_archive(name):
        raise HTTPException(status_code=404, detail="archive not found")
    return {"status": "ok", "deleted": name}


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
    "session_id","history","persona"}, server replies with the strict
    AiActionResponse."""
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_json()
            mode = msg.get("mode", "")
            session_id = msg.get("session_id", "default")
            history = [ChatTurn(**t) for t in msg.get("history", [])
                       if isinstance(t, dict)] or list(_session(session_id))
            _IN_FLIGHT[session_id] = _IN_FLIGHT.get(session_id, 0) + 1
            try:
                resp = bridge.ask(
                    f"MODE: {mode}\n"
                    f"DEVICE_CONTEXT: {msg.get('device_context', {})}\n"
                    f"RAW_DATA:\n{msg.get('raw_data', '')}",
                    mode=mode, history=history,
                    persona=msg.get("persona"))
                resp = _sanitize(resp, mode)
                resp = _vet_model_ir(resp)
                _IN_FLIGHT[session_id] = max(0, _IN_FLIGHT.get(session_id, 1) - 1)
                _remember(session_id, f"[{mode}] {msg.get('raw_data', '')}",
                          f"{resp.action}: {resp.explanation}")
                _archive_turn(session_id, mode, msg.get("raw_data", ""), resp,
                              meta={"device_context": msg.get("device_context", {}),
                                    "channel": "ws"})
                await websocket.send_json(resp.model_dump())
            except Exception as exc:
                _IN_FLIGHT[session_id] = max(0, _IN_FLIGHT.get(session_id, 1) - 1)
                await websocket.send_json({"status": "error", "detail": str(exc)})
    except WebSocketDisconnect:
        log.info("ws client disconnected")


@app.get("/")
def root():
    return {"name": "Do Cats Eat Bats PC Server", "docs": "/docs",
            "ws": "/ws/v1/ai", "modes": list(MODES),
            "uptime_s": round(time.time() - STARTED, 1)}

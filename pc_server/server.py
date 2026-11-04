"""Do Cats Eat Bats — PC AI backend (FastAPI).

Endpoints:
  GET  /api/v1/health          ping + AI session + IRDB stats
  POST /api/v1/process         generic task from the phone
  POST /api/v1/parse_dump      analyze raw IR/Sub-GHz/dmesg/NFC dump
  POST /api/v1/dumps/upload    store a raw dump sent from the phone
  GET  /api/v1/irdb/search?q=  search the local .ir database
  GET  /api/v1/irdb/file       fetch one .ir file by category/brand/model
"""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import btscan
import irdb_resolver
import netscan
from ai_bridge import bridge
from irdb import dumps_count, list_entries, read_entry, save_dump, search
from schemas import (ActionKind, AiActionResponse, HealthResponse,
                     ParseDumpRequest, ProcessRequest)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dceb.server")

VERSION = "0.1.0"
STARTED = time.time()

app = FastAPI(title="Do Cats Eat Bats — Capricorn AI Bridge", version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # lab tool on a private network
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        ai_backend=f"arenaaispofandpars/{bridge.available and 'capricorn' or 'offline'}",
        ai_session_ok=bridge.available,
        irdb_entries=len(list_entries()),
        dumps_stored=dumps_count(),
        version=VERSION,
    )


RESOLVE_MODES = {"resolve_ir", "ir", "transmit_ir", "irdb", "ir_request"}


@app.post("/api/v1/process", response_model=AiActionResponse)
def process(req: ProcessRequest) -> AiActionResponse:
    """Answer a phone request.

    Two paths, and the difference matters:

    * ``mode`` in RESOLVE_MODES (or ``irdb_only``) — the query names a device
      and a button, and the answer is looked up in the local database. The
      model is not consulted at all, so it cannot invent a waveform.
    * anything else — a free-form analysis task goes to Capricorn, and if the
      model proposes ``transmit_ir`` its pattern is cross-checked against the
      database before it is allowed to leave the server.
    """
    mode = req.mode.strip().lower()
    if req.irdb_only or mode in RESOLVE_MODES:
        return resolve_query(req.raw_data or req.mode)
    if mode in ("network_scan", "scan_network", "lan_scan"):
        return _scan_action()
    if mode in ("ble_scan", "bluetooth_scan", "scan_ble"):
        return _ble_action()

    payload = (f"MODE: {req.mode}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW_DATA:\n{req.raw_data}")
    try:
        result = bridge.ask(payload)
    except Exception as exc:
        log.exception("process failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    return _vet_model_ir(result)


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
    be the source of the timings. Any ``transmit_ir`` whose pattern does not
    appear verbatim in a database entry is downgraded to ``info_only`` rather
    than transmitted on faith.
    """
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


@app.post("/api/v1/parse_dump", response_model=AiActionResponse)
def parse_dump(req: ParseDumpRequest) -> AiActionResponse:
    payload = (f"DUMP_TYPE: {req.dump_type}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW:\n{req.raw_data}")
    try:
        result = bridge.ask(payload)
    except Exception as exc:
        log.exception("parse_dump failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")
    # Never let the model escalate a dump analysis into a HID attack on a
    # machine we cannot verify is the owner's: downgrade execute_hid to info.
    if req.dump_type != "hid" and result.action == ActionKind.execute_hid:
        result.action = ActionKind.info_only
        result.explanation = "[safety] HID action downgraded to info_only " \
                             "(dump analysis may not emit HID). " + result.explanation
    return result


@app.post("/api/v1/dumps/upload")
def upload_dump(name: str = Query("dump"), body: str = ""):
    if not body.strip():
        raise HTTPException(status_code=400, detail="empty body")
    path = save_dump(name, body)
    return {"status": "ok", "stored": str(path)}


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


@app.websocket("/ws/v1/ai")
async def ai_ws(websocket: WebSocket):
    """Streaming AI channel: phone sends {"mode","raw_data","device_context"},
    server replies with the same strict AiActionResponse JSON per message."""
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_json()
            try:
                resp = bridge.ask(
                    f"MODE: {msg.get('mode', '')}\n"
                    f"DEVICE_CONTEXT: {msg.get('device_context', {})}\n"
                    f"RAW_DATA:\n{msg.get('raw_data', '')}")
                await websocket.send_json(resp.model_dump())
            except Exception as exc:
                await websocket.send_json({"status": "error", "detail": str(exc)})
    except WebSocketDisconnect:
        log.info("ws client disconnected")


@app.get("/")
def root():
    return {"name": "Do Cats Eat Bats PC Server", "docs": "/docs",
            "ws": "/ws/v1/ai",
            "uptime_s": round(time.time() - STARTED, 1)}

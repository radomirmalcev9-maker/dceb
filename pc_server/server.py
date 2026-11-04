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


@app.post("/api/v1/process", response_model=AiActionResponse)
def process(req: ProcessRequest) -> AiActionResponse:
    payload = (f"MODE: {req.mode}\n"
               f"DEVICE_CONTEXT: {req.device_context}\n"
               f"RAW_DATA:\n{req.raw_data}")
    try:
        return bridge.ask(payload)
    except Exception as exc:
        log.exception("process failed")
        raise HTTPException(status_code=502, detail=f"AI bridge error: {exc}")


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

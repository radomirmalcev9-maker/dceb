"""Strict request/response schemas for the DCEB AI bridge.

Every answer produced by the Capricorn model MUST validate against
``AiActionResponse`` before it is sent to the phone.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class Channel(str, Enum):
    adb_reverse = "adb_reverse"   # http://127.0.0.1:8000 on the phone
    lan = "lan"                   # http://<pc-lan-ip>:8000
    tailscale = "tailscale"       # http://<100.x.y.z>:8000


class ActionKind(str, Enum):
    transmit_ir = "transmit_ir"
    execute_hid = "execute_hid"
    run_shell = "run_shell"
    ui_action = "ui_action"      # screen read / tap on the OWNER's own phone
    network_scan = "network_scan"  # read-only inventory of the owner's LAN
    ble_scan = "ble_scan"          # read-only Bluetooth LE inventory
    info_only = "info_only"


class ProcessRequest(BaseModel):
    mode: str = Field(..., description="free-form task mode, e.g. 'analyze_ir', 'hid_help'")
    raw_data: str = Field("", description="raw payload: timings, logs, dump text")
    device_context: dict = Field(default_factory=dict)
    # When true the server must NOT consult the language model: it resolves
    # straight from the IR database and returns "no match" honestly. Used by
    # the phone's "send exactly this" path and by tests.
    irdb_only: bool = Field(False, description="resolve from the local DB only")


class ParseDumpRequest(BaseModel):
    dump_type: str = Field(..., description="ir | subghz | dmesg | console | nfc")
    raw_data: str = Field(...)
    device_context: dict = Field(default_factory=dict)


class IrParams(BaseModel):
    frequency: int = 38000
    duty_cycle: float = 0.33
    pattern: List[int] = Field(default_factory=list, description="microseconds")
    protocol: Optional[str] = None
    name: Optional[str] = None


class AiActionResponse(BaseModel):
    """The ONLY shape the phone will ever act on."""
    status: str = "ok"
    action: ActionKind
    params: dict = Field(default_factory=dict)
    explanation: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("params")
    @classmethod
    def _validate_params(cls, v: dict, info) -> dict:
        action = info.data.get("action")
        if action == ActionKind.transmit_ir:
            IrParams(**{k: v.get(k) for k in ("frequency", "duty_cycle", "pattern") if k in v})
            if "pattern" in v and not v["pattern"]:
                raise ValueError("transmit_ir requires a non-empty pattern")
        return v


class HealthResponse(BaseModel):
    status: str
    ai_backend: str          # "arenaaispofandpars/capricorn" | "offline-fallback"
    ai_session_ok: bool
    irdb_entries: int
    dumps_stored: int
    version: str

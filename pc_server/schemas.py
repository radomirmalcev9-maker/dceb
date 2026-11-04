"""Strict request/response schemas for the DCEB AI bridge.

Every answer produced by the Capricorn model MUST validate against
``AiActionResponse`` before it is sent to the phone.

Safety model: only ``transmit_ir``, ``execute_hid`` and ``ui_action`` are ever
executed by the client, and only after an explicit Approve tap. The inventory
kinds (``net_inventory``, ``bt_inventory``, ``hardening``, ``ctf_answer``) are
advisory — they carry analysis and never trigger hardware or shell execution.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional

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
    info_only = "info_only"
    net_inventory = "net_inventory"    # advisory: authorized LAN inventory
    bt_inventory = "bt_inventory"      # advisory: Bluetooth device inventory
    hardening = "hardening"            # advisory: hardening checklist output
    ctf_answer = "ctf_answer"          # advisory: CTF/lab practice answer


# Kinds the phone is allowed to execute behind the Approve button.
EXECUTABLE_KINDS = frozenset({
    ActionKind.transmit_ir, ActionKind.execute_hid, ActionKind.ui_action})

# Kinds that are pure advice and must never be executed.
ADVISORY_KINDS = frozenset({
    ActionKind.info_only, ActionKind.net_inventory, ActionKind.bt_inventory,
    ActionKind.hardening, ActionKind.ctf_answer})


class ChatTurn(BaseModel):
    """One remembered exchange, used to give the model short-term memory."""
    role: Literal["user", "assistant"] = "user"
    content: str = Field("", description="text of the turn")


class ProcessRequest(BaseModel):
    mode: str = Field(..., description="free-form task mode, e.g. 'analyze_ir', 'hid_help'")
    raw_data: str = Field("", description="raw payload: timings, logs, dump text")
    device_context: dict = Field(default_factory=dict)
    session_id: str = Field("default", description="conversation id for server-side memory")
    history: List[ChatTurn] = Field(default_factory=list,
                                    description="client-side transcript, oldest first")


class ParseDumpRequest(BaseModel):
    dump_type: str = Field(..., description="ir | subghz | dmesg | console | nfc | bluetooth")
    raw_data: str = Field(...)
    device_context: dict = Field(default_factory=dict)
    session_id: str = Field("default")
    history: List[ChatTurn] = Field(default_factory=list)


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

    @property
    def advisory(self) -> bool:
        """True when this response must never be executed by the client."""
        return self.action in ADVISORY_KINDS


class NetScanRequest(BaseModel):
    """Authorized LAN inventory request.

    ``confirm_owner`` is a deliberate speed bump: the operator states the target
    is theirs (or covered by written permission). Public ranges additionally
    need ``allow_public``.
    """
    target: str = Field(..., description="ip, CIDR (e.g. 192.168.1.0/24) or hostname")
    ports: List[int] = Field(default_factory=lambda: [22, 80, 443, 445, 3389, 8000, 8080, 9100])
    confirm_owner: bool = Field(False, description="operator confirms authorized scope")
    allow_public: bool = Field(False, description="required for non-private ranges")
    max_hosts: int = Field(256, ge=1, le=4096)
    timeout: float = Field(0.4, gt=0.0, le=5.0)
    grab_banner: bool = Field(False, description="read a short service greeting")
    do_ping: bool = Field(False, description="ICMP sweep first (needs ping permission)")
    session_id: str = "default"
    history: List[ChatTurn] = Field(default_factory=list)

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, v: List[int]) -> List[int]:
        ports = [int(p) for p in v if 0 < int(p) < 65536]
        if len(ports) > 128:
            raise ValueError("keep the port list to 128 entries or fewer")
        return ports


class NetHost(BaseModel):
    host: str
    alive: bool = False
    open_ports: List[int] = Field(default_factory=list)
    banners: dict = Field(default_factory=dict)
    error: Optional[str] = None


class NetScanResponse(BaseModel):
    status: str = "ok"
    target: str
    scanned_hosts: int = 0
    hosts_with_open_ports: int = 0
    open_port_total: int = 0
    ports_probed: List[int] = Field(default_factory=list)
    banner_grabbing: bool = False
    ping_sweep: bool = False
    results: List[NetHost] = Field(default_factory=list)
    note: str = ""
    advice: Optional[AiActionResponse] = None


class HealthResponse(BaseModel):
    status: str
    ai_backend: str          # "arenaaispofandpars/capricorn" | "offline-fallback"
    ai_session_ok: bool
    ai_probe_ok: bool = False      # an actual tiny completion succeeded
    ai_last_error: Optional[str] = None
    irdb_entries: int
    dumps_stored: int
    version: str
    modes: List[str] = Field(default_factory=list)

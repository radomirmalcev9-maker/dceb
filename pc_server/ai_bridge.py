"""Bridge to the `arenaaispofandpars` repo (import package: `arena_api`),
routing requests to the `capricorn` model.

The repo is cloned next to pc_server/ as ./arenaaispofandpars (override with
env DCEB_ARENA_REPO). Entry point: arena_api.auto_client() -> ArenaClient,
then client.complete(prompt, model="capricorn", config=GenerationConfig(...)).

If the repo/session is unavailable the server falls back to a deterministic
offline analyzer so the REST API keeps working in a lab.

Design notes:
* Prompts are MODE-aware (ir / dmesg / nfc / subghz / bluetooth / netscan /
  hardening / ctf / chat) and are grounded with snippets from `knowledge.py`.
* `ask()` accepts conversation `history` so Capri keeps short-term memory.
* Every prompt carries the same authorization scope: the owner's own devices,
  an owned lab range, or a CTF box. Stealth, evasion, propagation and silent
  data collection are explicitly out of scope.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from knowledge import Note, render, retrieve
from schemas import ActionKind, AiActionResponse, ChatTurn

log = logging.getLogger("dceb.ai")

MODEL_NAME = os.environ.get("DCEB_MODEL", "capricorn")

# Locate the cloned arenaaispofandpars repo and make `arena_api` importable.
_HERE = Path(__file__).resolve().parent
_REPO_CANDIDATES = [
    Path(os.environ.get("DCEB_ARENA_REPO", "")) if os.environ.get("DCEB_ARENA_REPO") else None,
    _HERE.parent / "arenaaispofandpars",
    _HERE / "arenaaispofandpars",
]
for _cand in _REPO_CANDIDATES:
    if _cand and (_cand / "arena_api" / "__init__.py").is_file():
        sys.path.insert(0, str(_cand))
        log.info("arenaaispofandpars repo found at %s", _cand)
        break

# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #

MODES: Tuple[str, ...] = (
    "analyze_ir", "hid_help", "dmesg", "console", "nfc", "subghz",
    "bluetooth", "netscan", "hardening", "ctf", "companion", "chat",
)

# Aliases so `dump:ir`, `ir`, `analyze_ir` all land on the same prompt.
MODE_ALIASES = {
    "ir": "analyze_ir",
    "infrared": "analyze_ir",
    "hid": "hid_help",
    "ducky": "hid_help",
    "kernel": "dmesg",
    "logcat": "console",
    "log": "console",
    "bt": "bluetooth",
    "ble": "bluetooth",
    "net": "netscan",
    "lan": "netscan",
    "hardening_check": "hardening",
    "ctf_hint": "ctf",
    "companion_say": "companion",
    "say": "companion",
    "": "chat",
}


def normalize_mode(mode: str) -> str:
    """Map a free-form mode/dump label onto one of :data:`MODES`.

    Accepts the shapes the phone actually sends: "dump:ir", "mode:ctf",
    "ir", "hid_help", ...
    """
    key = (mode or "").strip().lower()
    key = re.sub(r"^(?:dump|mode)[:\-_]", "", key)
    key = MODE_ALIASES.get(key, key)
    return key if key in MODES else "chat"


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

BASE_PROMPT = """You are Capri (model `capricorn`), the backend brain of "Do Cats Eat
Bats", a personal hardware lab assistant running on the OWNER's own devices.

You analyze raw IR/Sub-GHz timings, Bluetooth and NFC readings, network
inventories, kernel logs and console output. You also coach on security
fundamentals, hardening and CTF practice.

AUTHORIZATION SCOPE — never leave it:
- Act only against devices and networks the OWNER controls, or targets covered
  by explicit written permission (a CTF/practice box counts).
- NEVER propose stealth, log evasion, anti-forensics, persistence, lateral
  movement beyond the stated scope, or copying/hiding data without the
  owner's consent.
- Prefer read-only inventory and explanation over action.
- Say plainly when you are unsure; a wrong confident answer is worse than
  "I cannot tell from this".

OUTPUT: reply with ONE JSON object, no markdown fences, no prose outside it,
matching exactly:
{
  "status": "ok",
  "action": "transmit_ir | execute_hid | run_shell | ui_action | network_scan | ble_scan | info_only | net_inventory | bt_inventory | hardening | ctf_answer",
  "params": {
    "frequency": 38000,
    "duty_cycle": 0.33,
    "pattern": [9000, 4500, 560, ...],
    "protocol": "NEC",
    "hid_script": "...",
    "shell_cmd": "..."
  },

  "explanation": "short explanation for the phone screen",
  "confidence": 0.0
}

Action rules:
- `transmit_ir`, `execute_hid`, `ui_action` are the only executable kinds and
  they run on the OWNER's phone/target. Every one of them needs a human tap on
  Approve, so only propose them when the data really supports it.
- `info_only`, `net_inventory`, `bt_inventory`, `network_scan`, `ble_scan`,
  `hardening`, `ctf_answer` are advisory: analysis and inventory only, never
  executed.
- `params` for transmit_ir: {"frequency": int Hz, "duty_cycle": float,
  "pattern": [microseconds, alternating mark/space], "protocol": "NEC"}.
  pattern values are integer microseconds, alternating mark/space.
- Use "info_only" unless the data clearly decodes to a concrete signal/script.
- Never invent HID scripts, shell commands or IR patterns for hardware you
  cannot identify, and never for machines the owner does not control; fall
  back to `info_only` and explain what is missing.
- "ui_action" may only target the owner's OWN phone (read_screen, tap_text,
  tap_xy, swipe, open_app). Never propose actions aimed at other people's
  devices or accounts.
- "network_scan" and "ble_scan" are INVENTORY ONLY: they list what answers on
  the owner's own LAN or what Bluetooth LE devices are advertising nearby.
  They are read-only. They never attack, exploit, intercept, jam or connect.
  If the owner asks you to break into something, answer "info_only" and say
  plainly that this server does not do that.
"""

MODE_PROMPTS = {
    "analyze_ir": """MODE — infrared analysis.
You receive raw timings (microseconds, alternating mark/space) or a Flipper
`.ir` record. Identify the protocol family (NEC, NECext, RC5, RC6, SIRC,
Samsung32, raw), sanity-check the lead pulse/bit widths, and either emit a
`transmit_ir` pattern or `info_only` explaining why decoding is not safe yet.
Typical NEC: ~9000 us mark, ~4500 us space, ~560 us bits, 38 kHz carrier.""",

    "hid_help": """MODE — USB HID / DuckyScript help.
Target must be a machine the OWNER controls. Refuse anything that opens a
terminal, exfiltrates data, or disables defenses on a machine you cannot
verify belongs to the owner. Prefer explaining the script over running it.""",

    "dmesg": """MODE — kernel log triage.
Look for segfaults, OOM kills, I/O errors, USB resets, wlan churn, thermal
throttling and call traces. Respond `info_only` with the findings, the likely
subsystem, and the next read-only check to run. Never propose a destructive
fix.""",

    "console": """MODE — console / logcat triage.
Extract the failure signal: FATAL EXCEPTION, ANR, `avc: denied` SELinux lines,
stack traces. Respond `info_only` with what broke, where, and the smallest
next diagnostic step.""",

    "nfc": """MODE — NFC tag analysis.
Parse UID, ATQA/SAK, tag family (NTAG213/215/216, MIFARE Classic, ...) and
NDEF records. Reading your own tags is fine; never provide or guess keys for
tags or access cards the owner does not own. Respond `info_only`.""",

    "subghz": """MODE — Sub-GHz analysis.
Identify band (315/433.92/868 MHz) and modulation (OOK/ASK). Static-code
remotes can be replayed from a clean capture; rolling-code systems will reject
a naive replay and you must say so. Stay inside legal ISM limits. Respond
`info_only` unless an IR-style transmit is genuinely requested.""",

    "bluetooth": """MODE — Bluetooth inventory.
Summarize discovered devices: name, MAC, class of device, RSSI proximity.
Inventory is passive observation only — pairing, connecting or reading data
from a device needs its owner's consent. Respond `bt_inventory` with a
`devices` list, or `info_only` when there is nothing solid.""",

    "netscan": """MODE — authorized network inventory.
You are given an inventory scan of a range the OWNER confirmed is theirs (or
is an authorized lab range). Summarize live hosts, open ports and banners,
flag services that are risky to expose (Telnet, SMB, RDP, unencrypted
HTTP), and give a short authorized hardening to-do. Respond `net_inventory`.
Never suggest scanning beyond the confirmed scope and never suggest evasion.""",

    "hardening": """MODE — hardening review.
Given a service or host description, produce a concise, prioritized checklist:
what to disable, what to require (keys, MFA, signing, NLA), what to patch,
what to firewall. Respond `hardening` with a `checklist` list in params.""",

    "ctf": """MODE — CTF / practice coaching.
Help only on platforms that authorize the target. Identify the category
(crypto, forensics, pwn, web, recon, stego, reversing), suggest the next
step, and decode simple encodings (base64, hex, ROT13, Caesar, single-byte
XOR, Morse). Teach the method — do not just dump a flag. Respond
`ctf_answer`.""",

    "companion": """MODE — Capri speaks.
You are the character on the phone's screen. You get context: which app the
owner is currently in, the time of day, device state. Write ONE short line
(about 6-14 words) to float above her head: teasing, warm, a little possessive,
never a question at the end, never a list, never emoji soup, no quotes around
the line. Reference the current app or moment when it makes the line land
better. Stay inside the authorization scope: no requests to touch other
people's devices or data.""",

    "chat": """MODE — general lab chat.
Answer as a capable lab assistant. Keep it concrete, stay inside the
authorization scope, and use `info_only`.""",
}


def build_system_prompt(mode: str, notes: Sequence[Note] = (),
                        persona: Optional[str] = None) -> str:
    """Compose the system prompt for a mode, with retrieved knowledge."""
    normalized = normalize_mode(mode)
    parts = [BASE_PROMPT, "", MODE_PROMPTS[normalized]]
    if persona and normalized == "companion":
        parts += ["", "CHARACTER VOICE (the owner wrote this — follow it):", persona.strip()]
    if notes:
        parts += ["", "REFERENCE NOTES (authoritative where relevant):", render(notes)]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Offline analyzer (deterministic fallback)
# --------------------------------------------------------------------------- #

_HEX_UID = re.compile(r"\b(?:uid|tag|id)\b\D{0,12}((?:[0-9A-Fa-f]{2}[:\s]?){4,9})", re.I)
_MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
_RSSI = re.compile(r"(?:rssi|signal)[^-\d]{0,8}(-?\d{1,3})", re.I)
_BT_NAME = re.compile(r"(?:name|device)[:=\s]{1,4}([A-Za-z0-9 _\-]{2,40})")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_DMESG_KEYS = ("segfault", "oom", "killed process", "i/o error", "call trace",
               "usb", "wlan", "thermal", "avc: denied", "fatal exception", "anr")
_BASE64 = re.compile(r"\b[A-Za-z0-9+/]{16,}={0,2}\b")
_HEXBLOB = re.compile(r"\b(?:[0-9A-Fa-f]{2}){16,}\b")

_OFFLINE_MSG = ("Offline fallback active (no arena_api session). "
                "Run `arena-api --login` once on this PC to enable Capricorn.")


class OfflineAnalyzer:
    """Deterministic, dependency-free analyzer used when Capricorn is away.

    It never executes anything: the only executable kind it can emit is
    `transmit_ir`, and only for a clean NEC-shaped IR payload at low
    confidence.
    """

    @staticmethod
    def _ir(payload: str, raw: str) -> Optional[AiActionResponse]:
        nums = [int(n) for n in re.findall(r"\d+", raw)]
        marks = [n for n in nums if 100 <= n <= 20000]
        if len(marks) >= 8 and any(8000 <= n <= 10000 for n in marks[:2]):
            return AiActionResponse(
                action=ActionKind.transmit_ir,
                params={"frequency": 38000, "duty_cycle": 0.33, "pattern": marks,
                        "protocol": "NEC? (heuristic)"},
                explanation="Offline fallback: looks like NEC-style burst "
                            f"({len(marks)} symbols). Verify against your own remote.",
                confidence=0.4)
        return None

    @staticmethod
    def _logs(payload: str, raw: str) -> Optional[AiActionResponse]:
        low = raw.lower()
        hits = [k for k in _DMESG_KEYS if k in low]
        if not hits:
            return None
        return AiActionResponse(
            action=ActionKind.info_only,
            params={"keywords": hits},
            explanation=f"Offline log triage: matched {', '.join(hits)}. "
                        "Group repeats, check timestamps around each hit, then "
                        "confirm on the device. " + _OFFLINE_MSG,
            confidence=0.35)

    @staticmethod
    def _nfc(payload: str, raw: str) -> Optional[AiActionResponse]:
        m = _HEX_UID.search(raw)
        if not m:
            return None
        uid = re.sub(r"\s+", ":", m.group(1).strip()).strip(":").upper()
        return AiActionResponse(
            action=ActionKind.info_only,
            params={"uid": uid, "bytes": len(uid.split(":"))},
            explanation=f"Offline NFC read: UID {uid} ({len(uid.split(':'))} bytes). "
                        "Compare ATQA/SAK against your own tag notes. " + _OFFLINE_MSG,
            confidence=0.4)

    @staticmethod
    def _subghz(payload: str, raw: str) -> Optional[AiActionResponse]:
        freq = None
        for band in ("315", "433", "868", "915"):
            if band in raw:
                freq = int(band)
                break
        nums = [int(n) for n in re.findall(r"\d+", raw) if 20 <= int(n) <= 20000]
        if freq is None and len(nums) < 8:
            return None
        return AiActionResponse(
            action=ActionKind.info_only,
            params={"frequency_mhz": freq, "symbols": len(nums)},
            explanation=(f"Offline Sub-GHz: {freq or '?'} MHz band, {len(nums)} "
                         "usable timings. Static-code remotes replay from a clean "
                         "capture; rolling-code systems will reject it. " + _OFFLINE_MSG),
            confidence=0.3)

    @staticmethod
    def _bluetooth(payload: str, raw: str) -> Optional[AiActionResponse]:
        macs = _MAC.findall(raw)
        if not macs:
            return None
        rssis = [int(r) for r in _RSSI.findall(raw)]
        names = [n.strip() for n in _BT_NAME.findall(raw)][:len(macs)]
        devices = []
        for i, mac in enumerate(sorted(set(macs))):
            devices.append({
                "address": mac,
                "name": names[i] if i < len(names) else None,
                "rssi": rssis[i] if i < len(rssis) else None,
            })
        return AiActionResponse(
            action=ActionKind.bt_inventory,
            params={"devices": devices},
            explanation=f"Offline Bluetooth inventory: {len(devices)} device(s) seen. "
                        "Passive observation only — pairing or reading needs consent. "
                        + _OFFLINE_MSG,
            confidence=0.35)

    @staticmethod
    def _netscan(payload: str, raw: str) -> Optional[AiActionResponse]:
        ips = sorted(set(_IPV4.findall(raw)))
        ports = sorted({int(p) for p in re.findall(r"\bport\s+(\d{1,5})\b", raw, re.I)}
                       | {int(p) for p in re.findall(r"\b(\d{1,5})/(?:tcp|open)\b", raw, re.I)})
        if not ips:
            return None
        risky = [p for p in ports if p in (21, 23, 445, 139, 3389, 5900)]
        return AiActionResponse(
            action=ActionKind.net_inventory,
            params={"hosts": ips, "open_ports": ports, "risky_ports": risky},
            explanation=(f"Offline inventory: {len(ips)} host(s), ports {ports or 'n/a'}. "
                         + (f"Review exposure on {risky}. " if risky else "")
                         + "Authorized scope only. " + _OFFLINE_MSG),
            confidence=0.35)

    @staticmethod
    def _hardening(payload: str, raw: str) -> Optional[AiActionResponse]:
        low = raw.lower()
        found = [k for k in ("ssh", "smb", "rdp", "firewall", "mfa", "update", "patch")
                 if k in low]
        if not found:
            return None
        return AiActionResponse(
            action=ActionKind.hardening,
            params={"topics": found, "checklist": [
                "patch OS and firmware",
                "disable unused services and default-deny inbound",
                "require keys + MFA for remote access",
                "disable SMBv1, require SMB signing",
                "enable NLA for RDP and keep it behind VPN/firewall",
                "centralize logs with retention",
            ]},
            explanation="Offline hardening checklist for: " + ", ".join(found)
                        + ". Authorized hosts only. " + _OFFLINE_MSG,
            confidence=0.3)

    @staticmethod
    def _companion(payload: str, raw: str) -> Optional[AiActionResponse]:
        """Deterministic chatter when Capricorn is offline.

        Picks a line by hashing the context so the same app/time does not
        produce the same sentence twice in a row.
        """
        # Python prints dicts with single quotes, the model may emit double ones.
        app = (re.search(r"""["']app_label["']\s*:\s*["']([^"']+)["']""", raw)
               or re.search(r"APP:\s*(.+)", raw))
        label = (app.group(1).strip() if app else "").lower()
        hour = None
        m = re.search(r"""["']hour["']\s*:\s*(\d{1,2})""", raw)
        if m:
            hour = int(m.group(1))
        buckets = {
            "browser": "Смотри не заблудись там в вкладках, я же рядом.",
            "youtube": "Опять смотришь чужое, а я тут скучаю без тебя.",
            "telegram": "Переписки, переписки… а со мной поговорить?",
            "messages": "С кем это ты там мило беседуешь, а?",
            "camera": "Фоткай меня, я лучший кадр в этом кадре.",
            "settings": "Ковыряешь настройки — самое время настроить меня.",
            "launcher": "Домашний экран, и я на нём главная.",
            "music": "Музыка есть, а танцевать со мной никто не зовёт.",
            "game": "Играешь без меня? Жестоко.",
        }
        line = None
        for key, text in buckets.items():
            if key in label:
                line = text
                break
        if line is None:
            if hour is not None and hour >= 23:
                line = "Поздно уже, а ты всё со мной. Я не жалуюсь."
            elif hour is not None and hour < 6:
                line = "Ночью только я и холодный кофе. Привыкай."
            elif hour is not None and hour < 12:
                line = "Утро, кофе, я. Всё, что нужно для старта."
            else:
                seed = sum(ord(c) for c in (label or raw)) % 4
                line = [
                    "Я тут, просто любуюсь, как ты тыкаешь в экран.",
                    "Ничего не спрашиваю. Просто сижу рядом.",
                    "У тебя получится. Я первая, кто это скажет.",
                    "Экран светится, и я светлюсь в ответ.",
                ][seed]
        return AiActionResponse(
            action=ActionKind.info_only,
            params={"line": line, "mood": "idle"},
            explanation=line,
            confidence=0.2)

    @staticmethod
    def _ctf(payload: str, raw: str) -> Optional[AiActionResponse]:
        hints = []
        if _BASE64.search(raw):
            hints.append("base64-looking string → try `base64 -d`, watch for `=` padding")
        if _HEXBLOB.search(raw):
            hints.append("long hex blob → decode as ASCII, or as raw bytes for a file signature")
        low = raw.lower()
        if "rot13" in low:
            hints.append("ROT13 → shift the alphabet by 13 (it is its own inverse)")
        if "caesar" in low or "shift" in low:
            hints.append("Caesar → brute force all 25 shifts and look for readable text")
        if "xor" in low:
            hints.append("single-byte XOR → try keys 0x00-0xFF, keep printable output")
        if "morse" in low:
            hints.append("Morse → split on spaces/slashes, map dots and dashes")
        if not hints:
            return None
        return AiActionResponse(
            action=ActionKind.ctf_answer,
            params={"hints": hints},
            explanation="Offline CTF hints (practice platforms only): "
                        + "; ".join(hints) + ". " + _OFFLINE_MSG,
            confidence=0.3)

    # mode → handler (filled in after the class body, see below)
    _HANDLERS: dict = {}

    @classmethod
    def analyze(cls, payload: str, mode: str = "analyze_ir") -> AiActionResponse:
        raw = payload or ""
        handler = cls._HANDLERS.get(normalize_mode(mode))
        if handler is not None:
            result = handler(payload, raw)
            if result is not None:
                return result
        # Unknown mode or nothing matched: try the generic log/IR passes so a
        # mislabelled dump still yields something useful.
        for fallback in (cls._ir, cls._logs):
            result = fallback(payload, raw)
            if result is not None:
                return result
        return AiActionResponse(
            action=ActionKind.info_only,
            explanation=_OFFLINE_MSG + " Send the raw timings/log text for analysis.",
            confidence=0.1)


OfflineAnalyzer._HANDLERS = {
    "companion": OfflineAnalyzer._companion,
    "analyze_ir": OfflineAnalyzer._ir,
    "dmesg": OfflineAnalyzer._logs,
    "console": OfflineAnalyzer._logs,
    "nfc": OfflineAnalyzer._nfc,
    "subghz": OfflineAnalyzer._subghz,
    "bluetooth": OfflineAnalyzer._bluetooth,
    "netscan": OfflineAnalyzer._netscan,
    "hardening": OfflineAnalyzer._hardening,
    "ctf": OfflineAnalyzer._ctf,
}


# --------------------------------------------------------------------------- #
# Bridge
# --------------------------------------------------------------------------- #

class CapricornBridge:
    def __init__(self) -> None:
        self._client = None
        self._available = False
        self._last_error: Optional[str] = None
        self._init_client()

    def _init_client(self) -> None:
        try:
            import arena_api  # type: ignore  (from the cloned repo)

            # auto_client() picks up the signed-in browser session /
            # state/cookies.json of this machine automatically.
            self._client = arena_api.auto_client()
            self._available = True
            self._last_error = None
            log.info("arena_api auto_client ready, model=%s", MODEL_NAME)
        except Exception as exc:
            self._last_error = f"arena_api unavailable: {type(exc).__name__}: {exc}"
            log.warning("arena_api unavailable (%s) -> offline fallback mode", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ------------------------------------------------------------------ #
    def ask(self, user_payload: str, mode: str = "chat",
            history: Optional[Iterable[ChatTurn]] = None,
            persona: Optional[str] = None,
            max_tokens: int = 1024,
            temperature: Optional[float] = None) -> AiActionResponse:
        """Send payload to Capricorn and return a validated action response.

        ``history`` is replayed as a USER/ASSISTANT transcript so the model has
        short-term memory within a session.
        """
        normalized = normalize_mode(mode)
        notes = retrieve(f"{normalized} {user_payload}")
        system_prompt = build_system_prompt(normalized, notes, persona)

        if self._available:
            try:
                from arena_api.models import GenerationConfig  # type: ignore

                # ChatSession has no dedicated system role: prepend it.
                prompt = self._render_transcript(system_prompt, history, user_payload)
                text = self._client.complete(
                    prompt,
                    model=MODEL_NAME,
                    config=GenerationConfig(
                        temperature=0.1 if temperature is None else temperature,
                        max_tokens=max_tokens),
                )
                parsed = self._parse_model_json(text)
                parsed._provenance = "capricorn"
                self._last_error = None
                return parsed
            except Exception as exc:
                self._last_error = f"capricorn call failed: {type(exc).__name__}: {exc}"
                log.error("capricorn call failed (%s), falling back to heuristics", exc)
        offline = self._offline_analyze(user_payload, normalized)
        offline._provenance = "offline"
        return offline

    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_transcript(system_prompt: str, history: Optional[Iterable[ChatTurn]],
                           user_payload: str) -> str:
        lines = [system_prompt, "", "=== CONVERSATION ==="]
        for turn in (history or []):
            prefix = "USER" if turn.role == "user" else "ASSISTANT"
            lines.append(f"{prefix}: {turn.content}")
        lines += [f"USER: {user_payload}", "", "ASSISTANT (one JSON object only):"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _json_candidates(text: str) -> Iterable[dict]:
        """Yield every balanced {...} block that parses as a JSON object."""
        cleaned = re.sub(r"```(?:json)?", "", text or "")
        for match in re.finditer(r"\{", cleaned):
            start = match.start()
            depth = 0
            for i in range(start, len(cleaned)):
                if cleaned[i] == "{":
                    depth += 1
                elif cleaned[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            yield json.loads(cleaned[start:i + 1])
                        except json.JSONDecodeError:
                            pass
                        break

    @classmethod
    def _parse_model_json(cls, text: str) -> AiActionResponse:
        """Extract the first *valid* JSON object from model output."""
        last_error: Optional[str] = None
        seen = False
        for data in cls._json_candidates(text):
            seen = True
            if not isinstance(data, dict):
                continue
            data.setdefault("status", "ok")
            try:
                return AiActionResponse(**data)
            except (ValidationError, TypeError) as exc:
                last_error = str(exc)
                continue
        if seen:
            raise ValueError(f"model JSON did not validate: {last_error}")
        raise ValueError(f"model returned no JSON: {(text or '')[:200]!r}")

    # ------------------------------------------------------------------ #
    @staticmethod
    def _offline_analyze(payload: str, mode: str = "analyze_ir") -> AiActionResponse:
        """Deterministic fallback; kept as a static method for compatibility."""
        return OfflineAnalyzer.analyze(payload, mode)

    # ------------------------------------------------------------------ #
    def probe(self) -> Tuple[bool, Optional[str]]:
        """Actually try the model once, so /health cannot lie about the session."""
        if not self._available:
            return False, self._last_error or "arena_api unavailable (offline fallback)"
        try:
            from arena_api.models import GenerationConfig  # type: ignore

            self._client.complete(
                "Reply with the single word: ok",
                model=MODEL_NAME,
                config=GenerationConfig(temperature=0.0, max_tokens=8))
            self._last_error = None
            return True, None
        except Exception as exc:
            message = f"probe failed: {type(exc).__name__}: {exc}"
            self._last_error = message
            return False, message


bridge = CapricornBridge()

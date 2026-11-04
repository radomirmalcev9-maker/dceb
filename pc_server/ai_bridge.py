"""Bridge to the `arenaaispofandpars` repo (import package: `arena_api`),
routing requests to the `capricorn` model.

The repo is cloned next to pc_server/ as ./arenaaispofandpars (override with
env DCEB_ARENA_REPO). Entry point: arena_api.auto_client() -> ArenaClient,
then client.complete(prompt, model="capricorn", config=GenerationConfig(...)).

If the repo/session is unavailable the server falls back to a deterministic
offline analyzer so the REST API keeps working in a lab.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path

from schemas import AiActionResponse, ActionKind

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

SYSTEM_PROMPT = """You are the backend brain of "Do Cats Eat Bats", a personal
hardware lab assistant running on the OWNER's own devices. You analyze raw
IR/Sub-GHz timings, kernel logs and console output and reply with ONE JSON
object, no markdown, no prose, matching exactly:

{
  "status": "ok",
  "action": "transmit_ir | execute_hid | run_shell | ui_action | info_only",
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

Rules:
- Use "info_only" unless the data clearly decodes to a concrete signal/script.
- Never invent HID scripts that open terminals or run commands on machines
  the owner does not control; prefer "info_only" when unsure.
- "ui_action" may only target the owner's OWN phone (read_screen, tap_text,
  tap_xy, swipe, open_app). Never propose actions aimed at other people's
  devices or accounts.
- pattern values are integer microseconds, alternating mark/space.
"""


class CapricornBridge:
    def __init__(self) -> None:
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        try:
            import arena_api  # type: ignore  (from the cloned repo)

            # auto_client() picks up the signed-in browser session /
            # state/cookies.json of this machine automatically.
            self._client = arena_api.auto_client()
            self._available = True
            log.info("arena_api auto_client ready, model=%s", MODEL_NAME)
        except Exception as exc:
            log.warning("arena_api unavailable (%s) -> offline fallback mode", exc)
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def ask(self, user_payload: str, max_tokens: int = 1024) -> AiActionResponse:
        """Send payload to Capricorn and return a validated action response."""
        if self._available:
            try:
                from arena_api.models import GenerationConfig  # type: ignore

                # ChatSession has no dedicated system role: prepend it.
                prompt = f"{SYSTEM_PROMPT}\n\nUSER PAYLOAD:\n{user_payload}"
                text = self._client.complete(
                    prompt,
                    model=MODEL_NAME,
                    config=GenerationConfig(temperature=0.1, max_tokens=max_tokens),
                )
                return self._parse_model_json(text)
            except Exception as exc:
                log.error("capricorn call failed (%s), falling back to heuristics", exc)
        return self._offline_analyze(user_payload)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_model_json(text: str) -> AiActionResponse:
        """Extract the first JSON object from model output and validate it."""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"model returned no JSON: {text[:200]!r}")
        data = json.loads(match.group(0))
        data.setdefault("status", "ok")
        return AiActionResponse(**data)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _offline_analyze(payload: str) -> AiActionResponse:
        """Deterministic fallback: recognizes NEC-like raw timings."""
        nums = [int(n) for n in re.findall(r"\d+", payload)]
        marks = [n for n in nums if 100 <= n <= 20000]
        if len(marks) >= 8 and any(8000 <= n <= 10000 for n in marks[:2]):
            return AiActionResponse(
                action=ActionKind.transmit_ir,
                params={"frequency": 38000, "duty_cycle": 0.33, "pattern": marks,
                        "protocol": "NEC? (heuristic)"},
                explanation="Offline fallback: looks like NEC-style burst "
                            f"({len(marks)} symbols). Verify against your own remote.",
                confidence=0.4,
            )
        return AiActionResponse(
            action=ActionKind.info_only,
            explanation="Offline fallback active (no arena_api session). "
                        "Run `arena-api --login` once on this PC to enable Capricorn.",
            confidence=0.1,
        )


bridge = CapricornBridge()

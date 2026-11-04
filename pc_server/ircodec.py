"""IR protocol encoders — the Python twin of ``IrParser.kt``.

Flipper ``.ir`` files store two kinds of entry:

* ``type: raw``      — a capture, ``frequency`` + ``data[]`` in microseconds
* ``type: parsed``   — ``protocol`` + ``address`` + ``command``, which the
  device has to *encode* into timings before it can transmit

96 of the 112 Samsung TV entries in the bundled database are ``parsed``. The
phone encodes them in Kotlin, but the server has to do it too: otherwise
Capri can name a Samsung ``Power`` button and still has no waveform to send.

Two details that make the port trustworthy rather than hopeful:

* **Constants are measured, not assumed.** Flipper derives NEC timings from
  ``unit = 1e6 / freq``, which puts the bit mark at 416 us on 38 kHz — but no
  real remote transmits that. Decoding 485 NEC captures from the bundled
  database gives header 8944/4446 us, bit mark 572 us, one-space 1658 us,
  zero-space 546 us, which is what Flipper's own NEC preset sends (563/1687)
  and what ``tests/test_ircodec.py`` pins down.
* **Encoders refuse to guess.** An unknown protocol returns an empty list and
  the caller reports "no waveform" instead of transmitting noise.

Reference bit layouts (Flipper firmware, ``protocol_*.c``):
  NEC      : 16 addr LSB-first (addr | ~addr), 8 cmd LSB-first, 8 ~cmd
  NECext   : 16 addr LSB-first, 16 cmd LSB-first
  Samsung32: 8 addr LSB-first, 8 addr, 8 cmd, 8 ~cmd
  RC5      : 2 start, toggle, 5 addr MSB-first, 6 cmd MSB-first
  RC6      : 1 start, 3 mode, 1 trailer, toggle, 8 addr, 8 cmd
  SIRC     : 7 cmd LSB-first, 5/8/13 addr LSB-first
"""
from __future__ import annotations

import re

# Measured from 485 donor captures at 38 kHz (see module docstring).
NEC_HEADER_MARK = 8944
NEC_HEADER_SPACE = 4446
NEC_BIT_MARK = 572
NEC_ONE_SPACE = 1658
NEC_ZERO_SPACE = 546
NEC_TRAILER = 572

SAMSUNG_HEADER_MARK = 4500
SAMSUNG_HEADER_SPACE = 4500
SAMSUNG_BIT_MARK = 572
SAMSUNG_ONE_SPACE = 1658
SAMSUNG_ZERO_SPACE = 546

RC5_HALF = 889            # 36 kHz, 32 cycles per half-bit
RC6_HALF = 444
SIRC_UNIT = 25            # 40 kHz -> 600 us = 24 units, 1200 us = 48 units


def _unit(freq_hz: int) -> int:
    return int(round(1_000_000 / max(freq_hz, 1)))


def _scale_38(value: int, freq_hz: int) -> int:
    """Rescale a 38 kHz constant to another carrier (frequency is a period)."""
    if freq_hz == 38000:
        return value
    return int(round(value * 38000 / max(freq_hz, 1)))


def hex_bytes(text: str) -> list[int]:
    """``"07 00 00 00"`` -> ``[7, 0, 0, 0]``; tolerant of stray separators."""
    out: list[int] = []
    for token in re.split(r"[\s,]+", (text or "").strip()):
        if not token:
            continue
        try:
            out.append(int(token, 16))
        except ValueError:
            continue
    return out


def _first(values: list[int], default: int = 0) -> int:
    return values[0] if values else default


# --------------------------------------------------------------------------- #
# NEC family
# --------------------------------------------------------------------------- #

def _frame(word: int, freq_hz: int, header_mark: int, header_space: int) -> list[int]:
    """32-bit LSB-first frame: header, 32 bit cells, trailing mark."""
    out = [_scale_38(header_mark, freq_hz), _scale_38(header_space, freq_hz)]
    for i in range(32):
        out.append(_scale_38(NEC_BIT_MARK, freq_hz))
        out.append(_scale_38(NEC_ONE_SPACE if (word >> i) & 1 else NEC_ZERO_SPACE, freq_hz))
    out.append(_scale_38(NEC_TRAILER, freq_hz))
    return out


def encode_nec(addr: list[int], cmd: list[int], freq_hz: int = 38000) -> list[int]:
    """NEC: 8 addr + 8 ~addr + 8 cmd + 8 ~cmd, all LSB-first."""
    a = _first(addr)
    c = _first(cmd)
    word = (a & 0xFF) | ((~a & 0xFF) << 8) | ((c & 0xFF) << 16) | ((~c & 0xFF) << 24)
    return _frame(word, freq_hz, NEC_HEADER_MARK, NEC_HEADER_SPACE)


def encode_nec_ext(addr: list[int], cmd: list[int], freq_hz: int = 38000) -> list[int]:
    """NECext: 16 addr + 16 cmd, LSB-first, no complement bytes."""
    hi, lo = (_first(addr), addr[1] if len(addr) > 1 else 0)
    a = (lo << 8) | hi                    # .ir stores little-endian
    c_lo, c_hi = _first(cmd), (cmd[1] if len(cmd) > 1 else 0)
    c = (c_hi << 8) | c_lo
    word = (a & 0xFFFF) | ((c & 0xFFFF) << 16)
    return _frame(word, freq_hz, NEC_HEADER_MARK, NEC_HEADER_SPACE)


# --------------------------------------------------------------------------- #
# Samsung32
# --------------------------------------------------------------------------- #

def encode_samsung32(addr: list[int], cmd: list[int], freq_hz: int = 38000) -> list[int]:
    """Samsung32: 8 addr, 8 addr **repeated** (not inverted), 8 cmd, 8 ~cmd.

    The repeat-not-invert detail is not a guess: decoding the donor's own raw
    capture (``TVs/Samsung/Samsung_BN59-01247A``, button "C") yields
    ``07 07 15 EA`` — address 0x07 twice, then command 0x15 and its complement.
    """
    a = _first(addr)
    c = _first(cmd)
    word = ((a & 0xFF) | ((a & 0xFF) << 8) | ((c & 0xFF) << 16)
            | (((~c) & 0xFF) << 24))
    out = [_scale_38(SAMSUNG_HEADER_MARK, freq_hz),
           _scale_38(SAMSUNG_HEADER_SPACE, freq_hz)]
    for i in range(32):
        out.append(_scale_38(SAMSUNG_BIT_MARK, freq_hz))
        out.append(_scale_38(SAMSUNG_ONE_SPACE if (word >> i) & 1
                             else SAMSUNG_ZERO_SPACE, freq_hz))
    out.append(_scale_38(SAMSUNG_BIT_MARK, freq_hz))
    return out


# --------------------------------------------------------------------------- #
# RC5 / RC6
# --------------------------------------------------------------------------- #

def _manchester(bits: list[int], half: int,
                lead_mark: int = 0, lead_space: int = 0) -> list[int]:
    """Merge a Manchester bit stream into alternating mark/space durations.

    Manchester: a ``1`` is mark-then-space, a ``0`` is space-then-mark, each
    half a bit long. Adjacent halves at the same level are merged into one
    duration — a zero-length entry would be rejected by ConsumerIrManager, and
    Flipper's own encoder merges rather than emitting it.

    The stream is expected to start on a mark (RC5/RC6 both idle high). The
    result therefore always alternates mark, space, mark, ... and ends on a
    mark: a trailing space is dropped rather than padded.
    """
    halves: list[int] = []
    for bit in bits:
        halves.extend((1, 0) if bit == 1 else (0, 1))

    out: list[int] = []
    if lead_mark > 0:
        out.append(lead_mark)
        halves.insert(0, 0)               # consumed by the lead-space slot
    else:
        out.append(0)                     # placeholder mark

    level = 1                             # level of the slot at out[-1]
    for i, half_level in enumerate(halves):
        if i == 0 and lead_mark > 0:
            out.append(lead_space)        # the lead space occupies this slot
            level = 0
            continue
        if half_level == level:
            out[-1] += half
        else:
            out.append(half)
            level = half_level

    if len(out) % 2 == 0:                 # ends on a space -> drop it
        out.pop()
    if out and out[0] == 0:
        out.pop(0)
    return out


def encode_rc5(addr: list[int], cmd: list[int], freq_hz: int = 36000,
               toggle: int = 0) -> list[int]:
    half = _unit(freq_hz)
    a = _first(addr) & 0x1F
    c = _first(cmd) & 0x3F
    bits = [1, 1, toggle & 1]
    bits += [(a >> i) & 1 for i in range(4, -1, -1)]
    bits += [(c >> i) & 1 for i in range(5, -1, -1)]
    raw = _manchester(bits, half)
    # RC5 has no trailing trailer; the last bit ends exactly on the mark.
    return raw


def encode_rc6(addr: list[int], cmd: list[int], freq_hz: int = 36000,
               toggle: int = 0) -> list[int]:
    half = _unit(freq_hz)
    a = _first(addr) & 0xFF
    c = _first(cmd) & 0xFF
    bits = [1] + [0, 0, 0] + [1 if toggle & 1 else 0]      # start, mode 0, trailer
    bits += [(a >> i) & 1 for i in range(7, -1, -1)]
    bits += [(c >> i) & 1 for i in range(7, -1, -1)]
    return _manchester(bits, half, lead_mark=6 * half, lead_space=2 * half)


# --------------------------------------------------------------------------- #
# Sony SIRC
# --------------------------------------------------------------------------- #

def encode_sirc(addr: list[int], cmd: list[int], bits: int = 12,
                freq_hz: int = 40000) -> list[int]:
    unit = _unit(freq_hz)
    a = _first(addr)
    c = _first(cmd)
    cmd_bits = {12: 7, 15: 7, 20: 8}.get(bits, 7)
    addr_bits = max(bits - 1 - cmd_bits, 5)
    stream = [(c >> i) & 1 for i in range(cmd_bits)]
    stream += [(a >> i) & 1 for i in range(addr_bits)]
    out = [96 * unit, 24 * unit]            # 2400 / 600 us at 40 kHz
    for bit in stream:
        out.append((48 if bit else 24) * unit)
        out.append(24 * unit)
    return out


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

_ALIASES = {
    "NEC": "nec", "NEC42": "nec", "NECEXT": "necext", "NEC42EXT": "necext",
    "SAMSUNG32": "samsung32", "SAMSUNG": "samsung32",
    "RC5": "rc5", "RC5X": "rc5", "RC6": "rc6",
    "SIRC": "sirc", "SIRC12": "sirc", "SIRC15": "sirc", "SIRC20": "sirc",
}


def supported_protocols() -> list[str]:
    return sorted(_ALIASES)


def encode(protocol: str, address: str, command: str,
           freq_hz: int = 38000) -> list[int]:
    """Encode one ``parsed`` entry. Empty list == unsupported protocol."""
    key = _ALIASES.get((protocol or "").strip().upper())
    if key is None:
        return []
    addr = hex_bytes(address)
    cmd = hex_bytes(command)
    if key == "nec":
        return encode_nec(addr, cmd, freq_hz)
    if key == "necext":
        return encode_nec_ext(addr, cmd, freq_hz)
    if key == "samsung32":
        return encode_samsung32(addr, cmd, freq_hz)
    if key == "rc5":
        return encode_rc5(addr, cmd, freq_hz)
    if key == "rc6":
        return encode_rc6(addr, cmd, freq_hz)
    if key == "sirc":
        upper = (protocol or "").strip().upper()
        width = int(upper[4:]) if upper[4:].isdigit() else 12
        return encode_sirc(addr, cmd, width, 40000)
    return []

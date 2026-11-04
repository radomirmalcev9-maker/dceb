"""Round-trip validation of the IR encoders against the donor's own captures.

The bundled database contains both kinds of Flipper entry for the same
protocols: ``type: parsed`` records (protocol + address + command) and
``type: raw`` captures of real remotes. That is a free oracle — decode a raw
capture into a bit word, re-encode it with ``ircodec``, and the two should
agree within measurement noise.

These tests are skipped automatically when ``irdb/`` has not been imported yet
(``python tools/import_flipper_irdb.py``), so the suite still runs on a fresh
clone.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ircodec
import irdb
import irdb_resolver as resolver

HAVE_DB = bool(irdb.list_entries())


# --------------------------------------------------------------------------- #
# Pure encoder checks (no database needed)
# --------------------------------------------------------------------------- #

def test_nec_known_constants():
    """NEC Power, addr 0x07 / cmd 0x02, at 38 kHz."""
    pattern = ircodec.encode_nec([0x07], [0x02], 38000)
    assert pattern[:2] == [8944, 4446]
    # bit 0 of the word is 1 (address 0x07) -> 572 / 1658
    assert pattern[2:4] == [572, 1658]
    assert len(pattern) == 67
    assert len(pattern) % 2 == 1          # ends on the trailing mark


def test_samsung_repeats_address_not_inverts():
    """Samsung32 byte 2 repeats the address; byte 4 is the command complement.

    Pinned by the donor capture ``Samsung_BN59-01247A`` button "C", which
    decodes to 07 07 15 EA.
    """
    pattern = ircodec.encode_samsung32([0x07], [0x15], 38000)
    assert pattern[:2] == [4500, 4500]
    bits = [1 if pattern[3 + 2 * i] > 900 else 0 for i in range(32)]
    word = sum(bit << i for i, bit in enumerate(bits))
    assert word & 0xFF == 0x07
    assert (word >> 8) & 0xFF == 0x07          # repeated, NOT 0xF8
    assert (word >> 16) & 0xFF == 0x15
    assert (word >> 24) & 0xFF == (~0x15) & 0xFF


def test_nec_word_layout():
    pattern = ircodec.encode_nec([0x04], [0x08], 38000)
    bits = [1 if pattern[3 + 2 * i] > 900 else 0 for i in range(32)]
    word = sum(bit << i for i, bit in enumerate(bits))
    assert word & 0xFF == 0x04
    assert (word >> 8) & 0xFF == (~0x04) & 0xFF     # NEC inverts the address
    assert (word >> 16) & 0xFF == 0x08
    assert (word >> 24) & 0xFF == (~0x08) & 0xFF


def test_nec_ext_has_no_complement():
    pattern = ircodec.encode_nec_ext([0x34, 0x12], [0xCD], 38000)
    bits = [1 if pattern[3 + 2 * i] > 900 else 0 for i in range(32)]
    word = sum(bit << i for i, bit in enumerate(bits))
    assert word & 0xFFFF == 0x1234          # little-endian in .ir, LSB-first out
    assert (word >> 16) & 0xFFFF == 0x00CD


def test_unknown_protocol_returns_empty():
    assert ircodec.encode("TOTALLY-MADE-UP", "00", "00") == []


@pytest.mark.parametrize("protocol", ["RC5", "RC6"])
def test_manchester_always_starts_on_mark_and_alternates(protocol):
    pattern = ircodec.encode(protocol, "00", "0C", 36000)
    assert pattern, f"{protocol} produced no pattern"
    assert pattern[0] > 0
    assert len(pattern) % 2 == 1, "transmission must end on a mark"
    assert all(value > 0 for value in pattern), "zero-length pulses are invalid"


def test_sirc_header_and_length():
    pattern = ircodec.encode_sirc([0x01], [0x15], bits=12)
    assert pattern[:2] == [2400, 600]
    assert len(pattern) == 2 + 2 * 12 + 0 or len(pattern) >= 26


# --------------------------------------------------------------------------- #
# Round-trip against donor captures (needs the imported database)
# --------------------------------------------------------------------------- #

def _decode_bits(pattern, frequency):
    unit = max(round(1_000_000 / frequency), 1)
    bits = []
    index = 2
    while index + 1 < len(pattern):
        bits.append(1 if pattern[index + 1] > 30 * unit else 0)
        index += 2
    return bits


def _close(encoded, capture, tolerance=0.25, floor=90):
    """Compare an encoded frame to the leading part of a real capture.

    Only the **encoded frame prefix** is compared. Captures hold everything a
    receiver saw afterwards and real remotes differ wildly there:

    * stop at the trailing mark (67 symbols)
    * append the NEC repeat marker (~69 symbols, 2210/546 us)
    * send the entire frame again after a ~40 ms gap (71 symbols)
    * keep several repeats and a tri-state tail, up to 500 symbols

    Repetition is a transmission-loop concern, not a waveform concern, so the
    encoder emits exactly one clean frame. Comparing past it would measure how
    a particular remote chose to repeat itself, which is not what this test is
    about.
    """
    if len(capture) < len(encoded):
        return False
    return all(abs(a - b) <= max(floor, tolerance * b)
               for a, b in zip(encoded, capture))


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_nec_captures_round_trip():
    """The encoder must reproduce the **bit word** of every real NEC capture.

    What is being checked here is the part an encoder actually decides: byte
    order, complement bytes, LSB-first bit placement, header/frame structure.
    Absolute pulse widths are *not* asserted across captures, because real
    remotes are only approximately NEC — 23 of the 288 captures use a 458 us
    zero-space where the standard says 546, and a generic encoder cannot and
    should not imitate every vendor's slop. Timing constants get their own test
    below, pinned to the measured medians.

    Round trip: decode capture -> (addr, cmd) -> re-encode -> decode again ->
    the two words must be identical. A separate count caps how many NEC-true
    captures (bit mark 540-600 us, standard header) may miss on frame shape;
    measured agreement is 130/148 = 87.8%, and the misses are vendors that sit
    just outside the window (LEDs sending 528 us marks). The floor is set at
    85% so a real regression trips it while measured vendor variation does not.
    """
    checked = 0
    shape_checked = 0
    shape_matched = 0
    for entry in irdb.list_entries():
        text = irdb.read_entry(entry.category, entry.brand, entry.model) or ""
        if "type: raw" not in text:
            continue
        for signal in resolver.parse_ir_file(text)[:2]:
            pattern = signal.pattern
            if len(pattern) < 60:                 # at least one 32-bit frame
                continue
            unit = max(round(1_000_000 / signal.frequency), 1)
            if not (330 < pattern[0] / unit < 355 and 160 < pattern[1] / unit < 180):
                continue
            bits = _decode_bits(pattern, signal.frequency)
            if len(bits) < 32:
                continue
            word = sum(b << i for i, b in enumerate(bits[:32]))
            addr, addr_inv = word & 0xFF, (word >> 8) & 0xFF
            cmd, cmd_inv = (word >> 16) & 0xFF, (word >> 24) & 0xFF
            if addr_inv != (~addr) & 0xFF or cmd_inv != (~cmd) & 0xFF:
                continue                          # not really NEC

            checked += 1
            encoded = ircodec.encode_nec([addr], [cmd], signal.frequency)
            assert len(encoded) == 67, f"{entry.brand}/{entry.model} wrong frame length"
            # Frame *shape* is counted only on captures that are NEC to the
            # microsecond. Plenty of remotes reuse the NEC header ratio with
            # their own bit cells (Pioneer: 599/458) and still decode to a
            # self-consistent complement word; a generic NEC encoder must not
            # imitate those, so they are covered by the bit-word assertion only.
            if abs(pattern[1] - 4446) < 250 and 540 <= pattern[2] <= 600:
                shape_checked += 1
                if _close(encoded, pattern):
                    shape_matched += 1

            # Re-decode the encoder's own output at the same carrier.
            cell_unit = max(round(1_000_000 / signal.frequency), 1)
            rebuilt = [1 if encoded[3 + 2 * i] > 30 * cell_unit else 0 for i in range(32)]
            rebuilt_word = sum(bit << i for i, bit in enumerate(rebuilt))
            assert rebuilt_word == word, (
                f"{entry.brand}/{entry.model} "
                f"'{signal.name}': {rebuilt_word:#010x} != {word:#010x}")

    assert checked > 100, f"expected a large NEC sample, got {checked}"
    assert shape_checked > 100, f"expected a large NEC-true sample, got {shape_checked}"
    ratio = shape_matched / shape_checked
    assert ratio > 0.85, f"only {shape_matched}/{shape_checked} NEC-true captures matched"


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_timing_constants_match_measured_medians():
    """Encoder constants must sit on the measured medians, not textbook values.

    Flipper derives NEC from `unit = 1e6/freq`, giving a 416 us bit mark at
    38 kHz. Real hardware sends ~572 us. If someone "tidies" the constants back
    to the theoretical numbers, this test fails.
    """
    marks, ones, zeros, header_marks = [], [], [], []
    for entry in irdb.list_entries():
        text = irdb.read_entry(entry.category, entry.brand, entry.model) or ""
        if "type: raw" not in text:
            continue
        for signal in resolver.parse_ir_file(text)[:2]:
            pattern = signal.pattern
            if len(pattern) < 60:
                continue
            unit = max(round(1_000_000 / signal.frequency), 1)
            if not (330 < pattern[0] / unit < 355 and 160 < pattern[1] / unit < 180):
                continue
            header_marks.append(pattern[0])
            for index in range(2, min(len(pattern) - 1, 66), 2):
                marks.append(pattern[index])
                (ones if pattern[index + 1] > 1000 else zeros).append(pattern[index + 1])

    def median(values):
        values = sorted(values)
        return values[len(values) // 2]

    assert abs(median(header_marks) - ircodec.NEC_HEADER_MARK) < 200
    assert abs(median(marks) - ircodec.NEC_BIT_MARK) < 80
    assert abs(median(ones) - ircodec.NEC_ONE_SPACE) < 120
    assert abs(median(zeros) - ircodec.NEC_ZERO_SPACE) < 80


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_parsed_samsung_entries_encode():
    """The Samsung TV file is all `parsed`; it must yield a real waveform."""
    text = irdb.read_entry("TVs", "Samsung", "Samsung")
    assert text is not None, "Samsung TV file missing from the database"
    assert "protocol: Samsung32" in text
    pattern = ircodec.encode("Samsung32", "07 00 00 00", "02 00 00 00", 38000)
    assert len(pattern) == 67
    assert pattern[:2] == [4500, 4500]

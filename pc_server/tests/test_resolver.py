"""Resolver tests — the part that decides whether Capri is useful or a liar.

Every case here is a phrase a human would actually type. The resolver must find
the right device, pick the right button, and refuse when it cannot.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import irdb
import irdb_resolver as resolver

HAVE_DB = bool(irdb.list_entries())
needs_db = pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")


def test_query_concepts_are_not_greedy():
    """'volume up' must not also emit UP or VOLUME_DOWN."""
    assert resolver._query_concepts("volume up") == ["VOLUME_UP"]
    assert resolver._query_concepts("volume down") == ["VOLUME_DOWN"]
    assert resolver._query_concepts("epson projector volume up") == ["VOLUME_UP"]


def test_russian_inflections_resolve():
    """Russian is inflected; the tables store stems, the sentence does not."""
    assert "POWER" in resolver._query_concepts("включи проектор")
    assert "PAUSE" in resolver._query_concepts("поставь паузу")
    assert "VOLUME_UP" in resolver._query_concepts("прибавь громкость")
    assert "VOLUME_DOWN" in resolver._query_concepts("сделай тише")


def test_name_concepts_cover_vendor_spellings():
    """The database spells the same button a dozen ways."""
    for name in ("Vol_up", "VOL+", "Volume_Up", "VolumeUp"):
        assert resolver._name_concept(name) == "VOLUME_UP", name
    for name in ("Vol_dn", "VOL-", "Volume_Down", "Vol_dwn"):
        assert resolver._name_concept(name) == "VOLUME_DOWN", name


def test_synonym_lookup_does_not_hijack_text():
    """'up' inside 'update' is not a button press."""
    assert resolver._query_concepts("update the firmware") != ["UP"]


@needs_db
def test_english_phrase_resolves_to_stored_waveform():
    resolution = resolver.resolve("epson projector power")
    assert resolution is not None
    assert resolution.brand == "Epson"
    assert resolution.category == "Projectors"
    assert resolution.button.lower() == "power"
    assert len(resolution.pattern) > 60


@needs_db
def test_russian_phrase_resolves_the_same_device():
    resolution = resolver.resolve("включи проектор epson")
    assert resolution is not None
    assert resolution.brand == "Epson"
    assert resolution.button == "Power"


@needs_db
def test_volume_up_is_not_down():
    """The failure that started all this: 'up' resolving to Volume_Down."""
    up = resolver.resolve("epson projector volume up")
    down = resolver.resolve("epson projector volume down")
    assert up is not None and down is not None
    assert "up" in up.button.lower().replace("_", " ")
    assert "down" in down.button.lower().replace("_", " ")


@needs_db
def test_category_word_beats_a_similar_model():
    """'projector' must not land on a motorised projector *screen*."""
    resolution = resolver.resolve("lg projector menu")
    assert resolution is not None
    assert resolution.category == "Projectors"


@needs_db
def test_parsed_protocol_entries_get_encoded():
    """The Samsung TV file is entirely `type: parsed`; it must still transmit."""
    resolution = resolver.resolve("samsung tv power")
    assert resolution is not None
    assert resolution.pattern, "parsed entry was not encoded into timings"
    assert len(resolution.pattern) == 67


@needs_db
def test_nonsense_returns_none():
    assert resolver.resolve("make me a sandwich") is None
    assert resolver.resolve("") is None


@needs_db
def test_unknown_button_returns_no_pattern():
    """Never transmit the wrong button just to have something to send."""
    resolution = resolver.resolve("epson projector flibbertigibbet")
    assert resolution is not None
    assert resolution.pattern == []
    assert resolution.available_buttons

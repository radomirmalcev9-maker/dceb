"""Offline tests for the DCEB PC server core (no network / no AI session)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_bridge import CapricornBridge
from schemas import ActionKind, AiActionResponse, IrParams
import irdb


# ----------------------------- schemas ---------------------------------- #

def test_action_response_valid_ir():
    r = AiActionResponse(action=ActionKind.transmit_ir,
                         params={"frequency": 38000, "pattern": [9000, 4500]})
    assert r.status == "ok" and r.action == ActionKind.transmit_ir


def test_action_response_rejects_empty_pattern():
    with pytest.raises(Exception):
        AiActionResponse(action=ActionKind.transmit_ir, params={"pattern": []})


def test_ir_params_defaults():
    p = IrParams()
    assert p.frequency == 38000 and 0 < p.duty_cycle <= 1


# ----------------------------- ai bridge -------------------------------- #

def test_parse_model_json_extracts_object():
    text = 'sure! here you go:\n{"status":"ok","action":"info_only",'
    text += '"params":{},"explanation":"hi","confidence":0.9}\nthanks'
    r = CapricornBridge._parse_model_json(text)
    assert r.action == ActionKind.info_only and r.confidence == 0.9


def test_offline_analyze_detects_nec_like():
    payload = "raw timings: 9000 4500 " + " ".join(["560"] * 64)
    r = CapricornBridge._offline_analyze(payload)
    assert r.action == ActionKind.transmit_ir
    assert r.params["pattern"][0] == 9000


def test_offline_analyze_info_for_noise():
    r = CapricornBridge._offline_analyze("hello world, nothing numeric here")
    assert r.action == ActionKind.info_only


# ----------------------------- irdb ------------------------------------- #

def test_irdb_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(irdb, "IRDB_ROOT", tmp_path)
    assert irdb.read_entry("TVs", "../../etc", "passwd") is None
    assert irdb.read_entry("..", "x", "y") is None


def test_irdb_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(irdb, "IRDB_ROOT", tmp_path)
    (tmp_path / "TVs" / "Acme").mkdir(parents=True)
    (tmp_path / "TVs" / "Acme" / "TV1.ir").write_text("Filetype: IR signals file\n")
    assert len(irdb.search("acme")) == 1
    assert "IR signals file" in irdb.read_entry("TVs", "Acme", "TV1")


def test_save_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(irdb, "DUMPS_ROOT", tmp_path)
    p = irdb.save_dump("my phone dump!", "data")
    assert p.is_file() and irdb.dumps_count() == 1

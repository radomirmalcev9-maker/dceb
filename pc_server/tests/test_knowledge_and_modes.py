"""Mode routing, prompt grounding and deterministic offline analysis."""
import pytest

from ai_bridge import (OfflineAnalyzer, build_system_prompt, normalize_mode,
                       CapricornBridge)
from knowledge import retrieve, render
from schemas import ActionKind


# --------------------------------------------------------------- mode routing

@pytest.mark.parametrize("raw,expected", [
    ("ir", "analyze_ir"),
    ("IR", "analyze_ir"),
    ("dump:ir", "analyze_ir"),
    ("kernel", "dmesg"),
    ("logcat", "console"),
    ("BLE", "bluetooth"),
    ("", "chat"),
    ("total_nonsense_mode", "chat"),
    ("ctf", "ctf"),
])
def test_normalize_mode(raw, expected):
    assert normalize_mode(raw) == expected


def test_build_system_prompt_includes_mode_and_notes():
    notes = retrieve("NEC infrared timing")
    prompt = build_system_prompt("analyze_ir", notes)
    assert "analyze_ir" in prompt.lower() or "infrared analysis" in prompt.lower()
    assert "AUTHORIZATION SCOPE" in prompt
    assert notes and all(n.topic in prompt for n in notes)


def test_retrieve_is_relevant():
    assert any(n.topic in {"ir-nec", "ir-protocols"} for n in retrieve("NEC 9000 4500"))
    assert any(n.topic == "scope-rules" for n in retrieve("is this authorized?"))
    assert any(n.topic == "hardening-ssh"
               for n in retrieve("how do I harden ssh on my box"))
    assert render([]) == ""


# --------------------------------------------------------------- JSON parsing

def test_parse_model_json_handles_fences_and_prose():
    text = '```json\n{"status":"ok","action":"info_only","explanation":"f","confidence":0.2}\n```'
    r = CapricornBridge._parse_model_json(text)
    assert r.action is ActionKind.info_only


def test_parse_model_json_skips_invalid_first_object():
    text = ('{"action":"not_a_real_action"}\n'
            '{"status":"ok","action":"info_only","explanation":"second","confidence":0.5}')
    r = CapricornBridge._parse_model_json(text)
    assert r.explanation == "second" and r.confidence == 0.5


def test_parse_model_json_raises_without_json():
    with pytest.raises(ValueError):
        CapricornBridge._parse_model_json("sorry, I cannot help")


# ------------------------------------------------------------ offline analyzer

def test_offline_ir_alias_mode_detects_nec():
    payload = "raw timings: 9000 4500 " + " ".join(["560"] * 64)
    r = OfflineAnalyzer.analyze(payload, "ir")
    assert r.action is ActionKind.transmit_ir
    assert r.params["pattern"][0] == 9000


def test_offline_dmesg_flags_keywords():
    r = OfflineAnalyzer.analyze(
        "[  123.4] traps: app[1234] segfault at 0 ip 0000 sp 0000", "dmesg")
    assert r.action is ActionKind.info_only
    assert "segfault" in r.params["keywords"]


def test_offline_console_flags_fatal():
    r = OfflineAnalyzer.analyze("E AndroidRuntime: FATAL EXCEPTION: main", "console")
    assert r.action is ActionKind.info_only
    assert "fatal exception" in r.params["keywords"]


def test_offline_nfc_extracts_uid():
    r = OfflineAnalyzer.analyze("Tag UID: 04 A2 3F 12 34 56 78", "nfc")
    uid = r.params["uid"]
    assert uid.startswith("04:A2") or uid.startswith("04:A2:3F")
    assert r.params["bytes"] >= 4


def test_offline_bluetooth_builds_inventory():
    payload = ("device: JBL Flip 5, 0C:A6:94:11:22:33, rssi -55\n"
               "device: LO-Phone, 4C:BB:CC:00:11:22, rssi -70")
    r = OfflineAnalyzer.analyze(payload, "bluetooth")
    assert r.action is ActionKind.bt_inventory
    assert len(r.params["devices"]) == 2
    assert r.params["devices"][0]["rssi"] in (-55, -70)


def test_offline_netscan_summarizes_hosts():
    payload = "192.168.1.10 port 445 open; 192.168.1.11 port 22 open"
    r = OfflineAnalyzer.analyze(payload, "netscan")
    assert r.action is ActionKind.net_inventory
    assert "192.168.1.10" in r.params["hosts"]
    assert 445 in r.params["risky_ports"]


def test_offline_hardening_returns_checklist():
    r = OfflineAnalyzer.analyze("harden my ssh server", "hardening")
    assert r.action is ActionKind.hardening
    assert "ssh" in r.params["topics"]
    assert len(r.params["checklist"]) >= 3


def test_offline_ctf_detects_base64():
    r = OfflineAnalyzer.analyze("dGhpcyBpcyBhIHRlc3Qgc3RyaW5nIGZvciB5b3U=", "ctf")
    assert r.action is ActionKind.ctf_answer
    assert any("base64" in h for h in r.params["hints"])


def test_offline_subghz_identifies_band():
    r = OfflineAnalyzer.analyze("433.92 MHz OOK, timings 300 900 300 900", "subghz")
    assert r.action is ActionKind.info_only
    assert r.params["frequency_mhz"] == 433


def test_offline_unknown_noise_is_info_only():
    r = OfflineAnalyzer.analyze("hello world, nothing numeric here", "chat")
    assert r.action is ActionKind.info_only
    assert r.confidence <= 0.15

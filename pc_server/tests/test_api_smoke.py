"""End-to-end smoke tests against the FastAPI app (no network, no model)."""
import pytest
from fastapi.testclient import TestClient

import netscan as ns_mod
import server
from schemas import ActionKind

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    server._SESSIONS.clear()
    monkeypatch.setattr(ns_mod, "tcp_probe",
                        lambda host, port, timeout=0.4, grab_banner=False:
                        (host.endswith(".5") and port == 80, ""))
    yield
    server._SESSIONS.clear()


def test_health_reports_offline_backend_without_probe():
    r = client.get("/api/v1/health", params={"probe": "false"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "offline" in body["ai_backend"]
    assert "netscan" in body["modes"]


def test_process_ir_returns_transmit_action():
    r = client.post("/api/v1/process", json={
        "mode": "analyze_ir",
        "raw_data": "raw timings: 9000 4500 " + " ".join(["560"] * 64)})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == ActionKind.transmit_ir.value
    assert body["params"]["pattern"][0] == 9000


def test_process_ctf_returns_advisory_answer():
    r = client.post("/api/v1/process", json={
        "mode": "ctf", "raw_data": "dGhpcyBpcyBhIHRlc3Qgc3RyaW5nIGZvciB5b3U="})
    assert r.json()["action"] == ActionKind.ctf_answer.value


def test_parse_dump_bluetooth_inventory():
    r = client.post("/api/v1/parse_dump", json={
        "dump_type": "bluetooth",
        "raw_data": "device: Speaker, 0C:A6:94:11:22:33, rssi -60"})
    body = r.json()
    assert body["action"] == ActionKind.bt_inventory.value
    assert body["params"]["devices"][0]["address"] == "0C:A6:94:11:22:33"


def test_parse_dump_never_emits_shell():
    r = client.post("/api/v1/parse_dump", json={
        "dump_type": "dmesg", "raw_data": "segfault in foo"})
    assert r.json()["action"] == ActionKind.info_only.value


def test_netscan_requires_consent():
    r = client.post("/api/v1/netscan", json={"target": "192.168.50.5", "ports": [80]})
    assert r.status_code == 403


def test_netscan_rejects_public_range():
    r = client.post("/api/v1/netscan", json={
        "target": "8.8.8.8", "ports": [80], "confirm_owner": True})
    assert r.status_code == 403


def test_netscan_authorized_returns_inventory_and_advice():
    r = client.post("/api/v1/netscan", json={
        "target": "192.168.50.0/24", "ports": [22, 80], "confirm_owner": True})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned_hosts"] == 254
    assert body["hosts_with_open_ports"] == 1
    opened = [h for h in body["results"] if h["open_ports"]]
    assert len(opened) == 1 and opened[0]["host"] == "192.168.50.5"
    assert opened[0]["open_ports"] == [80]
    assert body["advice"] is not None
    assert body["advice"]["action"] == ActionKind.net_inventory.value
    assert "authorized inventory only" in body["note"]


def test_session_memory_accumulates():
    payload = {"mode": "chat", "raw_data": "remember this device",
               "session_id": "smoke"}
    client.post("/api/v1/process", json=payload)
    client.post("/api/v1/process", json=payload)
    assert len(server._session("smoke")) == 4          # 2 user + 2 assistant

    r = client.post("/api/v1/session/reset", params={"session_id": "smoke"})
    assert r.json()["status"] == "ok"
    assert len(server._session("smoke")) == 0


def test_knowledge_endpoint():
    r = client.get("/api/v1/knowledge/search", params={"q": "NEC infrared"})
    topics = [x["topic"] for x in r.json()["results"]]
    assert any(t.startswith("ir-") for t in topics)


def test_root_lists_modes():
    assert "netscan" in client.get("/").json()["modes"]

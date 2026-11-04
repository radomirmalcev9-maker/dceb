"""Server tests: the resolve path and the model-output veto.

These run without a Capricorn session, which is the point — the useful half of
the server must work offline on a lab machine.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import irdb
import server
from schemas import ActionKind, AiActionResponse

HAVE_DB = bool(irdb.list_entries())
client = TestClient(server.app)


def test_root_and_health_reachable():
    assert client.get("/").status_code == 200
    health = client.get("/api/v1/health").json()
    assert health["status"] == "ok"
    assert health["version"]


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_resolve_endpoint_returns_a_real_waveform():
    response = client.get("/api/v1/irdb/resolve",
                          params={"q": "epson projector power"})
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "transmit_ir"
    pattern = body["params"]["pattern"]
    assert len(pattern) > 60
    # Carriers in real files sit in 33-40 kHz; the stored value is passed through.
    assert 30000 < body["params"]["frequency"] < 45000
    assert body["confidence"] > 0.5


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_resolve_endpoint_answers_russian():
    body = client.get("/api/v1/irdb/resolve",
                      params={"q": "выключи телевизор самсунг"}).json()
    assert body["action"] == "transmit_ir"
    assert "Samsung" in body["params"]["protocol"]


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_resolve_unknown_channel_asks_instead_of_guessing():
    body = client.get("/api/v1/irdb/resolve",
                      params={"q": "epson projector volume up"}).json()
    assert body["action"] == "transmit_ir"
    assert "Volume_Up" in body["params"]["name"]


def test_resolve_nonsense_is_info_only():
    body = server.resolve_query("make me a sandwich").model_dump()
    assert body["action"] == "info_only"
    assert body["confidence"] == 0.0


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_unknown_button_reports_available_buttons():
    body = server.resolve_query("epson projector flibbertigibbet").model_dump()
    assert body["action"] == "info_only"
    assert body["params"]["buttons"], "button list must be returned"


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_model_pattern_not_in_database_is_downgraded():
    """A hallucinated waveform must never leave the server as transmit_ir."""
    invented = AiActionResponse(
        action=ActionKind.transmit_ir,
        params={"frequency": 38000, "pattern": [111, 222, 333, 444, 555]},
        explanation="trust me")
    vetted = server._vet_model_ir(invented)
    assert vetted.action == ActionKind.info_only
    assert "[verified]" in vetted.explanation


@pytest.mark.skipif(not HAVE_DB, reason="run tools/import_flipper_irdb.py first")
def test_process_with_irdb_only_never_calls_the_model(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the model must not be consulted in irdb_only mode")

    monkeypatch.setattr(server.bridge, "ask", explode)
    response = client.post("/api/v1/process", json={
        "mode": "anything", "raw_data": "epson projector power", "irdb_only": True})
    assert response.status_code == 200
    assert response.json()["action"] == "transmit_ir"

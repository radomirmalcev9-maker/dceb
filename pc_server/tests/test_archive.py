"""Chat archiving: record, finalize, skip-when-busy, debts, retention."""
import json
import time

import pytest
from fastapi.testclient import TestClient

import server
from ai_bridge import OfflineAnalyzer
from archive import ChatArchive, Turn, _safe
from schemas import ActionKind

client = TestClient(server.app)


@pytest.fixture
def arch(tmp_path, monkeypatch):
    a = ChatArchive(root=tmp_path / "archives", retention_days=7, enabled=True)
    a.ensure()
    monkeypatch.setattr(server, "archive", a)
    yield a
    monkeypatch.undo()


def _turn(role, content, mode="chat"):
    return Turn(ts=time.time(), role=role, content=content, mode=mode)


# ----------------------------------------------------------------- basics

def test_safe_session_id_blocks_traversal():
    assert _safe("../../etc/passwd") == "....etcpasswd"
    assert _safe("") == "default"
    assert _safe("a b/c") == "abc"


def test_record_and_finalize_files_the_day(arch):
    arch.open_session("s1", {"who": "test"})
    arch.record_turn("s1", _turn("user", "привет", "companion"))
    arch.record_turn("s1", _turn("assistant", "и тебе привет", "companion"))
    assert arch.finalize("s1") is True

    days = [p for p in arch.root.glob("*.jsonl")]
    assert len(days) == 1
    rows = arch.read_archive(days[0].name)
    assert rows[0]["event"] == "session_start"
    assert any(r.get("role") == "user" and r["content"] == "привет" for r in rows)
    assert rows[-1]["event"] == "session_end"
    assert not (arch.open_dir / "s1.jsonl").exists()


def test_finalize_skips_when_next_turn_in_flight(arch):
    arch.open_session("busy")
    arch.record_turn("busy", _turn("user", "one"))
    busy = lambda sid: False                      # noqa: E731  (next turn waiting)
    assert arch.finalize("busy", busy) is False
    assert "busy" in arch.pending
    assert (arch.open_dir / "busy.jsonl").exists()  # transcript kept safe

    # Turn finished: the debt is paid.
    assert arch.finalize("busy", lambda sid: True) is True
    assert "busy" not in arch.pending


def test_debt_is_paid_on_a_later_turn(arch):
    arch.open_session("older")
    arch.record_turn("older", _turn("user", "first"))
    arch.finalize("older", lambda sid: False)      # skipped -> debt
    assert arch.pending == ["older"]

    arch.open_session("newer")
    arch.record_turn("newer", _turn("user", "second"))
    arch.finalize_async("newer", lambda sid: True)
    for _ in range(50):                            # worker runs in background
        if not arch.pending and not (arch.open_dir / "older.jsonl").exists():
            break
        time.sleep(0.02)
    assert not (arch.open_dir / "older.jsonl").exists()


def test_finalize_reports_miss_without_transcript(arch):
    assert arch.finalize("ghost") is False         # nothing recorded, no crash


def test_pending_list_is_bounded(arch):
    for i in range(80):
        arch._mark_pending(f"s{i}")
    assert len(arch.pending) == 64


# ------------------------------------------------------------------- reads

def test_list_read_delete_and_stats(arch):
    arch.open_session("live")
    arch.record_turn("live", _turn("user", "still typing"))
    arch.finalize("live")
    names = [a["name"] for a in arch.list_archives()]
    assert names
    rows = arch.read_archive(names[0])
    assert rows and rows[-1]["event"] == "session_end"

    stats = arch.stats()
    assert stats["days"] == 1 and stats["enabled"] is True

    assert arch.delete_archive(names[0]) is True
    assert arch.read_archive(names[0]) == []


def test_retention_purges_old_days(arch, monkeypatch):
    old = arch.root / "2000-01-01.jsonl"
    old.write_text("{}\n")
    os_old = time.time() - 30 * 86400
    import os
    os.utime(old, (os_old, os_old))
    assert arch.purge() == 1
    assert not old.exists()


# -------------------------------------------------------------- companion

def test_companion_offline_line_mentions_context():
    r = OfflineAnalyzer.analyze('{"app_label":"YouTube","hour":23}', "companion")
    assert r.action is ActionKind.info_only
    assert isinstance(r.params.get("line"), str) and r.params["line"]
    assert r.explanation == r.params["line"]

    other = OfflineAnalyzer.analyze('{"app_label":"Telegram","hour":9}', "companion")
    assert other.params["line"] != r.params["line"]


def test_companion_endpoint_uses_persona_and_context(arch):
    r = client.post("/api/v1/process", json={
        "mode": "companion",
        "raw_data": "say something, I am in the browser",
        "device_context": {"app_label": "Chrome", "hour": 21},
        "persona": "Capri: дерзкая, тёплая, ревнивая.",
        "session_id": "comp"})
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == ActionKind.info_only.value
    assert body["params"]["line"]


def test_companion_reads_the_real_payload_shape(arch):
    """The server formats device_context with str(dict) — single quotes."""
    r = client.post("/api/v1/process", json={
        "mode": "companion", "raw_data": "line",
        "device_context": {"app_label": "YouTube", "hour": 23},
        "session_id": "ctx"})
    assert r.status_code == 200
    assert "скучаю" in r.json()["params"]["line"]


def test_every_ai_request_leaves_an_archive(arch):
    for i in range(2):
        r = client.post("/api/v1/process", json={
            "mode": "chat", "raw_data": f"question {i}", "session_id": "arch-me"})
        assert r.status_code == 200
    for _ in range(50):
        if arch.stats()["days"] >= 1:
            break
        time.sleep(0.02)
    stats = arch.stats()
    assert stats["days"] == 1
    rows = arch.read_archive(arch.list_archives()[0]["name"])
    assert sum(1 for r in rows if r.get("role") == "user") == 2

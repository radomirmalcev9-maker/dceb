"""Conversation memory, prompt transcript assembly and safety downgrades."""
import server
from ai_bridge import CapricornBridge
from schemas import ActionKind, AiActionResponse, ChatTurn


def setup_function():
    server._SESSIONS.clear()


# ------------------------------------------------------------------- memory

def test_server_remembers_turns():
    server._remember("s1", "[analyze_ir] 9000 4500", "transmit_ir: looks like NEC")
    history = server._history_for([], "s1")
    assert [t.role for t in history] == ["user", "assistant"]
    assert "NEC" in history[1].content


def test_client_history_wins_when_supplied():
    server._remember("s2", "server-side", "server-answer")
    client = [ChatTurn(role="user", content="client-side")]
    history = server._history_for(client, "s2")
    assert len(history) == 1 and history[0].content == "client-side"


def test_session_is_capped():
    for i in range(50):
        server._remember("s3", f"q{i}", f"a{i}")
    assert len(server._session("s3")) == server.MAX_TURNS
    oldest = list(server._session("s3"))[0]
    assert oldest.content == "q40", oldest.content   # last 10 pairs kept


# ---------------------------------------------------------------- transcript

def test_render_transcript_contains_roles_and_payload():
    text = CapricornBridge._render_transcript(
        "SYSTEMY", [ChatTurn(role="user", content="hi"),
                    ChatTurn(role="assistant", content="hello")],
        "latest question")
    assert text.startswith("SYSTEMY")
    assert "USER: hi" in text and "ASSISTANT: hello" in text
    assert text.rstrip().endswith("ASSISTANT (one JSON object only):")
    assert "latest question" in text


# ------------------------------------------------------------------ sanitize

def test_run_shell_is_always_downgraded():
    resp = AiActionResponse(action=ActionKind.run_shell, params={"shell_cmd": "rm -rf /"},
                            explanation="do it", confidence=0.9)
    out = server._sanitize(resp, "console")
    assert out.action is ActionKind.info_only
    assert "shell action downgraded" in out.explanation


def test_hid_downgraded_outside_hid_context():
    resp = AiActionResponse(action=ActionKind.execute_hid,
                            params={"hid_script": "GUI r"}, explanation="go")
    assert server._sanitize(resp, "dmesg").action is ActionKind.info_only
    assert server._sanitize(resp, "hid").action is ActionKind.execute_hid


def test_safe_actions_pass_through():
    resp = AiActionResponse(action=ActionKind.transmit_ir,
                            params={"frequency": 38000, "pattern": [9000, 4500]})
    assert server._sanitize(resp, "analyze_ir").action is ActionKind.transmit_ir

"""Network inventory tests.

The scope rules get the most attention here: a scanner that can be pointed
anywhere is a problem, so "refuses non-private targets" is a feature with a
test, not a footnote.
"""
import ipaddress
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import netscan


# ----------------------------- scope rules --------------------------------- #

def test_public_ranges_are_refused():
    for target in ("8.8.8.8/24", "1.1.1.0/24", "203.0.113.0/24", "0.0.0.0/0"):
        with pytest.raises(netscan.ScopeError):
            netscan.assert_private(target)


def test_private_ranges_are_accepted():
    for target in ("192.168.1.0/24", "10.0.0.0/24", "172.16.5.0/24", "169.254.0.0/24"):
        assert netscan.assert_private(target)


def test_oversized_network_is_refused():
    with pytest.raises(netscan.ScopeError):
        netscan.assert_private("10.0.0.0/8")


def test_ipv6_is_refused():
    with pytest.raises(netscan.ScopeError):
        netscan.assert_private("fd00::/64")


def test_garbage_input_is_refused_not_crashed():
    with pytest.raises(netscan.ScopeError):
        netscan.assert_private("not-an-address")
    with pytest.raises(netscan.ScopeError):
        netscan.assert_private("")


# ----------------------------- discovery ----------------------------------- #

def test_port_probe_detects_a_listening_socket():
    """Start a real listener and make sure the probe finds it."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert netscan.probe_host("127.0.0.1", [port], timeout=0.5) == [port]
    finally:
        server.close()


def test_port_probe_reports_closed_ports_as_empty():
    # Port 1 on loopback is not listening in any sane environment.
    assert netscan.probe_host("127.0.0.1", [1], timeout=0.2) == []


def test_neighbours_parses_ip_neigh_output(monkeypatch):
    sample = (
        "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
        "192.168.1.44 dev eth0 lladdr 11:22:33:44:55:66 STALE\n"
        "192.168.1.9 dev eth0 FAILED\n"
    )
    monkeypatch.setattr(netscan.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": sample})())
    found = netscan.neighbours()
    assert found["192.168.1.1"] == "aa:bb:cc:dd:ee:ff"
    assert found["192.168.1.44"] == "11:22:33:44:55:66"
    assert "192.168.1.9" not in found          # FAILED entries carry no MAC


def test_neighbours_parses_bsd_arp_output(monkeypatch):
    sample = "? (192.168.1.7) at 0:1b:63:84:45:e6 on en0 ifscope [ethernet]\n"

    def fake_run(command, **kwargs):
        if command[0] == "ip":
            raise FileNotFoundError
        return type("R", (), {"stdout": sample})()

    monkeypatch.setattr(netscan.subprocess, "run", fake_run)
    assert netscan.neighbours() == {}


def test_host_summary_is_readable():
    host = netscan.Host(ip="192.168.1.44", mac="aa:bb:cc:dd:ee:ff",
                        name="epson-livingroom", open_ports=[80, 9100])
    text = host.summary()
    assert "192.168.1.44" in text
    assert "epson-livingroom" in text
    assert "ipp" not in text          # 631 is ipp, 9100 is jetdirect
    assert "jetdirect" in text


def test_describe_handles_empty_result():
    assert "No devices" in netscan.describe([])

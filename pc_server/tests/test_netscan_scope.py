"""Scope guards and behaviour of the authorized network scanner."""
import pytest
from pydantic import ValidationError

import netscan as ns_mod
from netscan import ScopeError, expand_target, scan
from schemas import NetScanRequest


def _fake_probe(host, port, timeout=0.4, grab_banner=False):
    """Only .5 answers, and only on port 80."""
    return (host.endswith(".5") and port == 80,
            "HTTP/1.1 200 OK" if (port == 80 and grab_banner) else "")


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    monkeypatch.setattr(ns_mod, "tcp_probe", _fake_probe)
    monkeypatch.setattr(ns_mod, "ping_host", lambda host, timeout_ms=1000: True)


# --------------------------------------------------------------- scope guards

def test_scan_requires_owner_confirmation():
    with pytest.raises(ScopeError):
        scan("192.168.1.0/24", confirm_owner=False)


def test_scan_rejects_public_target_without_flag():
    with pytest.raises(ScopeError):
        scan("8.8.8.8", confirm_owner=True)


def test_scan_allows_public_target_with_explicit_flag():
    result = scan("8.8.8.8", confirm_owner=True, allow_public=True, ports=[80])
    assert result["scanned_hosts"] == 1
    assert result["results"][0]["host"] == "8.8.8.8"


def test_scan_rejects_empty_port_list():
    with pytest.raises(ScopeError):
        scan("192.168.1.0/24", confirm_owner=True, ports=[])


def test_expand_target_caps_host_count():
    hosts = expand_target("10.0.0.0/24", max_hosts=10, allow_public=False)
    assert len(hosts) == 10


def test_expand_target_rejects_unresolvable_host():
    with pytest.raises(ScopeError):
        expand_target("no-such-host.invalid.example", max_hosts=5)


# ------------------------------------------------------------------ behaviour

def test_private_scan_reports_only_responding_host():
    result = scan("192.168.50.0/24", confirm_owner=True, ports=[22, 80])
    assert result["scanned_hosts"] == 254
    opened = [h for h in result["results"] if h["open_ports"]]
    assert len(opened) == 1
    assert opened[0]["host"] == "192.168.50.5"
    assert opened[0]["open_ports"] == [80]
    assert result["note"].startswith("authorized inventory")


def test_banner_grabbing_is_opt_in():
    with_banners = scan("192.168.50.5", confirm_owner=True, ports=[80], grab_banner=True)
    assert with_banners["results"][0]["banners"]["80"].startswith("HTTP")
    without = scan("192.168.50.5", confirm_owner=True, ports=[80], grab_banner=False)
    assert without["results"][0]["banners"] == {}


def test_single_host_target():
    result = scan("192.168.50.5", confirm_owner=True, ports=[80])
    assert result["scanned_hosts"] == 1
    assert result["hosts_with_open_ports"] == 1


# ------------------------------------------------------------------- schemas

def test_netscan_request_defaults_and_limits():
    req = NetScanRequest(target="192.168.1.0/24", confirm_owner=True)
    assert 445 in req.ports and req.allow_public is False

    with pytest.raises(ValidationError):
        NetScanRequest(target="192.168.1.0/24", ports=list(range(1, 500)))

    bad = NetScanRequest.model_construct(target="x", ports=[0, 99999])
    assert bad.ports == [0, 99999]      # model_construct skips validators


def test_netscan_request_filters_invalid_ports():
    req = NetScanRequest(target="192.168.1.0/24", confirm_owner=True,
                         ports=[0, 80, 70000, 443])
    assert req.ports == [80, 443]

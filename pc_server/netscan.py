"""Authorized local-network inventory scanner.

Scope-first design, on purpose:

* The caller must pass ``confirm_owner=True`` — a typed, explicit statement
  that the target belongs to the operator or is covered by written permission.
* Targets outside private/loopback/link-local space additionally require
  ``allow_public=True`` and are logged loudly.
* Only ordinary TCP connect() probes and an optional ICMP ping are used. There
  is no FIN/XMAS/NULL scanning, no fragmentation, no decoys, no timing or
  IDS evasion, no exploitation, no credential testing, no lateral movement and
  no data collection beyond a short service banner when asked for.
* Host counts are capped and concurrency is bounded so a lab LAN stays usable.

The output is plain asset inventory: which hosts answered, which of a small
port list are open, and what banner they offered.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

log = logging.getLogger("dceb.netscan")

# Ranges an operator normally owns: RFC1918 + loopback + link-local.
PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
)

DEFAULT_PORTS = (22, 80, 443, 445, 3389, 8000, 8080, 9100)

# Ports where a tiny request is needed before the service greets us.
HTTP_PORTS = {80, 8000, 8080, 8008, 8081, 8888}


class ScopeError(ValueError):
    """Raised when a scan request is not clearly authorized."""


@dataclass
class HostResult:
    host: str
    alive: bool = False
    open_ports: List[int] = field(default_factory=list)
    banners: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"host": self.host, "alive": self.alive,
                "open_ports": self.open_ports, "banners": self.banners,
                "error": self.error}


def is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in PRIVATE_NETS)


def _resolve(target: str) -> List[str]:
    """Expand a target into concrete host strings (IP or CIDR or hostname)."""
    target = target.strip()
    if not target:
        raise ScopeError("empty target")

    try:
        net = ipaddress.ip_network(target, strict=False)
    except ValueError:
        net = None

    if net is not None:
        if net.version == 6:
            raise ScopeError("IPv6 targets are not supported by this scanner")
        if not is_private(net.network_address):
            return []          # caller must pass allow_public for these
        hosts = [str(h) for h in net.hosts()]
        return hosts

    # Otherwise: a bare address or a hostname.
    try:
        ip = ipaddress.ip_address(target)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(target))
        except socket.gaierror as exc:
            raise ScopeError(f"cannot resolve {target!r}: {exc}") from exc
    return [str(ip)]


def expand_target(target: str, max_hosts: int = 256,
                  allow_public: bool = False) -> List[str]:
    """Resolve and validate a target, enforcing the private-range guard.

    Returns a capped host list. Raises :class:`ScopeError` when the target is
    unresolvable or public without ``allow_public``.
    """
    hosts = _resolve(target)
    if not hosts:
        # _resolve() returned [] only for a public CIDR.
        if not allow_public:
            raise ScopeError(
                f"{target} is public address space: set allow_public=True only "
                "with written authorization, and keep the scope minimal")
        net = ipaddress.ip_network(target, strict=False)
        hosts = [str(h) for h in net.hosts()]
        log.warning("PUBLIC RANGE SCAN authorized by operator: %s", net)

    if len(hosts) > max_hosts:
        log.info("capping target %s from %d to %d hosts", target, len(hosts), max_hosts)
        hosts = hosts[:max_hosts]
    return hosts


def ping_host(host: str, timeout_ms: int = 1000) -> bool:
    """Best-effort ICMP reachability. Returns False when ping is unavailable."""
    try:
        rc = subprocess.run(
            ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        return rc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _read_banner(sock: socket.socket, port: int, bufsize: int = 256) -> str:
    try:
        if port in HTTP_PORTS:
            sock.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
        sock.settimeout(0.6)
        data = sock.recv(bufsize)
        return data.decode("utf-8", errors="replace").strip().replace("\r\n", " | ")[:200]
    except OSError:
        return ""


def tcp_probe(host: str, port: int, timeout: float = 0.4,
              grab_banner: bool = False) -> tuple:
    """One ordinary connect() probe. Returns (is_open, banner)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            banner = _read_banner(sock, port) if grab_banner else ""
            return True, banner
    except (ConnectionRefusedError, socket.timeout, TimeoutError):
        return False, ""
    except OSError:
        return False, ""


def scan_host(host: str, ports: Sequence[int] = DEFAULT_PORTS, timeout: float = 0.4,
              grab_banner: bool = False) -> HostResult:
    open_ports: List[int] = []
    banners: Dict[str, str] = {}
    for port in ports:
        is_open, banner = tcp_probe(host, port, timeout=timeout, grab_banner=grab_banner)
        if is_open:
            open_ports.append(port)
            if banner:
                banners[str(port)] = banner
    return HostResult(host=host, alive=True, open_ports=open_ports, banners=banners)


def scan(target: str, ports: Sequence[int] = DEFAULT_PORTS, confirm_owner: bool = False,
         allow_public: bool = False, max_hosts: int = 256, timeout: float = 0.4,
         grab_banner: bool = False, do_ping: bool = False,
         max_workers: int = 32) -> dict:
    """Authorized inventory scan. Raises :class:`ScopeError` without consent."""
    if not confirm_owner:
        raise ScopeError(
            "confirm_owner must be True: only scan networks you own or have "
            "written permission to test")
    if not ports:
        raise ScopeError("port list is empty")

    ports = [int(p) for p in ports if 0 < int(p) < 65536]
    hosts = expand_target(target, max_hosts=max_hosts, allow_public=allow_public)

    alive: List[str] = []
    if do_ping:
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(hosts)))) as pool:
            for host, up in zip(hosts, pool.map(ping_host, hosts)):
                if up:
                    alive.append(host)
        log.info("ping sweep: %d/%d hosts answered", len(alive), len(hosts))
    else:
        alive = list(hosts)

    results: List[HostResult] = []
    if alive:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(alive))) as pool:
            for res in pool.map(
                    lambda h: scan_host(h, ports, timeout=timeout, grab_banner=grab_banner),
                    alive):
                results.append(res)

    with_open = [r for r in results if r.open_ports]
    return {
        "target": target,
        "scanned_hosts": len(alive),
        "hosts_with_open_ports": len(with_open),
        "open_port_total": sum(len(r.open_ports) for r in results),
        "ports_probed": list(ports),
        "banner_grabbing": bool(grab_banner),
        "ping_sweep": bool(do_ping),
        "results": [r.as_dict() for r in results],
        "note": "authorized inventory only — connect() probes, no evasion, no exploitation",
    }

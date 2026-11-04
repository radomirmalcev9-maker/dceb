"""Network inventory for the owner's own LAN.

Answers the question Capri actually needs answered before it can be useful:
*what is on my network, and which of those things is the projector?* Without
this, "turn on the projector" is a guess. With it, the phone knows the Epson
sits at 192.168.1.44 and whether it is already awake.

What this module deliberately does **not** do:

* no exploitation, no brute force, no credential capture
* no traffic interception, ARP spoofing or MITM
* no stealth — a host sweep is deliberately loud and fast, because it is
  meant to be run on your own subnet by you

Scope is enforced: only private/link-local address ranges, and only the local
interface's own subnet by default. Pointing this at the internet or at someone
else's range is refused rather than quietly performed.

Two entry points, two contracts:

* :func:`scan` — returns ``List[Host]``. Discovery-first (neighbour table,
  TCP-connect probes, reverse DNS). Used by ``GET /api/v1/network/scan``.
  Private ranges only, ``allow_public=True`` is the sole way past it.
* :func:`scan_report` — returns a JSON-ready dict and **requires**
  ``confirm_owner=True``. Adds optional banner grabbing and an ICMP sweep,
  and is what the phone's "Авторизованное сканирование" card calls
  (``POST /api/v1/netscan``).

Discovery order, cheapest first:

1. ``ip neigh`` / ``arp -an`` — the kernel already knows who answered recently
2. concurrent TCP-connect probes on the default port list — no raw sockets, so
   no root needed, and a completed connect is proof the host is up
3. reverse DNS for names, so the report reads like a home, not a hex dump
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

log = logging.getLogger("dceb.netscan")

DEFAULT_PORTS = (80, 443, 22, 8008, 8080, 631, 9100, 554, 445, 3389)
PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)

# Backwards-compatible alias (older code and tests import PRIVATE_NETS).
PRIVATE_NETS = PRIVATE_V4

# Ports whose being open says something about what the box is. Kept short on
# purpose: a label that is usually wrong is worse than no label.
PORT_HINTS = {
    22: "ssh",
    80: "http",
    443: "https",
    445: "smb",
    554: "rtsp (camera/projector stream)",
    631: "ipp (printer)",
    3389: "rdp",
    8008: "google-cast",
    8080: "http-alt",
    9100: "jetdirect (printer)",
}

# Ports where a tiny request is needed before the service greets us.
HTTP_PORTS = {80, 8000, 8008, 8080, 8081, 8888}


class ScopeError(ValueError):
    """Raised when a target is outside the authorized scope."""


@dataclass
class Host:
    ip: str
    mac: Optional[str] = None
    name: Optional[str] = None
    open_ports: List[int] = field(default_factory=list)
    banners: dict = field(default_factory=dict)

    def hints(self) -> List[str]:
        return [PORT_HINTS[port] for port in self.open_ports if port in PORT_HINTS]

    def summary(self) -> str:
        bits = [self.ip]
        if self.name:
            bits.append(f"({self.name})")
        if self.mac:
            bits.append(f"[{self.mac}]")
        if self.open_ports:
            bits.append("ports: " + ",".join(str(p) for p in self.open_ports))
        if self.hints():
            bits.append("· " + ", ".join(self.hints()))
        return " ".join(bits)

    def as_dict(self) -> dict:
        return {"host": self.ip, "alive": True, "mac": self.mac, "name": self.name,
                "open_ports": list(self.open_ports), "banners": dict(self.banners),
                "hints": self.hints(), "error": None}


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #

def is_private(ip) -> bool:
    return any(ip in net for net in PRIVATE_V4)


def local_subnet() -> Optional[ipaddress.IPv4Network]:
    """The /24 the machine is on, derived from the default route.

    ``ip route get 1.1.1.1`` prints the source address the kernel would use,
    which is the honest answer on a multi-homed box. Falls back to the hostname
    lookup, then gives up.
    """
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                             capture_output=True, text=True, timeout=5).stdout
        match = re.search(r"src\s+(\d+\.\d+\.\d+\.\d+)", out)
        if match:
            return ipaddress.ip_network(f"{match.group(1)}/24", strict=False)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        address = socket.gethostbyname(socket.gethostname())
        network = ipaddress.ip_network(f"{address}/24", strict=False)
        if any(network.subnet_of(private) for private in PRIVATE_V4
               if network.version == private.version):
            return network
    except OSError:
        pass
    return None


def assert_private(target: str) -> ipaddress.IPv4Network:
    """Validate that ``target`` is a private IPv4 network this tool may touch."""
    try:
        network = ipaddress.ip_network(target, strict=False)
    except ValueError as exc:
        raise ScopeError(f"not a network: {target!r}") from exc
    if network.version != 4:
        raise ScopeError("only IPv4 ranges are supported")
    if not any(network.subnet_of(private) for private in PRIVATE_V4):
        raise ScopeError(
            f"{network} is not private address space; this tool only inspects "
            "the owner's own LAN")
    if network.prefixlen < 22:
        raise ScopeError(f"{network} is too large for a host sweep (max /22)")
    return network


def expand_target(target: str, max_hosts: int = 256,
                  allow_public: bool = False) -> List[str]:
    """Resolve a target into concrete addresses, enforcing the private guard."""
    target = (target or "").strip()
    if not target:
        raise ScopeError("empty target")
    try:
        net = ipaddress.ip_network(target, strict=False)
        if net.version == 4:
            hosts = [str(h) for h in net.hosts()]
            if not any(net.subnet_of(p) for p in PRIVATE_V4):
                if not allow_public:
                    raise ScopeError(
                        f"{net} is public address space: allow_public=True is "
                        "required, and only with written authorization")
                log.warning("PUBLIC RANGE SCAN authorized by operator: %s", net)
            if len(hosts) > max_hosts:
                hosts = hosts[:max_hosts]
            return hosts
    except ValueError:
        pass

    # A bare address or a hostname.
    try:
        ip = ipaddress.ip_address(target)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(target))
        except socket.gaierror as exc:
            raise ScopeError(f"cannot resolve {target!r}: {exc}") from exc
    if not is_private(ip) and not allow_public:
        raise ScopeError(f"{ip} is public address space: allow_public=True required")
    return [str(ip)]


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

_NEIGH_LINE = re.compile(
    r"^(?P<ip>\d+\.\d+\.\d+\.\d+)\s+dev\s+\S+\s+(?:lladdr\s+(?P<mac>[0-9a-f:]{17}))?",
    re.MULTILINE)
_ARP_LINE = re.compile(
    r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\)\s+at\s+(?P<mac>[0-9a-f:]{17})", re.IGNORECASE)


def neighbours() -> dict[str, str]:
    """IP -> MAC from the kernel's own neighbour table. No packets sent."""
    found: dict[str, str] = {}
    for command in (["ip", "neigh"], ["arp", "-an"]):
        try:
            output = subprocess.run(command, capture_output=True, text=True,
                                    timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for pattern in (_NEIGH_LINE, _ARP_LINE):
            for match in pattern.finditer(output):
                mac = match.groupdict().get("mac")
                if mac:
                    found[match.group("ip")] = mac.lower()
    return found


def ping_host(host: str, timeout_ms: int = 1000) -> bool:
    """Best-effort ICMP reachability; False when ping is unavailable."""
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
    """One ordinary connect() probe. Returns ``(is_open, banner)``."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            banner = _read_banner(sock, port) if grab_banner else ""
            return True, banner
    except (ConnectionRefusedError, socket.timeout, TimeoutError):
        return False, ""
    except OSError:
        return False, ""


def _port_open(ip: str, port: int, timeout: float) -> bool:
    return tcp_probe(ip, port, timeout=timeout)[0]


def probe_host(ip: str, ports: Iterable[int], timeout: float = 0.35) -> List[int]:
    """Open TCP ports on one host. A completed connect proves it is alive."""
    return [port for port in ports if _port_open(ip, port, timeout)]


def scan_host(host: str, ports: Iterable[int] = DEFAULT_PORTS, timeout: float = 0.4,
              grab_banner: bool = False) -> Host:
    opened: List[int] = []
    banners: dict = {}
    for port in ports:
        is_open, banner = tcp_probe(host, port, timeout=timeout, grab_banner=grab_banner)
        if is_open:
            opened.append(port)
            if banner:
                banners[str(port)] = banner
    return Host(ip=host, open_ports=opened, banners=banners)


def _reverse_name(ip: str) -> Optional[str]:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror):
        return None


def scan(target: Optional[str] = None, ports: Iterable[int] = DEFAULT_PORTS,
         workers: int = 64, include_names: bool = True, timeout: float = 0.35,
         confirm_owner: Optional[bool] = None, allow_public: bool = False,
         max_hosts: int = 1024, grab_banner: bool = False,
         do_ping: bool = False) -> List[Host]:
    """Sweep ``target`` (default: the local subnet) and describe what answers.

    Returns only hosts that actually responded — neighbours with a MAC, or
    hosts with at least one open probed port. Silent addresses are not
    listed, because "no answer" on a filtered network means nothing.

    ``confirm_owner=False`` refuses outright; ``None`` means "not asked", which
    is accepted for private ranges (the private-range guard is itself the
    consent check the operator configured).
    """
    if confirm_owner is False:
        raise ScopeError("confirm_owner=False: only scan networks you own")

    if target:
        network = None if allow_public else assert_private(target)
        addresses = expand_target(target, max_hosts=max_hosts, allow_public=allow_public)
    else:
        network = local_subnet()
        if network is None:
            return []
        if not allow_public:
            assert_private(str(network))
        addresses = [str(ip) for ip in network.hosts()][:max_hosts]

    if do_ping:
        alive: List[str] = []
        with ThreadPoolExecutor(max_workers=min(16, max(1, len(addresses)))) as pool:
            for address, up in zip(addresses, pool.map(ping_host, addresses)):
                if up:
                    alive.append(address)
        log.info("ping sweep: %d/%d answered", len(alive), len(addresses))
        addresses = alive

    known = neighbours()
    hosts: dict[str, Host] = {}
    for address in addresses:
        if address in known:
            hosts[address] = Host(ip=address, mac=known[address])

    port_list = tuple(ports)

    def worker(address: str) -> tuple[str, List[int], dict]:
        opened: List[int] = []
        banners: dict = {}
        for port in port_list:
            is_open, banner = tcp_probe(address, port, timeout=timeout,
                                        grab_banner=grab_banner)
            if is_open:
                opened.append(port)
                if banner:
                    banners[str(port)] = banner
        return address, opened, banners

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for address, open_ports, banners in pool.map(worker, addresses):
            if open_ports:
                host = hosts.get(address) or Host(ip=address)
                host.open_ports = open_ports
                host.banners = banners
                hosts[address] = host

    if include_names:
        with ThreadPoolExecutor(max_workers=32) as pool:
            for host, name in zip(list(hosts.values()),
                                  pool.map(_reverse_name, list(hosts.keys()))):
                host.name = name

    def sort_key(host: Host) -> tuple:
        return tuple(int(part) for part in host.ip.split("."))

    return sorted(hosts.values(), key=sort_key)


def scan_report(target: Optional[str] = None, ports: Iterable[int] = DEFAULT_PORTS,
                confirm_owner: bool = False, allow_public: bool = False,
                max_hosts: int = 256, timeout: float = 0.4,
                grab_banner: bool = False, do_ping: bool = False,
                max_workers: int = 32, include_names: bool = False) -> dict:
    """JSON-ready inventory behind an explicit ownership confirmation.

    This is the contract used by ``POST /api/v1/netscan``: no consent, no scan.
    """
    if not confirm_owner:
        raise ScopeError(
            "confirm_owner must be True: only scan networks you own or have "
            "written permission to test")
    port_list = [int(p) for p in ports if 0 < int(p) < 65536]
    if not port_list:
        raise ScopeError("port list is empty")

    if target:
        addresses = expand_target(target, max_hosts=max_hosts, allow_public=allow_public)
    else:
        net = local_subnet()
        addresses = [str(ip) for ip in net.hosts()][:max_hosts] if net else []

    hosts = scan(target=target, ports=port_list, workers=max_workers,
                 include_names=include_names, timeout=timeout,
                 confirm_owner=True, allow_public=allow_public,
                 max_hosts=max_hosts, grab_banner=grab_banner, do_ping=do_ping)
    return {
        "target": target or "local-subnet",
        "scanned_hosts": len(addresses),
        "hosts_with_open_ports": len(hosts),
        "open_port_total": sum(len(h.open_ports) for h in hosts),
        "ports_probed": port_list,
        "banner_grabbing": bool(grab_banner),
        "ping_sweep": bool(do_ping),
        "results": [h.as_dict() for h in hosts],
        "note": "authorized inventory only — connect() probes, no evasion, no exploitation",
    }


def describe(hosts: List[Host]) -> str:
    """One human-readable report, ready to drop into a chat bubble."""
    if not hosts:
        return "No devices answered on this network."
    lines = [f"{len(hosts)} device(s) responded:"]
    lines += [f"  {host.summary()}" for host in hosts]
    return "\n".join(lines)

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

Discovery order, cheapest first:

1. ``ip neigh`` / ``arp -an`` — the kernel already knows who answered recently
2. concurrent TCP-connect probes on ``:80,443,22,8008,8080,631,9100,554`` —
   no raw sockets, so no root needed, and a connect that completes is proof
   the host is up
3. reverse DNS for names, so the report reads like a home, not a hex dump

An ICMP sweep is available only when ``fping`` exists, since raw ICMP needs
root and quietly failing is worse than not offering it.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

DEFAULT_PORTS = (80, 443, 22, 8008, 8080, 631, 9100, 554, 445, 3389)
PRIVATE_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)

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


class ScopeError(ValueError):
    """Raised when a target is outside private address space."""


@dataclass
class Host:
    ip: str
    mac: Optional[str] = None
    name: Optional[str] = None
    open_ports: List[int] = field(default_factory=list)

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


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #

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


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def probe_host(ip: str, ports: Iterable[int], timeout: float = 0.35) -> List[int]:
    """Open TCP ports on one host. A completed connect proves it is alive."""
    open_ports = []
    for port in ports:
        if _port_open(ip, port, timeout):
            open_ports.append(port)
    return open_ports


def _reverse_name(ip: str) -> Optional[str]:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror):
        return None


def scan(target: Optional[str] = None, ports: Iterable[int] = DEFAULT_PORTS,
         workers: int = 64, include_names: bool = True) -> List[Host]:
    """Sweep ``target`` (default: the local subnet) and describe what answers.

    Returns only hosts that actually responded — neighbours with a MAC, or
    hosts with at least one open probed port. Silent addresses are not listed,
    because "no answer" on a filtered network means nothing.
    """
    network = assert_private(target) if target else local_subnet()
    if network is None:
        return []
    assert_private(str(network))

    known = neighbours()
    addresses = [str(ip) for ip in network.hosts()]
    hosts: dict[str, Host] = {}

    for address in addresses:
        if address in known:
            hosts[address] = Host(ip=address, mac=known[address])

    port_list = tuple(ports)

    def worker(address: str) -> tuple[str, List[int]]:
        return address, probe_host(address, port_list)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for address, open_ports in pool.map(worker, addresses):
            if open_ports:
                host = hosts.get(address) or Host(ip=address)
                host.open_ports = open_ports
                hosts[address] = host

    if include_names:
        with ThreadPoolExecutor(max_workers=32) as pool:
            for host, name in zip(hosts.values(), pool.map(_reverse_name, hosts.keys())):
                host.name = name

    def sort_key(host: Host) -> tuple:
        return tuple(int(part) for part in host.ip.split("."))

    return sorted(hosts.values(), key=sort_key)


def describe(hosts: List[Host]) -> str:
    """One human-readable report, ready to drop into a chat bubble."""
    if not hosts:
        return "No devices answered on this network."
    lines = [f"{len(hosts)} device(s) responded:"]
    lines += [f"  {host.summary()}" for host in hosts]
    return "\n".join(lines)

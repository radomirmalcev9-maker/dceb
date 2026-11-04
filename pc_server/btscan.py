"""Bluetooth LE inventory — what is near me, and is it mine?

The useful question about Bluetooth in a home lab is not "how do I get into
someone's speaker", it is "which of these forty advertisements is my lamp, and
what service does it expose". That is a discovery problem, and discovery is
what this module does.

Two backends, picked at runtime:

* ``bleak`` (optional, ``pip install bleak``) — async BLE scan on Linux/macOS/
  Windows, no root needed for scanning
* ``bluetoothctl`` (BlueZ CLI) — fallback on Linux when bleak is absent

What it returns is only what the device itself broadcasts plus an optional
GATT service enumeration **on devices the caller names**. GATT reads are not
scanned blind: they are opt-in per address, because touching a stranger's
device is rude and, on some hardware, disruptive.

Not implemented, on purpose: pairing bypass, key extraction, impersonation,
packet injection, jamming. Those are attacks on other people's radios, and no
amount of "it's my own device" framing makes a jammer safe to ship.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# Standard 16-bit service UUIDs worth naming — a home lab is mostly these.
KNOWN_SERVICES = {
    "0000180a-0000-1000-8000-00805f9b34fb": "Device Information",
    "0000180f-0000-1000-8000-00805f9b34fb": "Battery",
    "0000180d-0000-1000-8000-00805f9b34fb": "Heart Rate",
    "00001809-0000-1000-8000-00805f9b34fb": "Health Thermometer",
    "0000fe07-0000-1000-8000-00805f9b34fb": "Xiaomi/MiBeacon-ish",
    "0000fee0-0000-1000-8000-00805f9b34fb": "Xiaomi Mi Band",
    "0000fff0-0000-1000-8000-00805f9b34fb": "Vendor specific",
    "0000fff1-0000-1000-8000-00805f9b34fb": "Vendor specific",
    "0000fff2-0000-1000-8000-00805f9b34fb": "Vendor specific",
}


class BluetoothUnavailable(RuntimeError):
    """No usable BLE backend on this machine."""


class ScopeError(ValueError):
    """An address was requested that is not a MAC the caller owns."""


@dataclass
class Device:
    address: str
    name: Optional[str] = None
    rssi: Optional[int] = None
    services: List[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [self.address]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.rssi is not None:
            bits.append(f"{self.rssi} dBm")
        if self.services:
            named = [KNOWN_SERVICES.get(s.lower(), s) for s in self.services]
            bits.append("services: " + ", ".join(named[:6]))
        return " ".join(bits)


def backends() -> List[str]:
    available = []
    try:
        import bleak  # noqa: F401
        available.append("bleak")
    except ImportError:
        pass
    if shutil.which("bluetoothctl"):
        available.append("bluetoothctl")
    return available


def _require_backend() -> str:
    found = backends()
    if not found:
        raise BluetoothUnavailable(
            "no BLE backend: install bleak (pip install bleak) or bluez "
            "(bluetoothctl) on this machine")
    return found[0]


def validate_address(address: str) -> str:
    """A MAC address, or a refusal. Also rejects the all-zero placeholder."""
    if not ADDRESS_RE.match(address or ""):
        raise ScopeError(f"not a Bluetooth address: {address!r}")
    if address.upper() == "00:00:00:00:00:00":
        raise ScopeError("all-zero address is not a device")
    return address.upper()


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #

async def _scan_bleak(timeout: float) -> List[Device]:
    from bleak import BleakScanner  # type: ignore

    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices: List[Device] = []
    for address, (device, adv) in discovered.items():
        devices.append(Device(
            address=address.upper(),
            name=device.name or adv.local_name,
            rssi=adv.rssi,
            services=list(adv.service_uuids or [])))
    return devices


def _scan_bluez(timeout: float) -> List[Device]:
    process = subprocess.Popen(
        ["bluetoothctl", "--timeout", str(int(timeout)), "scan", "on"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    devices: dict[str, Device] = {}
    line_re = re.compile(r"Device\s+([0-9A-Fa-f:]{17})\s*(.*)")
    try:
        for line in process.stdout or []:
            match = line_re.search(line)
            if not match:
                continue
            address, name = match.group(1).upper(), match.group(2).strip()
            # "Device AA:BB:... Name: Foo" and "Device AA:BB:... Foo" both occur
            name = re.sub(r"^Name:\s*", "", name.split("RSSI:")[0]).strip() or None
            device = devices.get(address) or Device(address=address)
            if name and name != address:
                device.name = name
            devices[address] = device
    finally:
        process.wait(timeout=5)
    return list(devices.values())


def scan(timeout: float = 6.0) -> List[Device]:
    """Discover advertising BLE devices for ``timeout`` seconds.

    bleak failing at import time means different things on different systems:
    no D-Bus at all (a container), no BlueZ service, or an adapter that is
    powered off. All three surface as ``BluetoothUnavailable`` with the
    underlying reason, because "FileNotFoundError" tells the owner nothing.
    """
    backend = _require_backend()
    try:
        if backend == "bleak":
            return asyncio.run(_scan_bleak(timeout))
        return _scan_bluez(timeout)
    except FileNotFoundError as exc:
        raise BluetoothUnavailable(
            "Bluetooth stack not reachable: no D-Bus/BlueZ on this machine "
            f"({exc.filename or exc}). On Linux install bluez and start "
            "bluetoothd; on Windows/macOS make sure the adapter is on."
        ) from exc
    except PermissionError as exc:
        raise BluetoothUnavailable(
            "Bluetooth adapter present but access was denied. Add the user to "
            "the 'bluetooth' group or run with the adapter unlocked."
        ) from exc
    except OSError as exc:
        raise BluetoothUnavailable(f"Bluetooth scan failed: {exc}") from exc


# --------------------------------------------------------------------------- #
# GATT (single, explicitly named device)
# --------------------------------------------------------------------------- #

async def _gatt_bleak(address: str) -> List[str]:
    from bleak import BleakClient  # type: ignore

    async with BleakClient(address, timeout=10.0) as client:
        return [str(service.uuid) for service in client.services]


def gatt_services(address: str) -> List[str]:
    """Enumerate services on one device the caller names.

    This connects. It is deliberately not part of ``scan()``: discovery is
    passive and safe, connecting is a choice made per device.
    """
    address = validate_address(address)
    backend = _require_backend()
    if backend != "bleak":
        raise BluetoothUnavailable(
            "GATT enumeration needs bleak; bluetoothctl is scan-only here")
    return asyncio.run(_gatt_bleak(address))


def describe(devices: List[Device]) -> str:
    if not devices:
        return "No Bluetooth LE devices advertising right now."
    devices = sorted(devices, key=lambda d: (d.rssi is None, -(d.rssi or 0)))
    lines = [f"{len(devices)} device(s) advertising:"]
    lines += [f"  {device.summary()}" for device in devices]
    return "\n".join(lines)

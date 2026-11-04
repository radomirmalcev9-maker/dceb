"""Bluetooth inventory tests. No radio required — parsing and scope only."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import btscan


def test_address_validation_accepts_real_macs():
    assert btscan.validate_address("aa:bb:cc:dd:ee:ff") == "AA:BB:CC:DD:EE:FF"
    assert btscan.validate_address("00:1B:63:84:45:E6") == "00:1B:63:84:45:E6"


def test_address_validation_rejects_junk():
    for bad in ("", "not-a-mac", "aa:bb:cc:dd:ee", "aa-bb-cc-dd-ee-ff",
                "00:00:00:00:00:00", None):
        with pytest.raises(btscan.ScopeError):
            btscan.validate_address(bad)


def test_gatt_requires_a_valid_address_before_touching_radio():
    """Bad input must fail on validation, never reach the backend."""
    with pytest.raises(btscan.ScopeError):
        btscan.gatt_services("192.168.1.1")


def test_device_summary_names_known_services():
    device = btscan.Device(
        address="AA:BB:CC:DD:EE:FF", name="Mi Band 7", rssi=-64,
        services=["0000180d-0000-1000-8000-00805f9b34fb"])
    text = device.summary()
    assert "Mi Band 7" in text
    assert "Heart Rate" in text          # UUID turned into something readable
    assert "-64 dBm" in text


def test_describe_sorts_by_signal_strength():
    devices = [
        btscan.Device(address="AA:BB:CC:DD:EE:01", name="far", rssi=-91),
        btscan.Device(address="AA:BB:CC:DD:EE:02", name="near", rssi=-42),
        btscan.Device(address="AA:BB:CC:DD:EE:03", name="unknown"),
    ]
    lines = btscan.describe(devices).splitlines()
    assert "near" in lines[1]
    assert "far" in lines[2]


def test_describe_handles_empty_scan():
    assert "No Bluetooth" in btscan.describe([])


def test_bluez_output_parsing(monkeypatch):
    """The bluetoothctl line format has changed twice; parse the current one."""
    sample = (
        "[NEW] Device AA:BB:CC:DD:EE:01 My Speaker\n"
        "[NEW] Device AA:BB:CC:DD:EE:02 Name: Mi Band\n"
        "[NEW] Device AA:BB:CC:DD:EE:03\n"
    )

    class FakeProcess:
        stdout = sample.splitlines()

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(btscan.subprocess, "Popen",
                        lambda *a, **k: FakeProcess())
    devices = {d.address: d for d in btscan._scan_bluez(1)}
    assert devices["AA:BB:CC:DD:EE:01"].name == "My Speaker"
    assert devices["AA:BB:CC:DD:EE:02"].name == "Mi Band"
    assert devices["AA:BB:CC:DD:EE:03"].name is None


def test_missing_backend_raises_a_useful_error(monkeypatch):
    monkeypatch.setattr(btscan, "backends", lambda: [])
    with pytest.raises(btscan.BluetoothUnavailable) as error:
        btscan.scan(0.1)
    assert "bleak" in str(error.value)

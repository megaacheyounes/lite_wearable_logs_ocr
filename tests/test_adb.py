from __future__ import annotations

import pytest

from wearable_logs_ocr.adb import AdbClient, DeviceEntry
from wearable_logs_ocr.errors import AdbError


def client_with_devices(devices: list[DeviceEntry]) -> AdbClient:
    client = object.__new__(AdbClient)
    client.serial = None
    client.list_devices = lambda: devices  # type: ignore[method-assign]
    return client


def test_selects_only_authorized_device() -> None:
    client = client_with_devices([DeviceEntry("ABC", "device", {"model": "phone"})])
    assert client.select_device(None).serial == "ABC"
    assert client.serial == "ABC"


@pytest.mark.parametrize(
    ("devices", "message"),
    [
        ([], "No authorized"),
        ([DeviceEntry("ABC", "unauthorized", {})], "No authorized"),
        ([DeviceEntry("ABC", "offline", {})], "No authorized"),
        ([DeviceEntry("A", "device", {}), DeviceEntry("B", "device", {})], "Multiple authorized"),
    ],
)
def test_device_selection_errors(devices: list[DeviceEntry], message: str) -> None:
    with pytest.raises(AdbError, match=message):
        client_with_devices(devices).select_device(None)


def test_explicit_unauthorized_serial_has_actionable_error() -> None:
    client = client_with_devices([DeviceEntry("ABC", "unauthorized", {})])
    with pytest.raises(AdbError, match="unauthorized"):
        client.select_device("ABC")

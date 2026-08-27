from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image
from io import BytesIO

from .errors import AdbError


@dataclass(frozen=True)
class DeviceEntry:
    serial: str
    state: str
    details: dict[str, str]


class AdbClient:
    def __init__(self, adb_path: str, serial: str | None = None, verbose: bool = False) -> None:
        self.adb_path = str(Path(adb_path).expanduser())
        self.serial = serial
        self.verbose = verbose
        if not Path(self.adb_path).is_file():
            raise AdbError(f"ADB was not found at {self.adb_path}. Update adbPath or install Android platform-tools.")

    def _command(self, arguments: Sequence[str], use_serial: bool = True) -> list[str]:
        command = [self.adb_path]
        if use_serial and self.serial:
            command.extend(["-s", self.serial])
        command.extend(arguments)
        return command

    def run(
        self,
        arguments: Sequence[str],
        *,
        binary: bool = False,
        timeout: float = 30,
        use_serial: bool = True,
        check: bool = True,
    ) -> bytes | str:
        command = self._command(arguments, use_serial=use_serial)
        if self.verbose:
            safe_command = " ".join(command)
            print(f"[adb] {safe_command}")
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB timed out while running: {' '.join(arguments)}") from exc
        except OSError as exc:
            raise AdbError(f"ADB could not start: {exc}") from exc
        if check and completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            stdout = completed.stdout.decode("utf-8", errors="replace").strip()
            detail = stderr or stdout or f"exit code {completed.returncode}"
            raise AdbError(f"ADB failed ({' '.join(arguments)}): {detail}")
        return completed.stdout if binary else completed.stdout.decode("utf-8", errors="replace").strip()

    def list_devices(self) -> list[DeviceEntry]:
        output = str(self.run(["devices", "-l"], use_serial=False))
        devices: list[DeviceEntry] = []
        for line in output.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            details: dict[str, str] = {}
            for field in fields[2:]:
                if ":" in field:
                    key, value = field.split(":", 1)
                    details[key] = value
            devices.append(DeviceEntry(fields[0], fields[1], details))
        return devices

    def select_device(self, requested_serial: str | None) -> DeviceEntry:
        devices = self.list_devices()
        if requested_serial:
            matches = [entry for entry in devices if entry.serial == requested_serial]
            if not matches:
                raise AdbError(f"Device {requested_serial!r} is not listed by ADB.")
            selected = matches[0]
        else:
            usable = [entry for entry in devices if entry.state == "device"]
            if len(usable) == 0:
                nonready = ", ".join(f"{entry.serial} ({entry.state})" for entry in devices)
                suffix = f" Found: {nonready}." if nonready else ""
                raise AdbError(f"No authorized ADB device is connected.{suffix}")
            if len(usable) > 1:
                serials = ", ".join(entry.serial for entry in usable)
                raise AdbError(f"Multiple authorized devices are connected ({serials}). Supply --serial.")
            selected = usable[0]
        if selected.state == "unauthorized":
            raise AdbError(f"Device {selected.serial} is unauthorized. Accept the USB debugging prompt on the phone.")
        if selected.state != "device":
            raise AdbError(f"Device {selected.serial} is not ready (state: {selected.state}).")
        self.serial = selected.serial
        return selected

    def screenshot(self) -> bytes:
        payload = bytes(self.run(["exec-out", "screencap", "-p"], binary=True, timeout=30))
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AdbError("ADB screenshot did not return a valid PNG stream.")
        try:
            image = Image.open(BytesIO(payload))
            image.verify()
        except Exception as exc:
            raise AdbError(f"ADB screenshot PNG is unreadable: {exc}") from exc
        return payload

    def swipe(self, width: int, height: int, geometry: list[float], duration_ms: int) -> None:
        x1, y1, x2, y2 = (
            round(geometry[0] * width),
            round(geometry[1] * height),
            round(geometry[2] * width),
            round(geometry[3] * height),
        )
        self.run(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)])

    def foreground_component(self) -> tuple[str | None, str | None]:
        output = str(self.run(["shell", "dumpsys", "activity", "activities"], timeout=30))
        match = re.search(
            r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[^\n]*?\s([A-Za-z0-9_.]+)/(\S+)",
            output,
        )
        return (match.group(1), match.group(2).rstrip("}")) if match else (None, None)

    def lock_state(self) -> bool | None:
        parts: list[str] = []
        for command in (["shell", "dumpsys", "window", "policy"], ["shell", "dumpsys", "power"]):
            try:
                parts.append(str(self.run(command, timeout=30)))
            except AdbError:
                continue
        output = "\n".join(parts)
        locked_true = (
            r"mShowingLockscreen=true",
            r"isStatusBarKeyguard=true",
            r"mDreamingLockscreen=true",
            r"showing=true\s+occluded=false",
        )
        locked_false = (
            r"mShowingLockscreen=false",
            r"isStatusBarKeyguard=false",
            r"mDreamingLockscreen=false",
            r"showing=false",
        )
        if any(re.search(pattern, output, re.IGNORECASE) for pattern in locked_true):
            return True
        if any(re.search(pattern, output, re.IGNORECASE) for pattern in locked_false):
            return False
        return None

    def package_info(self, package: str) -> dict[str, str | int | None]:
        path_output = str(self.run(["shell", "pm", "path", package], check=False))
        if not path_output.startswith("package:"):
            raise AdbError(f"DevEco Assistant package {package} is not installed on the selected phone.")
        output = str(self.run(["shell", "dumpsys", "package", package], timeout=30))
        version_name = re.search(r"versionName=([^\s]+)", output)
        version_code = re.search(r"versionCode=(\d+)", output)
        return {
            "package": package,
            "versionName": version_name.group(1) if version_name else None,
            "versionCode": int(version_code.group(1)) if version_code else None,
        }

    def device_info(self) -> dict[str, str | int | None]:
        def prop(name: str) -> str:
            return str(self.run(["shell", "getprop", name]))

        size_output = str(self.run(["shell", "wm", "size"]))
        density_output = str(self.run(["shell", "wm", "density"]))
        size_match = re.search(r"(?:Override|Physical) size:\s*(\d+)x(\d+)", size_output)
        density_matches = re.findall(r"(?:Override|Physical) density:\s*(\d+)", density_output)
        return {
            "serial": self.serial,
            "manufacturer": prop("ro.product.manufacturer"),
            "model": prop("ro.product.model"),
            "androidVersion": prop("ro.build.version.release"),
            "width": int(size_match.group(1)) if size_match else None,
            "height": int(size_match.group(2)) if size_match else None,
            "density": int(density_matches[-1]) if density_matches else None,
        }

    def version(self) -> str:
        return str(self.run(["version"], use_serial=False)).splitlines()[0]

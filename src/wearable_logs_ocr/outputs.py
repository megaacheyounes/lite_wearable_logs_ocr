from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import WearableLogsError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_pair() -> dict[str, str]:
    now = utc_now()
    return {"utc": now.isoformat(), "local": now.astimezone().isoformat()}


def git_state(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5, check=False
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"commit": None, "dirty": None}
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01 * (attempt + 1))


class RunOutput:
    def __init__(self, output_root: Path, command: str, tool_version: str, project_root: Path) -> None:
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / f".write-test-{secrets.token_hex(4)}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise WearableLogsError(f"Output directory is not writable: {output_root}: {exc}") from exc
        stamp = utc_now().strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_id = f"{stamp}_{secrets.token_hex(4)}"
        self.path = output_root / self.run_id
        self.path.mkdir()
        self.screenshots = self.path / "screenshots"
        self.processed = self.path / "processed"
        self.screenshots.mkdir()
        self.processed.mkdir()
        times = timestamp_pair()
        self.manifest: dict[str, Any] = {
            "runId": self.run_id,
            "command": command,
            "toolVersion": tool_version,
            "startedAt": times,
            "finishedAt": None,
            "git": git_state(project_root),
            "state": "running",
            "failure": None,
            "warnings": [],
            "screenshots": [],
            "processingOwner": {
                "pid": os.getpid(),
                "token": secrets.token_hex(16),
                "acquiredAt": times,
                "active": True,
            },
            "screenshotsCaptured": 0,
            "cropStateTransitions": 0,
            "uniqueCroppedScreens": 0,
            "ocrInvocations": 0,
            "ocrResultsReused": 0,
            "emptyCroppedScreens": 0,
            "processingDurationMs": 0,
        }
        self.write_manifest()

    @classmethod
    def open_existing(cls, path: Path) -> "RunOutput":
        resolved = path.resolve()
        try:
            manifest = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WearableLogsError(f"Cannot read run manifest: {resolved / 'manifest.json'}: {exc}") from exc
        instance = cls.__new__(cls)
        instance.path = resolved
        instance.run_id = str(manifest.get("runId", resolved.name))
        instance.screenshots = resolved / "screenshots"
        instance.processed = resolved / "processed"
        instance.manifest = manifest
        return instance

    def write_manifest(self) -> None:
        atomic_json(self.path / "manifest.json", self.manifest)

    def _release_owner(self) -> None:
        owner = self.manifest.get("processingOwner")
        if isinstance(owner, dict):
            owner["active"] = False
            owner["releasedAt"] = timestamp_pair()

    def fail(self, exc: BaseException, stage: str | None = None) -> None:
        self.manifest["state"] = "failed"
        self.manifest["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "stage": stage,
        }
        self.manifest["finishedAt"] = timestamp_pair()
        self._release_owner()
        self.write_manifest()

    def interrupt(self, stage: str, partial_outputs: bool) -> None:
        self.manifest["state"] = "interrupted"
        self.manifest["finishedAt"] = timestamp_pair()
        self.manifest["interruption"] = {
            "stage": stage,
            "screenshotsCompleted": int(self.manifest.get("screenshotsCaptured", 0)),
            "uniqueCropStatesCompleted": int(self.manifest.get("ocrResultsCompleted", 0)),
            "partialOutputs": partial_outputs,
            "userMessage": "Processing was interrupted; preserved evidence may be resumed.",
        }
        self.manifest["outputsPartial"] = partial_outputs
        self._release_owner()
        self.write_manifest()

    def complete(self) -> None:
        self.manifest["state"] = "completed"
        self.manifest["finishedAt"] = timestamp_pair()
        self.manifest["outputsPartial"] = False
        self._release_owner()
        self.write_manifest()

    def write_text(self, name: str, lines: list[str]) -> None:
        content = "\n".join(lines)
        if lines:
            content += "\n"
        (self.path / name).write_text(content, encoding="utf-8", newline="\n")

    def write_ocr_results(self, value: dict[str, Any]) -> None:
        atomic_json(self.path / "ocr-results.json", value)

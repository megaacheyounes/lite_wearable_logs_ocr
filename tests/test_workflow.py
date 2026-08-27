from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from wearable_logs_ocr.config import load_config
from wearable_logs_ocr.errors import WearableLogsError
from wearable_logs_ocr.outputs import RunOutput
from wearable_logs_ocr.workflow import capture_pages, execute_capture, preflight_phone


def png_bytes(color: tuple[int, int, int] = (255, 255, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (100, 200), color).save(buffer, format="PNG")
    return buffer.getvalue()


class RepeatingAdb:
    def __init__(self) -> None:
        self.swipes = 0

    def screenshot(self) -> bytes:
        return png_bytes()

    def swipe(self, width: int, height: int, geometry: list[float], duration_ms: int) -> None:
        assert (width, height) == (100, 200)
        self.swipes += 1


def test_repeated_screen_stopping(tmp_path: Path) -> None:
    config, _ = load_config(None)
    config["crop"] = [0, 0, 1, 1]
    config["repeatedScreenLimit"] = 2
    config["screenshotIntervalSeconds"] = 0
    run = RunOutput(tmp_path, "capture", "test", tmp_path)
    adb = RepeatingAdb()
    reason = capture_pages(adb, run, config, 10)
    assert reason == "repeated_screen_limit"
    assert len(run.manifest["screenshots"]) == 3
    assert adb.swipes == 2


def test_output_paths_with_spaces_and_non_ascii(tmp_path: Path) -> None:
    output = tmp_path / "path with spaces" / "العربية"
    run = RunOutput(output, "capture", "test", tmp_path)
    run.write_text("logs.txt", ["مرحبا", "value=-1.25"])
    assert (run.path / "logs.txt").read_text(encoding="utf-8") == "مرحبا\nvalue=-1.25\n"


class PhoneStateAdb:
    def __init__(self, package: str | None, locked: bool | None = False) -> None:
        self.package = package
        self.locked = locked

    def lock_state(self):
        return self.locked

    def package_info(self, package: str):
        return {"package": package, "versionName": "1.0", "versionCode": 1}

    def foreground_component(self):
        return self.package, ".activity.LogListActivity"

    def device_info(self):
        return {"serial": "ABC", "width": 1080, "height": 2400}


def test_wrong_app_fails_safely() -> None:
    config, _ = load_config(None)
    with pytest.raises(WearableLogsError, match="not in the foreground"):
        preflight_phone(PhoneStateAdb("com.example.other"), config)  # type: ignore[arg-type]


def test_locked_screen_fails_safely() -> None:
    config, _ = load_config(None)
    with pytest.raises(WearableLogsError, match="locked"):
        preflight_phone(PhoneStateAdb(config["assistantPackage"], True), config)  # type: ignore[arg-type]


def test_unknown_lock_state_is_warning() -> None:
    config, _ = load_config(None)
    _, warnings = preflight_phone(PhoneStateAdb(config["assistantPackage"], None), config)  # type: ignore[arg-type]
    assert any("could not be determined" in warning for warning in warnings)


def test_empty_ocr_is_an_error_with_failure_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = load_config(None)
    arguments = SimpleNamespace(
        command="capture",
        serial=None,
        output_dir=str(tmp_path),
        config=None,
        crop=None,
        swipe=None,
        interval=None,
        repeat_limit=None,
        swipe_duration_ms=None,
        ocr_pipeline=None,
        verbose=False,
        pages=1,
        no_swipe=True,
    )
    monkeypatch.setattr("wearable_logs_ocr.workflow.create_adb", lambda *_: (object(), object()))
    monkeypatch.setattr(
        "wearable_logs_ocr.workflow.preflight_phone",
        lambda *_: ({"device": {}, "assistant": {}, "foreground": {}, "locked": False}, []),
    )
    monkeypatch.setattr("wearable_logs_ocr.workflow.capture_pages", lambda *_: "page_limit")
    monkeypatch.setattr(
        "wearable_logs_ocr.workflow.run_ocr",
        lambda *_: (
            [],
            [],
            {
                "confidenceSummary": {
                    "count": 0,
                    "minimum": None,
                    "maximum": None,
                    "mean": None,
                    "lowConfidenceCount": 0,
                    "threshold": 0.8,
                },
                "models": [],
            },
        ),
    )
    with pytest.raises(WearableLogsError, match="OCR found no text"):
        execute_capture(config, arguments, "capture")
    manifests = list(tmp_path.glob("*/manifest.json"))
    assert len(manifests) == 1
    assert '"state": "failed"' in manifests[0].read_text(encoding="utf-8")

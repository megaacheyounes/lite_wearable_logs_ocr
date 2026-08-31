from __future__ import annotations

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from wearable_logs_ocr.config import load_config
from wearable_logs_ocr.errors import WearableLogsError
from wearable_logs_ocr.models import OcrDetection
from wearable_logs_ocr.outputs import RunOutput
from wearable_logs_ocr.workflow import (
    _save_screenshot,
    _update_capture_manifest,
    _pid_is_live,
    execute_capture,
    process_existing_run,
    run_ocr,
)


def screenshot_bytes(body: int, *, status: int = 0, changed_pixel: bool = False) -> bytes:
    image = Image.new("RGB", (20, 20), (body, body, body))
    for y in range(4):
        for x in range(20):
            image.putpixel((x, y), (status, status, status))
    if changed_pixel:
        image.putpixel((10, 10), ((body + 1) % 256, body, body))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeEngine:
    def __init__(self, _: dict[str, Any]) -> None:
        pass

    def model_info(self) -> list[dict[str, Any]]:
        return [{"role": "test", "sha256": "0" * 64}]


class CountingProcessor:
    def __init__(self, interrupt_on: int | None = None) -> None:
        self.calls = 0
        self.interrupt_on = interrupt_on

    def __call__(self, screenshot: Path, processed: Path, *_: Any) -> tuple[list[OcrDetection], tuple[int, int], dict[str, Any]]:
        self.calls += 1
        if self.interrupt_on == self.calls:
            raise KeyboardInterrupt
        processed.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (20, 16), (self.calls, 0, 0)).save(processed, format="PNG")
        detection = OcrDetection(
            text=f"1 12:34:56 [INFO] APP: state-{hashlib.sha256(screenshot.read_bytes()).hexdigest()[:8]}",
            confidence=0.91,
            bbox=[[1, 1], [19, 1], [19, 5], [1, 5]],
        )
        return [detection], (20, 16), {"engine": "fake", "pipeline": "crop"}


def processing_config() -> dict[str, Any]:
    config, _ = load_config(None)
    config["crop"] = [0.0, 0.2, 1.0, 1.0]
    config["ocr"]["pipeline"] = "crop"
    return config


def make_run(tmp_path: Path, payloads: list[bytes], *, state: str = "running") -> tuple[RunOutput, dict[str, Any]]:
    config = processing_config()
    run = RunOutput(tmp_path, "follow", str(config["toolVersion"]), tmp_path)
    run.manifest["configuration"] = {
        "crop": config["crop"],
        "swipe": config["swipe"],
        "swipeDurationMs": config["swipeDurationMs"],
        "screenshotIntervalSeconds": config["screenshotIntervalSeconds"],
        "repeatedScreenLimit": config["repeatedScreenLimit"],
        "ocr": config["ocr"],
        "reconstruction": config["reconstruction"],
    }
    for index, payload in enumerate(payloads):
        run.manifest["screenshots"].append(_save_screenshot(run, index, payload, config))
    run.manifest["stopReason"] = "duration_limit"
    _update_capture_manifest(run)
    if state == "interrupted":
        run.interrupt("ocr", False)
    return run, config


def test_181_screenshots_with_three_crop_hashes_make_three_ocr_calls(tmp_path: Path) -> None:
    payloads = [screenshot_bytes(20)] * 15 + [screenshot_bytes(40)] * 21 + [screenshot_bytes(60)] * 145
    run, config = make_run(tmp_path, payloads)
    processor = CountingProcessor()

    _, _, audit = run_ocr(run, config, engine_factory=FakeEngine, process_fn=processor)

    assert processor.calls == 3
    assert audit["statistics"] == {
        "screenshotsCaptured": 181,
        "cropStateTransitions": 3,
        "uniqueCroppedScreens": 3,
        "ocrInvocations": 3,
        "ocrResultsReused": 178,
        "emptyCroppedScreens": 0,
        "processingDurationMs": audit["statistics"]["processingDurationMs"],
        "modelInitializationDurationMs": audit["statistics"]["modelInitializationDurationMs"],
        "ocrResultsCompleted": 3,
    }


def test_consecutive_duplicates_collapse_to_one_transition(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20)] * 4)
    _, _, audit = run_ocr(run, config, engine_factory=FakeEngine, process_fn=CountingProcessor())
    assert [item["stateId"] for item in audit["transitions"]] == ["crop-0000"]
    assert audit["transitions"][0]["occurrenceCount"] == 4


def test_non_consecutive_a_b_a_chronology_is_preserved(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20), screenshot_bytes(40), screenshot_bytes(20)])
    processor = CountingProcessor()
    _, _, audit = run_ocr(run, config, engine_factory=FakeEngine, process_fn=processor)
    assert processor.calls == 2
    assert [item["stateId"] for item in audit["transitions"]] == ["crop-0000", "crop-0001", "crop-0000"]
    assert audit["screenshots"][2]["reuseDecision"] == "reused_exact_crop_hash"


def test_one_pixel_change_inside_crop_requires_separate_ocr(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20), screenshot_bytes(20, changed_pixel=True)])
    processor = CountingProcessor()
    run_ocr(run, config, engine_factory=FakeEngine, process_fn=processor)
    assert processor.calls == 2


def test_status_bar_change_outside_crop_reuses_ocr(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20, status=1), screenshot_bytes(20, status=200)])
    processor = CountingProcessor()
    run_ocr(run, config, engine_factory=FakeEngine, process_fn=processor)
    assert processor.calls == 1


class InterruptingCaptureAdb:
    def __init__(self) -> None:
        self.calls = 0

    def screenshot(self) -> bytes:
        self.calls += 1
        if self.calls == 3:
            raise KeyboardInterrupt
        return screenshot_bytes(self.calls * 20)

    def swipe(self, *_: Any) -> None:
        pass


def capture_arguments(tmp_path: Path, pages: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
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
        pages=pages,
        no_swipe=False,
    )


def patch_phone(monkeypatch: pytest.MonkeyPatch, adb: Any) -> None:
    monkeypatch.setattr("wearable_logs_ocr.workflow.create_adb", lambda *_: (adb, object()))
    monkeypatch.setattr(
        "wearable_logs_ocr.workflow.preflight_phone",
        lambda *_: ({"device": {}, "assistant": {}, "foreground": {}, "locked": False}, []),
    )


def test_ctrl_c_during_capture_finalizes_interrupted_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = processing_config()
    config["screenshotIntervalSeconds"] = 0
    patch_phone(monkeypatch, InterruptingCaptureAdb())
    with pytest.raises(KeyboardInterrupt):
        execute_capture(config, capture_arguments(tmp_path), "capture")
    manifest = json.loads(next(tmp_path.glob("*/manifest.json")).read_text(encoding="utf-8"))
    assert manifest["state"] == "interrupted"
    assert manifest["finishedAt"]
    assert manifest["interruption"]["stage"] == "capture"
    assert manifest["screenshotsCaptured"] == 2


class SequenceCaptureAdb:
    def __init__(self) -> None:
        self.calls = 0

    def screenshot(self) -> bytes:
        self.calls += 1
        return screenshot_bytes(self.calls * 20)

    def swipe(self, *_: Any) -> None:
        pass


def test_ctrl_c_during_ocr_preserves_partial_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = processing_config()
    config["screenshotIntervalSeconds"] = 0
    processor = CountingProcessor(interrupt_on=2)
    patch_phone(monkeypatch, SequenceCaptureAdb())
    monkeypatch.setattr("wearable_logs_ocr.workflow.RapidOcrEngine", FakeEngine)
    monkeypatch.setattr("wearable_logs_ocr.workflow.process_screenshot", processor)
    with pytest.raises(KeyboardInterrupt):
        execute_capture(config, capture_arguments(tmp_path), "capture")
    run_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    audit = json.loads((run_dir / "ocr-results.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "interrupted"
    assert manifest["interruption"]["stage"] == "ocr"
    assert manifest["ocrResultsCompleted"] == 1
    assert audit["partial"] is True
    assert len(audit["ocrResults"]) == 1
    assert (run_dir / "logs.raw.txt").is_file()
    assert not (run_dir / "logs.txt").exists()


def test_resume_completes_and_is_idempotent(tmp_path: Path) -> None:
    run, config = make_run(
        tmp_path,
        [screenshot_bytes(20), screenshot_bytes(40), screenshot_bytes(20)],
        state="interrupted",
    )
    first = CountingProcessor()
    process_existing_run(config, run.path, engine_factory=FakeEngine, process_fn=first)
    assert first.calls == 2
    manifest = json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "completed"
    assert (run.path / "manifest.pre-resume.json").is_file()
    assert (run.path / "logs.txt").is_file()

    second = CountingProcessor()
    process_existing_run(config, run.path, engine_factory=FakeEngine, process_fn=second)
    assert second.calls == 0
    assert len(json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))["resumeHistory"]) == 1


def test_resume_reuses_valid_partial_ocr_results(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20), screenshot_bytes(40)], state="interrupted")
    interrupted = CountingProcessor(interrupt_on=2)
    with pytest.raises(KeyboardInterrupt):
        run_ocr(run, config, engine_factory=FakeEngine, process_fn=interrupted)
    run.interrupt("ocr", True)
    resumed = CountingProcessor()
    process_existing_run(config, run.path, engine_factory=FakeEngine, process_fn=resumed)
    assert resumed.calls == 1
    assert json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))["ocrInvocations"] == 3


def test_resume_refuses_altered_screenshot_evidence(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20)], state="interrupted")
    screenshot = run.path / run.manifest["screenshots"][0]["file"]
    screenshot.write_bytes(screenshot_bytes(21))
    with pytest.raises(WearableLogsError, match="hash mismatch"):
        process_existing_run(config, run.path, engine_factory=FakeEngine, process_fn=CountingProcessor())
    assert not (run.path / "manifest.pre-resume.json").exists()


def test_resume_refuses_a_run_owned_by_a_live_process(tmp_path: Path) -> None:
    run, config = make_run(tmp_path, [screenshot_bytes(20)])
    with pytest.raises(WearableLogsError, match="live process"):
        process_existing_run(config, run.path, engine_factory=FakeEngine, process_fn=CountingProcessor())


def test_process_liveness_probe_is_safe_for_current_process() -> None:
    assert _pid_is_live(os.getpid()) is True


def test_process_liveness_probe_rejects_nonexistent_process() -> None:
    assert _pid_is_live(2_147_483_647) is False

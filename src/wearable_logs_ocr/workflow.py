from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .adb import AdbClient, DeviceEntry
from .config import project_root, resolve_output_root, write_local_config
from .errors import OcrError, WearableLogsError
from .ocr import RapidOcrEngine, crop_image, normalized_crop_box, process_screenshot
from .outputs import RunOutput, timestamp_pair
from .reconstruct import merge_screenshot_records, reconstruct_records, reconstruct_visual_lines


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {"python": platform.python_version()}
    for package in ("rapidocr", "onnxruntime", "opencv-python", "pillow"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = None
    return result


def apply_overrides(config: dict[str, Any], arguments: Any) -> dict[str, Any]:
    value = json.loads(json.dumps(config))
    if getattr(arguments, "crop", None) is not None:
        value["crop"] = arguments.crop
    if getattr(arguments, "swipe", None) is not None:
        value["swipe"] = arguments.swipe
    if getattr(arguments, "interval", None) is not None:
        value["screenshotIntervalSeconds"] = arguments.interval
    if getattr(arguments, "repeat_limit", None) is not None:
        value["repeatedScreenLimit"] = arguments.repeat_limit
    if getattr(arguments, "swipe_duration_ms", None) is not None:
        value["swipeDurationMs"] = arguments.swipe_duration_ms
    return value


def create_adb(config: dict[str, Any], serial: str | None, verbose: bool) -> tuple[AdbClient, DeviceEntry]:
    adb = AdbClient(str(config["adbPath"]), verbose=verbose)
    selected = adb.select_device(serial)
    return adb, selected


def preflight_phone(adb: AdbClient, config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    locked = adb.lock_state()
    if locked is True:
        raise WearableLogsError("The phone appears to be locked. Unlock it and open the DevEco Assistant Logs screen.")
    if locked is None:
        warnings.append("Phone lock state could not be determined from dumpsys output.")
    package_info = adb.package_info(str(config["assistantPackage"]))
    foreground_package, foreground_activity = adb.foreground_component()
    if foreground_package and foreground_package != config["assistantPackage"]:
        raise WearableLogsError(
            f"DevEco Assistant is not in the foreground (found {foreground_package}). Open its Logs screen and retry."
        )
    expected_activity = config.get("assistantActivity")
    if expected_activity and foreground_activity and not foreground_activity.endswith(str(expected_activity).lstrip(".")):
        warnings.append(
            f"Foreground Assistant activity is {foreground_activity}; configured log activity is {expected_activity}."
        )
    return {
        "device": adb.device_info(),
        "assistant": package_info,
        "foreground": {"package": foreground_package, "activity": foreground_activity},
        "locked": locked,
    }, warnings


def doctor(config: dict[str, Any], arguments: Any) -> int:
    checks: list[tuple[str, str, str]] = []
    try:
        adb, selected = create_adb(config, arguments.serial, arguments.verbose)
        checks.append(("PASS", "ADB", adb.version()))
        checks.append(("PASS", "Device", f"{selected.serial} ({selected.details.get('model', 'unknown model')})"))
        phone, warnings = preflight_phone(adb, config)
        checks.append(("PASS", "Assistant", f"{phone['assistant']['package']} {phone['assistant']['versionName']}"))
        foreground = phone["foreground"]
        checks.append(("PASS", "Foreground", f"{foreground['package']}/{foreground['activity']}"))
        for warning in warnings:
            checks.append(("WARN", "Phone", warning))
        screenshot = adb.screenshot()
        with Image.open(BytesIO(screenshot)) as image:
            checks.append(("PASS", "Screen capture", f"{image.width}x{image.height} PNG"))
        output_root = resolve_output_root(config, arguments.output_dir)
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / f".doctor-write-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        checks.append(("PASS", "Output", str(output_root)))
        engine = RapidOcrEngine(config["ocr"])
        versions = dependency_versions()
        recognition_model = next(model for model in engine.model_info() if model["role"] == "primary-recognition")
        checks.append(
            (
                "PASS",
                "OCR",
                f"RapidOCR {versions['rapidocr']}, ONNX Runtime {versions['onnxruntime']}, {recognition_model['file']}",
            )
        )
        checks.append(("PASS", "Python", sys.version.split()[0]))
    except Exception as exc:
        checks.append(("ERROR", "Doctor", str(exc)))
    for status, name, detail in checks:
        print(f"{status:5} {name}: {detail}")
    return 2 if any(status == "ERROR" for status, _, _ in checks) else 0


def _crop_pixel_hash(payload: bytes, crop: list[float]) -> tuple[str, tuple[int, int]]:
    with Image.open(BytesIO(payload)) as image:
        cropped = crop_image(image.convert("RGB"), crop)
    digest = hashlib.sha256()
    digest.update(f"{cropped.mode}:{cropped.width}x{cropped.height}:".encode("ascii"))
    digest.update(cropped.tobytes())
    return digest.hexdigest(), image.size


def _save_screenshot(run: RunOutput, index: int, payload: bytes, config: dict[str, Any]) -> dict[str, Any]:
    name = f"screen-{index:04d}.png"
    path = run.screenshots / name
    path.write_bytes(payload)
    crop_hash, size = _crop_pixel_hash(payload, config["crop"])
    return {
        "index": index,
        "file": f"screenshots/{name}",
        "capturedAt": timestamp_pair(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "croppedPixelSha256": crop_hash,
        "width": size[0],
        "height": size[1],
    }


def capture_pages(adb: AdbClient, run: RunOutput, config: dict[str, Any], pages: int) -> str:
    repeated = 0
    previous_hash: str | None = None
    stop_reason = "page_limit"
    for index in range(pages):
        payload = adb.screenshot()
        metadata = _save_screenshot(run, index, payload, config)
        run.manifest["screenshots"].append(metadata)
        run.write_manifest()
        current_hash = str(metadata["croppedPixelSha256"])
        repeated = repeated + 1 if current_hash == previous_hash else 0
        previous_hash = current_hash
        if repeated >= int(config["repeatedScreenLimit"]):
            stop_reason = "repeated_screen_limit"
            break
        if index + 1 < pages:
            adb.swipe(
                int(metadata["width"]),
                int(metadata["height"]),
                config["swipe"],
                int(config["swipeDurationMs"]),
            )
            time.sleep(float(config["screenshotIntervalSeconds"]))
    return stop_reason


def follow_screens(adb: AdbClient, run: RunOutput, config: dict[str, Any], seconds: float) -> str:
    interval = float(config["screenshotIntervalSeconds"])
    started = time.monotonic()
    deadline = started + seconds
    index = 0
    while True:
        payload = adb.screenshot()
        metadata = _save_screenshot(run, index, payload, config)
        metadata["elapsedSeconds"] = time.monotonic() - started
        run.manifest["screenshots"].append(metadata)
        run.write_manifest()
        index += 1
        next_capture = started + index * interval
        if next_capture > deadline:
            break
        time.sleep(max(0.0, next_capture - time.monotonic()))
    return "duration_limit"


def run_ocr(run: RunOutput, config: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    engine = RapidOcrEngine(config["ocr"])
    model_info = engine.model_info()
    screenshot_records = []
    raw_lines: list[str] = []
    screenshot_results: list[dict[str, Any]] = []
    confidence_values: list[float] = []
    for metadata in run.manifest["screenshots"]:
        index = int(metadata["index"])
        screenshot_path = run.path / str(metadata["file"])
        processed_path = run.processed / f"screen-{index:04d}-{config['ocr']['pipeline']}.png"
        detections, image_size, ocr_metadata = process_screenshot(
            screenshot_path,
            processed_path,
            config["crop"],
            str(config["ocr"]["pipeline"]),
            engine,
        )
        lines = reconstruct_visual_lines(detections, float(config["ocr"]["lowConfidenceThreshold"]))
        records, joins = reconstruct_records(lines, image_size, config["reconstruction"])
        screenshot_records.append(records)
        raw_lines.extend(line.text for line in lines)
        confidence_values.extend(line.confidence for line in lines)
        screenshot_results.append(
            {
                "screenshot": index,
                "source": metadata["file"],
                "processed": f"processed/{processed_path.name}",
                "ocr": ocr_metadata,
                "detections": [detection.to_dict() for detection in detections],
                "visualLines": [line.to_dict() for line in lines],
                "logicalRecords": [record.to_dict() for record in records],
                "joinDecisions": joins,
            }
        )
    merged = merge_screenshot_records(screenshot_records, config["reconstruction"])
    normalized = [record.text for record in merged.records]
    low_count = sum(1 for result in screenshot_results for line in result["visualLines"] if line["low_confidence"])
    confidence_summary = {
        "count": len(confidence_values),
        "minimum": min(confidence_values) if confidence_values else None,
        "maximum": max(confidence_values) if confidence_values else None,
        "mean": sum(confidence_values) / len(confidence_values) if confidence_values else None,
        "lowConfidenceCount": low_count,
        "threshold": config["ocr"]["lowConfidenceThreshold"],
    }
    audit = {
        "schemaVersion": 1,
        "models": model_info,
        "screenshots": screenshot_results,
        "deduplicationDecisions": merged.decisions,
        "confidenceSummary": confidence_summary,
    }
    return normalized, raw_lines, audit


def execute_capture(config: dict[str, Any], arguments: Any, command: str) -> Path:
    root = project_root()
    output_root = resolve_output_root(config, arguments.output_dir)
    run = RunOutput(output_root, command, str(config["toolVersion"]), root)
    run.manifest["arguments"] = vars(arguments)
    run.manifest["configuration"] = {
        "crop": config["crop"],
        "swipe": config["swipe"],
        "swipeDurationMs": config["swipeDurationMs"],
        "screenshotIntervalSeconds": config["screenshotIntervalSeconds"],
        "repeatedScreenLimit": config["repeatedScreenLimit"],
        "ocr": config["ocr"],
        "reconstruction": config["reconstruction"],
    }
    run.manifest["runtime"] = dependency_versions()
    try:
        adb, _ = create_adb(config, arguments.serial, arguments.verbose)
        phone, warnings = preflight_phone(adb, config)
        run.manifest.update(phone)
        run.manifest["warnings"].extend(warnings)
        if command == "capture":
            stop_reason = capture_pages(adb, run, config, int(arguments.pages))
        else:
            stop_reason = follow_screens(adb, run, config, float(arguments.seconds))
        run.manifest["stopReason"] = stop_reason
        normalized, raw_lines, audit = run_ocr(run, config)
        run.write_text("logs.raw.txt", raw_lines)
        run.write_text("logs.txt", normalized)
        run.write_ocr_results(audit)
        run.manifest["counts"] = {
            "screenshots": len(run.manifest["screenshots"]),
            "rawVisualLines": len(raw_lines),
            "normalizedRecords": len(normalized),
        }
        run.manifest["confidenceSummary"] = audit["confidenceSummary"]
        run.manifest["ocrModels"] = audit["models"]
        if audit["confidenceSummary"]["lowConfidenceCount"]:
            run.manifest["warnings"].append(
                f"{audit['confidenceSummary']['lowConfidenceCount']} OCR visual lines are below the confidence threshold."
            )
        if not raw_lines:
            raise OcrError("OCR found no text in the configured crop. Check calibration and the visible log screen.")
        if not normalized:
            raise OcrError("OCR found only incomplete edge text; no complete logical log records were produced.")
        run.complete()
        return run.path
    except Exception as exc:
        run.fail(exc)
        raise WearableLogsError(f"Run failed; evidence and failure manifest: {run.path}\n{exc}") from exc


def calibrate(config: dict[str, Any], local_config_path: Path, arguments: Any) -> Path:
    root = project_root()
    output_root = resolve_output_root(config, arguments.output_dir)
    run = RunOutput(output_root, "calibrate", str(config["toolVersion"]), root)
    try:
        adb, _ = create_adb(config, arguments.serial, arguments.verbose)
        phone, warnings = preflight_phone(adb, config)
        run.manifest.update(phone)
        run.manifest["warnings"].extend(warnings)
        payload = adb.screenshot()
        metadata = _save_screenshot(run, 0, payload, config)
        run.manifest["screenshots"].append(metadata)
        source = run.screenshots / "screen-0000.png"
        with Image.open(source) as image:
            overlay = image.convert("RGB")
        draw = ImageDraw.Draw(overlay)
        box = normalized_crop_box(overlay.size, config["crop"])
        line_width = max(3, round(overlay.width / 270))
        draw.rectangle(box, outline=(255, 0, 0), width=line_width)
        x1, y1, x2, y2 = config["swipe"]
        points = (round(x1 * overlay.width), round(y1 * overlay.height), round(x2 * overlay.width), round(y2 * overlay.height))
        draw.line(points, fill=(0, 90, 255), width=line_width)
        overlay.save(run.processed / "calibration-overlay.png", format="PNG")
        local_values = {
            "crop": config["crop"],
            "swipe": config["swipe"],
            "swipeDurationMs": config["swipeDurationMs"],
        }
        write_local_config(local_config_path, local_values)
        run.write_text("logs.txt", [])
        run.write_text("logs.raw.txt", [])
        run.write_ocr_results({"schemaVersion": 1, "screenshots": [], "note": "Calibration does not run OCR."})
        run.manifest["configurationWritten"] = str(local_config_path)
        run.manifest["counts"] = {"screenshots": 1, "rawVisualLines": 0, "normalizedRecords": 0}
        run.complete()
        return run.path
    except Exception as exc:
        run.fail(exc)
        raise WearableLogsError(f"Calibration failed; evidence and failure manifest: {run.path}\n{exc}") from exc

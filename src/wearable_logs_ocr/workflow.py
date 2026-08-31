from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import secrets
import shutil
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
from .models import LogicalRecord
from .outputs import RunOutput, atomic_json, timestamp_pair
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


def _update_capture_manifest(run: RunOutput) -> None:
    screenshots = list(run.manifest.get("screenshots", []))
    crop_hashes = [str(item["croppedPixelSha256"]) for item in screenshots]
    run.manifest["screenshotsCaptured"] = len(screenshots)
    run.manifest["uniqueCroppedScreens"] = len(set(crop_hashes))
    run.manifest["cropStateTransitions"] = sum(
        1 for index, crop_hash in enumerate(crop_hashes) if index == 0 or crop_hash != crop_hashes[index - 1]
    )
    run.write_manifest()


def capture_pages(adb: AdbClient, run: RunOutput, config: dict[str, Any], pages: int) -> str:
    repeated = 0
    previous_hash: str | None = None
    stop_reason = "page_limit"
    for index in range(pages):
        payload = adb.screenshot()
        metadata = _save_screenshot(run, index, payload, config)
        run.manifest["screenshots"].append(metadata)
        _update_capture_manifest(run)
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
        _update_capture_manifest(run)
        index += 1
        next_capture = started + index * interval
        if next_capture > deadline:
            break
        time.sleep(max(0.0, next_capture - time.monotonic()))
    return "duration_limit"


def _processing_config_hash(config: dict[str, Any]) -> str:
    selected = {
        "crop": config["crop"],
        "ocr": config["ocr"],
        "reconstruction": config["reconstruction"],
    }
    payload = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_crop_index(screenshots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    states: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    by_hash: dict[str, dict[str, Any]] = {}
    previous_hash: str | None = None
    for position, metadata in enumerate(screenshots):
        crop_hash = str(metadata["croppedPixelSha256"])
        state = by_hash.get(crop_hash)
        if state is None:
            state = {
                "stateId": f"crop-{len(states):04d}",
                "croppedPixelSha256": crop_hash,
                "representativeScreenshot": int(metadata["index"]),
                "representativeSource": str(metadata["file"]),
                "representativeSourceSha256": str(metadata["sha256"]),
                "occurrenceScreenshotIndexes": [],
            }
            by_hash[crop_hash] = state
            states.append(state)
        state["occurrenceScreenshotIndexes"].append(int(metadata["index"]))
        if crop_hash != previous_hash:
            transitions.append(
                {
                    "transitionOrdinal": len(transitions),
                    "stateId": state["stateId"],
                    "croppedPixelSha256": crop_hash,
                    "firstScreenshot": int(metadata["index"]),
                    "lastScreenshot": int(metadata["index"]),
                    "occurrenceCount": 1,
                }
            )
        else:
            transitions[-1]["lastScreenshot"] = int(metadata["index"])
            transitions[-1]["occurrenceCount"] += 1
        occurrence = {
            "screenshot": int(metadata["index"]),
            "source": str(metadata["file"]),
            "capturedAt": metadata.get("capturedAt"),
            "croppedPixelSha256": crop_hash,
            "stateId": state["stateId"],
            "transitionOrdinal": transitions[-1]["transitionOrdinal"],
            "ocrResultId": f"ocr-{crop_hash}",
            "reuseDecision": (
                "ocr_representative"
                if int(metadata["index"]) == int(state["representativeScreenshot"])
                else "reused_exact_crop_hash"
            ),
            "representativeScreenshot": state["representativeScreenshot"],
        }
        occurrences.append(occurrence)
        previous_hash = crop_hash
    return states, transitions, occurrences


def _logical_records(value: dict[str, Any]) -> list[LogicalRecord]:
    return [
        LogicalRecord(
            text=str(item["text"]),
            confidence=float(item["confidence"]),
            visual_line_indices=[int(index) for index in item["visual_line_indices"]],
            low_confidence=bool(item["low_confidence"]),
            edge_state=item.get("edge_state"),
        )
        for item in value.get("logicalRecords", [])
    ]


def _assemble_audit(
    config: dict[str, Any],
    states: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    occurrences: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    models: list[dict[str, Any]],
    ocr_invocations: int,
    processing_duration_ms: int,
    model_initialization_duration_ms: int,
) -> tuple[list[str], list[str], dict[str, Any]]:
    transition_records: list[list[LogicalRecord]] = []
    raw_lines: list[str] = []
    completed_transitions: list[dict[str, Any]] = []
    for transition in transitions:
        result = results.get(str(transition["croppedPixelSha256"]))
        if result is None:
            continue
        completed_transitions.append(transition)
        raw_lines.extend(str(line["text"]) for line in result.get("visualLines", []))
        transition_records.append(_logical_records(result))
    merged = merge_screenshot_records(transition_records, config["reconstruction"])
    normalized = [record.text for record in merged.records]
    decisions: list[dict[str, Any]] = []
    for decision in merged.decisions:
        enriched = dict(decision)
        transition_position = int(enriched["screenshot"])
        transition = completed_transitions[transition_position]
        enriched["transitionOrdinal"] = transition["transitionOrdinal"]
        enriched["stateId"] = transition["stateId"]
        enriched["croppedPixelSha256"] = transition["croppedPixelSha256"]
        decisions.append(enriched)
    confidence_values = [
        float(line["confidence"])
        for result in results.values()
        for line in result.get("visualLines", [])
    ]
    low_count = sum(
        1
        for result in results.values()
        for line in result.get("visualLines", [])
        if line.get("low_confidence")
    )
    confidence_summary = {
        "count": len(confidence_values),
        "minimum": min(confidence_values) if confidence_values else None,
        "maximum": max(confidence_values) if confidence_values else None,
        "mean": sum(confidence_values) / len(confidence_values) if confidence_values else None,
        "lowConfidenceCount": low_count,
        "threshold": config["ocr"]["lowConfidenceThreshold"],
    }
    completed_hashes = set(results)
    audit_states = []
    for state in states:
        item = dict(state)
        item["ocrResultId"] = f"ocr-{state['croppedPixelSha256']}"
        item["ocrStatus"] = "completed" if state["croppedPixelSha256"] in completed_hashes else "pending"
        audit_states.append(item)
    audit_transitions = []
    for transition in transitions:
        item = dict(transition)
        item["ocrResultId"] = f"ocr-{transition['croppedPixelSha256']}"
        item["ocrStatus"] = "completed" if transition["croppedPixelSha256"] in completed_hashes else "pending"
        audit_transitions.append(item)
    reused_occurrences = sum(
        max(0, len(state["occurrenceScreenshotIndexes"]) - 1)
        for state in states
        if state["croppedPixelSha256"] in completed_hashes
    )
    empty_states = sum(1 for result in results.values() if not result.get("visualLines"))
    audit = {
        "schemaVersion": 2,
        "partial": len(results) != len(states),
        "processingConfigSha256": _processing_config_hash(config),
        "models": models,
        "cropStates": audit_states,
        "transitions": audit_transitions,
        "screenshots": occurrences,
        "ocrResults": [results[state["croppedPixelSha256"]] for state in states if state["croppedPixelSha256"] in results],
        "deduplicationDecisions": decisions,
        "confidenceSummary": confidence_summary,
        "statistics": {
            "screenshotsCaptured": len(occurrences),
            "cropStateTransitions": len(transitions),
            "uniqueCroppedScreens": len(states),
            "ocrInvocations": ocr_invocations,
            "ocrResultsReused": reused_occurrences,
            "emptyCroppedScreens": empty_states,
            "processingDurationMs": processing_duration_ms,
            "modelInitializationDurationMs": model_initialization_duration_ms,
            "ocrResultsCompleted": len(results),
        },
    }
    return normalized, raw_lines, audit


def _update_processing_manifest(run: RunOutput, normalized: list[str], raw_lines: list[str], audit: dict[str, Any]) -> None:
    statistics = audit["statistics"]
    for key in (
        "screenshotsCaptured",
        "cropStateTransitions",
        "uniqueCroppedScreens",
        "ocrInvocations",
        "ocrResultsReused",
        "emptyCroppedScreens",
        "processingDurationMs",
        "ocrResultsCompleted",
    ):
        run.manifest[key] = statistics[key]
    run.manifest["counts"] = {
        "screenshots": statistics["screenshotsCaptured"],
        "rawVisualLines": len(raw_lines),
        "normalizedRecords": len(normalized),
    }
    run.manifest["confidenceSummary"] = audit["confidenceSummary"]
    run.manifest["ocrModels"] = audit["models"]
    run.manifest["outputsPartial"] = bool(audit["partial"])
    run.write_manifest()


def _persist_processing(run: RunOutput, normalized: list[str], raw_lines: list[str], audit: dict[str, Any]) -> None:
    run.write_ocr_results(audit)
    if audit["ocrResults"]:
        run.write_text("logs.raw.txt", raw_lines)
    _update_processing_manifest(run, normalized, raw_lines, audit)


def _load_reusable_results(
    run: RunOutput,
    config: dict[str, Any],
    states: list[dict[str, Any]],
    existing_audit: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], int, int, int]:
    if not existing_audit or existing_audit.get("schemaVersion") != 2:
        return {}, [], 0, 0, 0
    if existing_audit.get("processingConfigSha256") != _processing_config_hash(config):
        return {}, [], 0, 0, 0
    state_by_hash = {state["croppedPixelSha256"]: state for state in states}
    reusable: dict[str, dict[str, Any]] = {}
    for result in existing_audit.get("ocrResults", []):
        crop_hash = str(result.get("croppedPixelSha256", ""))
        state = state_by_hash.get(crop_hash)
        if state is None:
            continue
        if result.get("representativeSourceSha256") != state["representativeSourceSha256"]:
            continue
        if result.get("processingConfigSha256") != _processing_config_hash(config):
            continue
        processed_value = result.get("processed")
        processed_hash = result.get("processedSha256")
        if not processed_value or not processed_hash:
            continue
        processed_path = (run.path / str(processed_value)).resolve()
        if run.path not in processed_path.parents or not processed_path.is_file():
            continue
        if hashlib.sha256(processed_path.read_bytes()).hexdigest() != processed_hash:
            continue
        reusable[crop_hash] = result
    statistics = existing_audit.get("statistics", {})
    return (
        reusable,
        list(existing_audit.get("models", [])),
        int(statistics.get("ocrInvocations", len(reusable))),
        int(statistics.get("processingDurationMs", 0)),
        int(statistics.get("modelInitializationDurationMs", 0)),
    )


def run_ocr(
    run: RunOutput,
    config: dict[str, Any],
    *,
    existing_audit: dict[str, Any] | None = None,
    engine_factory: Any = None,
    process_fn: Any = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    engine_factory = engine_factory or RapidOcrEngine
    process_fn = process_fn or process_screenshot
    screenshots = list(run.manifest.get("screenshots", []))
    if not screenshots:
        raise OcrError("The run contains no screenshots to process.")
    states, transitions, occurrences = _build_crop_index(screenshots)
    results, models, ocr_invocations, prior_duration_ms, model_initialization_ms = _load_reusable_results(
        run, config, states, existing_audit
    )
    remaining = [state for state in states if state["croppedPixelSha256"] not in results]
    engine = None
    if remaining:
        model_started = time.monotonic()
        engine = engine_factory(config["ocr"])
        model_initialization_ms += round((time.monotonic() - model_started) * 1000)
        models = engine.model_info()
    processing_started = time.monotonic()
    last_audit: dict[str, Any] | None = None
    try:
        for state in remaining:
            crop_hash = str(state["croppedPixelSha256"])
            screenshot_index = int(state["representativeScreenshot"])
            screenshot_path = run.path / str(state["representativeSource"])
            processed_path = run.processed / f"{state['stateId']}-{crop_hash[:12]}-{config['ocr']['pipeline']}.png"
            ocr_invocations += 1
            detections, image_size, ocr_metadata = process_fn(
                screenshot_path,
                processed_path,
                config["crop"],
                str(config["ocr"]["pipeline"]),
                engine,
            )
            lines = reconstruct_visual_lines(detections, float(config["ocr"]["lowConfidenceThreshold"]))
            records, joins = reconstruct_records(lines, image_size, config["reconstruction"])
            results[crop_hash] = {
                "ocrResultId": f"ocr-{crop_hash}",
                "stateId": state["stateId"],
                "croppedPixelSha256": crop_hash,
                "representativeScreenshot": screenshot_index,
                "representativeSource": state["representativeSource"],
                "representativeSourceSha256": state["representativeSourceSha256"],
                "processingConfigSha256": _processing_config_hash(config),
                "processed": f"processed/{processed_path.name}",
                "processedSha256": hashlib.sha256(processed_path.read_bytes()).hexdigest(),
                "ocr": ocr_metadata,
                "detections": [detection.to_dict() for detection in detections],
                "visualLines": [line.to_dict() for line in lines],
                "logicalRecords": [record.to_dict() for record in records],
                "joinDecisions": joins,
                "completedAt": timestamp_pair(),
            }
            elapsed_ms = prior_duration_ms + round((time.monotonic() - processing_started) * 1000)
            normalized, raw_lines, last_audit = _assemble_audit(
                config,
                states,
                transitions,
                occurrences,
                results,
                models,
                ocr_invocations,
                elapsed_ms,
                model_initialization_ms,
            )
            _persist_processing(run, normalized, raw_lines, last_audit)
    except BaseException:
        elapsed_ms = prior_duration_ms + round((time.monotonic() - processing_started) * 1000)
        normalized, raw_lines, last_audit = _assemble_audit(
            config,
            states,
            transitions,
            occurrences,
            results,
            models,
            ocr_invocations,
            elapsed_ms,
            model_initialization_ms,
        )
        _persist_processing(run, normalized, raw_lines, last_audit)
        raise
    elapsed_ms = prior_duration_ms + round((time.monotonic() - processing_started) * 1000)
    normalized, raw_lines, last_audit = _assemble_audit(
        config,
        states,
        transitions,
        occurrences,
        results,
        models,
        ocr_invocations,
        elapsed_ms,
        model_initialization_ms,
    )
    _persist_processing(run, normalized, raw_lines, last_audit)
    return normalized, raw_lines, last_audit


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
    stage = "capture"
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
        run.manifest["stopReasonRecordedAtCapture"] = True
        run.write_manifest()
        stage = "ocr"
        normalized, raw_lines, audit = run_ocr(run, config)
        run.write_text("logs.txt", normalized)
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
    except KeyboardInterrupt:
        partial_outputs = bool(run.manifest.get("ocrResultsCompleted", 0))
        run.interrupt(stage, partial_outputs)
        raise
    except Exception as exc:
        run.fail(exc, stage)
        raise WearableLogsError(f"Run failed; evidence and failure manifest: {run.path}\n{exc}") from exc
    except BaseException as exc:
        run.fail(exc, stage)
        raise


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        error_invalid_parameter = 87
        error_not_found = 1168
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == error_access_denied:
                return True
            if error in (error_invalid_parameter, error_not_found):
                return False
            return True
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _owner_is_live(manifest: dict[str, Any]) -> bool:
    owner = manifest.get("processingOwner")
    if not isinstance(owner, dict) or not owner.get("active"):
        return False
    try:
        return _pid_is_live(int(owner["pid"]))
    except (KeyError, TypeError, ValueError):
        return False


def _validate_run_screenshots(run: RunOutput, config: dict[str, Any]) -> None:
    screenshots = list(run.manifest.get("screenshots", []))
    if not screenshots:
        raise WearableLogsError("The run has no completed screenshots to process.")
    expected_files: set[Path] = set()
    seen_indexes: set[int] = set()
    for metadata in screenshots:
        index = int(metadata["index"])
        if index in seen_indexes:
            raise WearableLogsError(f"Run manifest contains duplicate screenshot index {index}.")
        seen_indexes.add(index)
        path = (run.path / str(metadata["file"])).resolve()
        if run.screenshots.resolve() not in path.parents:
            raise WearableLogsError(f"Screenshot path escapes the run screenshot directory: {metadata['file']}")
        if not path.is_file():
            raise WearableLogsError(f"Screenshot evidence is missing: {path}")
        payload = path.read_bytes()
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != str(metadata["sha256"]):
            raise WearableLogsError(
                f"Screenshot evidence hash mismatch for {metadata['file']}; refusing to process altered evidence."
            )
        crop_hash, size = _crop_pixel_hash(payload, config["crop"])
        if crop_hash != str(metadata["croppedPixelSha256"]):
            raise WearableLogsError(
                f"Cropped pixel hash mismatch for {metadata['file']}; refusing to process altered evidence."
            )
        if size != (int(metadata["width"]), int(metadata["height"])):
            raise WearableLogsError(f"Screenshot dimensions changed for {metadata['file']}; refusing to process it.")
        expected_files.add(path)
    actual_files = {path.resolve() for path in run.screenshots.glob("*.png")}
    unexpected = actual_files - expected_files
    missing = expected_files - actual_files
    if unexpected or missing:
        raise WearableLogsError(
            "Screenshot directory does not exactly match the manifest; refusing to process untracked or missing evidence."
        )


def _read_existing_audit(run: RunOutput) -> dict[str, Any] | None:
    path = run.path / "ocr-results.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WearableLogsError(f"Cannot read existing OCR audit: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WearableLogsError(f"Existing OCR audit is not a JSON object: {path}")
    return value


def _completed_audit_is_idempotent(run: RunOutput, config: dict[str, Any], audit: dict[str, Any] | None) -> bool:
    if run.manifest.get("state") != "completed" or not audit or audit.get("partial"):
        return False
    states, _, _ = _build_crop_index(list(run.manifest.get("screenshots", [])))
    reusable, _, _, _, _ = _load_reusable_results(run, config, states, audit)
    return len(reusable) == len(states) and (run.path / "logs.txt").is_file()


def _completed_capture_stop_reason(manifest: dict[str, Any]) -> str | None:
    existing = manifest.get("stopReason")
    if existing:
        return str(existing)
    screenshots = list(manifest.get("screenshots", []))
    arguments = manifest.get("arguments", {})
    if not screenshots or not isinstance(arguments, dict):
        return None
    if manifest.get("command") == "follow":
        try:
            requested = float(arguments["seconds"])
            elapsed = float(screenshots[-1]["elapsedSeconds"])
        except (KeyError, TypeError, ValueError):
            return None
        if elapsed >= requested:
            return "duration_limit"
    if manifest.get("command") == "capture":
        try:
            requested_pages = int(arguments["pages"])
        except (KeyError, TypeError, ValueError):
            return None
        if len(screenshots) == requested_pages:
            return "page_limit"
        configuration = manifest.get("configuration", {})
        repeat_limit = int(configuration.get("repeatedScreenLimit", 0))
        trailing = 1
        for left, right in zip(reversed(screenshots[:-1]), reversed(screenshots[1:])):
            if left.get("croppedPixelSha256") != right.get("croppedPixelSha256"):
                break
            trailing += 1
        if repeat_limit > 0 and trailing >= repeat_limit + 1:
            return "repeated_screen_limit"
    return None


def process_existing_run(
    current_config: dict[str, Any],
    run_dir: str | Path,
    *,
    engine_factory: Any = None,
    process_fn: Any = None,
) -> Path:
    run = RunOutput.open_existing(Path(run_dir))
    stored_configuration = run.manifest.get("configuration")
    if not isinstance(stored_configuration, dict):
        raise WearableLogsError("Run manifest has no processing configuration; it cannot be resumed safely.")
    config = json.loads(json.dumps(current_config))
    for key in ("crop", "ocr", "reconstruction"):
        if key not in stored_configuration:
            raise WearableLogsError(f"Run manifest processing configuration is missing {key!r}.")
        config[key] = stored_configuration[key]
    stop_reason_was_inferred = run.manifest.get("stopReason") is None
    inferred_stop_reason = _completed_capture_stop_reason(run.manifest)
    if inferred_stop_reason is None:
        raise WearableLogsError("Capture did not finish, so this run cannot be resumed for OCR processing.")
    if _owner_is_live(run.manifest):
        owner = run.manifest["processingOwner"]
        raise WearableLogsError(f"Run is still owned by live process {owner['pid']}; refusing concurrent processing.")
    _validate_run_screenshots(run, config)
    existing_audit = _read_existing_audit(run)
    if _completed_audit_is_idempotent(run, config, existing_audit):
        if run.manifest.get("failure"):
            run.manifest["recoveredFailure"] = run.manifest["failure"]
            run.manifest["failure"] = None
            run.write_manifest()
        return run.path

    lock_path = run.path / ".processing.lock"
    lock_token = secrets.token_hex(16)
    lock_payload = {"pid": os.getpid(), "token": lock_token, "acquiredAt": timestamp_pair()}
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            existing_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            lock_pid = int(existing_lock.get("pid", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            lock_pid = 0
        if _pid_is_live(lock_pid):
            raise WearableLogsError(f"Run is still owned by live process {lock_pid}; refusing concurrent processing.")
        lock_path.unlink()
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(lock_payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    pre_resume = run.path / "manifest.pre-resume.json"
    resume_started = timestamp_pair()
    prior_state = str(run.manifest.get("state", "unknown"))
    prior_interruption = run.manifest.get("interruption")
    try:
        if not pre_resume.exists():
            shutil.copyfile(run.path / "manifest.json", pre_resume)
        run.manifest["stopReason"] = inferred_stop_reason
        run.manifest["stopReasonInferredDuringResume"] = stop_reason_was_inferred
        if prior_state == "running":
            run.manifest["state"] = "failed"
            run.manifest["failure"] = {
                "type": "UnexpectedWorkerTermination",
                "message": "The previous process terminated without finalizing the run.",
                "stage": "ocr" if run.manifest.get("stopReason") else "capture",
            }
            run.manifest["finishedAt"] = timestamp_pair()
            run.write_manifest()
        history = run.manifest.setdefault("resumeHistory", [])
        attempt: dict[str, Any] = {
            "startedAt": resume_started,
            "toolVersion": str(current_config["toolVersion"]),
            "priorState": prior_state,
            "priorInterruption": prior_interruption,
        }
        history.append(attempt)
        run.manifest["state"] = "processing"
        run.manifest["finishedAt"] = None
        run.manifest["toolVersion"] = str(current_config["toolVersion"])
        run.manifest["resumedAt"] = resume_started
        run.manifest["processingOwner"] = {
            "pid": os.getpid(),
            "token": lock_token,
            "acquiredAt": resume_started,
            "active": True,
        }
        run.write_manifest()
        try:
            normalized, raw_lines, audit = run_ocr(
                run,
                config,
                existing_audit=existing_audit,
                engine_factory=engine_factory or RapidOcrEngine,
                process_fn=process_fn or process_screenshot,
            )
            if not raw_lines:
                raise OcrError("OCR found no text in the configured crop. Check calibration and the captured evidence.")
            if not normalized:
                raise OcrError("OCR found only incomplete edge text; no complete logical log records were produced.")
            run.write_text("logs.txt", normalized)
            attempt["finishedAt"] = timestamp_pair()
            attempt["state"] = "completed"
            attempt["ocrInvocationsTotal"] = audit["statistics"]["ocrInvocations"]
            attempt["ocrResultsCompleted"] = audit["statistics"]["ocrResultsCompleted"]
            attempt["processingDurationMs"] = audit["statistics"]["processingDurationMs"]
            run.manifest["lastResumedAt"] = attempt["finishedAt"]
            if run.manifest.get("failure"):
                run.manifest["recoveredFailure"] = run.manifest["failure"]
                attempt["recoveredFailure"] = run.manifest["failure"]
                run.manifest["failure"] = None
            run.complete()
            return run.path
        except KeyboardInterrupt:
            attempt["finishedAt"] = timestamp_pair()
            attempt["state"] = "interrupted"
            run.interrupt("ocr", bool(run.manifest.get("ocrResultsCompleted", 0)))
            raise
        except Exception as exc:
            attempt["finishedAt"] = timestamp_pair()
            attempt["state"] = "failed"
            run.fail(exc, "ocr")
            raise WearableLogsError(f"Resume failed; preserved evidence and failure manifest: {run.path}\n{exc}") from exc
        except BaseException as exc:
            attempt["finishedAt"] = timestamp_pair()
            attempt["state"] = "failed"
            run.fail(exc, "ocr")
            raise
    finally:
        try:
            current_lock = json.loads(lock_path.read_text(encoding="utf-8"))
            if current_lock.get("token") == lock_token:
                lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            pass


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

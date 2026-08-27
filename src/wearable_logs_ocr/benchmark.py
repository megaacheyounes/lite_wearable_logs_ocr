from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .config import load_config
from .ocr import PIPELINES, RapidOcrEngine, process_screenshot
from .reconstruct import merge_screenshot_records, reconstruct_records, reconstruct_visual_lines


def _run_pipeline(
    fixture_dir: Path, ground_truth: dict[str, Any], config: dict[str, Any], pipeline: str, engine: RapidOcrEngine
) -> list[str]:
    screenshot_records = []
    with tempfile.TemporaryDirectory(prefix="wearable-ocr-benchmark-") as temporary:
        temporary_path = Path(temporary)
        for index, filename in enumerate(ground_truth["screenshots"]):
            detections, image_size, _ = process_screenshot(
                fixture_dir / filename,
                temporary_path / f"{index:04d}-{pipeline}.png",
                config["crop"],
                pipeline,
                engine,
            )
            lines = reconstruct_visual_lines(detections, float(config["ocr"]["lowConfidenceThreshold"]))
            records, _ = reconstruct_records(lines, image_size, config["reconstruction"])
            screenshot_records.append(records)
    return [record.text for record in merge_screenshot_records(screenshot_records, config["reconstruction"]).records]


def _metrics(actual: list[str], truth: dict[str, Any]) -> dict[str, Any]:
    expected = list(truth["expectedRecords"])
    expected_prefixes = list(truth.get("expectedPrefixes", []))
    expected_counts = Counter(expected)
    actual_counts = Counter(actual)
    recovered = sum((expected_counts & actual_counts).values())
    fabricated = list((actual_counts - expected_counts).elements())
    available = set(range(len(actual)))
    prefix_correct = 0
    for prefix in expected_prefixes:
        match = next((index for index in available if actual[index].startswith(prefix)), None)
        if match is not None:
            prefix_correct += 1
            available.remove(match)
    return {
        "expectedRecords": len(expected),
        "actualRecords": len(actual),
        "completeLineRecovery": recovered / len(expected) if expected else 1.0,
        "prefixAccuracy": prefix_correct / len(expected_prefixes) if expected_prefixes else 1.0,
        "fabricatedRecords": fabricated,
        "chronologicalOrderStable": actual == expected,
    }


def evaluate(fixture_dir: Path, pipeline: str | None = None) -> tuple[dict[str, Any], int]:
    truth_path = fixture_dir / "ground-truth.json"
    ground_truth = json.loads(truth_path.read_text(encoding="utf-8"))
    config, _ = load_config(None)
    engine = RapidOcrEngine(config["ocr"])
    candidates = [pipeline] if pipeline else list(PIPELINES)
    results: dict[str, Any] = {}
    outputs: dict[str, list[str]] = {}
    for candidate in candidates:
        first = _run_pipeline(fixture_dir, ground_truth, config, candidate, engine)
        second = _run_pipeline(fixture_dir, ground_truth, config, candidate, engine)
        outputs[candidate] = first
        metrics = _metrics(first, ground_truth)
        metrics["deterministic"] = first == second
        results[candidate] = metrics
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            -results[candidate]["completeLineRecovery"],
            -results[candidate]["prefixAccuracy"],
            len(results[candidate]["fabricatedRecords"]),
            list(PIPELINES).index(candidate),
        ),
    )
    selected = ordered[0]
    selected_metrics = results[selected]
    ready = (
        selected_metrics["completeLineRecovery"] >= 0.95
        and selected_metrics["prefixAccuracy"] >= 0.98
        and not selected_metrics["fabricatedRecords"]
        and selected_metrics["chronologicalOrderStable"]
        and selected_metrics["deterministic"]
    )
    report = {
        "fixture": str(fixture_dir),
        "selectedPipeline": selected,
        "ready": ready,
        "results": results,
        "selectedOutput": outputs[selected],
    }
    return report, 0 if ready else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate OCR pipelines against a private real-screen fixture.")
    parser.add_argument("--fixture-dir", required=True, type=Path)
    parser.add_argument("--pipeline", choices=PIPELINES)
    parser.add_argument("--report", type=Path)
    arguments = parser.parse_args()
    report, status = evaluate(arguments.fixture_dir.resolve(), arguments.pipeline)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    print(rendered, end="")
    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered, encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())

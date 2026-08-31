from __future__ import annotations

import argparse
import sys
from typing import Any

from .config import load_config, parse_normalized_geometry
from .errors import ConfigurationError, WearableLogsError
from .ocr import PIPELINES
from .workflow import apply_overrides, calibrate, doctor, execute_capture, process_existing_run


def _geometry_tokens(values: list[str] | None, label: str) -> list[float] | None:
    if values is None:
        return None
    return parse_normalized_geometry(",".join(values), 4, label)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--serial", help="ADB device serial; required only when more than one device is connected.")
    parser.add_argument("--output-dir", help="Override the root directory for timestamped run outputs.")
    parser.add_argument("--config", help="Override the local configuration file path.")
    parser.add_argument("--crop", nargs="+", help="Normalized left,top,right,bottom crop rectangle.")
    parser.add_argument("--swipe", nargs="+", help="Normalized start-x,start-y,end-x,end-y swipe.")
    parser.add_argument("--interval", type=float, help="Seconds between screenshots or after scrolls.")
    parser.add_argument("--repeat-limit", type=int, help="Consecutive unchanged cropped screens before scrolling stops.")
    parser.add_argument("--swipe-duration-ms", type=int, help="ADB swipe duration in milliseconds.")
    parser.add_argument("--ocr-pipeline", choices=PIPELINES, help="Override the deterministic OCR preprocessing pipeline.")
    parser.add_argument("--verbose", action="store_true", help="Print diagnostic commands and details without log text.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Get-WearableLogs.ps1",
        description="Capture Huawei Lite Wearable logs shown by DevEco Assistant and OCR them locally.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = subparsers.add_parser("doctor", help="Validate the local runtime and connected phone without swiping.")
    _add_common(doctor_parser)

    capture_parser = subparsers.add_parser("capture", help="Capture the current log view and optional bounded scrolling.")
    _add_common(capture_parser)
    capture_parser.add_argument("--pages", type=int, default=1, help="Number of screenshots/pages to request (default: 1).")
    capture_parser.add_argument("--no-swipe", action="store_true", help="Require single-screen mode and issue no swipe.")

    follow_parser = subparsers.add_parser("follow", help="Capture periodically for a bounded duration without swiping.")
    _add_common(follow_parser)
    follow_parser.add_argument("--seconds", type=float, help="Bounded follow duration in seconds.")

    calibrate_parser = subparsers.add_parser("calibrate", help="Save a reference/overlay and write normalized local geometry.")
    _add_common(calibrate_parser)
    process_parser = subparsers.add_parser("process-run", help="Resume validated OCR processing for an existing run.")
    process_parser.add_argument("--run-dir", required=True, help="Existing timestamped run directory to process.")
    process_parser.add_argument("--config", help="Override the local configuration file path for runtime defaults.")
    process_parser.add_argument("--verbose", action="store_true", help="Print diagnostic details without log text.")
    return parser


def _validate(arguments: Any, config: dict[str, Any]) -> None:
    if getattr(arguments, "interval", None) is not None and not 0.5 <= arguments.interval <= 60:
        raise ConfigurationError("--interval must be between 0.5 and 60 seconds.")
    if getattr(arguments, "repeat_limit", None) is not None and arguments.repeat_limit < 1:
        raise ConfigurationError("--repeat-limit must be at least 1.")
    if getattr(arguments, "swipe_duration_ms", None) is not None and not 50 <= arguments.swipe_duration_ms <= 5000:
        raise ConfigurationError("--swipe-duration-ms must be between 50 and 5000.")
    if arguments.command == "capture":
        if not 1 <= arguments.pages <= int(config["maximumPages"]):
            raise ConfigurationError(f"--pages must be between 1 and {config['maximumPages']}.")
        if arguments.no_swipe and arguments.pages != 1:
            raise ConfigurationError("--no-swipe cannot be combined with --pages greater than 1.")
    if arguments.command == "follow":
        if arguments.seconds is None:
            arguments.seconds = float(config["defaultFollowSeconds"])
        if not 1 <= arguments.seconds <= float(config["maximumDurationSeconds"]):
            raise ConfigurationError(
                f"--seconds must be between 1 and {config['maximumDurationSeconds']}."
            )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args(argv)
        arguments.crop = _geometry_tokens(getattr(arguments, "crop", None), "crop")
        arguments.swipe = _geometry_tokens(getattr(arguments, "swipe", None), "swipe")
        config, local_path = load_config(arguments.config)
        config = apply_overrides(config, arguments)
        if getattr(arguments, "ocr_pipeline", None):
            config["ocr"]["pipeline"] = arguments.ocr_pipeline
        _validate(arguments, config)
        if arguments.command == "doctor":
            return doctor(config, arguments)
        if arguments.command == "process-run":
            result = process_existing_run(config, arguments.run_dir)
            print(f"Completed: {result}")
            return 0
        if arguments.command in ("capture", "follow"):
            result = execute_capture(config, arguments, arguments.command)
        else:
            result = calibrate(config, local_path, arguments)
        print(f"Completed: {result}")
        return 0
    except (WearableLogsError, ConfigurationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: Interrupted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

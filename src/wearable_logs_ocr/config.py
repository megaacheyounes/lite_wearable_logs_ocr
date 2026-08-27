from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


def project_root() -> Path:
    configured = os.environ.get("WEARABLE_LOGS_ROOT")
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(explicit_path: str | None = None) -> tuple[dict[str, Any], Path]:
    root = project_root()
    default_path = root / "config" / "default.json"
    try:
        defaults = json.loads(default_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read default configuration: {default_path}: {exc}") from exc

    local_path = Path(explicit_path).expanduser() if explicit_path else root / defaults["localConfigPath"]
    if not local_path.is_absolute():
        local_path = root / local_path
    config = defaults
    if local_path.exists():
        try:
            config = _deep_merge(config, json.loads(local_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Cannot read local configuration: {local_path}: {exc}") from exc
    validate_config(config)
    return config, local_path


def parse_normalized_geometry(value: str, expected: int, label: str) -> list[float]:
    try:
        result = [float(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise ConfigurationError(f"{label} must be {expected} comma-separated numbers between 0 and 1.") from exc
    if len(result) != expected or any(number < 0 or number > 1 for number in result):
        raise ConfigurationError(f"{label} must be {expected} comma-separated numbers between 0 and 1.")
    if label == "crop" and (result[0] >= result[2] or result[1] >= result[3]):
        raise ConfigurationError("crop must be left,top,right,bottom with left < right and top < bottom.")
    return result


def validate_config(config: dict[str, Any]) -> None:
    crop = config.get("crop")
    swipe = config.get("swipe")
    if not isinstance(crop, list) or len(crop) != 4:
        raise ConfigurationError("Configuration crop must contain four normalized coordinates.")
    if not isinstance(swipe, list) or len(swipe) != 4:
        raise ConfigurationError("Configuration swipe must contain four normalized coordinates.")
    parse_normalized_geometry(",".join(map(str, crop)), 4, "crop")
    parse_normalized_geometry(",".join(map(str, swipe)), 4, "swipe")
    if int(config.get("maximumPages", 0)) < 1:
        raise ConfigurationError("maximumPages must be at least 1.")
    if float(config.get("maximumDurationSeconds", 0)) <= 0:
        raise ConfigurationError("maximumDurationSeconds must be positive.")


def resolve_output_root(config: dict[str, Any], override: str | None) -> Path:
    value = Path(override).expanduser() if override else Path(config["outputDirectory"])
    return value.resolve() if value.is_absolute() else (project_root() / value).resolve()


def write_local_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)

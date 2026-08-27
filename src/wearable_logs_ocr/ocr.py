from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from .errors import OcrError
from .models import OcrDetection


PIPELINES = ("crop", "upscale2x", "grayscale-clahe2x", "adaptive-threshold2x")


def normalized_crop_box(size: tuple[int, int], crop: list[float]) -> tuple[int, int, int, int]:
    width, height = size
    left, top, right, bottom = crop
    box = (round(left * width), round(top * height), round(right * width), round(bottom * height))
    if box[0] >= box[2] or box[1] >= box[3]:
        raise OcrError(f"Normalized crop produced an empty rectangle for image size {width}x{height}.")
    return box


def crop_image(image: Image.Image, crop: list[float]) -> Image.Image:
    return image.crop(normalized_crop_box(image.size, crop))


def preprocess(image: Image.Image, crop: list[float], pipeline: str) -> Image.Image:
    if pipeline not in PIPELINES:
        raise OcrError(f"Unknown OCR preprocessing pipeline {pipeline!r}.")
    cropped = crop_image(image.convert("RGB"), crop)
    if pipeline == "crop":
        return cropped
    if pipeline == "upscale2x":
        return cropped.resize((cropped.width * 2, cropped.height * 2), Image.Resampling.LANCZOS)
    gray = np.asarray(ImageOps.grayscale(cropped))
    if pipeline == "grayscale-clahe2x":
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    else:
        gray = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9
        )
    output = Image.fromarray(gray)
    return output.resize((output.width * 2, output.height * 2), Image.Resampling.LANCZOS)


class RapidOcrEngine:
    def __init__(self, ocr_config: dict[str, Any] | None = None) -> None:
        try:
            from rapidocr import RapidOCR
            from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

            def parameters(selection: dict[str, Any]) -> dict[str, Any]:
                language = str(selection.get("language", "ch_en")).lower()
                version = str(selection.get("recognitionModelVersion", "PP-OCRv6"))
                model_type = str(selection.get("recognitionModelType", "small"))
                language_map = {"ch_en": LangRec.CH, "ch": LangRec.CH, "latin": LangRec.LATIN, "en": LangRec.EN}
                version_map = {item.value: item for item in OCRVersion}
                type_map = {item.value: item for item in ModelType}
                if language not in language_map or version not in version_map or model_type not in type_map:
                    raise OcrError(
                        f"Unsupported RapidOCR model selection: language={language}, version={version}, type={model_type}."
                    )
                return {
                    "Rec.lang_type": language_map[language],
                    "Rec.ocr_version": version_map[version],
                    "Rec.model_type": type_map[model_type],
                }
            selection = ocr_config or {}
            self._engine = RapidOCR(params=parameters(selection) if ocr_config else None)
            assistant = selection.get("prefixAssistant", {})
            self._prefix_engine = RapidOCR(params=parameters(assistant)) if assistant.get("enabled") else None
            self._prefix_marker = str(assistant.get("endMarker", "APP:"))
        except Exception as exc:
            raise OcrError(
                "RapidOCR could not initialize in the project environment. Rerun setup or inspect doctor --verbose. "
                f"Details: {exc}"
            ) from exc

    def model_info(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        engines = [("primary", self._engine)]
        if self._prefix_engine is not None:
            engines.append(("prefix-assistant", self._prefix_engine))
        seen: set[tuple[str, str]] = set()
        for engine_role, engine in engines:
            for component_role, component in (
                ("detection", engine.text_det),
                ("classification", engine.text_cls),
                ("recognition", engine.text_rec),
            ):
                model_path = Path(component.session.session._model_path)
                key = (engine_role if component_role == "recognition" else "shared", str(model_path))
                if key in seen:
                    continue
                seen.add(key)
                digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
                result.append(
                    {
                        "role": f"{engine_role}-{component_role}",
                        "file": model_path.name,
                        "sha256": digest,
                        "sizeBytes": model_path.stat().st_size,
                    }
                )
        return result

    def _merge_prefix_witnesses(
        self, primary: Any, alternate: Any
    ) -> tuple[list[str], list[float], list[dict[str, Any]]]:
        selected = [str(text) for text in primary.txts]
        selected_scores = [float(score) for score in primary.scores]
        decisions: list[dict[str, Any]] = []
        if len(primary.txts) != len(alternate.txts):
            return selected, selected_scores, [
                {"decision": "not_aligned", "primaryCount": len(primary.txts), "alternateCount": len(alternate.txts)}
            ]
        for index, (primary_text, alternate_text) in enumerate(zip(primary.txts, alternate.txts)):
            if self._prefix_marker not in primary_text or self._prefix_marker not in alternate_text:
                continue
            primary_end = primary_text.index(self._prefix_marker) + len(self._prefix_marker)
            alternate_end = alternate_text.index(self._prefix_marker) + len(self._prefix_marker)
            primary_prefix = primary_text[:primary_end]
            alternate_prefix = alternate_text[:alternate_end]
            if re.sub(r"\s+", "", primary_prefix) != re.sub(r"\s+", "", alternate_prefix):
                continue
            if len(alternate_prefix.split()) <= len(primary_prefix.split()):
                continue
            selected[index] = alternate_prefix + primary_text[primary_end:]
            selected_scores[index] = min(float(primary.scores[index]), float(alternate.scores[index]))
            decisions.append(
                {
                    "detection": index,
                    "decision": "used_prefix_assistant_whitespace",
                    "primary": primary_text,
                    "alternate": alternate_text,
                    "selected": selected[index],
                    "primaryConfidence": float(primary.scores[index]),
                    "alternateConfidence": float(alternate.scores[index]),
                    "selectedConfidence": selected_scores[index],
                }
            )
        return selected, selected_scores, decisions

    def recognize(self, image: Image.Image) -> tuple[list[OcrDetection], dict[str, Any]]:
        try:
            pixels = np.asarray(image)
            result = self._engine(pixels)
            alternate = self._prefix_engine(pixels) if self._prefix_engine is not None else None
        except Exception as exc:
            raise OcrError(f"RapidOCR failed: {exc}") from exc
        if result is None or result.txts is None:
            return [], {"elapsed": None}
        texts = [str(text) for text in result.txts]
        scores = [float(score) for score in result.scores]
        prefix_decisions: list[dict[str, Any]] = []
        if alternate is not None and alternate.txts is not None:
            texts, scores, prefix_decisions = self._merge_prefix_witnesses(result, alternate)
        detections = [
            OcrDetection(
                text=str(text),
                confidence=float(score),
                bbox=[[float(point[0]), float(point[1])] for point in box],
            )
            for box, text, score in zip(result.boxes, texts, scores)
        ]
        return detections, {
            "elapsed": float(result.elapse) if isinstance(result.elapse, (int, float)) else result.elapse,
            "engine": "rapidocr",
            "prefixAssistantDecisions": prefix_decisions,
        }


def process_screenshot(
    screenshot_path: Path,
    processed_path: Path,
    crop: list[float],
    pipeline: str,
    engine: RapidOcrEngine,
) -> tuple[list[OcrDetection], tuple[int, int], dict[str, Any]]:
    with Image.open(screenshot_path) as source:
        image = preprocess(source, crop, pipeline)
    image.save(processed_path, format="PNG")
    detections, metadata = engine.recognize(image)
    metadata["pipeline"] = pipeline
    metadata["processedSize"] = {"width": image.width, "height": image.height}
    metadata["coordinateSpace"] = "processed-image-pixels"
    return detections, image.size, metadata

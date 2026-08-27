from __future__ import annotations

import pytest

from wearable_logs_ocr.config import parse_normalized_geometry
from wearable_logs_ocr.errors import ConfigurationError
from wearable_logs_ocr.ocr import normalized_crop_box


def test_normalized_crop_scales_to_different_phone_resolutions() -> None:
    crop = [0.1, 0.2, 0.9, 0.8]
    assert normalized_crop_box((1080, 2400), crop) == (108, 480, 972, 1920)
    assert normalized_crop_box((1280, 2832), crop) == (128, 566, 1152, 2266)


@pytest.mark.parametrize(
    "value",
    ["0,0,1", "-0.1,0,1,1", "0,0,1,1.1", "0.8,0,0.2,1", "hello,0,1,1"],
)
def test_invalid_crop_is_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_normalized_geometry(value, 4, "crop")


def test_valid_unicode_independent_geometry() -> None:
    assert parse_normalized_geometry("0.05, 0.1, 0.95, 0.9", 4, "crop") == [0.05, 0.1, 0.95, 0.9]

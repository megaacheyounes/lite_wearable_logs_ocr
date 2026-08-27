from __future__ import annotations

from copy import deepcopy

from wearable_logs_ocr.config import load_config
from wearable_logs_ocr.models import LogicalRecord, OcrDetection
from wearable_logs_ocr.reconstruct import (
    merge_screenshot_records,
    reconstruct_records,
    reconstruct_visual_lines,
)


def box(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def detection(text: str, top: float, right: float, confidence: float = 0.99) -> OcrDetection:
    return OcrDetection(text, confidence, box(45, top, right, top + 55))


def reconstruction_config() -> dict:
    config, _ = load_config(None)
    return deepcopy(config["reconstruction"])


def record(text: str) -> LogicalRecord:
    return LogicalRecord(text, 0.99, [0], False)


def test_visual_order_and_wrapped_logical_record() -> None:
    detections = [
        detection('0"', 335, 107),
        detection("27 14:14:49 0 0 E 31/", 112, 528),
        detection('alipay_nfc_offline_pay.c alipay_npay', 223, 840),
        detection('APP: ALIPAY:[ALIPAY][INFO]', 164, 665),
        detection('_get_quick_pay_flag[1207]:"get flag:', 283, 838),
    ]
    lines = reconstruct_visual_lines(detections, 0.8)
    records, decisions = reconstruct_records(lines, (960, 1900), reconstruction_config())
    assert [line.text for line in lines] == [
        "27 14:14:49 0 0 E 31/",
        "APP: ALIPAY:[ALIPAY][INFO]",
        "alipay_nfc_offline_pay.c alipay_npay",
        '_get_quick_pay_flag[1207]:"get flag:',
        '0"',
    ]
    assert records[0].text == (
        '27 14:14:49 0 0 E 31/ APP: ALIPAY:[ALIPAY][INFO] '
        'alipay_nfc_offline_pay.c alipay_npay_get_quick_pay_flag[1207]:"get flag:0"'
    )
    assert [decision["reason"] for decision in decisions] == [
        "soft_or_explicit_break",
        "soft_or_explicit_break",
        "hard_wrap_continuation",
        "hard_wrap_continuation",
    ]


def test_hard_wrap_between_words_restores_one_space() -> None:
    lines = reconstruct_visual_lines(
        [
            detection("27 14:14:52 0 0 I 31/APP: [Console", 100, 840),
            detection("Info] Application onCreate", 160, 600),
        ],
        0.8,
    )
    records, decisions = reconstruct_records(lines, (960, 1900), reconstruction_config())
    assert records[0].text.endswith("[Console Info] Application onCreate")
    assert decisions[0]["reason"] == "hard_wrap_word_boundary"


def test_punctuation_json_numbers_and_paths_are_unchanged() -> None:
    text = '27 09:01:02 0 0 E 31/ APP:[DEBUG] {"delta":-12.50,"path":"C:\\\\logs[a].txt"}'
    lines = reconstruct_visual_lines([detection(text, 100, 700)], 0.8)
    records, _ = reconstruct_records(lines, (960, 1900), reconstruction_config())
    assert records[0].text == text


def test_low_confidence_line_is_preserved_and_marked() -> None:
    lines = reconstruct_visual_lines([detection("27 09:01:02 uncertain", 100, 500, 0.42)], 0.8)
    assert lines[0].text == "27 09:01:02 uncertain"
    assert lines[0].low_confidence is True
    records, _ = reconstruct_records(lines, (960, 1900), reconstruction_config())
    assert records[0].low_confidence is True


def test_bottom_clipped_record_is_audited_but_excluded_from_normalized_output() -> None:
    lines = reconstruct_visual_lines([detection("27 09:01:02 partial", 1840, 500)], 0.8)
    records, _ = reconstruct_records(lines, (960, 1900), reconstruction_config())
    assert records[0].edge_state == "partial_bottom"
    merged = merge_screenshot_records([records], reconstruction_config())
    assert merged.records == []
    assert merged.decisions[0]["decision"] == "excluded_incomplete_edge"


def test_overlap_dedup_preserves_legitimate_repeats_and_order() -> None:
    first = [record("27 10:00:00 A"), record("27 10:00:01 B"), record("27 10:00:01 B"), record("27 10:00:02 C")]
    second = [record("27 10:00:01 B"), record("27 10:00:02 C"), record("27 10:00:03 D")]
    merged = merge_screenshot_records([first, second], reconstruction_config())
    assert [item.text for item in merged.records] == [
        "27 10:00:00 A",
        "27 10:00:01 B",
        "27 10:00:01 B",
        "27 10:00:02 C",
        "27 10:00:03 D",
    ]
    assert [decision["decision"] for decision in merged.decisions].count("deduplicated_overlap") == 2


def test_single_fuzzy_record_is_not_removed() -> None:
    first = [record("27 10:00:00 value=1234")]
    second = [record("27 10:00:00 value=123S")]
    config = reconstruction_config()
    config["fuzzyOverlapThreshold"] = 0.9
    merged = merge_screenshot_records([first, second], config)
    assert len(merged.records) == 2


def test_two_record_fuzzy_overlap_is_deterministically_removed() -> None:
    first = [record("27 10:00:00 value=1234"), record("27 10:00:01 path=/a/b/c")]
    second = [record("27 10:00:00 value=123S"), record("27 10:00:01 path=/a/b/c"), record("27 10:00:02 next")]
    config = reconstruction_config()
    config["fuzzyOverlapThreshold"] = 0.9
    merged = merge_screenshot_records([first, second], config)
    assert [item.text for item in merged.records][-1] == "27 10:00:02 next"
    assert len(merged.records) == 3

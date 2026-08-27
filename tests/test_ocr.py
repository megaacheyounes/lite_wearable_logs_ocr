from __future__ import annotations

from types import SimpleNamespace

from wearable_logs_ocr.ocr import RapidOcrEngine


def result(*texts: str) -> SimpleNamespace:
    return SimpleNamespace(txts=texts, scores=tuple(0.99 for _ in texts))


def engine_without_models() -> RapidOcrEngine:
    engine = object.__new__(RapidOcrEngine)
    engine._prefix_marker = "APP:"
    return engine


def test_prefix_assistant_can_supply_observed_whitespace_only() -> None:
    primary = result("27 14:14:52 0 0 I31/APP: [Console")
    alternate = result("27 14:14:52 0 0 I 31/APP: [Console")
    selected, scores, decisions = engine_without_models()._merge_prefix_witnesses(primary, alternate)
    assert selected == ["27 14:14:52 0 0 I 31/APP: [Console"]
    assert scores == [0.99]
    assert decisions[0]["decision"] == "used_prefix_assistant_whitespace"


def test_prefix_assistant_never_replaces_changed_characters() -> None:
    primary = result("27 14:14:52 0 0 I31/APP: [Console")
    alternate = result("27 14:14:52 0 0 1 31/APP: [Console")
    selected, _, decisions = engine_without_models()._merge_prefix_witnesses(primary, alternate)
    assert selected == list(primary.txts)
    assert decisions == []


def test_prefix_assistant_requires_aligned_detection_count() -> None:
    selected, _, decisions = engine_without_models()._merge_prefix_witnesses(result("A"), result("A", "B"))
    assert selected == ["A"]
    assert decisions[0]["decision"] == "not_aligned"

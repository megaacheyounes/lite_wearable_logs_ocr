from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median
from typing import Any

from .models import LogicalRecord, OcrDetection, VisualLine


def _rectangle(detections: list[OcrDetection]) -> list[list[float]]:
    left = min(item.left for item in detections)
    top = min(item.top for item in detections)
    right = max(item.right for item in detections)
    bottom = max(item.bottom for item in detections)
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def reconstruct_visual_lines(
    detections: list[OcrDetection], low_confidence_threshold: float
) -> list[VisualLine]:
    if not detections:
        return []
    indexed = sorted(enumerate(detections), key=lambda pair: ((pair[1].top + pair[1].bottom) / 2, pair[1].left))
    median_height = median(item.height for _, item in indexed)
    row_tolerance = max(3.0, median_height * 0.45)
    rows: list[list[tuple[int, OcrDetection]]] = []
    row_centers: list[float] = []
    for index, detection in indexed:
        center = (detection.top + detection.bottom) / 2
        if rows and abs(center - row_centers[-1]) <= row_tolerance:
            rows[-1].append((index, detection))
            row_centers[-1] = sum((item.top + item.bottom) / 2 for _, item in rows[-1]) / len(rows[-1])
        else:
            rows.append([(index, detection)])
            row_centers.append(center)

    visual_lines: list[VisualLine] = []
    for row in rows:
        row.sort(key=lambda pair: pair[1].left)
        parts: list[str] = []
        previous: OcrDetection | None = None
        for _, detection in row:
            if previous is not None:
                average_char_width = previous.height * 0.55
                if detection.left - previous.right > average_char_width * 0.5:
                    parts.append(" ")
            parts.append(detection.text)
            previous = detection
        items = [item for _, item in row]
        confidence = min(item.confidence for item in items)
        visual_lines.append(
            VisualLine(
                text="".join(parts),
                confidence=confidence,
                bbox=_rectangle(items),
                detections=[index for index, _ in row],
                low_confidence=confidence < low_confidence_threshold,
            )
        )
    return visual_lines


def reconstruct_records(
    lines: list[VisualLine], image_size: tuple[int, int], reconstruction: dict[str, Any]
) -> tuple[list[LogicalRecord], list[dict[str, Any]]]:
    if not lines:
        return [], []
    timestamp = re.compile(str(reconstruction["timestampPattern"]))
    starts = [index for index, line in enumerate(lines) if timestamp.search(line.text)]
    records: list[LogicalRecord] = []
    join_decisions: list[dict[str, Any]] = []
    groups: list[tuple[list[int], str | None]] = []
    if not starts:
        groups = [([index], None) for index in range(len(lines))]
    else:
        if starts[0] > 0:
            groups.append((list(range(0, starts[0])), "partial_top"))
        for position, start in enumerate(starts):
            end = starts[position + 1] if position + 1 < len(starts) else len(lines)
            groups.append((list(range(start, end)), None))

    image_width, image_height = image_size
    median_height = median(line.height for line in lines)
    bottom_margin = median_height * float(reconstruction["edgeMarginLineHeights"])
    wrap_boundary = image_width * float(reconstruction["hardWrapRightRatio"])
    for indices, initial_edge in groups:
        selected = [lines[index] for index in indices]
        edge_state = initial_edge
        if edge_state is None and selected[-1].bottom >= image_height - bottom_margin:
            edge_state = "partial_bottom"
        text = selected[0].text
        for previous_index, current_index in zip(indices, indices[1:]):
            previous = lines[previous_index]
            current = lines[current_index]
            if text.endswith((" ", "\t")) or current.text.startswith((" ", "\t")):
                separator = ""
                reason = "existing_whitespace"
            elif previous.right >= wrap_boundary:
                previous_character = previous.text[-1:]
                current_character = current.text[:1]
                if previous_character.isalnum() and current_character.isalnum():
                    separator = " "
                    reason = "hard_wrap_word_boundary"
                else:
                    separator = ""
                    reason = "hard_wrap_continuation"
            else:
                separator = " "
                reason = "soft_or_explicit_break"
            text += separator + current.text
            join_decisions.append(
                {
                    "fromVisualLine": previous_index,
                    "toVisualLine": current_index,
                    "separator": separator,
                    "reason": reason,
                    "previousRight": previous.right,
                    "hardWrapBoundary": wrap_boundary,
                }
            )
        confidence = min(line.confidence for line in selected)
        records.append(
            LogicalRecord(
                text=text,
                confidence=confidence,
                visual_line_indices=indices,
                low_confidence=any(line.low_confidence for line in selected),
                edge_state=edge_state,
            )
        )
    return records, join_decisions


def _timestamp(text: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _record_match(
    left: LogicalRecord,
    right: LogicalRecord,
    timestamp_pattern: re.Pattern[str],
    threshold: float,
) -> tuple[bool, str, float]:
    if left.text == right.text:
        return True, "exact", 1.0
    left_timestamp = _timestamp(left.text, timestamp_pattern)
    right_timestamp = _timestamp(right.text, timestamp_pattern)
    if not left_timestamp or left_timestamp != right_timestamp:
        return False, "none", 0.0
    ratio = SequenceMatcher(None, left.text, right.text, autojunk=False).ratio()
    return ratio >= threshold, "fuzzy" if ratio >= threshold else "none", ratio


@dataclass
class MergeResult:
    records: list[LogicalRecord]
    decisions: list[dict[str, Any]]


def merge_screenshot_records(
    screenshots: list[list[LogicalRecord]], reconstruction: dict[str, Any]
) -> MergeResult:
    output: list[LogicalRecord] = []
    decisions: list[dict[str, Any]] = []
    timestamp_pattern = re.compile(str(reconstruction["timestampPattern"]))
    threshold = float(reconstruction["fuzzyOverlapThreshold"])
    minimum_fuzzy = int(reconstruction["minimumFuzzyOverlapRecords"])
    for screenshot_index, all_records in enumerate(screenshots):
        current = [record for record in all_records if record.edge_state is None]
        for record_index, record in enumerate(all_records):
            if record.edge_state:
                decisions.append(
                    {
                        "screenshot": screenshot_index,
                        "record": record_index,
                        "decision": "excluded_incomplete_edge",
                        "reason": record.edge_state,
                    }
                )
        overlap = 0
        overlap_kind = "none"
        overlap_scores: list[float] = []
        for candidate in range(min(len(output), len(current)), 0, -1):
            comparisons = [
                _record_match(left, right, timestamp_pattern, threshold)
                for left, right in zip(output[-candidate:], current[:candidate])
            ]
            if not all(matched for matched, _, _ in comparisons):
                continue
            contains_fuzzy = any(kind == "fuzzy" for _, kind, _ in comparisons)
            if contains_fuzzy and candidate < minimum_fuzzy:
                continue
            overlap = candidate
            overlap_kind = "fuzzy" if contains_fuzzy else "exact"
            overlap_scores = [score for _, _, score in comparisons]
            break
        for index, record in enumerate(current):
            if index < overlap:
                decisions.append(
                    {
                        "screenshot": screenshot_index,
                        "record": index,
                        "decision": "deduplicated_overlap",
                        "kind": overlap_kind,
                        "similarity": overlap_scores[index],
                    }
                )
            else:
                output.append(record)
                decisions.append(
                    {"screenshot": screenshot_index, "record": index, "decision": "kept"}
                )
    return MergeResult(output, decisions)

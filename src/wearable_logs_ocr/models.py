from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class OcrDetection:
    text: str
    confidence: float
    bbox: list[list[float]]

    @property
    def left(self) -> float:
        return min(point[0] for point in self.bbox)

    @property
    def right(self) -> float:
        return max(point[0] for point in self.bbox)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.bbox)

    @property
    def bottom(self) -> float:
        return max(point[1] for point in self.bbox)

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class VisualLine:
    text: str
    confidence: float
    bbox: list[list[float]]
    detections: list[int] = field(default_factory=list)
    low_confidence: bool = False

    @property
    def left(self) -> float:
        return min(point[0] for point in self.bbox)

    @property
    def right(self) -> float:
        return max(point[0] for point in self.bbox)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.bbox)

    @property
    def bottom(self) -> float:
        return max(point[1] for point in self.bbox)

    @property
    def height(self) -> float:
        return max(1.0, self.bottom - self.top)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LogicalRecord:
    text: str
    confidence: float
    visual_line_indices: list[int]
    low_confidence: bool
    edge_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

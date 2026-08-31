# Changelog

## 2026-08-28 — 0.2.0

- Deduplicated OCR work by exact cropped-pixel SHA-256 while preserving every scheduled raw screenshot.
- Added chronological crop transitions and per-screenshot representative/reuse relationships to the OCR audit.
- Added incremental partial OCR output and correctly finalized capture/OCR interruptions with exit code 130.
- Added `process-run` with screenshot validation, live-owner protection, reusable completed OCR results, resume history, and idempotent completion.
- Added manifest processing counters and deterministic interruption/resume coverage.

## 2026-08-27 — 0.1.0

- Added the PowerShell bootstrap and local pinned Python 3.12 environment.
- Added ADB doctor, single/multi-page capture, bounded follow, and normalized calibration.
- Added RapidOCR/ONNX Runtime extraction with auditable prefix-model assistance.
- Added deterministic wrapped-record reconstruction, edge handling, overlap deduplication, and repeated-screen stopping.
- Added timestamped evidence outputs, fixture tests, real-screen benchmark evaluation, and maintenance documentation.

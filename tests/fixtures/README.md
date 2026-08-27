# Fixtures

Automated unit/integration tests construct deterministic synthetic screenshots and OCR detections at runtime. Real DevEco Assistant screenshots and their manual transcripts live under `private/`, which is intentionally ignored because it contains captured device log evidence.

A private fixture directory contains `ground-truth.json` plus the PNG filenames listed in its `screenshots` array. Never derive ground truth by copying OCR output; transcribe and verify the visible records manually.

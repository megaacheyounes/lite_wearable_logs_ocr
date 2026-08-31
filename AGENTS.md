# Project maintenance rules

## Safety and scope

- These are Lite Wearable logs displayed by DevEco Assistant. Never substitute Android `adb logcat`.
- Keep OCR local. Do not send screenshots, OCR text, or manifests to cloud services.
- Never issue destructive ADB commands, press Assistant Clear/Stop, clear app data, uninstall packages, or alter stored logs.
- Do not automate navigation to a remembered activity. Require the developer to open the Logs screen manually.
- Preserve existing runs, private fixtures, local configuration, and user changes. Never modify raw screenshots in a timestamped run. Only `process-run` may finalize an existing run's manifest and derived OCR/text outputs after validating its evidence and saving `manifest.pre-resume.json`.
- Do not commit `runs/`, `.venv/`, OCR model caches, `config/local.json`, or `tests/fixtures/private/`.

## Contracts

- Keep `Get-WearableLogs.ps1` commands and existing long option names backward compatible after `0.1.0`.
- Preserve UTF-8 `logs.txt`, `logs.raw.txt`, `manifest.json`, `ocr-results.json`, `screenshots/`, and `processed/`.
- Preserve raw screenshots byte-for-byte before deriving any crop or processed image.
- Keep every scheduled `follow` screenshot. Deduplicate OCR only by the exact `croppedPixelSha256`; never use perceptual/fuzzy image hashes to skip OCR.
- Preserve capture order. Do not alphabetically sort or reorder by parsed timestamps.
- Preserve chronological crop transitions, including non-consecutive returns to a prior exact crop state, and audit every representative/reuse relationship.
- Never silently correct OCR characters or values. Any multi-model selection, line join, edge exclusion, or deduplication must be deterministic and recorded in `ocr-results.json`.
- Keep low-confidence OCR text; flag it rather than removing it.
- Use normalized geometry and test at least two resolutions.

## Validation

Run from the repository root:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m wearable_logs_ocr.benchmark --fixture-dir .\tests\fixtures\private\deveco-live-1080x2400 --pipeline crop
.\Get-WearableLogs.ps1 doctor
git diff --check
```

Run `capture --no-swipe` on a physical phone when one is available. State explicitly which checks were fixture-only and which used a physical device. Do not call a release ready below 95% complete-line recovery, 98% prefix accuracy, zero fabricated records, stable chronology, and deterministic output.

If a real fixture is unavailable, tests may pass but benchmark readiness is unverified. Do not weaken thresholds or manufacture ground truth from OCR output.

## Dependency and behavior changes

- Pin every runtime/test dependency in `requirements.lock.txt`; the wrapper refreshes `.venv` when its SHA-256 changes.
- Validate every image transform against the real private fixture before changing the default pipeline.
- Update README and CHANGELOG whenever commands, outputs, configuration, models, or heuristics change.
- Keep modules small and typed where practical; inject ADB/OCR boundaries so offline tests remain possible.
- Finalize Ctrl+C as `interrupted` and unexpected worker termination as `failed`; never knowingly leave a terminated run in `running` state.

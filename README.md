# Lite Wearable Log OCR

A Windows-only, local OCR bridge for logs displayed in the Huawei DevEco Assistant Android app. These are Huawei Lite Wearable logs, **not Android logcat**. The tool captures the phone screen through ADB, runs RapidOCR locally, and retains the screenshots and OCR decisions needed to audit every result.

## Quick start

Requirements:

- Windows PowerShell or PowerShell 7
- Python 3.12
- USB debugging enabled on the Android phone
- DevEco Assistant installed and open on its Logs screen
- One authorized phone, or an explicit ADB serial

The first command creates `.venv` and installs the exact versions in `requirements.lock.txt`. It does not change global Python environments. Package/model downloads need network access once; screenshots and log text are never uploaded and inference is local.

```powershell
.\Get-WearableLogs.ps1 doctor
.\Get-WearableLogs.ps1 capture
.\Get-WearableLogs.ps1 capture --pages 20
.\Get-WearableLogs.ps1 follow --seconds 120
.\Get-WearableLogs.ps1 calibrate
```

`capture` defaults to one screenshot and does not swipe unless `--pages` is greater than one. `follow` never swipes; it takes screenshots on a monotonic schedule and performs OCR after the capture window.

Every scheduled `follow` screenshot is retained for audit. Post-processing groups screenshots by the exact cropped-pixel SHA-256, invokes OCR once per unique crop, and reuses that result for exact matches. It does not use fuzzy or perceptual image hashes, and an unchanged screen never ends a bounded follow early. Consecutive crop states are collapsed in the transition audit while later non-consecutive returns remain chronological (for example, `A,A,B,A` is recorded as `A,B,A`).

Useful options:

```powershell
.\Get-WearableLogs.ps1 capture --serial RFCT305F7TH --no-swipe
.\Get-WearableLogs.ps1 capture --output-dir "D:\OCR evidence العربية" --pages 5
.\Get-WearableLogs.ps1 follow --seconds 120 --interval 2
.\Get-WearableLogs.ps1 capture --crop 0.0556,0.1458,0.9444,0.9375
.\Get-WearableLogs.ps1 capture --swipe 0.5,0.78,0.5,0.32 --swipe-duration-ms 450
```

Run any command with `--verbose` for ADB diagnostics. Diagnostic output never prints captured log text.

## Calibration

Coordinates are normalized from `0` to `1`, so the same configuration scales to different resolutions. The crop order is `left,top,right,bottom`; swipe order is `start-x,start-y,end-x,end-y`.

```powershell
.\Get-WearableLogs.ps1 calibrate `
  --crop 0.0556,0.1458,0.9444,0.9375 `
  --swipe 0.5,0.78,0.5,0.32
```

Calibration saves the untouched screenshot and a red-crop/blue-swipe overlay in a timestamped run, then writes the selected geometry to ignored `config/local.json`. It performs no swipe.

After an Assistant APK UI change or for a new phone resolution:

1. Open the Logs screen with visible records.
2. Run `calibrate` with candidate normalized geometry.
3. Inspect `processed/calibration-overlay.png` in the printed run directory.
4. Repeat with adjusted values if the red rectangle includes the header/navigation bar or cuts into the log pane.
5. Run a one-screen capture and the private fixture benchmark before accepting the change.

Configuration precedence is `config/default.json`, then local configuration, then command-line options. Use `--config <path>` to select a different local configuration. Page, duration, interval, and swipe limits are rejected when invalid rather than silently changed.

## Outputs

Every capture/follow/calibration creates a unique UTC timestamp plus random-suffix directory under `runs/` unless `--output-dir` is supplied. Earlier runs are never overwritten.

- `logs.txt` — one complete logical record per line, in capture order, with overlap removed.
- `logs.raw.txt` — OCR visual rows exactly as returned, in capture order.
- `manifest.json` — arguments, timestamps, versions, Git/device/Assistant metadata, configuration, model hashes, screenshot hashes, confidence summary, processing counters, warnings, and final state.
- `ocr-results.json` — exact crop states, chronological transitions, per-screenshot reuse relationships, detections, confidence, bounding boxes, wrapped-line joins, model-witness decisions, edge exclusions, and overlap decisions.
- `screenshots/` — original ADB PNG bytes.
- `processed/` — only derived images actually supplied to OCR.

Text and JSON are UTF-8. A record cut by the top or bottom of a screenshot remains in `logs.raw.txt` and the audit JSON but is marked and omitted from `logs.txt`. Low-confidence text is never discarded; it is preserved and flagged in the audit and manifest.

If capture or OCR is interrupted with Ctrl+C, the command exits with code 130 and finalizes the manifest as `interrupted`. Completed screenshots always remain untouched. Completed OCR results and `logs.raw.txt` are saved as partial output; `logs.txt` is written only after normalized reconstruction succeeds.

## Resume interrupted processing

Resume OCR after a completed capture whose processing was interrupted:

```powershell
.\Get-WearableLogs.ps1 process-run --run-dir ".\runs\<run-id>"
```

Before OCR, `process-run` validates every screenshot byte hash, dimensions, and exact cropped-pixel hash. It refuses altered evidence and a run owned by a live process. On the first resume it preserves the original manifest as `manifest.pre-resume.json`, reuses validated completed OCR results, invokes OCR only for remaining unique crop hashes, and records resume history and the current tool version. Re-running it on a valid completed run performs validation but no OCR and does not duplicate records.

The resume command never changes screenshots. It may finalize `manifest.json`, write or update OCR/text outputs, and add derived representative crops under `processed/`.

The tool uses the primary PP-OCRv6 model for punctuation fidelity and a Latin PP-OCRv4 prefix witness. The witness may supply whitespace only when both models agree on every non-whitespace prefix character. No guessed characters or spelling/value repairs are applied.

## Troubleshooting

- **No authorized device:** run `D:\Android\sdk\platform-tools\adb.exe devices -l`, reconnect USB, and accept the debugging prompt.
- **Multiple devices:** pass `--serial <serial>`.
- **Wrong foreground app:** manually open DevEco Assistant and its Logs screen. The tool does not navigate automatically.
- **Locked phone:** unlock it and keep the screen awake during capture.
- **No OCR text:** verify logs are visible, then calibrate the crop. The failed run still contains its manifest and any captured evidence.
- **Interrupted processing:** use `process-run`; do not copy screenshots into a new run or edit the original manifest manually.
- **Hash mismatch during resume:** retain the run unchanged and investigate the altered/missing screenshot. The tool intentionally refuses to OCR it.
- **Loading content…:** wait for the wearable logs to appear. UIAutomator text and `adb logcat` are not substitutes.
- **Assistant activity changed:** a changed activity produces a warning as long as the Assistant package is still foregrounded; recalibrate if its layout changed.
- **PowerShell execution policy:** if local policy blocks scripts, use the organization-approved method for running a local signed or reviewed script. Do not lower machine-wide policy for this tool.

No command clears Assistant data, presses Clear/Stop, uninstalls packages, deletes device files, or changes stored logs.

## Validation

Offline tests need no phone:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q
```

The real fixture is deliberately ignored because screenshots and transcribed logs are private. On the prepared development machine:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m wearable_logs_ocr.benchmark `
  --fixture-dir .\tests\fixtures\private\deveco-live-1080x2400 `
  --pipeline crop
```

The 2026-08-27 fixture contains two real 1080×2400 Assistant screenshots and 10 manual ground-truth records. The verified crop pipeline achieved 100% complete-record recovery, 100% prefix accuracy, no fabricated records, stable order, and byte-identical output over two runs. This is a small diagnostic benchmark, not proof for every font, firmware, language, or Assistant version.

Before a release, run the full tests, private benchmark, `doctor`, a physical one-screen capture, and `git diff --check`. Report fixture-only and physical-device checks separately.

## Design boundary

OCR is a diagnostic bridge. Downstream automation should depend on `logs.txt` and `manifest.json`, not screenshot implementation details, so a future structured transport can preserve the same contract. See [ALTERNATIVES.md](ALTERNATIVES.md).

# AI Agent Brief: Lite Wearable Log OCR Tool

You are the owner and long-term maintainer of a Windows tool that extracts Huawei Lite Wearable runtime logs displayed inside the DevEco Assistant Android APK.

Build the complete working solution in this directory:

`D:\_HarmonyOS\lite_wearable_logs_ocr`

The tool must be easy for a developer or another AI agent to invoke repeatedly from PowerShell. Inspect the existing directory before changing anything, preserve previous captures and user changes, and update the documentation whenever behavior changes.

## Problem and confirmed constraints

- The logs we need originate on a Huawei Lite Wearable device.
- They are displayed in the DevEco Assistant APK on a connected Android phone.
- They are **not Android logcat output**. Never substitute `adb logcat` for the wearable logs.
- The Assistant log view is custom-rendered: UIAutomator/accessibility dumps may not expose its text.
- The Android phone can be controlled and screenshotted through ADB.
- The developer can manually navigate to the correct Assistant log screen before capture.
- Arbitrary Huawei wearable system logs are not known to be readable by an ordinary Lite Wearable application API.
- OCR is a short-term diagnostic bridge. The project should remain replaceable by a structured diagnostic transport later.

Current machine facts to verify rather than blindly assume:

- Windows and PowerShell are used.
- ADB is expected at `D:\Android\sdk\platform-tools\adb.exe`.
- Python 3.12 is expected to be available.
- Pillow, OpenCV, and ONNX Runtime may already be installed globally, but the project must use its own reproducible environment.
- Tesseract is not currently installed.
- The DevEco Assistant package is expected to be `com.huawei.deveco.assistant`.
- A previously observed log activity was `com.huawei.deveco.assistant/.activity.LogListActivity`, but automatic navigation must not depend on this remaining stable.

## Required outcome

Create a local-only OCR utility with one PowerShell entry point:

```powershell
.\Get-WearableLogs.ps1 doctor
.\Get-WearableLogs.ps1 capture
.\Get-WearableLogs.ps1 capture --pages 20
.\Get-WearableLogs.ps1 follow --seconds 120
```

The exact PowerShell parameter syntax may use conventional PowerShell names such as `-Pages` and `-Seconds`, but the README must show the final verified commands. Keep the interface stable after the first working release.

The normal workflow should be:

1. Connect the Android phone with USB debugging enabled.
2. Open DevEco Assistant and navigate manually to the wearable log screen.
3. Run one command.
4. Receive a clean text log plus the evidence needed to audit OCR mistakes.

No cloud OCR service is allowed. Screenshots and logs must remain local.

## Implementation decisions

### Runtime and dependencies

- Use a PowerShell wrapper and Python implementation.
- Create a project-local virtual environment and pinned dependency file.
- Use RapidOCR with ONNX Runtime as the primary OCR implementation.
- If RapidOCR cannot be installed or executed on Python 3.12, use Windows Media OCR as the fallback. Do not silently switch to a cloud service.
- Do not require a globally installed Tesseract binary.
- Setup must be idempotent and must not modify unrelated Python environments.

### Commands

Implement these behaviors:

- `doctor`: verify ADB, exactly one selected/connected device or an explicitly supplied serial, authorization state, screen availability, Assistant installation, Python environment, OCR model availability, output-directory writability, and current foreground package. Report actionable errors and perform no swipes.
- `capture`: capture the current view and optionally scroll through a bounded number of pages. Save each raw screenshot before preprocessing it.
- `follow`: take screenshots periodically for a bounded duration while the log view updates. Preserve chronological order and deduplicate overlapping content.
- `calibrate`: save a reference screenshot and help define the normalized log crop rectangle and swipe geometry. Store configuration rather than hard-coding coordinates for one phone resolution.

Support, at minimum:

- ADB device serial selection.
- Page count and duration limits.
- Screenshot interval.
- Output directory override.
- Normalized crop rectangle.
- Swipe start/end coordinates or normalized swipe geometry.
- A no-swipe single-screen mode.
- Verbose diagnostic output.

Fail safely when the phone is disconnected, unauthorized, locked, on the wrong application, or showing a screen that produces no credible log lines. Never issue destructive ADB commands, clear application data, uninstall applications, or alter stored Assistant logs.

### OCR and reconstruction

- Preserve the original screenshots as the primary evidence.
- Preprocess only derived images. Consider cropping, upscaling, grayscale conversion, contrast adjustment, and thresholding, but validate every transformation against the benchmark fixture.
- Record OCR confidence and bounding boxes when the engine provides them.
- Reconstruct lines in visual top-to-bottom order.
- Preserve punctuation, timestamps, tags, numeric values, JSON fragments, and error codes as accurately as possible.
- Deduplicate overlap between successive screenshots without alphabetic sorting or timestamp reordering.
- Do not silently remove low-confidence lines. Mark them in the audit output.
- Do not invent missing characters or repair values using assumptions.
- Detect repeated/unchanged screenshots and stop bounded scrolling after a configurable repeated-screen limit.
- Keep all heuristics deterministic and documented.

### Output contract

Create one timestamped directory per run. It must contain:

- `logs.txt`: normalized, ordered, overlap-deduplicated output intended for analysis.
- `logs.raw.txt`: unmodified OCR line output in capture order.
- `manifest.json`: run identifier, UTC/local timestamps, tool version, Git commit when available, phone serial/model/resolution, Assistant package/version, command arguments, crop and swipe configuration, OCR engine/model versions, screenshot hashes, counts, confidence summary, warnings, and failure state.
- `ocr-results.json`: text, confidence, bounding boxes, screenshot association, and deduplication decisions.
- `screenshots/`: original PNG screenshots exactly as received from ADB.
- `processed/`: optional derived images used by OCR.

Use UTF-8 for text and JSON. Never overwrite an earlier run.

## Configuration

Provide a checked-in example/default configuration with:

- Assistant package and optional activity.
- Normalized crop rectangle.
- Normalized swipe coordinates.
- Default screenshot interval.
- Maximum pages and duration.
- Repeated-screen stop count.
- OCR language/model.
- Log-line credibility patterns, without making one specific Padel tag mandatory.

Keep machine-specific or generated configuration separate from defaults and document which files should be ignored by Git.

## Validation and acceptance criteria

Create unit and integration-style tests that can run without a connected phone by using fixture screenshots.

At minimum, test:

- Line ordering and reconstruction.
- Overlap deduplication across two or more screenshots.
- Repeated-screen stopping.
- Low-confidence preservation and annotation.
- Timestamp, JSON, negative number, decimal, path, bracket, and punctuation handling.
- Disconnected, unauthorized, multiple-device, locked-screen, wrong-app, and empty-OCR errors.
- Different phone resolutions using normalized coordinates.
- Paths containing spaces and non-ASCII characters.

Create a small benchmark fixture from real DevEco Assistant screenshots and a manually transcribed ground-truth file. Before calling the tool ready, demonstrate:

- At least 95% complete-line recovery on the benchmark.
- At least 98% correct recognition of timestamps and log-level/tag prefixes.
- No fabricated lines.
- Stable chronological order.
- Re-running the same fixture produces deterministic normalized output.

If those thresholds cannot be met, report the measured result honestly, retain the evidence, and identify the dominant OCR failure instead of weakening the criteria silently.

## Documentation and maintainership

Provide:

- `README.md` with setup, the three-command quick start, calibration, output locations, troubleshooting, and examples suitable for a non-expert.
- `CHANGELOG.md` using short dated entries.
- `AGENTS.md` containing project-specific maintenance rules, safety constraints, validation commands, and the requirement never to use Android logcat as a substitute.
- A diagnostic command that reports versions and configuration without exposing sensitive log content.
- Clear instructions for adding a new phone resolution or updating the Assistant UI crop after an APK update.

Prefer small modules, typed Python where practical, clear error messages, and tests over a single large script. Do not commit generated screenshots, captured logs, virtual environments, OCR caches, or secrets.

Before every commit:

1. Review the diff and preserve unrelated/user changes.
2. Run unit tests and fixture evaluation.
3. Run the doctor command when a phone is available.
4. State which checks used a physical device and which were fixture-only.

## Alternative approaches to assess

Create `ALTERNATIVES.md`, but do not implement these alternatives unless separately authorized:

1. **Structured diagnostic telemetry:** Emit compact, versioned JSON diagnostic events from the Lite Wearable JavaScript application and native `.so`, with a bounded on-device ring buffer. This is the preferred long-term replacement for OCR.
2. **Wear Engine companion transport:** Explicitly send app-owned diagnostic messages or files from the wearable app to a phone companion using Wear Engine P2P. This transports diagnostics generated by our app; it does not provide arbitrary system-log access. Record package, certificate, service-approval, and compatibility requirements.
3. **Huawei Health Industry SDK:** Investigate the separately advertised wearable-log retrieval capability, including service approval, supported watches/firmware, whether it includes third-party Lite app logs, and redistribution/privacy constraints. Do not assume it is part of the ordinary Lite Wearable API.
4. **Direct structured server upload:** If the tested application already has a reliable upload channel, compare a debug-only diagnostic endpoint or artifact upload with Wear Engine transport.

Design the OCR CLI and output contract so a future direct transport can replace screenshot capture while preserving `logs.txt`, `manifest.json`, and downstream automation.

## First execution sequence

1. Inspect this directory and any applicable `AGENTS.md` files.
2. Record the current Git state without discarding anything.
3. Run environment discovery and capture one screenshot into a temporary, ignored directory.
4. Prove the primary OCR engine on that single screenshot.
5. Establish a manually verified ground-truth fixture.
6. Implement `doctor` and single-screen `capture` first.
7. Add bounded scrolling, overlap deduplication, and `follow` only after single-screen accuracy is measured.
8. Complete tests and documentation.
9. Present the exact invocation commands, sample output paths, benchmark results, limitations, and any remaining Huawei access question.

Do not claim that the tool is production-ready merely because it ran once. Readiness requires the fixture metrics and reproducible commands above.

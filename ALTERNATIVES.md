# Structured alternatives to OCR

OCR is intentionally replaceable. None of the alternatives below is implemented by this project.

## 1. Structured diagnostic telemetry

Preferred long-term direction. Emit compact, versioned JSON events from the Lite Wearable JavaScript application and native `.so`, retain them in a bounded ring buffer, and export them without screen parsing. Define explicit schema versions, event size/rate limits, redaction, retention, and crash behavior. This covers diagnostics owned by the application; it does not imply access to arbitrary wearable system logs.

## 2. Wear Engine companion transport

Send app-owned diagnostic messages or files from the wearable application to a phone companion through Wear Engine P2P. Investigation must confirm package and certificate identity, Huawei service approval, phone/watch compatibility, transfer limits, offline retry behavior, and user/privacy requirements. Wear Engine is a transport for diagnostics the app emits, not a general system-log reader.

## 3. Huawei Health Industry SDK

Investigate the separately advertised wearable-log retrieval capability with Huawei. Confirm service approval, supported devices and firmware, whether third-party Lite application logs are included, API availability, redistribution terms, regional restrictions, consent, retention, and privacy obligations. Do not assume this belongs to the ordinary Lite Wearable application API.

## 4. Direct structured server upload

If the tested application already has a reliable network channel, compare a debug-only diagnostic event endpoint or bounded artifact upload. Require authentication, explicit debug enablement, schema/version limits, retry/backoff, redaction, retention, and production disablement. Compare operational cost and privacy risk against Wear Engine transport.

## Compatibility target

Any replacement capture source should continue producing ordered `logs.txt`, an auditable `manifest.json`, and stable run identifiers/paths. Source-specific evidence can replace `screenshots/` while downstream analysis remains unchanged.

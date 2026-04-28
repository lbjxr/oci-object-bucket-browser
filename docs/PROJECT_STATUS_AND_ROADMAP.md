# Project Status and Roadmap

## Current status

`oci-object-bucket-browser` is now beyond basic MVP stage.

It already works as a practical lightweight OCI Object Storage web panel for self-use and small internal scenarios, with usable upload, download, delete, and browsing flows.

## What is already working

### Core panel features
- Single-account login.
- Object listing.
- Prefix filtering.
- Lightweight prefix navigation.
- Object download.
- Batch download.
- Text preview.
- Image preview.
- PDF preview.
- Image thumbnails.
- File-type icons.
- Mobile-friendly layout.
- `systemd` deployment.

### Upload features
- Small-file direct upload.
- Large-file multipart upload.
- Multipart parallel upload.
- Lightweight resumable upload.
- Upload cancel.
- Progress, speed, ETA, success prompt.
- Smarter failed-part retry classification.
- OCI `429` / throttling retry-after handling.
- Temporary multipart concurrency reduction after repeated throttling.
- OCI-side multipart reconciliation on resume.
- Conservative degraded resume when remote reconciliation is temporarily unavailable.

### Object management
- Single-object delete from the list.
- Batch delete.
- Current-result select-all / clear.
- Selected-count feedback.
- Inline row removal after successful delete while keeping current filter context.
- Folded failed-object feedback for long delete-failure lists.

### Download features
- Lightweight ZIP-based batch download.
- Partial batch-download tolerance: successful objects still export even if some objects fail.
- ZIP failure manifests (`_batch_download_failures.json` / `_batch_download_failures.txt`).
- Native browser-triggered batch download flow for better large-ZIP reliability.
- Range-based single-object download support for resumable / multi-thread-capable clients.

## Current recommended defaults

- `APP_UPLOAD_CHUNK_SIZE_MB=16`
- `APP_UPLOAD_PARALLELISM=6`
- `APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB=32`

## Practical conclusions from current testing

- Resume flow has been verified in real use.
- Missing parts can be resumed without re-uploading completed parts.
- Multipart completion is stable after the upload-session write-loss fix.
- Current speed display is more realistic after moving to a recent sliding-window average.
- Object delete is now available for cleaning uploaded test files directly from the UI.
- Batch download is now usable for everyday “select a few / a few dozen objects and package them” scenarios.
- Native browser batch-download triggering is more reliable than the older blob-based frontend trigger for larger ZIP responses.

## Known boundaries in current state

- Resume support is lightweight, not full cloud-drive-grade resumable upload.
- Retry policy is smarter than before, but still remains a relatively lightweight browser-led implementation.
- Multipart throttling recovery currently reduces concurrency conservatively within the session and does not yet implement full adaptive recovery.
- Batch ZIP download is still synchronous, not background-task-based.
- Batch ZIP download is not a true resumable download format; if the browser connection drops, the ZIP usually needs to be regenerated.
- Single-object download currently supports single-range requests, not multi-part `multipart/byteranges` responses.
- Prefix navigation is intentionally lightweight and inferred from current object results, not a heavy real directory tree.
- Rich object-detail expansion (P5) is intentionally postponed for now.

## Current next-step posture

The current project posture is:

- Upload reliability: already significantly improved and no longer the immediate emergency pain point.
- Batch delete / cleanup UX: already practical.
- Batch download: now practical and more robust after switching to native browser download flow.
- Prefix browsing: improved enough to feel more folder-like without adding a heavy tree.
- Rich object detail expansion (P5): intentionally deferred.

## Priority summary

If the project stays in a stabilization posture for now, that is fully reasonable.

The current version already supports:

> upload, resume, delete, batch delete, batch download, resumable single-object download, and lightweight prefix-based browsing

The next major additions should be driven by real usage needs rather than by feature pressure alone.

# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

### Changed

### Known limitations

## [v1.0.0] - 2026-04-22

### Added
- Fixed single-account login flow for a lightweight self-hosted OCI Object Storage web panel.
- Object listing with prefix filtering.
- Lightweight prefix navigation with breadcrumbs, parent navigation, and inferred child-prefix shortcuts.
- Object download.
- Lightweight batch download with ZIP packaging for selected objects.
- Range-based single-object download support for resumable / multi-thread-capable clients.
- Text / image / PDF preview.
- Image thumbnails and file-type icons in the object list.
- Large-file multipart upload for OCI Object Storage.
- Lightweight resumable upload sessions based on local upload session metadata.
- Upload progress UI with percentage, current speed, ETA, success prompt, and cancel support.
- Single-object delete action from the object list with confirmation.
- Batch delete with multi-select actions.
- Upload-chain and object-action tests covering init, part upload, complete, resume, cancel, delete, and batch-download paths.

### Changed
- Default multipart chunk size changed from `64 MB` to `16 MB`.
- Default multipart parallelism changed from `4` to `6`.
- Multipart browser upload implementation changed from `fetch` to `XMLHttpRequest` to expose real-time upload progress.
- Upload speed display changed to a recent `3-second` sliding-window average for more realistic UX.
- Multipart part retry policy now distinguishes timeout / connection interruption / HTTP 5xx / HTTP 4xx-like failures instead of applying the same basic retry behavior to every error.
- OCI throttling (`429`) now has dedicated retry handling, including `Retry-After` parsing and clearer frontend status messaging.
- Multipart upload now temporarily reduces concurrency after repeated `429` throttling responses to stabilize upload behavior under rate limits.
- Multipart upload status text now shows clearer failure reasons, whether the browser will retry, and when retrying has already stopped.
- Upload-part API responses now include structured retry hints (`error_code`, `retryable`, `reason`, `retry_after_seconds`) to help the frontend decide how to present and handle failures.
- Multipart session recovery now also reconciles OCI-side uploaded parts before resuming, so restart/reselect flows rely less on stale local metadata.
- Upload status / completion flow now reuses the reconciled remote part list, allowing the frontend to skip already committed parts more reliably.
- Multipart resume/status now degrades more gently when OCI-side reconciliation temporarily fails: it returns an explicit warning, continues conservatively from local session state, and keeps final completion checks strict.
- Delete UX now supports multi-select batch delete, current-result select-all / clear, visible selected-count feedback, keeps the current filtered list context, removes successfully deleted rows inline first, and avoids an immediate hard page reload.
- Delete failure feedback now folds long failed-object lists, keeping the page compact while still exposing the remaining names on demand.
- Bulk action toolbar styling is now more rectangular and visually aligned with the rest of the panel.
- Batch download now tolerates partial object-read failures: successful objects are still exported, and ZIP archives include `_batch_download_failures.json` and `_batch_download_failures.txt` when needed.
- Batch download frontend feedback is clearer for partial success scenarios.
- Batch download trigger no longer uses `fetch -> blob -> objectURL`; it now uses a native browser download flow via hidden form POST, improving large-ZIP reliability and reducing browser-side blob memory pressure.
- Object list browsing now feels closer to folder navigation while remaining prefix-based.
- README updated with deployment guidance, proxy/Cloudflare advice, resumable upload notes, benchmark notes, delete support, download behavior notes, current recommended defaults, and upload reliability notes.

### Fixed
- Fixed multipart upload progress feeling “stuck” for large chunks.
- Fixed upload session state loss caused by concurrent session writes.
- Fixed failed-part handling so only failed parts retry, without polluting already uploaded state.
- Fixed resume flow so previously uploaded parts can be skipped correctly after retry/reselect.
- Fixed restart/local-state-drift recovery so missing local parts can be rebuilt from OCI remote multipart state, and stale local-only parts are dropped conservatively.
- Fixed object delete flow so uploaded test files can be removed directly from the UI.
- Fixed bulk delete flow so successful objects are removed inline even when part of the batch fails.
- Fixed large batch-download flows that could return `200 OK` from the server but still fail to trigger a reliable browser download in some environments.

### Benchmarks
- With `16 MiB` chunks and `6` parallel uploads, a `382,938,704 bytes` sample file completed in about `130 seconds` from `init` to `complete`.
- End-to-end average throughput observed: about `2.95 MB/s` (`2.81 MiB/s`).
- Stable transfer phase observed around `3.5 MB/s`, with peaks around `4 MB/s`.

### Known limitations
- Resume support is still lightweight and primarily aimed at same-browser reselection / refresh recovery.
- Retry policy is smarter than before, but still remains a frontend-led first version rather than a fully adaptive upload scheduler.
- Multipart concurrency reduction after throttling is currently conservative and one-way within the active upload session.
- Remote reconciliation currently trusts OCI's listed part numbers + ETags and reconstructs expected part sizes from session total size/chunk size; it does not independently verify remote byte length per part.
- Batch ZIP download is still a synchronous server-side packaging flow, not a background task.
- Single-object download currently supports single-range requests, not multi-part `multipart/byteranges` responses.

# Changelog

All notable changes to this project will be documented in this file.

## [v1.0.0] - 2026-04-22

### Added
- Fixed single-account login flow for a lightweight self-hosted OCI Object Storage web panel.
- Object listing with prefix filtering.
- Download support.
- Text / image / PDF preview.
- Image thumbnails and file-type icons in the object list.
- Large-file multipart upload for OCI Object Storage.
- Lightweight resumable upload sessions based on local upload session metadata.
- Upload progress UI with percentage, current speed, ETA, success prompt, and cancel support.
- Single-object delete action from the object list with confirmation.
- Upload-chain tests covering init, part upload, complete, resume, cancel, and delete-related paths.

### Changed
- Default multipart chunk size changed from `64 MB` to `16 MB`.
- Default multipart parallelism changed from `4` to `6`.
- Multipart browser upload implementation changed from `fetch` to `XMLHttpRequest` to expose real-time upload progress.
- Upload speed display changed to a recent `3-second` sliding-window average for more realistic UX.
- Multipart part retry policy now distinguishes timeout / connection interruption / HTTP 5xx / HTTP 4xx-like failures instead of applying the same basic retry behavior to every error.
- Multipart upload status text now shows clearer failure reasons, whether the browser will retry, and when retrying has already stopped.
- Upload-part API responses now include structured retry hints (`error_code`, `retryable`, `reason`) to help the frontend decide how to present and handle failures.
- Multipart session recovery now also reconciles OCI-side uploaded parts before resuming, so restart/reselect flows rely less on stale local metadata.
- Upload status / completion flow now reuses the reconciled remote part list, allowing the frontend to skip already committed parts more reliably.
- Delete UX now supports multi-select batch delete, current-result select-all / clear, visible selected-count feedback, keeps the current filtered list context, removes successfully deleted rows inline first, and avoids an immediate hard page reload.
- Delete success / failure messages now include clearer object-specific details, including partial-failure feedback for batch delete.
- README updated with deployment guidance, proxy/Cloudflare advice, resumable upload notes, benchmark notes, delete support, current recommended defaults, and upload reliability notes.

### Fixed
- Fixed multipart upload progress feeling “stuck” for large chunks.
- Fixed upload session state loss caused by concurrent session writes.
- Fixed failed-part handling so only failed parts retry, without polluting already uploaded state.
- Fixed resume flow so previously uploaded parts can be skipped correctly after retry/reselect.
- Fixed restart/local-state-drift recovery so missing local parts can be rebuilt from OCI remote multipart state, and stale local-only parts are dropped conservatively.
- Fixed object delete flow so uploaded test files can be removed directly from the UI.
- Fixed bulk delete flow so successful objects are removed inline even when part of the batch fails.

### Benchmarks
- With `16 MiB` chunks and `6` parallel uploads, a `382,938,704 bytes` sample file completed in about `130 seconds` from `init` to `complete`.
- End-to-end average throughput observed: about `2.95 MB/s` (`2.81 MiB/s`).
- Stable transfer phase observed around `3.5 MB/s`, with peaks around `4 MB/s`.

### Known limitations
- Resume support is still lightweight and primarily aimed at same-browser reselection / refresh recovery.
- Retry policy is smarter than before, but still remains a frontend-led first version rather than a fully adaptive upload scheduler.
- Remote reconciliation currently trusts OCI's listed part numbers + ETags and reconstructs expected part sizes from session total size/chunk size; it does not independently verify remote byte length per part.

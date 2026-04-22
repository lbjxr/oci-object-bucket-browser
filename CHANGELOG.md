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
- Single-object delete UX now keeps the current filtered list context, removes the deleted row inline first, and avoids an immediate hard page reload.
- Delete success / failure messages now include clearer object-specific details.
- README updated with deployment guidance, proxy/Cloudflare advice, resumable upload notes, benchmark notes, delete support, and current recommended defaults.

### Fixed
- Fixed multipart upload progress feeling “stuck” for large chunks.
- Fixed upload session state loss caused by concurrent session writes.
- Fixed failed-part handling so only failed parts retry, without polluting already uploaded state.
- Fixed resume flow so previously uploaded parts can be skipped correctly after retry/reselect.
- Fixed object delete flow so uploaded test files can be removed directly from the UI.

### Benchmarks
- With `16 MiB` chunks and `6` parallel uploads, a `382,938,704 bytes` sample file completed in about `130 seconds` from `init` to `complete`.
- End-to-end average throughput observed: about `2.95 MB/s` (`2.81 MiB/s`).
- Stable transfer phase observed around `3.5 MB/s`, with peaks around `4 MB/s`.

### Known limitations
- Resume support is lightweight and primarily aimed at same-browser reselection / refresh recovery.
- Retry strategy is currently basic and does not yet branch by error class.
- OCI-side multipart part reconciliation after service restart is not yet implemented.

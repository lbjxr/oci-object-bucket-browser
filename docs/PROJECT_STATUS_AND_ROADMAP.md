# Project Status and Roadmap

## Current status

`oci-object-bucket-browser` is now in a practical **v1.0.0** state.

It is no longer just an MVP demo page. It already works as a lightweight OCI Object Storage web panel for real-world self-use and small internal scenarios.

### What is already working

#### Core panel features
- Single-account login.
- Object listing.
- Prefix filtering.
- Object download.
- Text preview.
- Image preview.
- PDF preview.
- Image thumbnails.
- File-type icons.
- Mobile-friendly layout.
- `systemd` deployment.

#### Upload features
- Small-file direct upload.
- Large-file multipart upload.
- Multipart parallel upload.
- Lightweight resumable upload.
- Upload cancel.
- Progress, speed, ETA, success prompt.
- Basic failed-part auto retry.

#### Object management
- Single-object delete from the list.
- Confirmation before delete.
- Clearer inline delete success / failure feedback.
- Inline row removal after successful delete while keeping current filter context.

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

## Known boundaries in v1.0.0

- Resume support is lightweight, not full cloud-drive-grade resumable upload.
- Retry policy is still a basic first version.
- No OCI-side multipart reconciliation after restart yet.
- No batch object actions yet.
- No rename / move feature yet.

## Next task list

The following phases remain the active next-step task list.

### Phase 1 — short-term polish and usability

#### P1.1 Delete workflow polish (highest priority in this phase)
Deletion is already available and the latest iteration already improved this line with clearer feedback, inline row removal, and filter-context retention.

Suggested follow-up:
- Consider optional undo-style affordance if later needed.
- Consider lightweight protection for dangerous names or folders if later needed.
- Consider more structured error typing if OCI returns richer delete failures.

#### P1.2 Changelog / release-note maintenance
- Keep `CHANGELOG.md` updated after each meaningful iteration.
- Keep release notes aligned with actual shipped behavior.

#### P1.3 UI wording polish
- Clarify terms like “current speed” vs. “average speed”.
- Reduce ambiguity in upload / retry / delete states.

### Phase 2 — upload reliability and diagnostics

#### P2.1 Smarter retry policy
- Different retry behavior for timeout / connection reset / 5xx.
- Better retry messages.
- Optional retry cap tuning via config.

#### P2.2 Multipart reconciliation
- Query OCI multipart parts when needed.
- Reconcile local session metadata with remote uploaded parts.
- Improve restart-time recovery confidence.

#### P2.3 Better observability
- Record upload start / finish / commit timing.
- Make average throughput calculation easier to verify.
- Improve debug visibility for slow or failed uploads.

### Phase 3 — richer object-management experience

#### P3.1 Batch object actions
- Batch delete.
- Batch download helpers.
- Multi-select object operations.

#### P3.2 Richer object metadata view
- ETag.
- Last-Modified.
- Storage tier.
- Custom metadata.

#### P3.3 Stronger browsing model
- Better pseudo-directory experience.
- More intuitive navigation for large buckets.
- Optional directory-tree style browsing.

## Priority summary

If only one next step is chosen, it should be:

> **Continue improving object deletion UX first**

Reason:
- It solves an immediate real cleanup need.
- It is already partially implemented.
- It has direct value after upload testing.
- It is lower risk than more complex multipart engineering.

After that, the best technical next step is:

> **Upload reliability / diagnostics improvements**

And only then:

> **Richer object-management UX such as batch actions and stronger browsing**

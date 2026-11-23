# `apps/attachments`

What this app holds and how its parts fit. Loaded when you work in this directory; the
rules that hold everywhere are in the root `CLAUDE.md`. Keep this file current when the
app changes (introduced M6.4, Q8).

## Layout

- `Attachment` (one parent of request/receipt/handover, CHECK "exactly one", immutable but for its one removal by trigger, `attachments.0002`)
- `StorageQuota` (singleton, L8)
- `AttachmentReservation` (lease)
- `MediaDeletion`
- `parents` (`PARENTS`: per parent type the view/add/moderate capabilities, lock level, open/draft rules, pages to go back to)
- `files` (`ATTACHMENT_ROOT/files|staging`, magic-byte `sniff`)
- `scanner` (M8.3: `clamd`'s `INSTREAM` over a unix socket; `Infected`, `ScannerUnavailable`)
- `decoder` (the child process: rlimits, Pillow, WebP; runs as `python -I`, imports no project code)
- `decoder_service` (M8.3: the `decoder` container's process, one child per request; stdlib only, runs as `python -I`) and `decoder_client` (`web`'s side; what comes back is untrusted)
- `processing` (phase 2: type, scan, decode with a wall-clock kill, `Rejected` codes)
- `quota` (L8 helpers: `lock_parent`, `lock_quota`, `release`, the 80 % alert)
- `uploads` (`ReservingUploadHandler`: admission in `handle_raw_input`, stream within the reservation)
- `services` (`admit_upload`, `finalize_upload` (idempotent), `release_reservation`, `remove_attachment`)
- `housekeeping` (worker tasks `attachment_reservations` and `attachment_media`: lease expiry, two-phase purge under `gmat:media-backup`)
- `selectors` (`parent`, `attachment`, `panel`: always through the parent's scope)
- `checks` (`gmat.E020`), read-only admin
- `views` under `/attachments/` (`request|receipt|handover/<id>/` the upload entry point: the one allowlisted `csrf_exempt`, an inner `csrf_protect`; `<id>/` the download; `<id>/remove/`)
- `templates/attachments/panel.html` included on the request, review, receipt and handover pages

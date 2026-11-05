# ADR-0015: Uploads are scanned by clamd, and decoded in a container of their own

- **Status:** Accepted (the maintainer's decisions of 2026-11-05 on exceptions X7 and X11)
- **Date:** 2026-11-05

## Context

The M8.3 review proposed to accept two things about uploads: PDFs stored without a malware scan (X7), and an image decoder running under the same uid as `web` (X11). The maintainer refused both. A decoder sharing `web`'s uid can read `/proc/<web>/environ` (the database password, `SECRET_KEY`), every stored file and the database's network; a PDF stored unscanned reaches everyone who can see the document.

Containers here run non-root with every capability dropped and `no-new-privileges`, so a process cannot change its uid inside `web`. A different uid has to mean a different container.

## Decision

**Scan.** Every upload is scanned before anything else touches its bytes (`apps.attachments.scanner`), including the original of an image that will be re-encoded. `clamd` runs in a container with no network, reached through a unix socket on a volume only it and `web` mount (`web` read-only). `freshclam`, in a separate container with no sight of any file, keeps the signature volume current through its own network. **It fails closed:** no verdict means the file is not stored (503 with `Retry-After`). What cannot be scanned is refused too (`AlertExceedsMax`, `AlertEncryptedDoc`). A refusal is audited on the document (`attachment.malware_refused`) and logged, by signature, never by file name.

**Decoder.** In production images are decoded in the `decoder` container (`apps.attachments.decoder_service`): uid 1001, no network, no volume but its socket and a scratch tmpfs, and none of the application's secrets in its environment. For each request it runs `decoder.py` as a child under the same rlimits and wall-clock kill as before. What comes back is untrusted: `web` bounds its size and checks it is a WebP file.

`gmat.E022` refuses production without either socket, so a misconfiguration cannot quietly store unscanned files or decode inside `web`. Outside production (`dev`, `test`) the sockets are empty: no scan, and a child process of `web`.

`deploy.sh` starts both behind the gates, and `verify-internal` asks each for an answer, so a release that cannot accept uploads never opens. The restore drill does not start them; it stores no upload.

**Image.** `clamav/clamav:1.5.4` (Alpine) pinned by digest, x86-64 only. The multi-arch Debian variant was scanned and rejected: 11 HIGH or CRITICAL findings that the Alpine image does not have, among them libxml2 flaws `clamd` reaches when it parses a hostile document.

## Consequences

- **The VPS must be x86-64.** An ARM server cannot run this stack until the Alpine image is published for it. To be chosen before H8.6 buys the server.
- **About 1 GB more memory, all the time.** `clamd` holds its signatures in memory: 950 MiB measured. The stack idles at about 1.4 GB plus the system, and reaches about 3.2 GB in the worst realistic moment (two images decoding). A 4 GB server has room but little headroom, so the runbook adds swap; 8 GB is the comfortable size. `ConcurrentDatabaseReload no` avoids a second copy in memory during a signature reload, at the price of scans waiting, or refused and retried, for those seconds.
- **A third-party image with egress.** `freshclam` is the only process that fetches anything other than mail and backups, and it only reaches ClamAV's mirror. Its image is in the nightly scan and Dependabot's watch.
- **Uploads depend on two more services.** If either is down, uploads are refused and everything else keeps working; the health checks and `docker compose ps` show it.
- **What a scan does not do.** Signature scanning catches known malware, not a new one, and files stored before a signature appeared are not scanned again. Files stay served only as sandboxed attachments (plan §5), which is the defence that does not depend on a signature.
- **Residual in the decoder container.** Two concurrent conversions run under the same uid, so an exploited child could read the image being converted next to it. It still cannot send it anywhere.

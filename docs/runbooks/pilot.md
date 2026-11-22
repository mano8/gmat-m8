# Pilot: the 0.x releases before v1.0.0

The first releases are numbered `0.x`. They run on the production server, for a small group of real users, so that everything the test suites cannot prove is tried for real before the application is called stable: the server, the real mail and offsite storage, a real restore, an upgrade, and people doing their work on their own phones. The pilot ends when the [exit criteria](#exit-criteria-for-v100) hold; then v1.0.0 is released and the other branches join.

Written for the maintainer. It uses the other runbooks rather than repeating them; keep a copy of this page (or an issue made from it) and tick the boxes as you go. Commands run on the VPS as `deploy`, as in the other runbooks:

```bash
cd ~/gmat-m8/docker_compose/gmat_m8
alias dc='docker compose -f docker-compose.yml -f docker-compose.production.yml'
```

## Versions during the pilot

- **v0.1.0** is the first release. Each round of corrections that the pilot asks for is a new release, as many as needed (v0.2.0, v0.3.0, …). The number says what an upgrade involves:
  - **`0.Y.0`** when the release carries a migration, or changes what people do or may do: a new screen or step, a changed rule or permission;
  - **`0.Y.Z`** otherwise: fixes that make the application do what it already claims to do, without a migration.

  When in doubt, take `0.Y.0`. Semantic Versioning promises nothing stable below 1.0.0; this project still writes every change into `CHANGELOG.md`.
- Every 0.x release goes through the whole pipeline, exactly as v1.0.0 will: a `## [X.Y.Z]` section in `CHANGELOG.md` merged by PR, `verify_release.py`, the tag, `docker-publish`, `./deploy.sh X.Y.Z` ([../release.md](../release.md), [deploy-rollback.md](deploy-rollback.md)). No step is skipped because the version starts with 0: the pilot is also the rehearsal of the release routine.
- The migration statements apply from v0.2.0 on: write `Irreversible migration: …` or `Old code works on the new schema: …` in the notes when they are true.
- While the pilot runs, `main` takes what the pilot needs: fixes, corrections of screens and wording, documentation. Large new features wait until v1.0.0 is tagged, so that each 0.x release changes only what the pilot is testing.

## Before v0.1.0

- [ ] The server is ready: [vps-setup.md](vps-setup.md) up to its *Done when*, keys and env files copied to the offline storage.
- [ ] Decide **who** takes part: one branch, two or three of its projects, and at least one person per role that branch uses (group administrator, material manager or engineer, warehouse operator, site manager, technician; a partner if you work with one). Tell them what the pilot is, how long it should last, and how to report a problem (below).
- [ ] Decide **what data** goes in. The pilot runs on production settings, so `seed_demo` does not exist there, and whatever is entered is real data that stays when the server moves to later versions: an upgrade is an ordinary deploy, and the application deactivates, voids and cancels, it never deletes. Two ways:
  - the real organization from the start (recommended): the real companies, branches, catalog, suppliers and projects, with real requests;
  - or, for practice, a separate company with its own branch and projects. At the end, finish or cancel its open documents, then deactivate the company: its documents stay, outside every scope.

  There is no tool to empty the database later. Decide before the first invitation.
- [ ] Release v0.1.0 ([../release.md](../release.md)): the notes are in `CHANGELOG.md` under `## [0.1.0]`; run the dry run of `docker-publish` before this first tag; then make both image packages public before the first deploy ([vps-setup.md](vps-setup.md#8-first-start-and-first-checks)).

## 1. First deploy and first checks

- [ ] `./deploy.sh 0.1.0` finishes; `./recover.sh status` shows both gates off and the first line of `deployed.log`.
- [ ] The certificate is valid on the real domain (`curl -sI https://<domain>/healthz/` answers 200 without `-k`).
- [ ] Security headers are there: `curl -sI https://<domain>/accounts/login/` shows `Content-Security-Policy`, `Cache-Control: no-store` and `Referrer-Policy`.
- [ ] The Django admin is off: `https://<domain>/<ADMIN_URL_PATH>/` answers 404 (unless you turned it on on purpose).
- [ ] The first administrator, a platform operator, exists and has enrolled MFA; the recovery codes are offline ([user-admin.md](user-admin.md#the-first-administrator)).
- [ ] Monitoring is set up and each alert has been seen once, for example by pausing the uptime check's target or stopping the heartbeat ([vps-setup.md](vps-setup.md#9-monitoring-and-reviews)).

## 2. Backups and a real restore

- [ ] A scheduled dump has run and reached offsite: `ls -l backups/.last-success`, `dc logs --since 12h backup`.
- [ ] The first Sunday's level-2 check has run: `backups/.last-verify-ok` is recent ([backup-restore.md](backup-restore.md#verification-three-levels)).
- [ ] The first **offsite drill** has run on an isolated machine, **before real data goes in**, and its line is in [drill-log.md](drill-log.md) ([backup-restore.md](backup-restore.md#quarterly-offsite-drill)).
- [ ] `python scripts/offsite_prune.py --baseline` then `--dry-run` work from your machine with the delete-capable credentials ([backup-restore.md](backup-restore.md#offsite-retention-and-pruning-maintainers-machine-only)).

## 3. Mail through the real SMTP

- [ ] An invitation reaches a real mailbox (not the spam folder), and its link works once only.
- [ ] A password reset arrives, and the reset does not sign the person in.
- [ ] Changing a password or a second factor sends its owner the security notice.
- [ ] The delivery badge on the invitation goes from queued to sent.
- [ ] `touch flags/worker.paused` holds the mail back, and removing the flag sends it ([email.md](email.md)).
- [ ] Business notifications arrive (a submitted request reaches its reviewers, an approval reaches the requester), and a person who turned email off on *My notifications* gets none.
- [ ] *Administration → Notifications*: turn *A request to review* off by email for everyone, give one reviewer *always* on their page, submit a request: only that reviewer is mailed. Turn it back on afterwards unless the pilot wants it off ([user-admin.md](user-admin.md#which-business-notifications-are-sent)).
- [ ] *My notifications* and the bell: a requester sets *My request approved* to *do not send it to me* by email; approve one of their requests: no email, the bell in their header shows 1, opening it leads to the request and the count goes back to 0. Put the choice back to *as the administrators set it*. A person's own choice wins over an administrator's *never* or *always* (tell the pilot's users about *My notifications* and the bell).
- [ ] Supplier dispatch: set `PO_DISPATCH=manual` in `app.env` (an upgrade keeps `off`), give a test supplier your own address, issue an order, use **Send to the supplier** on its page, read the message naming the address, watch the panel turn to *Sent* and check the mail arrives once; then buyers send to suppliers who agreed ([email.md](email.md#supplier-dispatches)).

## 4. Setting up the organization

- [ ] Companies, branches, warehouses and units are created from the *Administration* screens ([user-admin.md](user-admin.md#setting-up-the-organization)).
- [ ] Each import runs once with `--dry-run`, then for real: `import_catalog`, `import_suppliers`, `import_projects` (templates in `docs/import-templates/`). Take a `backup-now --label before-import` first.
- [ ] The pilot's people are invited with their full names and roles; each one signs in, and the privileged ones enroll MFA.

## 5. The work itself, on phones and computers

Each person does their own part on their own device; you watch, and note what was unclear. At least once:

- [ ] A technician (and a site manager) requests materials for a project from a phone, including a free-text line.
- [ ] A reviewer is assigned, edits a line, and approves; an engineer or material manager decides their own request with a second factor.
- [ ] Purchasing builds an order from the worklist, issues it, prints it, amends it, and closes a line short.
- [ ] Goods are received at a warehouse against the order, and at a worksite from the project's page by a site manager and by a technician.
- [ ] A handover from the warehouse is posted; a receipt and a handover are voided with a reason, and the documents reopen as they should.
- [ ] Requests close by themselves once everything is delivered; one is closed short by hand and reopened.
- [ ] Files are attached to a request and a receipt (a photo from a phone, a PDF). An antivirus test file is refused by the scanner: a small PDF that carries the standard EICAR string in a stream, built as `INFECTED_PDF` in `tests/live/test_attachments.py` (the bare string is refused earlier, for its type, which proves nothing about the scanner). The request's history then shows `attachment.malware_refused`.
- [ ] The dashboards, the lists with their filters and pages, the CSV exports and the audit history show what was done.
- [ ] The screens work in Spanish, and in English and French for anyone who switches; in the light and the dark theme.

### What changed for the pilot's users with the first round of feedback

Issues [#64](https://github.com/mano8/gmat-m8/issues/64)–[#69](https://github.com/mano8/gmat-m8/issues/69). Tell the people concerned after the deploy, then check each change with them once:

- [ ] **Technicians and site managers:** on a draft request, Enter after a quantity moves to the next one, and the last Enter leads to *Review and submit*.
- [ ] **Reviewers:** *Approve all pending lines* is at the top of the review page (it asks for confirmation); after approving or rejecting one line, the page opens at the next line still waiting.
- [ ] **Group administrators:** the organisation's panels (companies and branches, people per branch, requests per branch) left the home page for *Administration › Overview*; the home page keeps the work to do.
- [ ] **Buyers:** with `PO_DISPATCH=manual` (section 3), issuing emails nothing: **Send to the supplier** on the issued order's page shows the address and queues the order; the message after each action says what was queued and to which address, and the *Sent to the supplier* panel turns to *Sent* without a reload.
- [ ] **Everyone:** the left menu (and the phone's *More* sheet) opens with the current page's entry in view.
- [ ] **Privileged people enrolling a second factor:** the QR code reads dark on white in the dark theme too; scan it with the phone's authenticator app without switching theme.

## 6. Access, seen from the people who should not see

- [ ] A technician, a site manager and a partner see no price and no other project's quantities, and see suppliers only by name on their deliveries.
- [ ] A partner sees only the projects shared with them.
- [ ] A person is offboarded: their sessions end at once, their invitations and links stop working.
- [ ] A lost second factor is reset from the person's page, and the person enrolls again ([user-admin.md](user-admin.md#lost-second-factor)).
- [ ] Logs carry no tokens: after opening an invitation or reset link, `dc logs --since 10m caddy web worker | grep -c '<the first 10 characters of its token>'` prints 0.

## 7. Upgrades

- [ ] At least one later release (v0.1.1 or v0.2.0) is deployed while people are using the application: deployed outside working hours, the maintenance page shown during the window, nobody's work lost, sessions kept.
- [ ] One of them carries a migration, and `./deploy.sh` reports it and finishes within the expected window.
- [ ] You have read the [decision tree](deploy-rollback.md#the-decision-tree) for each failure you could meet, and know which of them loses data.

## 8. Operations over time

- [ ] HTTPS has worked for some days; HSTS is turned on one switch at a time ([vps-setup.md](vps-setup.md#turning-on-hsts)).
- [ ] The nightly image scan has no open issue, or each finding has an accepted, dated exception ([vulnerability-exceptions.md](vulnerability-exceptions.md)).
- [ ] Disk use after a few weeks of dumps and attachments leaves room (`df -h /`, `docker system df`), and the server's memory holds under a morning's sign-ins (`docker stats --no-stream`).
- [ ] The *Calendar* of [README.md](README.md) is in your diary.

## Reporting what the pilot finds

Each problem is a GitHub issue made with the bug or accessibility form, with the page, the role, the device and what was expected; never a password, token, address or screenshot that shows someone's data. Put `pilot` in the title, and say whether it **blocks** v1.0.0. A blocker is something that loses or exposes data, stops a role from doing its work, or makes a deploy or restore unsafe. The fixes ship in the next 0.x release.

## Exit criteria for v1.0.0

- [ ] Every box above is ticked, or you have written down why it does not apply.
- [ ] No open `pilot` issue that blocks.
- [ ] The pilot branch has worked with the application for a period you chose in advance, with the last 0.x release in place and no blocker found during it.
- [ ] A drill has passed with the version that becomes v1.0.0, or with a release whose migrations it shares.

Then v1.0.0: a `## [1.0.0]` section in `CHANGELOG.md` that says the 0.x series ended and what changed in its last release, the usual [release procedure](../release.md), and the other branches are set up and invited as in sections 4 and 5.

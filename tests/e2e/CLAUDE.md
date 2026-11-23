# `tests/e2e`

The browser layer: Playwright (Chromium) against a started compose stack. Loaded when you work
in this directory; how to run it is in the root `CLAUDE.md` (Commands) and below.

## Running

- `E2E_REQUIRED=1 uv run pytest tests/e2e -m e2e` runs against a **fresh** compose stack (empty database, `docker compose down --volumes` first); it provisions the stack itself and takes about 90 s (+30 s of provisioning).
- Once: `uv sync --group e2e && uv run playwright install --with-deps chromium` (the optional group; axe-core comes with it, `axe-playwright-python`, injected through `page.evaluate` so the CSP is untouched).
- `E2E_BASE_URL`, `E2E_COMPOSE_DIR`, `E2E_COMPOSE_PROJECT` (a stack started with `-p`), `E2E_STATE_FILE` (keeps the enrolled authenticators so a second run can reuse the same stack; its data stays), `E2E_SCREENSHOTS=1` (the main screens at 360 and 1280 px, light and dark, to `test-results/screens/`; no pixel baseline).

## Layout

- `conftest.py` (the `browsing` fixture opens a context per person on a phone (360 px) or desktop viewport, signs in once per person and reuses the session; every page is watched, and a CSP violation, an uncaught script error or a 5xx fails the test; screenshots of failed tests go to `test-results/`)
- `provision.py` (a fresh stack: `bootstrap_admin`, the link accepted in the browser, TOTP enrollment, `seed_demo` in a one-off dev-settings container, MFA enrollment of the privileged demo users)
- `totp.py` (RFC 6238; a code is taken once per 30 s step)
- `mailbox.py` (Mailpit read from inside the worker container, the worker's pause flag)
- `pages.py` (one driver per thing a person does; wait on the page's state, never on the clock; `data-testid`s and labels, English texts)
- The tests: `test_flow_mvp`
- `test_flow_review` (M7b)
- `test_flow_project_receiving` (M9.3: the seven roles receive at a project from its page, phone and desktop, redaction for the site-only roles, a manager's void)
- `test_flow_administration` (M10.6: a group administrator makes a company, a branch managed by another, a warehouse and a unit, invites a person to the branch, reads the deactivation's preview and sees the person refused afterwards)
- `test_administration_screens` (M10.6: company and branch pages, their dialogs, the new lists' tab order, the Administration menu: phone fit, axe, keyboard)
- `test_mfa_reset` (M10.4: the reset and the re-enrolment)
- `test_email_badge` (M7c)
- `test_lists` (M7d)
- `test_phone_layout` (no sideways scroll at 360 px)
- `test_accessibility` (M8b.6: axe-core on the entrance pages, every role's home page and lists and the document pages with a dialog open, both colour schemes, 360 px and desktop; a serious or critical finding fails, the rest goes to `test-results/axe-report.txt`)
- `test_keyboard` (the shell's tab order, each kind of dialog, the menus and the More sheet by keyboard, a visible focus at every stop)
- `test_screenshots` (opt-in, `E2E_SCREENSHOTS=1`)
- `documents.py` (the request, order and receiving form those three audit, made once per run through the screens)
- `test_browser_guards` (CSP listener, Back after logout)
- `test_sentinel` (plan §5 level 3)
- the later flows, each named after what it proves (`test_notification_settings`, `test_notification_channels`, …)
- `collect_ignore` in `tests/conftest.py` keeps the directory out unless Playwright is installed and `E2E_REQUIRED=1` (its event loop breaks every later database test, so a plain `pytest` never enters it)
- A new screen with a flow worth proving in a browser gets a test here, and a new list or role screen goes in `test_phone_layout`.

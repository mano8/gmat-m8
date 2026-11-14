"""A group administrator resets a lost second factor from the person's page (M10.4), and the
privileged person sets up a new one at their next sign-in (the step-up of M1.3)."""

import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox
from tests.e2e.provision import Person, enroll_authenticator

pytestmark = pytest.mark.e2e

# A privileged person of the demo: after the test they have a new authenticator, which the
# session's ``people`` (and the state file, when there is one) keep for later tests.
PERSON = "eng-mad"
# The notice to the owner (the stack's mail is in English, as the other tests read it).
NOTICE = "removed the second factors"


def open_person(admin: Page, person: Person) -> None:
    admin.goto(f"/org/people/?q={person.key}")
    admin.get_by_role("link", name=person.email).click()
    expect(admin.get_by_test_id("page-header")).to_contain_text(person.email)


def reset_from_the_page(admin: Page) -> None:
    admin.get_by_test_id("mfa-reset-open").click()
    dialog = admin.get_by_test_id("mfa-reset-dialog")
    expect(dialog).to_be_visible()
    form = dialog.get_by_test_id("mfa-reset-form")
    # Without the identity tick the dialog stays open with the reason for the refusal.
    form.get_by_label("Reason").fill("Lost phone, no recovery code")
    form.get_by_test_id("mfa-reset-submit").click()
    summary = dialog.locator("[data-error-summary]")
    expect(summary).to_be_focused()
    expect(summary).to_contain_text("identity")
    form = dialog.get_by_test_id("mfa-reset-form")
    form.get_by_label("I checked this person's identity outside the application.").check()
    form.get_by_test_id("mfa-reset-submit").click()
    expect(admin.get_by_test_id("mfa-reset-open")).to_have_count(0)
    expect(admin.get_by_role("status").filter(has_text="Second factors removed")).to_be_visible()


def keep_new_secret(browsing: Browsing, person: Person) -> None:
    browsing.states.pop(person.key, None)  # every session of the account has ended
    state = os.environ.get("E2E_STATE_FILE")
    if state and Path(state).is_file():
        secrets = json.loads(Path(state).read_text(encoding="utf-8"))
        assert person.authenticator is not None
        secrets[person.key] = person.authenticator.secret
        Path(state).write_text(json.dumps(secrets), encoding="utf-8")


def test_reset_then_the_person_enrolls_again(browsing: Browsing, mailbox: Mailbox) -> None:
    person = browsing.people[PERSON]
    before = browsing.fresh_sign_in(PERSON)
    admin = browsing.as_("admin", "phone")
    open_person(admin, person)
    reset_from_the_page(admin)
    mailbox.wait_for(person.email, containing=NOTICE)

    # The session the person had is over.
    before.goto("/requests/")
    expect(before).to_have_url(re.compile(r"/accounts/login/"))

    # Signed in with the password alone, a privileged account is sent to set up a factor.
    old = person.authenticator
    person.authenticator = None
    try:
        page = browsing.fresh_sign_in(PERSON)
        page.goto("/requests/")
        expect(page).to_have_url(re.compile(r"/accounts/2fa/"))
        page.context.clear_cookies()
        enroll_authenticator(page, person)
    except BaseException:
        person.authenticator = old
        raise
    keep_new_secret(browsing, person)
    # The next sign-in asks for a code of the new authenticator, and the pages open again.
    again = browsing.fresh_sign_in(PERSON)
    again.goto("/requests/")
    expect(again).to_have_url(re.compile(r"/requests/$"))

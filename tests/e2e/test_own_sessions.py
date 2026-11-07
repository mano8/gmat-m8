"""A person sees where they are signed in and ends a session they do not want (M8.3, X5)."""

import re

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import Browsing

pytestmark = pytest.mark.e2e

PERSON = "partner"


def test_ending_the_other_session_signs_that_browser_out(browsing: Browsing) -> None:
    here = browsing.fresh_sign_in(PERSON)
    elsewhere = browsing.fresh_sign_in(PERSON, "phone")
    # A session another test kept for this person would be ended too: it must not be reused.
    browsing.states.pop(PERSON, None)

    here.goto("/account/sessions/")
    expect(here.get_by_test_id("session-current")).to_have_count(1)
    rows = here.get_by_test_id("session-row")
    expect(rows.nth(1)).to_be_visible()

    here.get_by_test_id("sessions-end-others").get_by_role("button").click()
    expect(here.get_by_test_id("session-row")).to_have_count(1)
    expect(here.get_by_test_id("sessions-end-others")).to_have_count(0)

    elsewhere.goto("/requests/")
    expect(elsewhere).to_have_url(re.compile(r"/accounts/login/"))
    here.goto("/requests/")
    expect(here).to_have_url(re.compile(r"/requests/$"))

"""The authenticator setup page draws its QR code dark on white in the dark theme too (M14.6)."""

import io
import os

import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import ROOT, Browsing
from tests.e2e.test_accessibility import SCHEMES

pytestmark = pytest.mark.e2e

# Someone without a second factor: the page is opened and left, so no authenticator is enrolled
# and the other tests' sign-ins are untouched.
PERSON = "tech-mad"
WHITE = (255, 255, 255)


def open_setup(browsing: Browsing, viewport: str) -> Page:
    page = browsing.fresh_sign_in(PERSON, viewport)
    page.goto("/accounts/2fa/totp/activate/")
    page.fill("input[name=password]", browsing.people[PERSON].password)  # reauthentication
    page.click("main form button[type=submit]")
    page.wait_for_selector("#authenticator_secret")
    return page


def qr_code(page: Page) -> Locator:
    return page.locator("main img[src^='data:image/svg+xml']").first


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_qr_code_is_dark_on_white_in_both_themes(browsing: Browsing, viewport: str) -> None:
    page = open_setup(browsing, viewport)
    for scheme in SCHEMES:
        page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
        image = qr_code(page)
        expect(image).to_be_visible()
        picture = Image.open(io.BytesIO(image.screenshot())).convert("RGB")
        # The corner is the quiet zone: white whatever the page behind it.
        assert picture.getpixel((0, 0)) == WHITE, scheme
        assert picture.getpixel((picture.width - 1, picture.height - 1)) == WHITE, scheme
        # The modules themselves are dark: the code has black pixels, not only white ones.
        assert min(picture.convert("L").tobytes()) < 60, scheme
        if os.environ.get("E2E_SCREENSHOTS") == "1":
            folder = ROOT / "test-results" / "screens"
            folder.mkdir(parents=True, exist_ok=True)
            width = 360 if viewport == "phone" else 1280
            page.screenshot(path=folder / f"mfa-setup-{width}-{scheme}.png", full_page=True)

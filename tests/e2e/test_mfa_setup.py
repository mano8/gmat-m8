"""The authenticator setup page draws its QR code dark on white in the dark theme too (M14.6)."""

import io
import math
import os
from typing import Any, Literal, cast

import pytest
from PIL import Image
from playwright.sync_api import Locator, Page, expect

from tests.e2e.conftest import ROOT, Browsing

pytestmark = pytest.mark.e2e

# Someone without a second factor: the page is opened and left, so no authenticator is enrolled
# and the other tests' sign-ins are untouched.
PERSON = "tech-mad"
WHITE = (255, 255, 255)
# test_accessibility audits this page and imports ``open_setup``: its own SCHEMES would be circular.
SCHEMES: tuple[Literal["light", "dark"], ...] = ("light", "dark")


def open_setup(browsing: Browsing, viewport: str) -> Page:
    page = browsing.fresh_sign_in(PERSON, viewport)
    page.goto("/accounts/2fa/totp/activate/")
    page.fill("input[name=password]", browsing.people[PERSON].password)  # reauthentication
    page.click("main form button[type=submit]")
    page.wait_for_selector("#authenticator_secret")
    return page


def qr_code(page: Page) -> Locator:
    return page.locator("main img[src^='data:image/svg+xml']").first


def inside_of(page: Page, image: Locator) -> bytes:
    """A screenshot of the whole CSS pixels inside the image's box. The box is fractional
    (185.2 px), and an element's screenshot rounds it outwards, so its edges would be the page
    beside the image (more than one pixel on a phone's denser screen)."""
    box = image.bounding_box()
    assert box is not None
    left, top = math.ceil(box["x"]), math.ceil(box["y"])
    right, bottom = math.floor(box["x"] + box["width"]), math.floor(box["y"] + box["height"])
    clip = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    # ``cast``: Playwright's ``FloatRect`` locally, ``Any`` where the e2e group is not installed.
    shot: bytes = page.screenshot(clip=cast(Any, clip))
    return shot


@pytest.mark.parametrize("viewport", ["phone", "desktop"])
def test_qr_code_is_dark_on_white_in_both_themes(browsing: Browsing, viewport: str) -> None:
    page = open_setup(browsing, viewport)
    for scheme in SCHEMES:
        page.emulate_media(color_scheme=scheme, reduced_motion="reduce")
        image = qr_code(page)
        expect(image).to_be_visible()
        picture = Image.open(io.BytesIO(inside_of(page, image))).convert("RGB")
        # The corners are the quiet zone: white whatever the page behind it.
        assert picture.getpixel((0, 0)) == WHITE, scheme
        assert picture.getpixel((picture.width - 1, picture.height - 1)) == WHITE, scheme
        # The modules themselves are dark: the code has black pixels, not only white ones.
        assert min(picture.convert("L").tobytes()) < 60, scheme
        if os.environ.get("E2E_SCREENSHOTS") == "1":
            folder = ROOT / "test-results" / "screens"
            folder.mkdir(parents=True, exist_ok=True)
            width = 360 if viewport == "phone" else 1280
            page.screenshot(path=folder / f"mfa-setup-{width}-{scheme}.png", full_page=True)

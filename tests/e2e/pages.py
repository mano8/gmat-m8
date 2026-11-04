"""Small drivers for the screens the flows cross, one function per thing a person does.

They click and type what a person would, wait on the page's own state (a URL, a line that
appears, a heading) rather than on the clock, and return what the next step needs (a number
the application assigned). Texts are the English ones: the browser contexts ask for ``en-US``.
"""

import re
from collections.abc import Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

REQUEST_NUMBER = re.compile(r"SOL-[A-Z]+-[A-Z]+-\d{4}-\d{5}")


def line_of(page: Page, reference: str) -> Locator:
    """The draft line for a catalog reference."""
    return page.locator("#draft-lines [data-line]", has_text=reference)


# --- requester ----------------------------------------------------------------------------


def add_material(page: Page, reference: str, quantity: int) -> None:
    """Search the catalog on a draft request, pick the material and set its quantity."""
    page.fill("#id_q", reference)
    page.click(f"button[data-material='{reference}']")
    line = line_of(page, reference)
    expect(line).to_be_visible()
    field = line.locator("input[name=requested_qty]")
    for attempt in (1, 2, 3):
        # Enter submits the stepper's form. A line only just swapped in may not have its htmx
        # handlers yet (a phone is slow enough to show it), so a missed submit is typed again.
        try:
            with page.expect_response(
                lambda r: "/update/" in r.url and r.request.method == "POST", timeout=4_000
            ):
                field.fill(str(quantity))
                field.press("Enter")
            break
        except PlaywrightTimeoutError:
            if attempt == 3:
                raise
    expect(field).to_have_value(str(quantity))


def start_draft(page: Page, project: str) -> None:
    page.goto("/requests/new/")
    page.click(f"button[data-project='{project}']")
    page.wait_for_url(re.compile(r"/requests/[0-9a-f-]{36}/$"))


def submit_request(
    page: Page,
    project: str,
    lines: dict[str, int],
    *,
    priority: str = "Normal",
    reviewer: str = "No one in particular",
    notes: str = "",
) -> str:
    """A technician's request, from the project to the submitted request; returns its number."""
    start_draft(page, project)
    for reference, quantity in lines.items():
        add_material(page, reference, quantity)
    page.get_by_role("link", name="Review and submit").click()
    page.wait_for_url("**/review/")
    page.select_option("#id_priority", label=priority)
    page.select_option("#id_reviewer", label=reviewer)
    if notes:
        page.fill("#id_notes", notes)
    page.click("main form button[type=submit]")
    page.wait_for_url(re.compile(r"/requests/[0-9a-f-]{36}/$"))
    heading = str(page.locator("h1").inner_text()).strip()
    assert REQUEST_NUMBER.fullmatch(heading), heading
    return heading


# --- reviewer and material manager --------------------------------------------------------


def open_in_inbox(page: Page, number: str, status: str = "") -> None:
    """The review page of ``number``, reached from the approval inbox."""
    page.goto("/requests/inbox/" + (f"?status={status}" if status else ""))
    row = page.locator("[data-testid=inbox-row]", has_text=number)
    row.get_by_role("link").first.click()
    page.wait_for_url("**/examine/")
    expect(page.get_by_test_id("examine-number")).to_have_text(number)


def approve_all(page: Page, number: str) -> None:
    open_in_inbox(page, number)
    page.get_by_test_id("approve-all").click()
    page.wait_for_url("**/examine/")
    expect(page.locator("main")).to_contain_text("Approved")
    expect(page.get_by_test_id("approve-all")).to_have_count(0)


def review_line(page: Page, reference: str) -> Locator:
    return page.get_by_test_id("review-line").filter(has_text=reference)


def source_by_purchase(page: Page, number: str, references: list[str]) -> None:
    """The material manager decides, line by line, that the material is bought."""
    open_in_inbox(page, number, status="APPROVED")
    for reference in references:
        form = review_line(page, reference).get_by_test_id("sourcing-form")
        form.locator("select").select_option(label="Purchase")
        form.get_by_role("button", name="Save sourcing").click()
        page.wait_for_url("**/examine/")
        expect(review_line(page, reference)).to_contain_text("Purchase")


def order_from_worklist(
    page: Page, number: str, references: list[str], supplier: str, ship_to: str
) -> str:
    """Tick the request's lines on the worklist, make the draft order, price it and issue it."""
    page.goto("/purchasing/")
    for reference in references:
        group = page.get_by_test_id("worklist-material").filter(has_text=reference)
        group.get_by_test_id("worklist-line").filter(has_text=number).locator(
            "input[name=line]"
        ).check()
    page.get_by_role("button", name="Start an order with the ticked lines").click()
    page.wait_for_url("**/purchasing/new/**")
    page.select_option("#id_supplier", label=supplier)
    page.select_option("#id_ship_to", label=ship_to)
    page.get_by_test_id("new-order-form").get_by_role("button", name="Create the draft").click()
    page.wait_for_url(re.compile(r"/purchasing/orders/[0-9a-f-]{36}/$"))
    expect(page.locator("h1")).to_have_text("Draft order")
    issue_order(page)
    return str(page.locator("h1").inner_text()).strip()


def issue_order(page: Page, unit_price: str = "1.25") -> None:
    for editor in page.locator("main details", has_text="Edit the line").all():
        editor.locator("summary").click()
        editor.locator("input[name=unit_price]").fill(unit_price)
        editor.get_by_role("button").first.click()
        page.wait_for_load_state()
    page.locator("[data-testid=issue-form] input[name=confirm]").check()
    page.locator("[data-testid=issue-form] button[type=submit]").click()
    expect(page.locator("main")).to_contain_text("Issued")


# --- receiving and handovers --------------------------------------------------------------


def open_receipt_form(page: Page, order_number: str) -> None:
    page.goto("/receiving/orders/")
    page.fill("#id_q", order_number)
    row = page.get_by_test_id("receivable-order").filter(has_text=order_number)
    row.get_by_role("link", name="Receive").click()
    page.wait_for_url("**/receive/")


def post_receipt(page: Page, quantities: dict[str, str], delivery_note: str = "") -> str:
    """Fill ``quantities`` (reference -> accepted quantity, "" for nothing arrived), post the
    receipt; returns its number."""
    if delivery_note:
        page.fill("#id_delivery_note_ref", delivery_note)
    for reference, quantity in quantities.items():
        line = page.get_by_test_id("receipt-line").filter(has_text=reference)
        line.locator("input[name^=qty_]").fill(quantity)
    page.get_by_test_id("receipt-form").get_by_role("button", name="Post the receipt").click()
    page.wait_for_url(lambda url: "/receive/" not in url)
    expect(page.get_by_test_id("receipt-number")).to_be_visible()
    return str(page.get_by_test_id("receipt-number").inner_text()).strip()


def hand_over(
    page: Page, project: str, number: str, quantities: dict[str, str], receiver: str
) -> str:
    """Hand material of the warehouse over, for the request ``number``, to a person of the
    project; returns the handover's number."""
    page.goto("/handovers/new/")
    page.get_by_test_id("handover-project").filter(has_text=project).get_by_role("link").click()
    page.wait_for_url("**/handovers/new/?project=*")
    page.select_option("#id_receiver_user", label=receiver)
    for reference, quantity in quantities.items():
        line = (
            page.get_by_test_id("handover-line").filter(has_text=reference).filter(has_text=number)
        )
        line.locator("input[name^=qty_]").fill(quantity)
    page.get_by_test_id("handover-form").get_by_role(
        "button", name="Hand the material over"
    ).click()
    page.wait_for_url(re.compile(r"/handovers/[0-9a-f-]{36}/$"))
    return str(page.get_by_test_id("handover-number").inner_text()).strip()


def receive_at_worksite(
    page: Page, project: str, place: str, order_number: str, quantities: dict[str, str]
) -> str:
    """The site manager finds the order among the expected deliveries and receives its goods."""
    page.goto("/receiving/expected/")
    page.get_by_test_id("expected-project").filter(has_text=project).get_by_role("link").click()
    order = page.get_by_test_id("expected-order").filter(has_text=order_number)
    order.get_by_role("link", name="Receive these goods").click()
    page.wait_for_url("**/receive/**")
    expect(page.get_by_test_id("place-name")).to_contain_text(place)
    return post_receipt(page, quantities)


def change_line(
    page: Page, reference: str, quantity: str, reason: str, *, approve: bool = False
) -> None:
    """A reviewer's edit of a line (``Change the line``), saved, or saved and approved."""
    review_line(page, reference).get_by_test_id("edit-line").click()
    page.wait_for_url("**/edit/")
    form = page.get_by_test_id("line-form")
    form.locator("input[name=requested_qty]").fill(quantity)
    form.locator("[name=reason]").fill(reason)
    form.get_by_test_id("save-and-approve" if approve else "save-edit").click()
    page.wait_for_url("**/examine/")


def invite(
    page: Page, email: str, full_name: str, role: str, branch: str = "", employer: str = ""
) -> None:
    """The group administrator invites a person; the page lists the invitation afterwards."""
    page.goto("/org/invitations/new/")
    page.fill("#id_email", email)
    page.fill("#id_full_name", full_name)
    if employer:
        page.select_option("#id_employer_company", label=employer)
    page.select_option("#id_role", label=role)
    if branch:
        page.select_option("#id_branch", label=branch)
    page.get_by_role("button", name="Send invitation").click()
    page.wait_for_url(lambda url: "/invitations/new/" not in url)


def _settle(page: Page, move: Callable[[], object]) -> None:
    """A history step, then let htmx's history restore (a reload of the address) settle. The
    reload can cancel the step's own navigation (``ERR_ABORTED``): the page is then where the
    reload takes it."""
    try:
        move()
    except PlaywrightError as error:
        if "ERR_ABORTED" not in str(error):
            raise
    page.wait_for_load_state("networkidle")


def back(page: Page) -> None:
    _settle(page, page.go_back)


def forward(page: Page) -> None:
    _settle(page, page.go_forward)

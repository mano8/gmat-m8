"""A group administrator sets up an organization without the Django admin (M10.6): a company, a
branch managed by another, a warehouse and a unit of measure, then deactivates the branch and
the person who worked there loses their access. Every step goes through the screens of M10.2 and
M10.3; the dialogs state what they do, and the deactivation's preview names who is left without
a branch or project."""

import re
import uuid

from playwright.sync_api import Locator, Page, expect

from tests.e2e import pages
from tests.e2e.conftest import Browsing
from tests.e2e.mailbox import Mailbox
from tests.e2e.provision import Person, sign_in


def open_lazy(page: Page, opener: str, dialog: str) -> Locator:
    """A button that loads its form into the page's shared dialog."""
    page.get_by_test_id(opener).first.click()
    found = page.get_by_test_id(dialog)
    expect(found).to_be_visible()
    return found


def submit(page: Page, dialog: Locator, button: str) -> None:
    """A valid form: the page navigates to what was made, or reloads with the flash."""
    pages.send_dialog(page, dialog, button)


def choose_containing(select: Locator, text: str) -> None:
    """The option of a select whose label holds ``text`` (labels carry codes and names)."""
    labels = select.locator("option").all_inner_texts()
    match = [label for label in labels if text in label]
    assert match, f"no option with {text!r} among {labels}"
    select.select_option(label=match[0])


def create_company(page: Page, code: str, name: str) -> None:
    page.goto("/org/structure/companies/")
    dialog = open_lazy(page, "company-new-open", "company-new-dialog")
    form = dialog.get_by_test_id("company-new-form")
    form.locator("[name=code]").fill(code)
    form.locator("[name=legal_name]").fill(name)
    submit(page, dialog, "company-new-submit")
    expect(page.get_by_test_id("page-header")).to_contain_text(name)
    # The head-office branch was made with the company.
    expect(page.get_by_test_id("company-branch")).to_have_count(1)


def create_managed_branch(page: Page, company_code: str, code: str, name: str) -> None:
    dialog = open_lazy(page, "branch-new-open", "branch-new-dialog")
    form = dialog.get_by_test_id("branch-new-form")
    form.locator("[name=code]").fill(code)
    form.locator("[name=name]").fill(name)
    choose_containing(form.locator("[name=managed_by]"), f"{company_code}-")
    submit(page, dialog, "branch-new-submit")
    expect(page.get_by_test_id("page-header")).to_contain_text(name)
    expect(page.get_by_test_id("managing-branch")).to_contain_text(company_code)


def create_warehouse(page: Page, name: str) -> None:
    dialog = open_lazy(page, "warehouse-new-open", "warehouse-new-dialog")
    form = dialog.get_by_test_id("warehouse-new-form")
    form.locator("[name=name]").fill(name)
    submit(page, dialog, "warehouse-new-submit")
    expect(page.get_by_role("status").filter(has_text=f"Warehouse {name} created.")).to_be_visible()
    page.goto("/org/warehouses/?q=" + name)
    expect(page.get_by_test_id("warehouse-row").filter(has_text=name)).to_have_count(1)


def create_unit(page: Page, code: str, name: str) -> None:
    page.goto("/catalog/units/")
    dialog = open_lazy(page, "unit-new-open", "unit-new-dialog")
    form = dialog.get_by_test_id("unit-new-form")
    form.locator("[name=code]").fill(code)
    form.locator("[name=name]").fill(name)
    form.locator("[name=decimals]").fill("2")
    submit(page, dialog, "unit-new-submit")
    expect(page.get_by_test_id("unit-row").filter(has_text=name)).to_be_visible()


def accept_and_sign_in(browsing: Browsing, mailbox: Mailbox, person: Person) -> Page:
    link = mailbox.link(
        mailbox.wait_for(person.email, containing="/invite/"),
        "/invite/",
    )
    page = browsing.anonymous("phone")
    page.goto(link)
    expect(page).to_have_url(re.compile(r"/invite/accept/$"))
    page.fill("#id_password1", person.password)
    page.fill("#id_password2", person.password)
    page.click("main form button[type=submit]")
    page.wait_for_url("**/accounts/login/")
    sign_in(page, person)
    return page


def test_set_up_an_organization_then_deactivate_a_branch(
    browsing: Browsing, mailbox: Mailbox
) -> None:
    run = uuid.uuid4().hex[:6].upper()
    code, branch_code, unit = f"S{run}", "SECOND", f"U{run}"
    company_name, branch_name = f"Setup {run} SA", f"Second {run}"
    person = Person(f"setup-{run.lower()}")

    admin = browsing.as_("admin", "desktop")
    create_company(admin, code, company_name)
    create_managed_branch(admin, code, branch_code, branch_name)
    create_warehouse(admin, f"Depot {run}")
    create_unit(admin, unit, f"Crate {run}")

    # A person whose only role is at the new branch.
    pages.invite(
        admin,
        person.email,
        f"Setup Person {run}",
        "Warehouse operator",
        branch=f"{code}-{branch_code} · {branch_name}",
    )
    worker = accept_and_sign_in(browsing, mailbox, person)
    response = worker.goto("/receiving/orders/")
    assert response is not None and response.status == 200

    # Deactivating the branch shows who it leaves without access before it is confirmed.
    admin.goto("/org/structure/branches/")
    admin.get_by_test_id("branch-row").filter(has_text=branch_name).get_by_role(
        "link"
    ).first.click()
    dialog = open_lazy(admin, "activity-open", "activity-dialog")
    losing = dialog.get_by_test_id("preview-losing")
    expect(losing).to_contain_text(f"Setup Person {run}")
    expect(dialog.get_by_test_id("preview-memberships")).to_have_text("1")
    submit(admin, dialog, "activity-submit")
    expect(admin.get_by_test_id("page-header")).to_contain_text("Inactive")

    # The person's role grants nothing now: the page that opened is refused.
    response = worker.goto("/receiving/orders/")
    assert response is not None and response.status == 403

"""How a person is named on a page (ADR-0029): by name, never by address, but where the
maintainer allows the address."""

from types import SimpleNamespace

from django.template import Context, Template

from apps.accounts.people import NO_NAME, name_and_address, name_of

ADA = SimpleNamespace(full_name=" Ada Lin ", email="ada@example.org")
NAMELESS = SimpleNamespace(full_name="", email="old@example.org")


def render(text: str, user: object) -> str:
    return Template("{% load people %}" + text).render(Context({"user": user}))


def test_a_person_is_named_never_addressed() -> None:
    assert (name_of(ADA), name_of(NAMELESS), name_of(None)) == ("Ada Lin", NO_NAME, NO_NAME)
    assert render("{{ user|person }}", NAMELESS) == NO_NAME


def test_the_address_only_through_the_contact_filter() -> None:
    assert name_and_address(ADA) == "Ada Lin <ada@example.org>"
    assert name_and_address(NAMELESS) == "old@example.org"
    assert render("{{ user|person_contact }}", ADA) == "Ada Lin &lt;ada@example.org&gt;"

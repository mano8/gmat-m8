"""``{% load people %}``: ``user|person`` (the name, never the address) and
``user|person_contact`` (the name and the address, for the screens ADR-0029 allows)."""

from typing import Any

from django import template

from apps.accounts.people import name_and_address, name_of

register = template.Library()


@register.filter
def person(user: Any) -> str:
    return name_of(user)


@register.filter
def person_contact(user: Any) -> str:
    return name_and_address(user)

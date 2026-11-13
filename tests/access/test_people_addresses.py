"""No page shows another person's address to who does not manage people (ADR-0029, M9.4).

Every GET route of the acceptance matrix (every object position, the htmx variants and the
later pages included) is read as each actor who holds no managing role (group administrator,
material manager, engineer): the warehouse operator, the site manager, the technician, the
partner and the revoked or inactive cases. None may see the address of another person of the
fixture. The fixture's people have no full name, so a page that fell back to the address
(the rule before ADR-0029) is found here. The requests CSV export keeps the requester's
address by the maintainer's choice; it is reserved to the managing roles anyway.
"""

import re
from typing import Any

import pytest

from apps.access.roles import PRIVILEGED_ROLES
from apps.organizations.models import Membership
from tests.access.matrix import ACTORS, World, send
from tests.access.test_acceptance_matrix import ROUTES

pytestmark = pytest.mark.django_db

# Requesters of the world's documents who are none of the actors.
OUTSIDERS = ("sentinel-requester@example.org", "sev-requester@example.org")
KEPT = ("exports:",)  # the requests CSV keeps the requester's address (ADR-0029 §4)


def not_managing(world: World) -> list[str]:
    """Signed-in actors whose memberships hold no privileged role, nor a superuser."""
    privileged = set(
        Membership.objects.filter(role__in=PRIVILEGED_ROLES).values_list("user_id", flat=True)
    )
    names = []
    for actor in ACTORS:
        if actor.user is None:
            continue
        user = world.user(actor.user)
        if user.pk not in privileged and not user.is_superuser:
            names.append(actor.name)
    return names


def body(response: Any) -> str:
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content).decode("utf-8", "replace")
    return str(response.content.decode("utf-8", "replace"))


def others(world: World, actor: str) -> list[str]:
    """The addresses an actor may not see: everybody's but their own."""
    own = world.user(str(next(a.user for a in ACTORS if a.name == actor))).email
    everybody = [user.email for user in world.people.all().values()] + list(OUTSIDERS)
    return [address for address in everybody if address != own]


def test_the_actors_checked_are_the_ones_meant(world: World) -> None:
    assert {"wo_mad", "tech_mad", "site_manager", "partner"} <= set(not_managing(world))
    assert not {"group_admin", "operator", "mm_mad", "mm_sev", "engineer", "mixed"} & set(
        not_managing(world)
    )


@pytest.mark.parametrize("key", sorted(key for key in ROUTES if not key.startswith(KEPT)))
def test_no_page_shows_another_persons_address(world: World, key: str) -> None:
    problems = []
    for actor in not_managing(world):
        addresses = "|".join(re.escape(address) for address in others(world, actor))
        pattern = re.compile(rf"(?<![\w.+-])(?:{addresses})(?![\w.-])", re.I)
        for route in ROUTES[key]:
            response = send(world.client(actor), "GET", route.url(key, world), None, route.headers)
            found = pattern.search(body(response))
            if found:
                label = f"{key} ({route.position})" if route.position else key
                problems.append(f"GET {label} as {actor}: shows {found.group(0)}")
    assert problems == [], "\n".join(problems)

from unittest import mock

import pytest
from django.db import OperationalError
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
class TestHealth:
    def test_healthz_is_ok_and_uncached(self, client: Client) -> None:
        response = client.get(reverse("core:healthz"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert "no-cache" in response["Cache-Control"]
        assert "no-store" in response["Cache-Control"]

    def test_readyz_checks_database(self, client: Client) -> None:
        response = client.get(reverse("core:readyz"))
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readyz_hides_failure_details(self, client: Client) -> None:
        with mock.patch(
            "apps.core.views.connection.cursor",
            side_effect=OperationalError("password authentication failed for gmat"),
        ):
            response = client.get(reverse("core:readyz"))
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert b"password" not in response.content

    @pytest.mark.parametrize("name", ["core:healthz", "core:readyz"])
    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_unsupported_methods_are_405(self, client: Client, name: str, method: str) -> None:
        response = getattr(client, method)(reverse(name))
        assert response.status_code == 405

    @pytest.mark.parametrize("name", ["core:healthz", "core:readyz"])
    def test_head_is_allowed(self, client: Client, name: str) -> None:
        assert client.head(reverse(name)).status_code == 200

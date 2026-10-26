import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.core.ids import new_id


@pytest.mark.django_db
class TestUser:
    def test_create_user_normalizes_email_and_sets_defaults(self) -> None:
        user = User.objects.create_user("Tech.One@Example.ORG", "a-long-test-password")
        assert user.email == "tech.one@example.org"
        assert user.is_active and not user.is_staff and not user.is_superuser
        assert user.offboarded_at is None
        assert user.privilege_elevated_at is None
        assert user.authz_generation == 0
        assert user.session_epoch == 0
        assert user.check_password("a-long-test-password")
        assert str(user) == "tech.one@example.org"

    def test_primary_key_is_uuid7(self) -> None:
        user = User.objects.create_user("a@example.org")
        assert isinstance(user.pk, uuid.UUID)
        assert user.pk.version == 7

    def test_new_ids_are_time_ordered(self) -> None:
        first, second = new_id(), new_id()
        assert first < second

    def test_create_superuser_is_staff_and_superuser(self) -> None:
        user = User.objects.create_superuser("root@example.org", "a-long-test-password")
        assert user.is_staff and user.is_superuser

    def test_create_user_ignores_privilege_flags_by_default(self) -> None:
        user = User.objects.create_user("b@example.org")
        assert not user.is_staff and not user.is_superuser

    def test_email_required(self) -> None:
        with pytest.raises(ValueError, match="email"):
            User.objects.create_user("")

    def test_email_unique_regardless_of_case(self) -> None:
        User.objects.create_user("dup@example.org")
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user("DUP@example.org")

    def test_database_rejects_mixed_case_email(self) -> None:
        user = User(email="Mixed@Example.org")
        with pytest.raises(IntegrityError), transaction.atomic():
            user.save()

    def test_clean_lowercases_email(self) -> None:
        user = User(email="  Mixed@Example.org ")
        user.clean()
        assert user.email == "mixed@example.org"

    def test_login_field_is_email(self) -> None:
        assert User.USERNAME_FIELD == "email"

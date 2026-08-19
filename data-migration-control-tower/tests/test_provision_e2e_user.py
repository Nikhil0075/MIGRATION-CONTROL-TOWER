from __future__ import annotations

from dataclasses import dataclass

import pytest

from tools.provision_e2e_user import E2E_MARKER, delete_test_user, provision_user


class UserNotFoundError(Exception):
    pass


@dataclass
class FakeUser:
    uid: str
    email: str
    custom_claims: dict | None = None


class FakeAuth:
    UserNotFoundError = UserNotFoundError

    def __init__(self, user: FakeUser | None = None):
        self.user = user
        self.claims = None
        self.deleted = None
        self.revoked = None

    def get_user_by_email(self, email):
        if self.user is None or self.user.email != email:
            raise UserNotFoundError()
        return self.user

    def create_user(self, **kwargs):
        self.user = FakeUser("uid-created", kwargs["email"])
        return self.user

    def update_user(self, uid, **kwargs):
        assert self.user and self.user.uid == uid
        return self.user

    def set_custom_user_claims(self, uid, claims):
        self.claims = claims
        assert self.user and self.user.uid == uid
        self.user.custom_claims = claims

    def revoke_refresh_tokens(self, uid):
        self.revoked = uid

    def delete_user(self, uid):
        self.deleted = uid


def test_new_user_gets_full_e2e_roles_without_returning_the_password() -> None:
    auth = FakeAuth()
    result = provision_user(
        "E2E.Operator@Example.Test",
        "Strong-E2E-Password-2048",
        auth_module=auth,
    )
    assert result == {
        "uid": "uid-created",
        "email": "e2e.operator@example.test",
        "created": True,
        "estate_roles": {"*": ["approver", "operator", "viewer"]},
    }
    assert auth.claims[E2E_MARKER] is True
    assert auth.revoked == "uid-created"
    assert "password" not in result


def test_existing_non_test_identity_is_refused_without_explicit_authorization() -> None:
    auth = FakeAuth(FakeUser("human", "human@example.test", {"department": "finance"}))
    with pytest.raises(ValueError, match="non-test Firebase account"):
        provision_user(
            "human@example.test",
            "Strong-E2E-Password-2048",
            auth_module=auth,
        )


def test_existing_claims_and_estate_grants_are_preserved() -> None:
    user = FakeUser(
        "e2e",
        "e2e@example.test",
        {E2E_MARKER: True, "department": "migration", "estate_roles": {"estate-a": ["viewer"]}},
    )
    auth = FakeAuth(user)
    provision_user(
        user.email,
        "Strong-E2E-Password-2048",
        estates=("estate-a", "estate-b"),
        auth_module=auth,
    )
    assert auth.claims["department"] == "migration"
    assert auth.claims["estate_roles"]["estate-a"] == ["approver", "operator", "viewer"]
    assert auth.claims["estate_roles"]["estate-b"] == ["approver", "operator", "viewer"]


def test_configured_domain_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROL_TOWER_E2E_EMAIL_DOMAIN", "corp.example")
    with pytest.raises(ValueError, match="@corp.example"):
        provision_user(
            "e2e@other.example",
            "Strong-E2E-Password-2048",
            auth_module=FakeAuth(),
        )


def test_weak_password_is_refused_before_firebase_is_called() -> None:
    with pytest.raises(ValueError, match="14 characters"):
        provision_user("e2e@example.test", "too-short", auth_module=FakeAuth())


def test_only_marked_test_accounts_can_be_deleted() -> None:
    auth = FakeAuth(FakeUser("human", "human@example.test", {}))
    with pytest.raises(ValueError, match="not marked"):
        delete_test_user("human@example.test", auth_module=auth)
    assert auth.deleted is None

    auth.user.custom_claims = {E2E_MARKER: True}
    result = delete_test_user("human@example.test", auth_module=auth)
    assert result["deleted"] is True
    assert auth.deleted == "human"

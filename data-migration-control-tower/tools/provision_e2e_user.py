#!/usr/bin/env python
"""Provision a dedicated Firebase account for dashboard E2E testing.

The browser login form authenticates an identity; it never grants roles.
This administrator-run command is the separate authorization step. It uses
Firebase Admin, marks the account as test-only, and grants viewer/operator/
approver through the same ``estate_roles`` custom claim production uses.

Passwords are accepted only from CONTROL_TOWER_E2E_PASSWORD or a no-echo
prompt. They are deliberately not command-line arguments, logs, or files.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
from collections.abc import Iterable
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]

E2E_MARKER = "migration_control_tower_e2e"
E2E_ROLES = ("viewer", "operator", "approver")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _normalized_email(email: str) -> str:
    normalized = email.strip().lower()
    if not EMAIL_RE.fullmatch(normalized):
        raise ValueError("A valid domain email address is required.")
    allowed_domain = os.environ.get("CONTROL_TOWER_E2E_EMAIL_DOMAIN", "").strip().lower()
    allowed_domain = allowed_domain.removeprefix("@")
    if allowed_domain and normalized.rsplit("@", 1)[1] != allowed_domain:
        raise ValueError(
            f"The E2E account must use the configured @{allowed_domain} domain."
        )
    return normalized


def _validate_password(password: str) -> str:
    if len(password) < 14:
        raise ValueError("The E2E password must contain at least 14 characters.")
    categories = sum(
        bool(pattern.search(password))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"), re.compile(r"[^A-Za-z0-9]"))
    )
    if categories < 3:
        raise ValueError("The E2E password must use at least three character categories.")
    return password


def _claims(existing: dict | None, estates: Iterable[str]) -> dict:
    claims = dict(existing or {})
    raw_grants = claims.get("estate_roles")
    grants = dict(raw_grants) if isinstance(raw_grants, dict) else {}
    estate_ids = [estate.strip() for estate in estates if estate.strip()] or ["*"]
    for estate_id in estate_ids:
        current = grants.get(estate_id, [])
        current_roles = set(current if isinstance(current, (list, tuple, set)) else [])
        grants[estate_id] = sorted(current_roles | set(E2E_ROLES))
    claims["estate_roles"] = grants
    claims[E2E_MARKER] = True
    return claims


def _admin_auth():
    import firebase_admin
    from firebase_admin import auth

    if not firebase_admin._apps:
        project_id = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID")
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID or GCP_PROJECT_ID must be configured.")
        # End-user Application Default Credentials do not carry a quota
        # project by default. Identity Toolkit rejects Admin user-management
        # calls in that state even when the identity has permission. Binding
        # quota to the same Firebase project makes local provisioning behave
        # like service-account-backed deployment credentials.
        os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", project_id)
        firebase_admin.initialize_app(options={"projectId": project_id})
    return auth


def provision_user(
    email: str,
    password: str,
    *,
    estates: Iterable[str] = ("*",),
    authorize_existing: bool = False,
    auth_module=None,
) -> dict:
    """Create/rotate a test account and attach role claims.

    Existing non-test accounts are refused unless ``authorize_existing`` is
    explicit. This prevents a typo from setting a password or broad claims on
    someone's normal Google identity.
    """
    normalized = _normalized_email(email)
    validated_password = _validate_password(password)
    auth = auth_module or _admin_auth()

    created = False
    try:
        user = auth.get_user_by_email(normalized)
    except auth.UserNotFoundError:
        user = auth.create_user(
            email=normalized,
            password=validated_password,
            email_verified=True,
            display_name="Migration Control Tower E2E Operator",
            disabled=False,
        )
        created = True
    else:
        existing_claims = dict(user.custom_claims or {})
        if not existing_claims.get(E2E_MARKER) and not authorize_existing:
            raise ValueError(
                "That email already belongs to a non-test Firebase account. "
                "Use --authorize-existing only if changing that identity is intentional."
            )
        user = auth.update_user(
            user.uid,
            password=validated_password,
            email_verified=True,
            disabled=False,
        )

    custom_claims = _claims(user.custom_claims, estates)
    auth.set_custom_user_claims(user.uid, custom_claims)
    auth.revoke_refresh_tokens(user.uid)
    return {
        "uid": user.uid,
        "email": normalized,
        "created": created,
        "estate_roles": custom_claims["estate_roles"],
    }


def delete_test_user(email: str, *, auth_module=None) -> dict:
    normalized = _normalized_email(email)
    auth = auth_module or _admin_auth()
    user = auth.get_user_by_email(normalized)
    if not (user.custom_claims or {}).get(E2E_MARKER):
        raise ValueError("Refusing to delete an account that is not marked as an MCT E2E user.")
    auth.delete_user(user.uid)
    return {"uid": user.uid, "email": normalized, "deleted": True}


def _password_from_environment_or_prompt() -> str:
    password = os.environ.get("CONTROL_TOWER_E2E_PASSWORD")
    if password:
        return password
    first = getpass.getpass("E2E password (14+ characters, not stored): ")
    second = getpass.getpass("Confirm E2E password: ")
    if first != second:
        raise ValueError("The passwords do not match.")
    return first


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("CONTROL_TOWER_E2E_EMAIL"),
        help="Dedicated test email. Defaults to CONTROL_TOWER_E2E_EMAIL.",
    )
    parser.add_argument(
        "--estate",
        action="append",
        dest="estates",
        help="Estate ID to authorize. Repeatable; defaults to '*' for the full E2E flow.",
    )
    parser.add_argument(
        "--authorize-existing",
        action="store_true",
        help="Explicitly allow adding password auth/claims to an existing non-test account.",
    )
    parser.add_argument("--delete", action="store_true", help="Delete this marked E2E account.")
    args = parser.parse_args()
    if not args.email:
        parser.error("--email or CONTROL_TOWER_E2E_EMAIL is required")

    if args.delete:
        result = delete_test_user(args.email)
        print(f"Deleted E2E user {result['email']} ({result['uid']}).")
        return

    result = provision_user(
        args.email,
        _password_from_environment_or_prompt(),
        estates=args.estates or ("*",),
        authorize_existing=args.authorize_existing,
    )
    action = "Created" if result["created"] else "Updated"
    print(f"{action} E2E user {result['email']} ({result['uid']}).")
    print(f"Authorized estate roles: {result['estate_roles']}")
    print("Sign out of the console, then sign in with this email and password.")


if __name__ == "__main__":
    main()

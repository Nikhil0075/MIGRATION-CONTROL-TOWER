"""Authentication and role-based authorization for the Control Tower UI.

Firebase proves identity; roles are granted to the account, never chosen
by the person signing in — otherwise anyone with a Google account could
self-grant operator and onboard estates.

Two ways an account gets a role:

  1. Firebase custom claims (`estate_roles`) — per-estate, the production
     path.
  2. OPERATOR_ALLOWLIST / APPROVER_ALLOWLIST — a deliberately small
     bootstrap for local development and first run. Global, unscoped, and
     meant to be emptied once claims are populated.

**Roles are scoped to an estate** (Day 11 Phase 5). A global `operator`
role was defensible while the platform ran one estate; once a deployment
hosts several, it means an operator onboarded for one customer's estate
can start runs against another's. The claim shape is:

    {
      "estate_roles": {
        "*": ["viewer"],                             # every estate
        "wwi-demo-estate": ["operator", "approver"]  # this estate only
      }
    }

The previous `{"roles": ["operator"]}` shape is still accepted and means
`{"*": ["operator"]}` — a global grant. Existing tokens keep working, and
`GET /api/v1/session` still reports a flat `roles` list (the union across
estates) so the console's coarse "can this user act at all?" checks are
unchanged.

**Known ceiling, stated rather than discovered later.** Firebase custom
claims are capped at roughly 1000 bytes, so this shape stops scaling past
roughly 15-20 estates. The escape hatch is an `estate_grants/{uid}`
Firestore collection as the authority with claims as a cache; the
`roles_for()` seam below is where that would attach. Not built yet — this
deployment model is one estate per project (§32.10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from fastapi import Header, HTTPException

KNOWN_ROLES = frozenset({"viewer", "operator", "approver"})

#: Grants that apply to every estate.
WILDCARD_ESTATE = "*"

#: Beyond roughly this many per-estate grants a token risks exceeding
#: Firebase's custom-claim size limit. Surfaced by session() so the
#: problem is visible before tokens start failing to mint.
ESTATE_GRANT_SOFT_LIMIT = 15


@dataclass(frozen=True)
class UserContext:
    uid: str
    email: str
    #: Union of every role this user holds anywhere. Coarse route gating
    #: (require_role) uses this; anything that acts on a specific estate
    #: must use has_role(role, estate_id) instead.
    roles: frozenset[str]
    estate_roles: Mapping[str, frozenset[str]] = None  # type: ignore[assignment]

    def roles_for(self, estate_id: str | None) -> frozenset[str]:
        """Roles this user holds on one estate: wildcard grants plus that
        estate's own. `None` means "not estate-specific" and returns the
        wildcard grants only."""
        grants = self.estate_roles or {}
        combined = set(grants.get(WILDCARD_ESTATE, frozenset()))
        if estate_id:
            combined |= set(grants.get(estate_id, frozenset()))
        return frozenset(combined)

    def has_role(self, role: str, estate_id: str | None = None) -> bool:
        if estate_id is None:
            return role in self.roles
        return role in self.roles_for(estate_id)

    @property
    def scoped_estates(self) -> list[str]:
        return sorted(e for e in (self.estate_roles or {}) if e != WILDCARD_ESTATE)


def _load_allowlist(variable: str) -> list[str]:
    raw = os.environ.get(variable, "")
    return [entry.strip().lower() for entry in raw.split(",") if entry.strip()]


def _load_approver_allowlist() -> list[str]:
    return _load_allowlist("APPROVER_ALLOWLIST")


def _load_operator_allowlist() -> list[str]:
    """Emails granted `operator` without Firebase custom claims.

    Without this there was no way to hold the operator role at all. A
    normal Google sign-in carries no custom claims, so a real user got an
    empty role set: every read returned 403 and "Onboard estate" was
    permanently disabled. The only grant path was APPROVER_ALLOWLIST,
    which grants `approver` — not `operator` — so nobody could onboard an
    estate through the console at all.

    Same deliberately small bootstrap shape as APPROVER_ALLOWLIST, and the
    same limitation: it is a GLOBAL grant with no estate scope, intended
    for local development and first-run bootstrap. Real deployments should
    populate per-estate `estate_roles` custom claims and leave both
    allowlists empty.
    """
    return _load_allowlist("OPERATOR_ALLOWLIST")


def _is_allowlisted(email: str, allowlist: list[str]) -> bool:
    normalized = email.strip().lower()
    domain = normalized.split("@", 1)[-1] if "@" in normalized else ""
    return any(
        (entry.startswith("@") and domain == entry[1:])
        or (not entry.startswith("@") and normalized == entry)
        for entry in allowlist
    )


def _normalize_roles(raw) -> set[str]:
    """Accepts a string, list, or {role: enabled} mapping."""
    if isinstance(raw, str):
        roles = {raw}
    elif isinstance(raw, dict):
        roles = {name for name, enabled in raw.items() if enabled}
    elif isinstance(raw, (list, tuple, set)):
        roles = {str(value) for value in raw}
    else:
        roles = set()
    roles &= KNOWN_ROLES
    # Elevated roles inherit read access; a token with no recognized role is
    # authenticated but receives no console authorization.
    if roles & {"operator", "approver"}:
        roles.add("viewer")
    return roles


def _estate_roles_from_claims(decoded: dict) -> dict[str, frozenset[str]]:
    """Builds the estate -> roles map from either claim shape.

    `roles` (the pre-Phase-5 shape) is treated as a wildcard grant, which
    is what makes every existing token keep working. `estate_roles` is
    merged on top, so a token may carry both.
    """
    grants: dict[str, set[str]] = {}

    legacy = _normalize_roles(decoded.get("roles", []))
    if legacy:
        grants[WILDCARD_ESTATE] = set(legacy)

    scoped = decoded.get("estate_roles")
    if isinstance(scoped, dict):
        for estate_id, raw in scoped.items():
            roles = _normalize_roles(raw)
            if roles:
                grants.setdefault(str(estate_id), set()).update(roles)

    return {estate_id: frozenset(roles) for estate_id, roles in grants.items()}


def _roles_from_claims(decoded: dict) -> set[str]:
    """Union of every role held anywhere. Kept as a module function
    because it is the coarse check require_role() uses and the shape
    GET /api/v1/session reports."""
    union: set[str] = set()
    for roles in _estate_roles_from_claims(decoded).values():
        union |= set(roles)
    return union


def get_user_context(authorization: str | None = Header(default=None)) -> UserContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in is required.")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="The bearer token is empty.")

    import firebase_admin
    from firebase_admin import auth as firebase_auth

    if not firebase_admin._apps:
        project_id = os.environ.get("FIREBASE_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID")
        firebase_admin.initialize_app(options={"projectId": project_id})
    try:
        decoded = firebase_auth.verify_id_token(token)
    except Exception as exc:  # noqa: BLE001 - all verification failures are authentication failures
        raise HTTPException(status_code=401, detail=f"Invalid or expired sign-in token: {exc}") from exc

    email = str(decoded.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=401, detail="The sign-in token has no email claim.")

    estate_roles = {k: set(v) for k, v in _estate_roles_from_claims(decoded).items()}

    # APPROVER_ALLOWLIST stays a GLOBAL grant for one more release. It
    # predates estate scoping and has no estate to attach to; narrowing it
    # silently would lock out the existing approval flow. Populate
    # estate_roles claims, then remove it.
    if _is_allowlisted(email, _load_approver_allowlist()):
        estate_roles.setdefault(WILDCARD_ESTATE, set()).update({"approver", "viewer"})
    if _is_allowlisted(email, _load_operator_allowlist()):
        estate_roles.setdefault(WILDCARD_ESTATE, set()).update({"operator", "viewer"})

    frozen = {estate_id: frozenset(roles) for estate_id, roles in estate_roles.items()}
    union = frozenset().union(*frozen.values()) if frozen else frozenset()
    return UserContext(
        uid=str(decoded.get("uid") or decoded.get("sub") or email),
        email=email,
        roles=union,
        estate_roles=frozen,
    )


def require_role(role: str) -> Callable:
    """Coarse route gate: does this user hold `role` on ANY estate?

    Deliberately not the whole story. A user with `operator` on estate A
    passes this gate for a request naming estate B — the estate-specific
    check is authorize_estate(), called inside each mutating handler. The
    two-step exists because a FastAPI dependency cannot cleanly read an
    arbitrary request-body field, and tests/test_estate_rbac.py enumerates
    every mutating route to prove none of them skips the second step.
    """

    def dependency(authorization: str | None = Header(default=None)) -> UserContext:
        user = get_user_context(authorization)
        if not user.has_role(role):
            raise HTTPException(status_code=403, detail=f"The {role!r} role is required for this action.")
        return user

    return dependency


def authorize_estate(user: UserContext, estate_id: str | None, role: str) -> None:
    """Estate-specific authorization. Raises 403 when the user lacks `role`
    on `estate_id`.

    Every handler that changes something must call this. The error names
    the estate but never enumerates the user's other grants — knowing
    which estates exist is itself information a caller may not be entitled
    to.
    """
    if not user.has_role(role, estate_id):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{user.email!r} does not hold the {role!r} role on estate "
                f"{estate_id!r}."
            ),
        )


def get_approver_identity(authorization: str | None = Header(default=None)) -> str:
    user = get_user_context(authorization)
    if not user.has_role("approver"):
        raise HTTPException(
            status_code=403,
            detail=f"{user.email!r} is authenticated but not authorized to approve cutovers.",
        )
    return user.email

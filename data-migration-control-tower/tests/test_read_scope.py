"""An unscoped read must not return another estate's data.

The bug this file exists for: `_authorize_read(user, estate_id)` was
`if estate_id: authorize_estate(...)`, so a read with NO estate filter
authorized nothing at all and every aggregate returned rows from every
estate in the project. That was not an exotic path — `estatePath()` in
the console omits the scope whenever no estate is active, which it is on
first load, so the default console request was the unauthorized one.

Two kinds of coverage here, and both are needed:

  * behaviour, at the two chokepoints (`_for_estate`, `_all_estates`)
    every estate-scoped read funnels through, plus the thread pool and
    the response cache, which are the two places a correct filter can
    still be undone; and
  * an enumeration over the router, so that an endpoint written next
    month cannot reopen the hole by forgetting a call.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from frontend import api_v1
from frontend.security import UserContext


def _envelope(data: list) -> dict:
    return {"data": data, "meta": {"freshness": "live"}}


def _user(**estate_roles: list[str]) -> UserContext:
    grants = {estate: frozenset(roles) for estate, roles in estate_roles.items()}
    union = frozenset().union(*grants.values()) if grants else frozenset()
    return UserContext(uid="u", email="u@example.internal", roles=union, estate_roles=grants)


@pytest.fixture()
def scope_reset():
    token = api_v1._READ_SCOPE.set(None)
    yield
    api_v1._READ_SCOPE.reset(token)


RUNS = [
    {"run_id": "a", "estate_id": "alpha"},
    {"run_id": "b", "estate_id": "beta"},
    {"run_id": "legacy"},  # pre-Phase-2: no estate_id, belongs to the default
]


def test_the_scope_is_applied_when_no_estate_filter_was_asked_for(scope_reset):
    """The whole point: omitting the filter must narrow, never widen."""
    api_v1._READ_SCOPE.set(frozenset({"alpha"}))
    assert [r["run_id"] for r in api_v1._for_estate(RUNS, None)] == ["a"]


def test_asking_for_an_estate_outside_the_scope_returns_nothing(scope_reset):
    # Reaching `_for_estate` at all means `_authorize_read` already
    # rejected an unauthorized explicit estate with a 403. This is the
    # second line: even if some future caller skips that check, the data
    # does not come back.
    api_v1._READ_SCOPE.set(frozenset({"alpha"}))
    assert api_v1._for_estate(RUNS, "beta") == []


def test_a_record_with_no_estate_id_is_scoped_as_the_default_estate(scope_reset):
    from tools.connection_context import DEFAULT_ESTATE_ID

    api_v1._READ_SCOPE.set(frozenset({DEFAULT_ESTATE_ID}))
    assert [r["run_id"] for r in api_v1._for_estate(RUNS, None)] == ["legacy"]


def test_an_unrestricted_scope_leaves_everything_alone(scope_reset):
    api_v1._READ_SCOPE.set(None)
    assert len(api_v1._for_estate(RUNS, None)) == 3


def test_a_wildcard_grant_resolves_to_unrestricted_not_to_a_list_of_estates():
    """`None`, not "every estate that exists right now".

    Enumerating would drop a run whose estate document has since been
    deleted — and would cost a registry read on every request.
    """
    assert api_v1._visible_estate_ids(_user(**{"*": ["viewer"]})) is None


def test_a_scoped_grant_resolves_to_exactly_the_granted_estates(monkeypatch):
    monkeypatch.setattr(
        "tools.connection_context.list_estate_documents",
        lambda: [{"estate_id": "alpha"}, {"estate_id": "beta"}, {"estate_id": "gamma"}],
    )
    visible = api_v1._visible_estate_ids(_user(alpha=["viewer"], gamma=["operator", "viewer"]))
    assert visible == frozenset({"alpha", "gamma"})


def test_a_grant_that_is_not_viewer_does_not_confer_read(monkeypatch):
    monkeypatch.setattr(
        "tools.connection_context.list_estate_documents",
        lambda: [{"estate_id": "alpha"}],
    )
    assert api_v1._visible_estate_ids(_user(alpha=["approver"])) == frozenset()


def test_estate_listings_are_scoped_too(monkeypatch, scope_reset):
    monkeypatch.setattr(
        "tools.connection_context.list_estate_documents",
        lambda: [{"estate_id": "alpha"}, {"estate_id": "beta"}],
    )
    api_v1._READ_SCOPE.set(frozenset({"beta"}))
    assert [e["estate_id"] for e in api_v1._all_estates()] == ["beta"]


def test_overlapped_reads_do_not_escape_the_scope(scope_reset):
    """The subtle one.

    `_gather` overlaps Firestore round trips on a thread pool, and a pool
    thread starts with an EMPTY context. Without an explicit context copy
    the reads run with `_READ_SCOPE` unset — so the optimisation that
    made the console fast would have silently undone the authorization
    the endpoint had just performed, on exactly the endpoints
    (`/approvals`, `/policies`, `/incidents`) that read the most.
    """
    api_v1._READ_SCOPE.set(frozenset({"alpha"}))
    seen = api_v1._gather({key: (lambda: api_v1._READ_SCOPE.get()) for key in ("x", "y", "z")})
    assert set(seen.values()) == {frozenset({"alpha"})}


def test_the_response_cache_cannot_serve_one_grant_to_another(monkeypatch, scope_reset):
    """Otherwise the cache is the leak the filter was meant to close.

    Two users hit `_cached("runs-source:None", ...)`, the first stores a
    scoped result, and the second is served it — with more rows than they
    are entitled to, or fewer, depending on who arrived first.
    """
    monkeypatch.setattr(api_v1, "_CACHE_TTL_SECONDS", 60)
    api_v1.clear_response_cache()

    # Exercised through `_cached`, not through `_scope_key`. Asserting
    # that two scopes hash differently proves nothing about the cache if
    # the cache never consults the hash — which is exactly the mistake
    # the first version of this test made.
    api_v1._READ_SCOPE.set(frozenset({"alpha"}))
    assert api_v1._cached("runs-source:None", lambda: _envelope(["alpha-row"]))["data"] == ["alpha-row"]

    api_v1._READ_SCOPE.set(frozenset({"beta"}))
    served = api_v1._cached("runs-source:None", lambda: _envelope(["beta-row"]))
    assert served["data"] == ["beta-row"], "a second grant was served the first one's cached rows"

    # And the first user still gets a hit, so scoping the key did not
    # simply disable caching.
    api_v1._READ_SCOPE.set(frozenset({"alpha"}))
    again = api_v1._cached("runs-source:None", lambda: _envelope(["should-not-rebuild"]))
    assert again["data"] == ["alpha-row"]
    assert again["meta"]["freshness"] == "cached"


def test_the_scope_key_does_not_depend_on_grant_ordering(scope_reset):
    api_v1._READ_SCOPE.set(frozenset({"alpha", "beta"}))
    one = api_v1._scope_key()
    api_v1._READ_SCOPE.set(frozenset(["beta", "alpha"]))
    assert api_v1._scope_key() == one


def test_every_authenticated_read_route_is_scoped_by_the_router_itself():
    """The enumeration, and the reason this is a router dependency.

    Scoping each endpoint by hand means the guarantee is "nobody forgot",
    which is not a guarantee. Hanging it off the router makes the DEFAULT
    for a route added tomorrow a scoped read; forgetting `_authorize_read`
    can then only make a response too narrow, which someone notices,
    rather than too wide, which nobody does.
    """
    dependencies = [dep.dependency for dep in api_v1.router.dependencies]
    assert api_v1._scope_reads in dependencies

    scoped_routes = [
        route
        for route in api_v1.router.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    ]
    # A guard against this test passing vacuously if the router is ever
    # restructured and the read endpoints move elsewhere.
    assert len(scoped_routes) > 10


def test_the_scope_dependency_is_async_so_sync_endpoints_can_see_it():
    """Not a style preference — a correctness requirement.

    A sync endpoint runs in a worker thread with a COPY of the request's
    context. A context variable set by an async dependency is part of
    that context and therefore visible. One set by a SYNC dependency is
    set inside a different copy and is silently gone by the time the
    endpoint runs, leaving every read unscoped while the code reads as
    though it were protected.
    """
    import inspect

    assert inspect.iscoroutinefunction(api_v1._scope_reads)


@pytest.mark.requires_firestore
def test_a_single_estate_viewer_gets_one_estate_from_an_unscoped_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end proof, through the real request pipeline.

    Everything above tests the pieces. This is the only test that
    exercises the handoff the whole design depends on: an ASYNC router
    dependency setting a context variable that a SYNC endpoint, running
    in a worker thread with a copy of that context, has to be able to
    read. Get that wrong and every unit test above still passes while
    every real request runs unscoped.

    `/estates` with no `estate_id`, from a viewer granted exactly one.
    """
    import firebase_admin
    from fastapi.testclient import TestClient
    from firebase_admin import auth

    from frontend.app import app

    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    monkeypatch.delenv("OPERATOR_ALLOWLIST", raising=False)
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda _token: {
            "uid": "scoped-user",
            "email": "scoped-user@example.internal",
            # No legacy `roles` key: that shape is a WILDCARD grant, which
            # would make this test pass without any scoping at all.
            "estate_roles": {"beta": ["viewer"]},
        },
    )
    monkeypatch.setattr(
        "tools.connection_context.list_estate_documents",
        lambda: [
            {"estate_id": "alpha", "display_name": "Alpha"},
            {"estate_id": "beta", "display_name": "Beta"},
            {"estate_id": "gamma", "display_name": "Gamma"},
        ],
    )
    api_v1.clear_response_cache()

    response = TestClient(app).get(
        "/api/v1/estates", headers={"Authorization": "Bearer verified-token"}
    )
    assert response.status_code == 200
    assert [estate["estate_id"] for estate in response.json()["data"]] == ["beta"]

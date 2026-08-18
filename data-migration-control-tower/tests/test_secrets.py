"""Credential resolution and per-estate connection binding (Day 11 Phase 1).

Secret Manager is always monkeypatched here — never a real API call, so
these run in any environment and cannot leak or consume a real secret.

The tests worth reading first are the negative ones: that a resolution
failure raises instead of returning an empty password, that a silent
fallback is impossible (it always logs, naming the path), and that a
resolved credential does not appear in any repr. Those three are the
properties that make "the wizard collects references, not secrets" true in
practice rather than only in the schema.
"""

from __future__ import annotations

import logging

import pytest

from tools import secret_resolver as secrets
from tools.connection_context import (
    DEFAULT_ESTATE_ID,
    EstateNotFound,
    SourceBinding,
    SourceNotFound,
    binding_for,
    binding_from_estate,
    list_estate_documents,
    load_estate_document,
)
from tools.secret_resolver import (
    BACKEND_ENV,
    BACKEND_ENV_FALLBACK,
    BACKEND_SECRET_MANAGER,
    ResolvedConnection,
    SecretResolutionError,
    resolve_connection,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _clean_secret_cache():
    secrets.clear_cache()
    yield
    secrets.clear_cache()


def _fake_secret_manager(monkeypatch, value="from-secret-manager", exc=None):
    calls = []

    def _fake(ref, project_id):
        calls.append(ref)
        if exc is not None:
            raise exc
        return value

    monkeypatch.setattr(secrets, "_from_secret_manager", _fake)
    return calls


# ---------------------------------------------------------------------------
# resolve_secret
# ---------------------------------------------------------------------------


def test_no_ref_uses_declared_env_fallback(monkeypatch):
    monkeypatch.setenv("MY_PASSWORD", "s3cret")
    assert resolve_secret(None, env_fallback="MY_PASSWORD") == "s3cret"


def test_no_ref_and_no_fallback_raises():
    with pytest.raises(SecretResolutionError, match="no env_fallback"):
        resolve_secret(None)


def test_env_prefixed_ref_reads_that_variable(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "pg-secret")
    assert resolve_secret("env:PGPASSWORD") == "pg-secret"


def test_env_prefixed_ref_does_not_fall_back(monkeypatch):
    """An explicit 'env:' reference names one variable. Falling back to a
    different one would resolve a credential the operator did not name."""
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.setenv("OTHER", "wrong-secret")
    with pytest.raises(SecretResolutionError, match="unset or empty"):
        resolve_secret("env:PGPASSWORD", env_fallback="OTHER")


def test_secret_manager_is_used_when_available(monkeypatch):
    calls = _fake_secret_manager(monkeypatch, value="sm-value")
    assert resolve_secret("wwi-password", env_fallback="IGNORED") == "sm-value"
    assert calls == ["wwi-password"]


def test_empty_env_value_is_treated_as_unset(monkeypatch):
    """An empty password becomes a confusing driver-level auth error much
    later; fail here, where the cause is still visible."""
    monkeypatch.setenv("MY_PASSWORD", "")
    with pytest.raises(SecretResolutionError):
        resolve_secret(None, env_fallback="MY_PASSWORD")


def test_bare_ref_without_project_raises(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    with pytest.raises(SecretResolutionError, match="no project"):
        secrets._qualified_name("wwi-password", None)


def test_full_resource_path_is_used_verbatim():
    ref = "projects/other-project/secrets/shared/versions/3"
    assert secrets._qualified_name(ref, "ignored") == ref


def test_bare_ref_is_qualified_against_the_project():
    assert (
        secrets._qualified_name("wwi-password", "proj")
        == "projects/proj/secrets/wwi-password/versions/latest"
    )


# ---------------------------------------------------------------------------
# The fallback path — the most dangerous convenience in the module
# ---------------------------------------------------------------------------


def test_fallback_fires_when_secret_manager_is_unavailable(monkeypatch):
    _fake_secret_manager(monkeypatch, exc=RuntimeError("no ADC"))
    monkeypatch.setenv("SQLSERVER_PASSWORD", "local-dev")
    assert resolve_secret("wwi-password", env_fallback="SQLSERVER_PASSWORD") == "local-dev"


def test_fallback_always_logs_which_path_answered(monkeypatch, caplog):
    """A fallback firing unnoticed in production yields a *working*
    connection to the wrong thing. It must never be silent."""
    _fake_secret_manager(monkeypatch, exc=RuntimeError("permission denied"))
    monkeypatch.setenv("SQLSERVER_PASSWORD", "local-dev")
    with caplog.at_level(logging.WARNING, logger="secrets"):
        resolve_secret("wwi-password", env_fallback="SQLSERVER_PASSWORD")
    assert caplog.records, "fallback must emit a WARNING"
    message = caplog.records[0].getMessage()
    assert "SQLSERVER_PASSWORD" in message
    assert "NOT Secret Manager" in message


def test_fallback_never_logs_the_secret_value(monkeypatch, caplog):
    _fake_secret_manager(monkeypatch, exc=RuntimeError("permission denied"))
    monkeypatch.setenv("SQLSERVER_PASSWORD", "hunter2-do-not-log")
    with caplog.at_level(logging.DEBUG, logger="secrets"):
        resolve_secret("wwi-password", env_fallback="SQLSERVER_PASSWORD")
    for record in caplog.records:
        assert "hunter2-do-not-log" not in record.getMessage()


def test_no_fallback_declared_means_the_failure_surfaces(monkeypatch):
    _fake_secret_manager(monkeypatch, exc=RuntimeError("secret not found"))
    with pytest.raises(SecretResolutionError, match="no password_env fallback"):
        resolve_secret("wwi-password")


def test_backend_is_reported_for_the_fallback_path(monkeypatch):
    _fake_secret_manager(monkeypatch, exc=RuntimeError("no ADC"))
    monkeypatch.setenv("SQLSERVER_PASSWORD", "local-dev")
    resolve_secret("wwi-password", env_fallback="SQLSERVER_PASSWORD")
    assert secrets.resolution_backend("wwi-password") == BACKEND_ENV_FALLBACK
    assert "fallback" in secrets.describe_resolution(
        "wwi-password", env_fallback="SQLSERVER_PASSWORD"
    )


def test_backend_is_reported_for_the_secret_manager_path(monkeypatch):
    _fake_secret_manager(monkeypatch, value="sm")
    resolve_secret("wwi-password")
    assert secrets.resolution_backend("wwi-password") == BACKEND_SECRET_MANAGER


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_repeated_resolution_hits_the_cache(monkeypatch):
    calls = _fake_secret_manager(monkeypatch, value="v")
    resolve_secret("wwi-password")
    resolve_secret("wwi-password")
    assert len(calls) == 1


def test_clear_cache_forces_re_resolution(monkeypatch):
    calls = _fake_secret_manager(monkeypatch, value="v")
    resolve_secret("wwi-password")
    secrets.clear_cache()
    resolve_secret("wwi-password")
    assert len(calls) == 2


def test_cache_expires_so_a_rotated_secret_is_picked_up(monkeypatch):
    calls = _fake_secret_manager(monkeypatch, value="v")
    monkeypatch.setenv("SECRET_CACHE_TTL_SECONDS", "0")
    resolve_secret("wwi-password")
    resolve_secret("wwi-password")
    assert len(calls) == 2, "a zero TTL must not serve a stale credential"


# ---------------------------------------------------------------------------
# ResolvedConnection — damage control on logging
# ---------------------------------------------------------------------------


def test_resolved_connection_redacts_its_password():
    conn = ResolvedConnection(
        host="h", port=1433, database="db", user="sa", password="hunter2"
    )
    assert "hunter2" not in repr(conn)
    assert "hunter2" not in str(conn)
    assert "hunter2" not in f"{conn}"
    assert conn.password == "hunter2"


def test_resolved_connection_password_survives_formatting_in_logs(caplog):
    conn = ResolvedConnection(
        host="h", port=1433, database="db", user="sa", password="hunter2"
    )
    log = logging.getLogger("test_secrets_repr")
    with caplog.at_level(logging.INFO, logger="test_secrets_repr"):
        log.info("connecting with %s", conn)
    assert "hunter2" not in caplog.records[0].getMessage()


# ---------------------------------------------------------------------------
# resolve_connection
# ---------------------------------------------------------------------------


def test_resolve_connection_prefers_literals_over_env(monkeypatch):
    monkeypatch.setenv("H", "from-env")
    monkeypatch.setenv("PW", "pw")
    conn = resolve_connection(
        {"host": "literal-host", "host_env": "H", "port": 1433,
         "user": "sa", "password_env": "PW"},
        config={"database": "db"},
    )
    assert conn.host == "literal-host"


def test_resolve_connection_reads_env_names(monkeypatch):
    monkeypatch.setenv("H", "envhost")
    monkeypatch.setenv("P", "1444")
    monkeypatch.setenv("U", "envuser")
    monkeypatch.setenv("PW", "pw")
    conn = resolve_connection(
        {"host_env": "H", "port_env": "P", "user_env": "U", "password_env": "PW"},
        config={"database": "db"},
    )
    assert (conn.host, conn.port, conn.user, conn.database) == ("envhost", 1444, "envuser", "db")


def test_resolve_connection_applies_adapter_defaults(monkeypatch):
    monkeypatch.setenv("SQLSERVER_PASSWORD", "pw")
    from tools.connection_context import ADAPTER_CONNECTION_DEFAULTS

    conn = resolve_connection(
        {}, config={}, defaults=ADAPTER_CONNECTION_DEFAULTS["sqlserver"]
    )
    assert (conn.host, conn.port, conn.user) == ("localhost", 1433, "sa")
    assert conn.database == "WideWorldImporters"


def test_resolve_connection_rejects_a_non_numeric_port(monkeypatch):
    monkeypatch.setenv("P", "not-a-port")
    monkeypatch.setenv("PW", "pw")
    with pytest.raises(SecretResolutionError, match="non-numeric port"):
        resolve_connection({"port_env": "P", "password_env": "PW"}, config={"database": "d"})


# ---------------------------------------------------------------------------
# SourceBinding / estate documents
# ---------------------------------------------------------------------------


def test_committed_demo_estate_is_discoverable():
    estate = load_estate_document(DEFAULT_ESTATE_ID)
    assert estate["estate_id"] == DEFAULT_ESTATE_ID
    assert [s["source_id"] for s in estate["sources"]] == [
        "wwi-sqlserver", "oracle-corpus", "dag-artifacts",
    ]


def test_list_estate_documents_includes_the_demo_estate():
    assert DEFAULT_ESTATE_ID in {e["estate_id"] for e in list_estate_documents()}


def test_unknown_estate_names_what_is_available():
    """The message must say where it looked — Phase 2 added the Firestore
    registry in front of the YAML search path, so 'not found' now means
    'in neither place', and the error says so."""
    with pytest.raises(EstateNotFound, match="in the registry or on disk"):
        load_estate_document("no-such-estate")
    with pytest.raises(EstateNotFound, match="Known committed estates"):
        load_estate_document("no-such-estate")


def test_unknown_source_names_what_is_declared():
    with pytest.raises(SourceNotFound, match="Declared sources"):
        binding_for(DEFAULT_ESTATE_ID, "no-such-source")


def test_binding_carries_the_declared_connection_profile():
    binding = binding_for(DEFAULT_ESTATE_ID, "wwi-sqlserver")
    assert binding.adapter == "sqlserver"
    assert binding.config == {"database": "WideWorldImporters"}
    assert binding.requires_connection
    assert binding.connection_profile["password_secret_ref"] == "sqlserver-wwi-password"


def test_static_file_sources_declare_no_connection():
    binding = binding_for(DEFAULT_ESTATE_ID, "oracle-corpus")
    assert not binding.requires_connection
    with pytest.raises(SourceNotFound, match="static-file source"):
        binding.resolve()


def test_health_key_is_scoped_by_estate():
    """Two estates using the same adapter must not overwrite each other's
    connection_health snapshot."""
    a = SourceBinding(estate_id="estate-a", source_id="src", adapter="sqlserver")
    b = SourceBinding(estate_id="estate-b", source_id="src", adapter="sqlserver")
    assert a.health_key != b.health_key
    assert a.health_key == "estate-a__src"


def test_demo_estate_resolves_through_the_documented_env_fallback(monkeypatch):
    """The committed estate declares a password_secret_ref and no
    password_env; the sqlserver adapter defaults supply SQLSERVER_PASSWORD,
    which is what keeps a plain local checkout working exactly as before."""
    _fake_secret_manager(monkeypatch, exc=RuntimeError("no Secret Manager locally"))
    monkeypatch.setenv("SQLSERVER_PASSWORD", "local-dev-password")
    conn = binding_for(DEFAULT_ESTATE_ID, "wwi-sqlserver").resolve()
    assert conn.database == "WideWorldImporters"
    assert conn.password == "local-dev-password"
    assert conn.backend == BACKEND_ENV_FALLBACK


def test_two_estates_resolve_independently(monkeypatch):
    """The property this whole phase exists for: before it, both of these
    would have taken their password from one process-global variable."""
    monkeypatch.setenv("ESTATE_A_PW", "password-a")
    monkeypatch.setenv("ESTATE_B_PW", "password-b")

    estate_a = {
        "estate_id": "estate-a",
        "sources": [{
            "source_id": "src", "adapter": "sqlserver",
            "config": {"database": "DB_A"},
            "connection_profile": {"host": "host-a", "port": 1433, "user": "ua",
                                   "password_env": "ESTATE_A_PW"},
        }],
    }
    estate_b = {
        "estate_id": "estate-b",
        "sources": [{
            "source_id": "src", "adapter": "sqlserver",
            "config": {"database": "DB_B"},
            "connection_profile": {"host": "host-b", "port": 1434, "user": "ub",
                                   "password_env": "ESTATE_B_PW"},
        }],
    }

    a = binding_from_estate(estate_a, "src").resolve()
    b = binding_from_estate(estate_b, "src").resolve()

    assert (a.host, a.port, a.user, a.database, a.password) == (
        "host-a", 1433, "ua", "DB_A", "password-a")
    assert (b.host, b.port, b.user, b.database, b.password) == (
        "host-b", 1434, "ub", "DB_B", "password-b")


def test_backend_defaults_to_env_when_no_ref_is_declared(monkeypatch):
    monkeypatch.setenv("PW", "pw")
    conn = resolve_connection({"host": "h", "port": 1, "user": "u", "password_env": "PW"})
    assert conn.backend == BACKEND_ENV

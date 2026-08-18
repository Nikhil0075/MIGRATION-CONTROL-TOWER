"""Credential resolution by reference (Day 11 Phase 1, master doc §32.2).

Named `secret_resolver`, not `secrets`, on purpose. Python puts a script's
own directory at sys.path[0], so `python tools/anything.py` would make a
`tools/secrets.py` shadow the standard library's `secrets` module for the
whole process — which broke tools/export_openapi.py the first time it ran
(starlette does `from secrets import token_hex`). The stdlib name is not
worth the ambiguity.

Before this module, `password_secret_ref` in estate.yaml was decoration:
the value was declared, documented as "what a real deployment would use",
and then never read by anything. tools/sqlserver_client.py took its
password from a single process-global `SQLSERVER_PASSWORD`, which is why
two SQL Server estates could not coexist in one process — the second
estate would silently connect with the first estate's credentials.

The contract here is deliberately narrow:

  - The onboarding wizard, estate.yaml, Firestore and the API all carry
    **references only** (a Secret Manager name, or the NAME of an
    environment variable). contracts/metadata_model.json's
    ConnectionProfile is closed (`additionalProperties: false`) and
    tests/test_contracts.py asserts it declares no credential-value
    field, so this is enforced rather than merely intended.
  - Resolution happens here, as late as possible, and the resolved value
    is wrapped in ResolvedConnection whose repr redacts it. A plain dict
    of connection parameters gets logged eventually; a type that cannot
    print its own password does not.

Fallback discipline (the Rung-2 pattern CLAUDE.md documents for ADK,
Gemini and Gemma): when Secret Manager is unavailable — package not
installed, no ADC, missing IAM binding, secret absent — resolution falls
back to the profile's declared `password_env` if there is one, and logs a
WARNING **naming which path answered**. This is the single most dangerous
convenience in this module: a fallback firing unnoticed in a real
deployment yields a *working* connection to the wrong database. That is
why the path is always logged, never silent, and why
describe_resolution() exists so health_check() can surface it in the UI.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("secrets")

DEFAULT_CACHE_TTL_SECONDS = 300

#: Prefix that forces environment-variable resolution, e.g. "env:PGPASSWORD".
ENV_REF_PREFIX = "env:"

BACKEND_SECRET_MANAGER = "secret_manager"
BACKEND_ENV = "env"
BACKEND_ENV_FALLBACK = "env_fallback"


class SecretResolutionError(RuntimeError):
    """A credential reference could not be resolved by any available path."""


@dataclass(frozen=True)
class ResolvedConnection:
    """Live connection parameters, with the password shielded from repr.

    `password` is a real value — this object must never be returned from an
    API handler, written to Firestore, or placed in a Pub/Sub payload. Its
    repr redacts so that logging the object, or a traceback that includes
    it as a local, does not leak the credential; that is damage control,
    not permission to pass it around.
    """

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    backend: str = BACKEND_ENV
    """Which resolution path produced `password` — surfaced by health_check."""

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"ResolvedConnection(host={self.host!r}, port={self.port!r}, "
            f"database={self.database!r}, user={self.user!r}, "
            f"password=<redacted>, backend={self.backend!r})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# TTL cache — a plain lru_cache would pin a rotated secret for process life
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[str, float, str]] = {}
_cache_lock = threading.Lock()


def _cache_ttl() -> int:
    try:
        return int(os.environ.get("SECRET_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS))
    except ValueError:
        return DEFAULT_CACHE_TTL_SECONDS


def clear_cache() -> None:
    """Drops every cached secret. Called by tests, and safe to call after a
    deliberate rotation rather than waiting out the TTL."""
    with _cache_lock:
        _cache.clear()


def _cache_get(key: str) -> tuple[str, str] | None:
    with _cache_lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    value, expires_at, backend = entry
    if time.monotonic() >= expires_at:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return value, backend


def _cache_put(key: str, value: str, backend: str) -> None:
    with _cache_lock:
        _cache[key] = (value, time.monotonic() + _cache_ttl(), backend)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _qualified_name(ref: str, project_id: str | None) -> str:
    """Bare names are qualified against the configured project; a full
    resource path is used verbatim so a shared//cross-project secret can be
    referenced explicitly."""
    if ref.startswith("projects/"):
        return ref if "/versions/" in ref else f"{ref}/versions/latest"
    project = project_id or os.environ.get("GCP_PROJECT_ID")
    if not project:
        raise SecretResolutionError(
            f"Cannot resolve bare secret reference {ref!r}: no project. Set "
            f"GCP_PROJECT_ID, pass project_id, or use a full "
            f"'projects/{{p}}/secrets/{{name}}/versions/latest' reference."
        )
    return f"projects/{project}/secrets/{ref}/versions/latest"


def _from_secret_manager(ref: str, project_id: str | None) -> str:
    # Imported lazily: this project runs locally without the package and
    # without ADC, and importing at module scope would make every source
    # that never touches Secret Manager depend on it.
    from google.cloud import secretmanager

    name = _qualified_name(ref, project_id)
    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


def resolve_secret(
    ref: str | None,
    *,
    env_fallback: str | None = None,
    project_id: str | None = None,
    use_cache: bool = True,
) -> str:
    """Resolves a credential reference to its value.

    Order:
      1. `ref` is None/empty  -> `env_fallback` only.
      2. `ref` starts "env:"  -> that environment variable (explicit, no fallback).
      3. otherwise            -> Secret Manager, falling back to `env_fallback`
                                 on any failure, with a WARNING naming the path.

    Raises SecretResolutionError when no path yields a value — never
    returns an empty string, because an empty password silently becomes a
    confusing driver-level authentication error much later.
    """
    if not ref:
        if not env_fallback:
            raise SecretResolutionError(
                "No credential reference and no env_fallback declared. Set "
                "password_secret_ref or password_env on the connection profile."
            )
        value = os.environ.get(env_fallback)
        if not value:
            raise SecretResolutionError(
                f"Environment variable {env_fallback!r} is unset or empty, and no "
                f"password_secret_ref was declared."
            )
        return value

    if ref.startswith(ENV_REF_PREFIX):
        var = ref[len(ENV_REF_PREFIX):]
        value = os.environ.get(var)
        if not value:
            raise SecretResolutionError(
                f"Credential reference {ref!r} names environment variable {var!r}, "
                f"which is unset or empty."
            )
        return value

    if use_cache:
        cached = _cache_get(ref)
        if cached is not None:
            return cached[0]

    try:
        value = _from_secret_manager(ref, project_id)
    except Exception as exc:  # noqa: BLE001 — any failure means "try the fallback"
        if not env_fallback:
            raise SecretResolutionError(
                f"Could not resolve secret {ref!r} from Secret Manager ({exc}), and "
                f"no password_env fallback is declared on this connection profile."
            ) from exc
        value = os.environ.get(env_fallback)
        if not value:
            raise SecretResolutionError(
                f"Could not resolve secret {ref!r} from Secret Manager ({exc}), and "
                f"the declared fallback {env_fallback!r} is unset or empty."
            ) from exc
        logger.warning(
            "secret %r resolved from environment variable %s, NOT Secret Manager "
            "(%s). In a deployed environment this usually means a missing IAM "
            "binding or an absent secret — verify this is the intended source "
            "before trusting the connection.",
            ref, env_fallback, exc,
        )
        if use_cache:
            _cache_put(ref, value, BACKEND_ENV_FALLBACK)
        return value

    logger.debug("secret %r resolved from Secret Manager", ref)
    if use_cache:
        _cache_put(ref, value, BACKEND_SECRET_MANAGER)
    return value


def resolution_backend(ref: str | None, *, env_fallback: str | None = None) -> str:
    """Which path most recently answered for `ref` (best-effort, cache-based).

    Used by adapter health_check() to report *how* it authenticated, so an
    operator can see in the UI that a source is running on the local-dev
    fallback rather than Secret Manager.
    """
    if not ref:
        return BACKEND_ENV
    if ref.startswith(ENV_REF_PREFIX):
        return BACKEND_ENV
    cached = _cache_get(ref)
    if cached is not None:
        return cached[1]
    return BACKEND_SECRET_MANAGER


def describe_resolution(ref: str | None, *, env_fallback: str | None = None) -> str:
    """Human-readable, credential-free description for logs and the UI."""
    backend = resolution_backend(ref, env_fallback=env_fallback)
    if backend == BACKEND_SECRET_MANAGER:
        return f"Secret Manager reference {ref!r}"
    if backend == BACKEND_ENV_FALLBACK:
        return f"environment variable {env_fallback!r} (Secret Manager fallback)"
    if ref and ref.startswith(ENV_REF_PREFIX):
        return f"environment variable {ref[len(ENV_REF_PREFIX):]!r}"
    return f"environment variable {env_fallback!r}"


# ---------------------------------------------------------------------------
# Connection profiles
# ---------------------------------------------------------------------------


def _profile_value(profile: dict, literal_key: str, env_key: str, default=None):
    """A profile may name a value literally (`host`) or name the environment
    variable holding it (`host_env`). Literal wins; neither is a secret."""
    literal = profile.get(literal_key)
    if literal not in (None, ""):
        return literal
    env_name = profile.get(env_key)
    if env_name:
        from_env = os.environ.get(env_name)
        if from_env not in (None, ""):
            return from_env
    return default


def resolve_connection(
    profile: dict | None,
    *,
    config: dict | None = None,
    defaults: dict | None = None,
) -> ResolvedConnection:
    """Turns a ConnectionProfile plus adapter config into live parameters.

    `config` is the estate source's adapter kwargs, which is where the
    database name lives (`{"database": "WideWorldImporters"}`) — the
    profile describes how to reach the *server*, the config which database
    on it.

    `defaults` supplies per-adapter-family fallbacks (port, user) so this
    function stays source-agnostic.
    """
    profile = profile or {}
    config = config or {}
    defaults = defaults or {}

    host = _profile_value(profile, "host", "host_env", defaults.get("host", "localhost"))
    raw_port = _profile_value(profile, "port", "port_env", defaults.get("port"))
    user = _profile_value(profile, "user", "user_env", defaults.get("user"))
    database = config.get("database") or defaults.get("database")

    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise SecretResolutionError(
            f"Connection profile resolved a non-numeric port {raw_port!r}. Check the "
            f"profile's 'port' / 'port_env' declaration."
        ) from exc

    secret_ref = profile.get("password_secret_ref")
    env_fallback = profile.get("password_env") or defaults.get("password_env")
    password = resolve_secret(secret_ref, env_fallback=env_fallback)

    return ResolvedConnection(
        host=str(host),
        port=port,
        database=str(database) if database else "",
        user=str(user) if user else "",
        password=password,
        backend=resolution_backend(secret_ref, env_fallback=env_fallback),
    )

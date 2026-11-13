"""Blob storage for workflows: named stores over S3-compatible services, Azure Blob, GCS or local disk.

Stores are declared in ``kind: Storage`` documents and reached by name, through
:func:`get_store`. Everything is plain ``httpx`` plus request signing — no cloud SDK.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from tuvl.storage.azure import AzureBackend
from tuvl.storage.creds import AwsCredentialSource, azure_token_source, gcs_token_source
from tuvl.storage.dialects import GOOGLE
from tuvl.storage.errors import (
    AuthError,
    ConfigError,
    Conflict,
    Forbidden,
    InvalidKey,
    NotFound,
    RateLimited,
    StorageError,
    Timeout,
    TooLarge,
    Unavailable,
    Unsupported,
)
from tuvl.storage.gcs import GcsBackend
from tuvl.storage.http import Http
from tuvl.storage.local import LocalBackend
from tuvl.storage.s3 import S3Backend
from tuvl.storage.sigv4 import AwsCreds
from tuvl.storage.spec import (
    SECRET_FIELDS,
    STORE_REGISTRY,
    StoreSpec,
    check_endpoint,
    parse_store,
    register_store_from_dict,
)
from tuvl.storage.store import Store
from tuvl.storage.types import Backend, Blob, Listing, Presigned

GCS_S3_ENDPOINT = "https://storage.googleapis.com"
_OPEN: dict[str, tuple[StoreSpec, Store]] = {}


def _expand(value: str, store: str, field: str) -> str:
    from tuvl.core.engine.templating import expand_env

    try:
        return expand_env(value, source=f"storage '{store}' {field}") or ""
    except OSError as exc:
        raise ConfigError(str(exc)) from exc


def resolved(name: str, spec: StoreSpec) -> dict[str, Any]:
    """The spec's fields with ``${ENV}`` references expanded."""
    out: dict[str, Any] = {}
    for field, value in spec.model_dump().items():
        out[field] = _expand(value, name, field) if isinstance(value, str) else value
    try:
        out["endpoint"] = check_endpoint(out["endpoint"])
    except ValueError as exc:
        raise ConfigError(f"storage '{name}': {exc}") from exc
    return out


def required_env(spec: StoreSpec) -> list[str]:
    """Environment variables the store's ``${…}`` references read without a default."""
    import re

    names: list[str] = []
    for value in spec.model_dump().values():
        if isinstance(value, str):
            for var, default in re.findall(r"\$\{([^}:]+)(:[^}]*)?\}", value):
                if not default and var not in names:
                    names.append(var)
    return names


def build_backend(
    name: str, spec: StoreSpec, *, transport: httpx.AsyncBaseTransport | None = None
) -> Backend:
    r = resolved(name, spec)
    http = Http(spec.timeout, transport)
    meta = Http(2.0, transport)
    provider = spec.provider
    if provider == "local":
        from tuvl.core.config import settings

        root = Path(r["root"]).expanduser()
        return LocalBackend(root if root.is_absolute() else settings.project_dir / root)

    if provider == "azure":
        key, sas = r["account_key"], r["sas_token"]
        return AzureBackend(
            account=r["account"],
            container=r["container"],
            endpoint=r["endpoint"],
            account_key=key,
            sas_token=sas,
            token=None if key or sas else azure_token_source(http, meta),
            http=http,
        )

    access, secret = r["access_key"], r["secret_key"]
    if bool(access) != bool(secret):
        raise ConfigError(f"storage '{name}': access_key and secret_key go together")
    if provider == "gcs" and not access:
        return GcsBackend(
            bucket=r["bucket"],
            endpoint=r["endpoint"],
            token=gcs_token_source(
                http, access_token=r["access_token"], credentials=r["credentials"], meta=meta
            ),
            http=http,
        )
    static = AwsCreds(access, secret, r["session_token"]) if access else None
    region = r["region"] or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "")
    if provider == "gcs":
        return S3Backend(
            bucket=r["bucket"],
            region="auto",
            endpoint=r["endpoint"] or GCS_S3_ENDPOINT,
            path_style=True,
            creds=AwsCredentialSource(static=static, region="auto", http=http, metadata_http=meta),
            http=http,
            dialect=GOOGLE,
        )
    return S3Backend(
        bucket=r["bucket"],
        region=region,
        endpoint=r["endpoint"],
        path_style=r["path_style"],
        creds=AwsCredentialSource(
            static=static, region=region, role_arn=r["role_arn"], http=http, metadata_http=meta
        ),
        http=http,
    )


def get_store(name: str) -> Store:
    """The store declared under *name*, opened once per process (and per reload of its spec)."""
    spec = STORE_REGISTRY.get(name)
    if spec is None:
        known = ", ".join(sorted(STORE_REGISTRY)) or "none declared"
        raise ConfigError(f"unknown storage '{name}' ({known})")
    cached = _OPEN.get(name)
    if cached is not None and cached[0] is spec:
        return cached[1]
    store = Store(name, spec, build_backend(name, spec))
    _OPEN[name] = (spec, store)
    return store


async def close_all() -> None:
    stores = [s for _, s in _OPEN.values()]
    _OPEN.clear()
    for store in stores:
        await store.aclose()


__all__ = [
    "SECRET_FIELDS",
    "STORE_REGISTRY",
    "AuthError",
    "Backend",
    "Blob",
    "ConfigError",
    "Conflict",
    "Forbidden",
    "InvalidKey",
    "Listing",
    "NotFound",
    "Presigned",
    "RateLimited",
    "StorageError",
    "Store",
    "StoreSpec",
    "Timeout",
    "TooLarge",
    "Unavailable",
    "Unsupported",
    "build_backend",
    "close_all",
    "get_store",
    "parse_store",
    "register_store_from_dict",
    "required_env",
    "resolved",
]

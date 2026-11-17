"""Credential sources: static values, the environment, and the identity services of AWS, Azure and GCP."""

from __future__ import annotations

import asyncio
import json
import os
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from tuvl.storage.errors import AuthError, ConfigError, StorageError
from tuvl.storage.http import Http
from tuvl.storage.sigv4 import AwsCreds

SKEW = 60.0
_IMDS = "http://169.254.169.254"
_AZURE_SCOPE = "https://storage.azure.com/.default"
_GCS_SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
_GCS_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GCS_METADATA = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC).timestamp()


class CachedToken:
    """A bearer token fetched on demand and refreshed shortly before it expires."""

    def __init__(self, fetch: Callable[[], Awaitable[tuple[str, float]]]) -> None:
        self._fetch = fetch
        self._value = ""
        self._expires = 0.0
        self._lock: asyncio.Lock | None = None

    async def get(self) -> str:
        if self._value and time.time() < self._expires - SKEW:
            return self._value
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._value or time.time() >= self._expires - SKEW:
                self._value, self._expires = await self._fetch()
        return self._value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(root: ET.Element, name: str) -> str:
    for el in root.iter():
        if _local_name(el.tag) == name and el.text:
            return el.text.strip()
    return ""


# ── AWS ──────────────────────────────────────────────────────────────────────


class AwsCredentialSource:
    """Static keys, then the environment, web identity (EKS), container credentials, EC2 IMDSv2."""

    def __init__(
        self,
        *,
        static: AwsCreds | None,
        region: str,
        role_arn: str = "",
        http: Http,
        metadata_http: Http | None = None,
    ) -> None:
        self._static = static
        self._region = region
        self._role_arn = role_arn
        self._http = http
        self._meta = metadata_http or Http(timeout=2.0)
        self._cached: AwsCreds | None = None
        self._lock: asyncio.Lock | None = None

    async def get(self) -> AwsCreds:
        if self._static is not None:
            return self._static
        env = self._from_env()
        if env is not None:
            return env
        cached = self._cached
        if cached is not None and (cached.expires_at or 0) - SKEW > time.time():
            return cached
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            cached = self._cached
            if cached is not None and (cached.expires_at or 0) - SKEW > time.time():
                return cached
            self._cached = await self._discover()
            return self._cached

    @staticmethod
    def _from_env() -> AwsCreds | None:
        key, secret = os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")
        if key and secret:
            return AwsCreds(key, secret, os.environ.get("AWS_SESSION_TOKEN", ""))
        return None

    async def _discover(self) -> AwsCreds:
        token_file = os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        role = self._role_arn or os.environ.get("AWS_ROLE_ARN", "")
        if token_file and role:
            return await self._web_identity(Path(token_file), role)
        if os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") or os.environ.get(
            "AWS_CONTAINER_CREDENTIALS_FULL_URI"
        ):
            return await self._container()
        try:
            return await self._imds()
        except StorageError as exc:
            raise AuthError(
                "no AWS credentials: set access_key/secret_key, AWS_ACCESS_KEY_ID/"
                "AWS_SECRET_ACCESS_KEY, or run with a web identity, container or instance role"
            ) from exc

    async def _web_identity(self, token_file: Path, role: str) -> AwsCreds:
        try:
            token = (await asyncio.to_thread(token_file.read_text)).strip()
        except OSError as exc:
            raise ConfigError(f"cannot read AWS_WEB_IDENTITY_TOKEN_FILE: {exc}") from exc
        host = f"sts.{self._region}.amazonaws.com" if self._region else "sts.amazonaws.com"
        response = await self._http.expect(
            "POST",
            f"https://{host}/",
            data={
                "Action": "AssumeRoleWithWebIdentity",
                "Version": "2011-06-15",
                "RoleArn": role,
                "RoleSessionName": os.environ.get("AWS_ROLE_SESSION_NAME", "tuvl"),
                "WebIdentityToken": token,
            },
        )
        root = ET.fromstring(response.text)
        return AwsCreds(
            _xml_text(root, "AccessKeyId"),
            _xml_text(root, "SecretAccessKey"),
            _xml_text(root, "SessionToken"),
            _epoch(_xml_text(root, "Expiration")),
        )

    async def _container(self) -> AwsCreds:
        relative = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        url = (
            f"http://169.254.170.2{relative}"
            if relative
            else os.environ["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
        )
        headers: dict[str, str] = {}
        auth = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        auth_file = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")
        if auth_file:
            auth = (await asyncio.to_thread(Path(auth_file).read_text)).strip()
        if auth:
            headers["Authorization"] = auth
        response = await self._meta.expect("GET", url, headers=headers)
        return self._parse_json(response.json())

    async def _imds(self) -> AwsCreds:
        token = (
            await self._meta.expect(
                "PUT",
                f"{_IMDS}/latest/api/token",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            )
        ).text
        headers = {"X-aws-ec2-metadata-token": token}
        base = f"{_IMDS}/latest/meta-data/iam/security-credentials/"
        role = (await self._meta.expect("GET", base, headers=headers)).text.split()[0]
        return self._parse_json(
            (await self._meta.expect("GET", base + role, headers=headers)).json()
        )

    @staticmethod
    def _parse_json(doc: dict[str, str]) -> AwsCreds:
        expires = doc.get("Expiration")
        return AwsCreds(
            doc["AccessKeyId"],
            doc["SecretAccessKey"],
            doc.get("Token", ""),
            _epoch(expires) if expires else None,
        )


# ── Azure ────────────────────────────────────────────────────────────────────


def azure_token_source(http: Http, meta: Http | None = None) -> CachedToken:
    """Workload identity (AKS) when its variables are set, else managed identity (IMDS)."""
    meta = meta or Http(timeout=2.0)

    async def fetch() -> tuple[str, float]:
        file = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        tenant, client = os.environ.get("AZURE_TENANT_ID"), os.environ.get("AZURE_CLIENT_ID")
        if file and tenant and client:
            assertion = (await asyncio.to_thread(Path(file).read_text)).strip()
            authority = os.environ.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com")
            response = await http.expect(
                "POST",
                f"{authority.rstrip('/')}/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client,
                    "scope": _AZURE_SCOPE,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": assertion,
                },
            )
            doc = response.json()
            return doc["access_token"], time.time() + float(doc.get("expires_in", 3600))
        params = "api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F"
        if client:
            params += f"&client_id={client}"
        try:
            response = await meta.expect(
                "GET",
                f"{_IMDS}/metadata/identity/oauth2/token?{params}",
                headers={"Metadata": "true"},
            )
        except StorageError as exc:
            raise AuthError(
                "no Azure credentials: set account_key or sas_token, or run with a managed identity"
            ) from exc
        doc = response.json()
        return doc["access_token"], float(doc.get("expires_on") or time.time() + 3600)

    return CachedToken(fetch)


# ── GCP ──────────────────────────────────────────────────────────────────────


def _load_service_account(value: str) -> dict[str, str]:
    text = value.strip()
    try:
        doc = json.loads(text if text.startswith("{") else Path(text).read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(
            "credentials is neither service-account JSON nor a readable file"
        ) from exc
    if not isinstance(doc, dict) or "private_key" not in doc or "client_email" not in doc:
        raise ConfigError("service-account JSON needs client_email and private_key")
    return doc


def gcs_token_source(
    http: Http, *, access_token: str = "", credentials: str = "", meta: Http | None = None
) -> CachedToken:
    """A fixed token, a service account (JWT bearer grant), or the metadata server (GKE, GCE)."""
    meta = meta or Http(timeout=2.0)

    async def fetch() -> tuple[str, float]:
        if access_token:
            return access_token, time.time() + 3000
        if credentials:
            import jwt

            sa = _load_service_account(credentials)
            now = int(time.time())
            audience = sa.get("token_uri", _GCS_TOKEN_URL)
            assertion = jwt.encode(
                {
                    "iss": sa["client_email"],
                    "scope": _GCS_SCOPE,
                    "aud": audience,
                    "iat": now,
                    "exp": now + 3600,
                },
                sa["private_key"],
                algorithm="RS256",
            )
            response = await http.expect(
                "POST",
                audience,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        else:
            try:
                response = await meta.expect(
                    "GET", _GCS_METADATA, headers={"Metadata-Flavor": "Google"}
                )
            except StorageError as exc:
                raise AuthError(
                    "no GCS credentials: set credentials or access_token, or run on GKE/GCE"
                ) from exc
        doc = response.json()
        return doc["access_token"], time.time() + float(doc.get("expires_in", 3600))

    return CachedToken(fetch)

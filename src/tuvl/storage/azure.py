"""Azure Blob Storage: Shared Key, SAS token, or an Entra bearer token (managed / workload identity)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx

from tuvl.storage.creds import CachedToken, _local_name, _xml_text
from tuvl.storage.dialects import AZURE
from tuvl.storage.errors import (
    AuthError,
    ConfigError,
    Conflict,
    NotFound,
    StorageError,
    Unsupported,
    from_status,
)
from tuvl.storage.http import Http, describe
from tuvl.storage.types import Method, RawInfo

VERSION = "2021-08-06"
_SIGNED_HEADERS = (
    "content-encoding",
    "content-language",
    "content-length",
    "content-md5",
    "content-type",
    "date",
    "if-modified-since",
    "if-match",
    "if-none-match",
    "if-unmodified-since",
    "range",
)


def _iso(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return value


def shared_key_signature(
    account: str, key_b64: str, method: str, url: str, headers: dict[str, str]
) -> str:
    """The ``Authorization`` value for a Blob service request signed with the account key."""
    lower = {k.lower(): v for k, v in headers.items()}
    length = lower.get("content-length", "")
    if length == "0":
        length = ""
    standard = [
        length if name == "content-length" else lower.get(name, "") for name in _SIGNED_HEADERS
    ]
    canonical_headers = "".join(
        f"{k}:{' '.join(v.split())}\n" for k, v in sorted(lower.items()) if k.startswith("x-ms-")
    )
    parts = urlsplit(url)
    grouped: dict[str, list[str]] = {}
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        grouped.setdefault(name.lower(), []).append(value)
    resource = f"/{account}{parts.path}" + "".join(
        f"\n{name}:{','.join(sorted(values))}" for name, values in sorted(grouped.items())
    )
    to_sign = "\n".join([method, *standard]) + "\n" + canonical_headers + resource
    digest = hmac.new(base64.b64decode(key_b64), to_sign.encode(), hashlib.sha256).digest()
    return f"SharedKey {account}:{base64.b64encode(digest).decode()}"


class AzureBackend:
    def __init__(
        self,
        *,
        account: str,
        container: str,
        endpoint: str,
        account_key: str,
        sas_token: str,
        token: CachedToken | None,
        http: Http,
    ) -> None:
        self.account = account
        self.container = container
        self._base = endpoint or f"https://{account}.blob.core.windows.net"
        self._key = account_key
        self._sas = sas_token.lstrip("?")
        self._token = token
        self._http = http
        if account_key:
            try:
                base64.b64decode(account_key, validate=True)
            except ValueError as exc:
                raise ConfigError("account_key is not valid base64") from exc

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"/{self.container}" + (f"/{quote(key, safe='/~')}" if key else "")
        if self._sas and not self._key:
            query = f"{query}&{self._sas}" if query else self._sas
        return f"{self._base}{path}" + (f"?{query}" if query else "")

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        ok: tuple[int, ...] = (200,),
        max_bytes: int | None = None,
    ) -> httpx.Response:
        out = dict(headers or {})
        out["x-ms-date"] = format_datetime(datetime.now(UTC), usegmt=True)
        out["x-ms-version"] = VERSION
        if body is not None:
            out["content-length"] = str(len(body))
        if self._key:
            out["authorization"] = shared_key_signature(self.account, self._key, method, url, out)
        elif self._token is not None and not self._sas:
            out["authorization"] = f"Bearer {await self._token.get()}"
        response = await self._http.send(
            method, url, headers=out, content=body, max_bytes=max_bytes
        )
        if response.status_code in ok:
            return response
        raise _error(method, url, response)

    @staticmethod
    def _info(key: str, response: httpx.Response, size: int | None = None) -> RawInfo:
        h = response.headers
        return RawInfo(
            key=key,
            size=size if size is not None else int(h.get("content-length", 0)),
            content_type=h.get("content-type", ""),
            etag=h.get("etag", "").strip('"'),
            sha256=h.get(f"{AZURE.meta_prefix}sha256", ""),
            modified=_iso(h.get("last-modified", "")),
        )

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str],
        overwrite: bool,
    ) -> RawInfo:
        headers = {"x-ms-blob-type": "BlockBlob", "content-type": content_type}
        headers.update({f"{AZURE.meta_prefix}{k.lower()}": v for k, v in metadata.items()})
        if not overwrite:
            headers[AZURE.create_only[0]] = AZURE.create_only[1]
        response = await self._send("PUT", self._url(key), headers=headers, body=data, ok=(201,))
        return RawInfo(
            key=key,
            size=len(data),
            content_type=content_type,
            etag=response.headers.get("etag", "").strip('"'),
            sha256=metadata.get("sha256", ""),
            modified=_iso(response.headers.get("last-modified", "")),
        )

    async def get(self, key: str, max_bytes: int) -> tuple[bytes, RawInfo]:
        response = await self._send("GET", self._url(key), max_bytes=max_bytes)
        return response.content, self._info(key, response, len(response.content))

    async def head(self, key: str) -> RawInfo:
        return self._info(key, await self._send("HEAD", self._url(key)))

    async def list(
        self, prefix: str, limit: int, cursor: str | None
    ) -> tuple[list[RawInfo], str | None]:
        params = [("comp", "list"), ("maxresults", str(limit)), ("restype", "container")]
        if prefix:
            params.append(("prefix", prefix))
        if cursor:
            params.append(("marker", cursor))
        query = "&".join(f"{k}={quote(v, safe='~')}" for k, v in sorted(params))
        response = await self._send("GET", self._url("", query))
        root = ET.fromstring(response.text)
        items: list[RawInfo] = []
        for blob in root.iter():
            if _local_name(blob.tag) != "Blob":
                continue
            name = _xml_text(blob, "Name")
            props = {
                _local_name(p.tag): (p.text or "")
                for e in blob
                if _local_name(e.tag) == "Properties"
                for p in e
            }
            items.append(
                RawInfo(
                    key=name,
                    size=int(props.get("Content-Length", 0)),
                    content_type=props.get("Content-Type", ""),
                    etag=props.get("Etag", "").strip('"'),
                    modified=_iso(props.get("Last-Modified", "")),
                )
            )
        return items, _xml_text(root, "NextMarker") or None

    async def delete(self, key: str) -> None:
        await self._send("DELETE", self._url(key), ok=(202, 404))

    async def copy(self, src: str, dst: str, *, overwrite: bool) -> RawInfo | None:
        return None

    async def presign(self, key: str, method: Method, expires_in: int) -> str:
        if not self._key:
            raise Unsupported(
                "presigned URLs for Azure need account_key (a SAS signed with the key)"
            )
        expiry = (datetime.now(UTC) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
        https_only = urlsplit(self._base).scheme == "https"
        perms = "r" if method == "GET" else "cw"
        protocol = "https" if https_only else "https,http"
        resource = f"/blob/{self.account}/{self.container}/{key}"
        to_sign = "\n".join(
            [
                perms,
                "",
                expiry,
                resource,
                "",
                "",
                protocol,
                VERSION,
                "b",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
        digest = hmac.new(base64.b64decode(self._key), to_sign.encode(), hashlib.sha256).digest()
        sig = base64.b64encode(digest).decode()
        query = (
            f"sp={perms}&se={quote(expiry, safe='')}&spr={quote(protocol, safe='')}"
            f"&sv={VERSION}&sr=b&sig={quote(sig, safe='')}"
        )
        return f"{self._base}/{self.container}/{quote(key, safe='/~')}?{query}"

    async def aclose(self) -> None:
        await self._http.aclose()


def _error(method: str, url: str, response: httpx.Response) -> StorageError:
    message = describe(method, url, response)
    code = response.headers.get("x-ms-error-code", "")
    if code in ("BlobNotFound", "ContainerNotFound", "ResourceNotFound"):
        return NotFound(message)
    if code in ("BlobAlreadyExists", "ConditionNotMet"):
        return Conflict(message)
    if code in ("AuthenticationFailed", "AuthorizationFailure"):
        return AuthError(message)
    return from_status(response.status_code, message)


__all__ = ["AzureBackend", "shared_key_signature", "unquote"]

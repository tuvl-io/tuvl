"""S3 and S3-compatible stores (MinIO, R2, Wasabi, Spaces, B2, Oracle Object Storage; GCS through HMAC keys)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import httpx

from tuvl.storage.creds import AwsCredentialSource, _local_name, _xml_text
from tuvl.storage.dialects import AWS, Dialect
from tuvl.storage.errors import (
    AuthError,
    Conflict,
    Forbidden,
    NotFound,
    RateLimited,
    StorageError,
    Unavailable,
    from_status,
)
from tuvl.storage.http import Http, describe
from tuvl.storage.sigv4 import EMPTY_SHA256, UNSIGNED, presign_url, sha256_hex, sign_headers
from tuvl.storage.types import Method, RawInfo

_CODES: dict[str, type[StorageError]] = {
    "NoSuchKey": NotFound,
    "NoSuchBucket": NotFound,
    "NotFound": NotFound,
    "AccessDenied": Forbidden,
    "InvalidAccessKeyId": AuthError,
    "SignatureDoesNotMatch": AuthError,
    "ExpiredToken": AuthError,
    "PreconditionFailed": Conflict,
    "ConditionalRequestConflict": Conflict,
    "SlowDown": RateLimited,
    "RequestTimeout": Unavailable,
    "InternalError": Unavailable,
    "ServiceUnavailable": Unavailable,
}


def _quote(key: str) -> str:
    return quote(key, safe="/~")


def _iso(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = (
            parsedate_to_datetime(value)
            if "," in value
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return value
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class S3Backend:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint: str,
        path_style: bool | None,
        creds: AwsCredentialSource,
        http: Http,
        dialect: Dialect = AWS,
    ) -> None:
        signing = dialect.signing
        if signing is None:
            raise ValueError(
                f"the {dialect.name} dialect has no request signing for the S3 backend"
            )
        self.bucket = bucket
        self.region = region or "us-east-1"
        self._creds = creds
        self._http = http
        self._dialect = dialect
        self._scheme = signing
        if endpoint:
            parts = urlsplit(endpoint)
            self._path_style = True if path_style is None else path_style
            self._origin = f"{parts.scheme}://{parts.netloc}"
            self._base_path = parts.path.rstrip("/")
            self._host = parts.netloc
        else:
            self._path_style = path_style if path_style is not None else "." in bucket
            self._origin = "https://s3.amazonaws.com"
            self._base_path = ""
            self._host = f"s3.{self.region}.amazonaws.com"
        scheme = urlsplit(self._origin).scheme
        if self._path_style:
            self._origin = f"{scheme}://{self._host}"
        else:
            self._origin = f"{scheme}://{bucket}.{self._host}"

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"{self._base_path}/{self.bucket}" if self._path_style else self._base_path
        path += f"/{_quote(key)}" if key else ("/" if not self._path_style else "")
        return f"{self._origin}{path}" + (f"?{query}" if query else "")

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
        payload = sha256_hex(body) if body is not None else EMPTY_SHA256
        signed = sign_headers(
            method,
            url,
            headers or {},
            payload,
            await self._creds.get(),
            self.region,
            scheme=self._scheme,
        )
        response = await self._http.send(
            method, url, headers=signed, content=body, max_bytes=max_bytes
        )
        if response.status_code in ok:
            return response
        raise _error(method, url, response)

    def _info(self, key: str, response: httpx.Response, size: int | None = None) -> RawInfo:
        headers = response.headers
        return RawInfo(
            key=key,
            size=size if size is not None else int(headers.get("content-length", 0)),
            content_type=headers.get("content-type", ""),
            etag=headers.get("etag", "").strip('"'),
            sha256=headers.get(f"{self._dialect.meta_prefix}sha256", ""),
            modified=_iso(headers.get("last-modified", "")),
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
        headers = {"content-type": content_type}
        headers.update({f"{self._dialect.meta_prefix}{k.lower()}": v for k, v in metadata.items()})
        if not overwrite:
            name, value = self._dialect.create_only
            headers[name] = value
        response = await self._send(
            "PUT", self._url(key), headers=headers, body=data, ok=(200, 201)
        )
        return RawInfo(
            key=key,
            size=len(data),
            content_type=content_type,
            etag=response.headers.get("etag", "").strip('"'),
            sha256=metadata.get("sha256", ""),
            modified=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    async def get(self, key: str, max_bytes: int) -> tuple[bytes, RawInfo]:
        response = await self._send("GET", self._url(key), max_bytes=max_bytes)
        return response.content, self._info(key, response, len(response.content))

    async def head(self, key: str) -> RawInfo:
        return self._info(key, await self._send("HEAD", self._url(key)))

    async def list(
        self, prefix: str, limit: int, cursor: str | None
    ) -> tuple[list[RawInfo], str | None]:
        params = [("list-type", "2"), ("max-keys", str(limit))]
        if prefix:
            params.append(("prefix", prefix))
        if cursor:
            params.append(("continuation-token", cursor))
        query = "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(params))
        response = await self._send("GET", self._url("", query))
        root = ET.fromstring(response.text)
        items: list[RawInfo] = []
        for el in root.iter():
            if _local_name(el.tag) != "Contents":
                continue
            fields = {_local_name(c.tag): (c.text or "") for c in el}
            items.append(
                RawInfo(
                    key=fields.get("Key", ""),
                    size=int(fields.get("Size", 0)),
                    etag=fields.get("ETag", "").strip('"'),
                    modified=_iso(fields.get("LastModified", "")),
                )
            )
        truncated = _xml_text(root, "IsTruncated").lower() == "true"
        return items, (_xml_text(root, "NextContinuationToken") or None) if truncated else None

    async def delete(self, key: str) -> None:
        await self._send("DELETE", self._url(key), ok=(200, 204, 404))

    async def copy(self, src: str, dst: str, *, overwrite: bool) -> RawInfo | None:
        if not overwrite:
            try:
                await self.head(dst)
            except NotFound:
                pass
            else:
                raise Conflict(f"'{dst}' already exists")
        source = f"/{self.bucket}/{_quote(src)}"
        response = await self._send(
            "PUT", self._url(dst), headers={self._dialect.copy_source: source}, ok=(200,)
        )
        if "<Error>" in response.text[:200]:
            raise Unavailable(
                f"copy of '{src}' failed: {_xml_text(ET.fromstring(response.text), 'Code')}"
            )
        return await self.head(dst)

    async def presign(self, key: str, method: Method, expires_in: int) -> str:
        return presign_url(
            method,
            self._url(key),
            await self._creds.get(),
            self.region,
            expires_in,
            scheme=self._scheme,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _error(method: str, url: str, response: httpx.Response) -> StorageError:
    message = describe(method, url, response)
    code = ""
    if response.content[:1] == b"<":
        try:
            code = _xml_text(ET.fromstring(response.text), "Code")
        except ET.ParseError:
            code = ""
    cls = _CODES.get(code)
    return cls(message) if cls else from_status(response.status_code, message)


__all__ = ["S3Backend", "UNSIGNED"]

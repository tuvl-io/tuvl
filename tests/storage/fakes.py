"""In-memory stand-ins for the object services, served through ``httpx.MockTransport``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import httpx


@dataclass
class Obj:
    data: bytes
    headers: dict[str, str] = field(default_factory=dict)
    version: int = 1


def etag(data: bytes) -> str:
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class FakeS3:
    """Path-style S3 at ``http://s3.test`` (bucket first in the path)."""

    algorithm = "AWS4-HMAC-SHA256"
    prefix = "x-amz-"

    def __init__(self, bucket: str = "b") -> None:
        self.bucket = bucket
        self.objects: dict[str, Obj] = {}
        self.requests: list[httpx.Request] = []
        self.fail_next: list[int] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _key(self, request: httpx.Request) -> str:
        path = unquote(urlsplit(str(request.url)).path)
        return path.removeprefix(f"/{self.bucket}").lstrip("/")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(
                self.fail_next.pop(0), text="<Error><Code>SlowDown</Code></Error>"
            )
        if not request.headers.get("authorization", "").startswith(self.algorithm):
            return httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>")
        refused = self._refuse(request)
        if refused is not None:
            return refused
        key, query = self._key(request), parse_qs(urlsplit(str(request.url)).query)
        method = request.method
        if method == "GET" and not key:
            return self._list(query)
        if method == "PUT":
            return self._put(request, key)
        obj = self.objects.get(key)
        if method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        if obj is None:
            body = "" if method == "HEAD" else "<Error><Code>NoSuchKey</Code></Error>"
            return httpx.Response(404, text=body)
        headers = {
            "etag": f'"{etag(obj.data)}"',
            "last-modified": "Sat, 17 Oct 2026 10:00:00 GMT",
            **obj.headers,
        }
        headers["content-length"] = str(len(obj.data))
        return httpx.Response(200, headers=headers, content=b"" if method == "HEAD" else obj.data)

    def _refuse(self, request: httpx.Request) -> httpx.Response | None:
        return None

    def _create_only(self, request: httpx.Request) -> bool:
        return request.headers.get("if-none-match") == "*"

    def _put(self, request: httpx.Request, key: str) -> httpx.Response:
        source = request.headers.get(f"{self.prefix}copy-source")
        if self._create_only(request) and key in self.objects:
            return httpx.Response(412, text="<Error><Code>PreconditionFailed</Code></Error>")
        if source:
            src = self.objects.get(unquote(source).removeprefix(f"/{self.bucket}/"))
            if src is None:
                return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")
            self.objects[key] = Obj(src.data, dict(src.headers))
            return httpx.Response(200, text="<CopyObjectResult><ETag>x</ETag></CopyObjectResult>")
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.startswith(f"{self.prefix}meta-") or k == "content-type"
        }
        self.objects[key] = Obj(request.content, headers)
        return httpx.Response(200, headers={"etag": f'"{etag(request.content)}"'})

    def _list(self, query: dict[str, list[str]]) -> httpx.Response:
        prefix = query.get("prefix", [""])[0]
        limit = int(query.get("max-keys", ["1000"])[0])
        after = query.get("continuation-token", [""])[0]
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
        page = keys[:limit]
        more = len(keys) > limit
        items = "".join(
            f"<Contents><Key>{escape(k)}</Key><Size>{len(self.objects[k].data)}</Size>"
            f'<ETag>"{etag(self.objects[k].data)}"</ETag>'
            "<LastModified>2026-10-17T10:00:00.000Z</LastModified></Contents>"
            for k in page
        )
        token = f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>" if more else ""
        xml = (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{str(more).lower()}</IsTruncated>{items}{token}</ListBucketResult>"
        )
        return httpx.Response(200, text=xml)


class FakeGoogleXml(FakeS3):
    """Google's XML API as Google serves it: GOOG4 signatures, ``x-goog-*`` headers only, and only its own create-only
    precondition (``If-None-Match`` is ignored and the object is overwritten)."""

    algorithm = "GOOG4-HMAC-SHA256"
    prefix = "x-goog-"

    def _refuse(self, request: httpx.Request) -> httpx.Response | None:
        names = {k.lower() for k in request.headers}
        if any(n.startswith("x-amz-") for n in names) and any(
            n.startswith("x-goog-") for n in names
        ):
            return httpx.Response(400, text="<Error><Code>ExcessHeaderValues</Code></Error>")
        if any(n.startswith("x-amz-") for n in names):
            return httpx.Response(400, text="<Error><Code>InvalidArgument</Code></Error>")
        return None

    def _create_only(self, request: httpx.Request) -> bool:
        return request.headers.get("x-goog-if-generation-match") == "0"


class FakeGcs:
    """The JSON API of one bucket at ``https://gcs.test``."""

    def __init__(self, bucket: str = "b", token: str = "tok") -> None:
        self.bucket, self.token = bucket, token
        self.objects: dict[str, Obj] = {}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _resource(self, name: str) -> dict[str, object]:
        obj = self.objects[name]
        return {
            "name": name,
            "size": str(len(obj.data)),
            "etag": etag(obj.data),
            "contentType": obj.headers.get("content-type", ""),
            "updated": "2026-10-17T10:00:00.123Z",
            "metadata": json.loads(obj.headers.get("meta", "{}")),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthenticated"})
        url = urlsplit(str(request.url))
        query = parse_qs(url.query)
        if url.path.startswith("/upload/"):
            return self._upload(request, query)
        rest = url.path.split("/o", 1)[1].lstrip("/") if "/o" in url.path else ""
        if not rest:
            prefix = query.get("prefix", [""])[0]
            limit = int(query.get("maxResults", ["1000"])[0])
            after = query.get("pageToken", [""])[0]
            keys = sorted(k for k in self.objects if k.startswith(prefix) and k > after)
            page = keys[:limit]
            doc: dict[str, object] = {"items": [self._resource(k) for k in page]}
            if len(keys) > limit:
                doc["nextPageToken"] = page[-1]
            return httpx.Response(200, json=doc)
        if "/copyTo/" in rest:
            src, dst = (unquote(p) for p in (rest.split("/copyTo/b/")[0], rest.split("/o/")[-1]))
            if src not in self.objects:
                return httpx.Response(404, json={"error": "not found"})
            if query.get("ifGenerationMatch") == ["0"] and dst in self.objects:
                return httpx.Response(412, json={"error": "conflict"})
            self.objects[dst] = self.objects[src]
            return httpx.Response(200, json=self._resource(dst))
        name = unquote(rest)
        if request.method == "DELETE":
            self.objects.pop(name, None)
            return httpx.Response(204)
        if name not in self.objects:
            return httpx.Response(404, json={"error": "not found"})
        if query.get("alt") == ["media"]:
            obj = self.objects[name]
            headers = {"content-type": obj.headers.get("content-type", ""), "etag": etag(obj.data)}
            for k, v in json.loads(obj.headers.get("meta", "{}")).items():
                headers[f"x-goog-meta-{k}"] = v
            return httpx.Response(200, content=obj.data, headers=headers)
        return httpx.Response(200, json=self._resource(name))

    def _upload(self, request: httpx.Request, query: dict[str, list[str]]) -> httpx.Response:
        boundary = request.headers["content-type"].split("boundary=")[1]
        parts = request.content.split(f"--{boundary}".encode())
        meta = json.loads(parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0])
        body = parts[2].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        name = meta["name"]
        if query.get("ifGenerationMatch") == ["0"] and name in self.objects:
            return httpx.Response(412, json={"error": "conflict"})
        self.objects[name] = Obj(
            body, {"content-type": meta["contentType"], "meta": json.dumps(meta["metadata"])}
        )
        return httpx.Response(200, json=self._resource(name))

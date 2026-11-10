"""``multipart/form-data`` on a trigger: store each uploaded file, start the run with its reference.

A ``file`` input receives a :class:`~tuvl.contract.files.FileRef`, never bytes. The caller is
authenticated before the body is read, the body is capped while it streams, and a file is only ever
written under a server-generated key. References are accepted nowhere else at the edge: a JSON body
that names a ``file`` input is refused, so a caller cannot point a run at an object it did not upload.
"""

from __future__ import annotations

import json
import mimetypes
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import structlog
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from tuvl.api.auth import trigger_user
from tuvl.contract.facts import field_types
from tuvl.contract.files import FileRef
from tuvl.contract.schema import Upload, WorkflowDocument
from tuvl.contract.types import TypeRef
from tuvl.storage import get_store
from tuvl.storage.errors import StorageError, TooLarge

logger = structlog.get_logger(__name__)

FORM_OVERHEAD = 1024 * 1024
MAX_FIELDS = 100
MAX_FIELD_BYTES = 256 * 1024


def is_multipart(request: Request) -> bool:
    return request.headers.get("content-type", "").lower().startswith("multipart/form-data")


def file_inputs(doc: WorkflowDocument) -> dict[str, bool]:
    """``{input name: takes several files}`` for the trigger's ``file`` / ``list[file]`` inputs."""
    out: dict[str, bool] = {}
    for name, ref in field_types(doc.spec.trigger.input).items():
        if ref.kind == "scalar" and ref.name == "file":
            out[name] = False
        elif ref.kind == "list" and ref.item is not None and _is_file(ref.item):
            out[name] = True
    return out


def _is_file(ref: TypeRef) -> bool:
    return ref.kind == "scalar" and ref.name == "file"


def reject_file_values(doc: WorkflowDocument, payload: dict[str, Any]) -> None:
    named = sorted(set(file_inputs(doc)) & set(payload))
    if named:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{', '.join(named)}: a file is sent as multipart/form-data, not in a JSON body or query",
        )


async def _limited(stream: AsyncGenerator[bytes], limit: int) -> AsyncGenerator[bytes]:
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE, f"the upload is larger than {limit} bytes"
            )
        yield chunk


def _safe_name(filename: str) -> tuple[str, str]:
    """``(display name, key-safe name)`` from a client-supplied file name."""
    base = re.split(r"[\\/]", filename)[-1]
    display = "".join(c for c in base if ord(c) >= 32 and ord(c) != 127).strip()[:255] or "file"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", display).lstrip(".")[:100] or "file"
    return display, safe


def _accepted(content_type: str, accept: list[str]) -> bool:
    if not accept:
        return True
    kind = content_type.split(";")[0].strip().lower()
    return any(kind == a or (a.endswith("/*") and kind.startswith(a[:-1])) for a in accept)


def _structured(ref: TypeRef | None) -> bool:
    return ref is not None and (
        ref.kind in ("list", "model") or (ref.kind == "scalar" and ref.name == "json")
    )


def _field_value(name: str, values: list[str], ref: TypeRef | None) -> Any:
    """A text form field → the value the input expects (structured types are sent as JSON text)."""
    if ref is not None and ref.kind == "list" and len(values) > 1:
        return values
    if len(values) > 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: sent more than once")
    text = values[0]
    if not _structured(ref):
        return text
    try:
        return json.loads(text)
    except ValueError:
        if ref is not None and ref.kind == "list":
            return [text]
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: expected JSON text"
        ) from None


@dataclass
class Received:
    payload: dict[str, Any]
    stored: list[FileRef] = field(default_factory=list)

    async def discard(self) -> None:
        """Delete what this request stored (the run never started)."""
        for ref in self.stored:
            try:
                await get_store(ref.store).delete(ref.key)
            except StorageError:
                logger.warning("upload.discard_failed", store=ref.store, key=ref.key)


async def receive(
    request: Request, doc: WorkflowDocument, upload: Upload, *, path_params: bool
) -> Received:
    limit = upload.max_bytes * upload.max_files + FORM_OVERHEAD
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"the upload is larger than {limit} bytes"
        )
    parser = MultiPartParser(
        request.headers,
        _limited(request.stream(), limit),
        max_files=upload.max_files,
        max_fields=MAX_FIELDS,
        max_part_size=MAX_FIELD_BYTES,
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.message) from exc

    received = Received(payload={})
    try:
        types = field_types(doc.spec.trigger.input)
        files = file_inputs(doc)
        texts: dict[str, list[str]] = {}
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                if name not in files:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: not a file input"
                    )
                continue
            if name in files:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: send a file, not text"
                )
            texts.setdefault(name, []).append(value)
        for name, values in texts.items():
            received.payload[name] = _field_value(name, values, types.get(name))

        store = get_store(upload.store)
        for name, many in files.items():
            uploads = [v for v in form.getlist(name) if isinstance(v, UploadFile)]
            if len(uploads) > 1 and not many:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, f"{name}: takes one file"
                )
            refs: list[FileRef] = []
            for item in uploads:
                refs.append(await _store_one(store, upload, item))
                received.stored.append(refs[-1])
            if many and refs:
                received.payload[name] = [r.model_dump() for r in refs]
            elif refs:
                received.payload[name] = refs[0].model_dump()
    except BaseException:
        await received.discard()
        raise
    finally:
        await form.close()
    if path_params:
        received.payload.update(request.path_params)
    return received


async def _store_one(store: Any, upload: Upload, item: UploadFile) -> FileRef:
    display, safe = _safe_name(item.filename or "")
    content_type = (
        item.content_type or mimetypes.guess_type(display)[0] or "application/octet-stream"
    )
    if not _accepted(content_type, upload.accept):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{display}: content type {content_type!r} is not accepted",
        )
    if (item.size or 0) > upload.max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{display}: {item.size} bytes exceeds the {upload.max_bytes} byte limit",
        )
    # The parser has already spooled the part to a temporary file; the read is bounded by
    # max_bytes above. Every backend takes the object as bytes, so it is read once here.
    data = await item.read()
    key = f"{upload.prefix}{uuid.uuid4().hex}/{safe}"
    try:
        blob = await store.put(key, data, content_type=content_type)
    except TooLarge as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"{display}: {exc}") from exc
    except StorageError as exc:
        logger.error("upload.store_failed", store=store.name, error=str(exc))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the upload store is unavailable"
        ) from exc
    return FileRef(
        store=store.name,
        key=key,
        name=display,
        size=len(data),
        content_type=content_type,
        sha256=blob.sha256,
    )


async def start_upload_run(
    request: Request, doc: WorkflowDocument, *, trigger: str = "http", wait: bool = True
) -> JSONResponse:
    from tuvl.api.runs import start_run

    http = doc.spec.trigger.http
    if http is None or http.upload is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "this trigger does not accept file uploads"
        )
    await trigger_user(request, doc)
    received = await receive(request, doc, http.upload, path_params=trigger == "http")
    try:
        return await start_run(
            request, doc, received.payload, trigger=trigger, wait=wait, uploaded=True
        )
    except BaseException:
        await received.discard()
        raise


async def store_dev_file(doc: WorkflowDocument, item: UploadFile) -> FileRef:
    """Store one file under the workflow's upload settings (Insight's run form)."""
    http = doc.spec.trigger.http
    if http is None or http.upload is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{doc.metadata.name} has no http.upload"
        )
    return await _store_one(get_store(http.upload.store), http.upload, item)

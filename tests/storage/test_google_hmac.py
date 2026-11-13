"""Google Cloud Storage with an HMAC key: the S3 backend in Google's dialect."""

from __future__ import annotations

import pytest

from tuvl.storage import Conflict, NotFound, Store, StoreSpec, build_backend
from tuvl.storage.creds import AwsCredentialSource
from tuvl.storage.dialects import AWS, AZURE, GOOGLE
from tuvl.storage.http import Http
from tuvl.storage.s3 import S3Backend
from tuvl.storage.sigv4 import AwsCreds

from .fakes import FakeGoogleXml, FakeS3

CREDS = AwsCreds("GOOGKEY", "SECRET")


def make(fake: FakeGoogleXml, *, prefix: str = "") -> Store:
    http = Http(5, fake.transport())
    backend = S3Backend(
        bucket=fake.bucket,
        region="auto",
        endpoint="http://gcs.test",
        path_style=True,
        creds=AwsCredentialSource(static=CREDS, region="auto", http=http),
        http=http,
        dialect=GOOGLE,
    )
    return Store("g", StoreSpec(provider="gcs", bucket=fake.bucket, prefix=prefix), backend)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr("tuvl.storage.store.asyncio.sleep", instant)


async def test_requests_use_google_names_only() -> None:
    fake = FakeGoogleXml()
    store = make(fake)
    await store.put("a.txt", b"hello", content_type="text/plain", metadata={"k": "v"})
    await store.get("a.txt")
    await store.list()
    for request in fake.requests:
        names = {k.lower() for k in request.headers}
        assert not [n for n in names if n.startswith("x-amz-")]
        assert request.headers["authorization"].startswith("GOOG4-HMAC-SHA256 Credential=GOOGKEY/")
    assert fake.objects["a.txt"].headers["x-goog-meta-k"] == "v"
    assert "SECRET" not in str(fake.requests[0].headers)


async def test_a_second_write_of_the_same_key_is_refused_and_leaves_the_object() -> None:
    fake = FakeGoogleXml()
    store = make(fake)
    await store.put("k", b"one")
    assert (await store.put("k", b"one")).size == 3
    with pytest.raises(Conflict):
        await store.put("k", b"two")
    assert fake.objects["k"].data == b"one"
    assert (await store.put("k", b"two", if_exists="overwrite")).size == 3


async def test_metadata_round_trips_so_identical_content_is_recognised() -> None:
    store = make(FakeGoogleXml())
    first = await store.put("k", b"hello")
    assert (await store.head("k")).sha256 == first.sha256
    data, got = await store.get("k")
    assert (data, got.sha256) == (b"hello", first.sha256)


async def test_copy_list_delete_and_missing() -> None:
    fake = FakeGoogleXml()
    store = make(fake, prefix="p/")
    await store.put("src", b"data")
    copied = await store.copy("src", "dst")
    assert copied.key == "dst" and fake.objects["p/dst"].data == b"data"
    with pytest.raises(Conflict):
        await store.copy("src", "dst")
    assert [b.key for b in (await store.list()).items] == ["dst", "src"]
    await store.delete("src")
    with pytest.raises(NotFound):
        await store.get("src")


async def test_presigned_urls_are_google_signed() -> None:
    store = make(FakeGoogleXml())
    signed = await store.presign("a b.txt", expires_in=60)
    assert "X-Goog-Signature=" in signed.url and "X-Goog-Expires=60" in signed.url
    assert "X-Amz" not in signed.url and "SECRET" not in signed.url


async def test_the_amazon_dialect_would_be_refused_by_google() -> None:
    fake = FakeGoogleXml()
    http = Http(5, fake.transport())
    backend = S3Backend(
        bucket=fake.bucket,
        region="auto",
        endpoint="http://gcs.test",
        path_style=True,
        creds=AwsCredentialSource(static=CREDS, region="auto", http=http),
        http=http,
        dialect=AWS,
    )
    with pytest.raises(Exception, match="403|AccessDenied|Forbidden|Auth"):
        await Store("g", StoreSpec(provider="gcs", bucket="b"), backend).put("k", b"x")


def test_gcs_with_an_hmac_key_builds_the_google_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("G_ACCESS", "GOOGKEY")
    monkeypatch.setenv("G_SECRET", "SECRET")
    spec = StoreSpec(provider="gcs", bucket="b", access_key="${G_ACCESS}", secret_key="${G_SECRET}")
    backend = build_backend("g", spec)
    assert isinstance(backend, S3Backend) and backend._dialect is GOOGLE


def test_s3_stays_on_the_amazon_dialect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A_ACCESS", "AKID")
    monkeypatch.setenv("A_SECRET", "SECRET")
    spec = StoreSpec(
        provider="s3",
        bucket="b",
        endpoint="https://ns.compat.objectstorage.me-jeddah-1.oraclecloud.com",
        region="me-jeddah-1",
        access_key="${A_ACCESS}",
        secret_key="${A_SECRET}",
    )
    backend = build_backend("o", spec)
    assert isinstance(backend, S3Backend) and backend._dialect is AWS


def test_a_dialect_without_signing_cannot_drive_the_s3_backend() -> None:
    http = Http(5, FakeS3().transport())
    with pytest.raises(ValueError, match="azure"):
        S3Backend(
            bucket="b",
            region="r",
            endpoint="http://x",
            path_style=True,
            creds=AwsCredentialSource(static=CREDS, region="r", http=http),
            http=http,
            dialect=AZURE,
        )


def test_azure_vocabulary_is_the_one_its_backend_sends() -> None:
    assert AZURE.meta_prefix == "x-ms-meta-"
    assert AZURE.create_only == ("if-none-match", "*")

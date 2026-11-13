"""SigV4 against signatures produced by botocore for the same inputs and clock."""

from __future__ import annotations

from datetime import UTC, datetime

from tuvl.storage.sigv4 import EMPTY_SHA256, AwsCreds, presign_url, sha256_hex, sign_headers

NOW = datetime(2013, 5, 24, tzinfo=UTC)
CREDS = AwsCreds("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")


def test_presigned_get_matches_botocore() -> None:
    url = presign_url(
        "GET",
        "https://examplebucket.s3.us-east-1.amazonaws.com/test%20file.txt",
        CREDS,
        "us-east-1",
        86400,
        now=NOW,
    )
    assert url == (
        "https://examplebucket.s3.us-east-1.amazonaws.com/test%20file.txt"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
        "&X-Amz-Signature=081b7f4e0bdc109e6d111432886b7126549d2e9093482a9efcf523ef84db46bb"
    )


def test_put_headers_match_botocore() -> None:
    out = sign_headers(
        "PUT",
        "http://localhost:9000/bucket/a/b%20c.txt",
        {"content-type": "text/plain", "x-amz-meta-sha256": "abc"},
        sha256_hex(b"hello"),
        CREDS,
        "us-east-1",
        now=NOW,
    )
    assert out["x-amz-date"] == "20130524T000000Z"
    assert out["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date;x-amz-meta-sha256, "
        "Signature=dbf495898e8911701bee00463a5d7ab8764e9486417d45b7938c8d432a10ad0d"
    )


def test_query_and_session_token_match_botocore() -> None:
    creds = AwsCreds(CREDS.access_key, CREDS.secret_key, "TOKEN123")
    out = sign_headers(
        "GET",
        "http://localhost:9000/bucket?list-type=2&max-keys=10&prefix=a%2Fb",
        {},
        EMPTY_SHA256,
        creds,
        "eu-west-1",
        now=NOW,
    )
    assert out["authorization"].endswith(
        "SignedHeaders=host;x-amz-content-sha256;x-amz-date;x-amz-security-token, "
        "Signature=8451cdfa6e4b6283cc01440238608034d886110525f47c2b9f040eb101f1991d"
    )


def _independent_goog4(secret: str, canonical: str, date: str, stamp: str, region: str) -> str:
    """GOOG4-HMAC-SHA256 worked out from Google's description, without the module under test."""
    import hashlib
    import hmac

    key = ("GOOG4" + secret).encode()
    for part in (date, region, "storage", "goog4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    to_sign = "\n".join(
        [
            "GOOG4-HMAC-SHA256",
            stamp,
            f"{date}/{region}/storage/goog4_request",
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()


def test_google_headers_use_goog4_and_no_amazon_names() -> None:
    from tuvl.storage.dialects import GOOG4

    out = sign_headers(
        "PUT",
        "https://storage.googleapis.com/bucket/a.txt",
        {"content-type": "text/plain", "x-goog-meta-sha256": "abc"},
        sha256_hex(b"hello"),
        CREDS,
        "auto",
        now=NOW,
        scheme=GOOG4,
    )
    assert out["x-goog-date"] == "20130524T000000Z"
    assert out["x-goog-content-sha256"] == sha256_hex(b"hello")
    assert not [k for k in out if k.startswith("x-amz-")]
    canonical = "\n".join(
        [
            "PUT",
            "/bucket/a.txt",
            "",
            "content-type:text/plain\nhost:storage.googleapis.com\n"
            f"x-goog-content-sha256:{sha256_hex(b'hello')}\n"
            "x-goog-date:20130524T000000Z\nx-goog-meta-sha256:abc\n",
            "content-type;host;x-goog-content-sha256;x-goog-date;x-goog-meta-sha256",
            sha256_hex(b"hello"),
        ]
    )
    expected = _independent_goog4(
        CREDS.secret_key, canonical, "20130524", "20130524T000000Z", "auto"
    )
    assert out["authorization"] == (
        f"GOOG4-HMAC-SHA256 Credential={CREDS.access_key}/20130524/auto/storage/goog4_request, "
        "SignedHeaders=content-type;host;x-goog-content-sha256;x-goog-date;x-goog-meta-sha256, "
        f"Signature={expected}"
    )


def test_google_presigned_url_has_goog_query_names() -> None:
    from tuvl.storage.dialects import GOOG4

    url = presign_url(
        "GET",
        "https://storage.googleapis.com/bucket/a%20b.txt",
        CREDS,
        "auto",
        600,
        now=NOW,
        scheme=GOOG4,
    )
    assert "X-Goog-Algorithm=GOOG4-HMAC-SHA256" in url
    assert (
        "X-Goog-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fauto%2Fstorage%2Fgoog4_request" in url
    )
    assert "X-Goog-Expires=600" in url and "X-Goog-Signature=" in url
    assert "X-Amz" not in url


def test_the_amazon_scheme_is_still_the_default() -> None:
    from tuvl.storage.dialects import AWS4

    plain = sign_headers("GET", "http://h/b", {}, EMPTY_SHA256, CREDS, "us-east-1", now=NOW)
    explicit = sign_headers(
        "GET", "http://h/b", {}, EMPTY_SHA256, CREDS, "us-east-1", now=NOW, scheme=AWS4
    )
    assert plain == explicit and plain["authorization"].startswith("AWS4-HMAC-SHA256")

"""How each object-store family names its headers, signs its requests and refuses to overwrite.

The S3 backend speaks to AWS, MinIO, Cloudflare R2, Wasabi, Spaces, B2 and Oracle Cloud Object Storage (its S3
compatibility API) with Amazon's vocabulary, and to Google Cloud Storage (HMAC keys) with Google's: Google rejects a
request that mixes ``x-amz-*`` and ``x-goog-*`` headers, and only enforces its own create-only precondition. Azure Blob
shares none of the signing (SharedKey, SAS or a bearer token) but names its metadata and create-only headers in the
same way, so its vocabulary lives here too.

A new S3-compatible service that needs different names is one ``Dialect`` below; the backends take it as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SigningScheme:
    """The HMAC-SHA256 request-signing family: the same algorithm under different labels."""

    algorithm: str
    key_label: str
    terminator: str
    service: str
    header_prefix: str
    query_prefix: str

    @property
    def date_header(self) -> str:
        return f"{self.header_prefix}date"

    @property
    def hash_header(self) -> str:
        return f"{self.header_prefix}content-sha256"

    @property
    def token_header(self) -> str:
        return f"{self.header_prefix}security-token"


@dataclass(frozen=True)
class Dialect:
    name: str
    meta_prefix: str
    create_only: tuple[str, str]
    copy_source: str
    signing: SigningScheme | None


AWS4 = SigningScheme(
    algorithm="AWS4-HMAC-SHA256",
    key_label="AWS4",
    terminator="aws4_request",
    service="s3",
    header_prefix="x-amz-",
    query_prefix="X-Amz-",
)

GOOG4 = SigningScheme(
    algorithm="GOOG4-HMAC-SHA256",
    key_label="GOOG4",
    terminator="goog4_request",
    service="storage",
    header_prefix="x-goog-",
    query_prefix="X-Goog-",
)

AWS = Dialect(
    name="aws",
    meta_prefix="x-amz-meta-",
    create_only=("If-None-Match", "*"),
    copy_source="x-amz-copy-source",
    signing=AWS4,
)

GOOGLE = Dialect(
    name="google",
    meta_prefix="x-goog-meta-",
    create_only=("x-goog-if-generation-match", "0"),
    copy_source="x-goog-copy-source",
    signing=GOOG4,
)

AZURE = Dialect(
    name="azure",
    meta_prefix="x-ms-meta-",
    create_only=("if-none-match", "*"),
    copy_source="x-ms-copy-source",
    signing=None,
)

__all__ = ["AWS", "AWS4", "AZURE", "GOOG4", "GOOGLE", "Dialect", "SigningScheme"]

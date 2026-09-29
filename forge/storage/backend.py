"""Object storage, with two interchangeable backends.

Local disk in development, S3 in production. The choice is made by whether
FORGE_S3_BUCKET is set, so moving to Railway is configuration rather than a
code change - which is the only way to be confident the deployed path is the
one that was tested.

The interface is deliberately small: put, get, open, delete, and a URL the
browser can use. Nothing above this module knows which backend is in use.
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

from ..config import REPO_ROOT


class Storage(ABC):
    name: str

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def url(self, key: str, expires: int = 900) -> str:
        """A URL the browser can fetch this object from."""


class LocalStorage(Storage):
    """Development backend: files under a directory in the repo.

    Serving goes through the application, which is fine for one developer and
    would not be for production - which is exactly why S3 exists below.
    """

    name = "local"

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # A key is built from UUIDs by storage.keys, never from user input, but
        # resolve and re-check anyway: path traversal is cheap to prevent and
        # expensive to discover later.
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root.resolve()):
            raise ValueError(f"refusing to write outside the storage root: {key}")
        return candidate

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def url(self, key: str, expires: int = 900) -> str:
        return f"/files/{key}"


class S3Storage(Storage):
    """Production backend.

    Downloads are presigned URLs, so file bytes never stream back through the
    application. That is what keeps the web container's memory flat regardless
    of how large the uploads get.
    """

    name = "s3"

    def __init__(self, bucket: str, prefix: str, region: str | None,
                 access_key: str | None, secret_key: str | None,
                 endpoint: str | None = None):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            endpoint_url=endpoint or None,
            # SigV4 explicitly. Left to itself botocore presigns with SigV2 -
            # `AWSAccessKeyId=...&Signature=...` - which AWS rejects in every
            # region created after 2014, so every download link would 403 with
            # nothing in the application to explain it. The default is a
            # backwards-compatibility decision inside botocore, not a sensible
            # one here.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def check(self) -> str:
        """Confirm the bucket is reachable and writable. Returns "" when fine.

        Called at start-up so a missing key or a misspelled bucket is a line in
        the deploy log rather than a 500 on somebody's first upload.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        probe = self._full(".forge-write-probe")
        try:
            self.client.put_object(Bucket=self.bucket, Key=probe,
                                   Body=b"ok", ContentType="text/plain")
            self.client.delete_object(Bucket=self.bucket, Key=probe)
        except (ClientError, BotoCoreError) as exc:
            return f"{type(exc).__name__}: {exc}"[:300]
        return ""

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            Bucket=self.bucket, Key=self._full(key),
            Body=data, ContentType=content_type,
        )

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=self._full(key))
        return obj["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._full(key))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError:
            return False

    def url(self, key: str, expires: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full(key)},
            ExpiresIn=expires,
        )


def for_org(org_id: str | None) -> Storage:
    """The backend this workspace's files belong in.

    A workspace with its own bucket configured in Settings gets that bucket.
    Anything else falls back to the deployment-wide backend, so a single
    customer install needs nothing filled in.

    Per workspace rather than per deployment because the requirement is that
    nothing gets mixed between clients. Every key already leads with `org_id`,
    so separate folders were true already; separate buckets under separate
    credentials means one client's files are somewhere another client's key
    cannot reach, which is a different and better guarantee.

    Cached per org, because this is called several times per request and each
    call would otherwise be two queries and a decrypt.
    """
    if not org_id:
        return get_storage()
    return _org_storage(str(org_id))


@lru_cache(maxsize=64)
def _org_storage(org_id: str) -> Storage:
    from .. import settings_store

    try:
        config = settings_store.storage_config(org_id)
    except Exception:  # noqa: BLE001
        # A settings table that cannot be read must not take uploads down with
        # it; the deployment-wide backend is still there.
        return get_storage()

    if not config["bucket"]:
        return get_storage()

    return S3Storage(
        bucket=config["bucket"],
        prefix=config["prefix"],
        region=config["region"] or None,
        access_key=config["access_key"] or None,
        secret_key=config["secret_key"] or None,
        endpoint=config["endpoint"] or None,
    )


def forget_org(org_id: str) -> None:
    """Drop the cached backend after its settings change.

    Without this, saving a new bucket would appear to do nothing until the
    process restarted - the kind of bug that gets diagnosed as "the save
    button is broken".
    """
    _org_storage.cache_clear()


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    bucket = os.environ.get("FORGE_S3_BUCKET", "").strip()
    if bucket:
        return S3Storage(
            bucket=bucket,
            prefix=os.environ.get("FORGE_S3_PREFIX", "forge"),
            region=os.environ.get("FORGE_S3_REGION"),
            access_key=os.environ.get("FORGE_S3_ACCESS_KEY_ID"),
            secret_key=os.environ.get("FORGE_S3_SECRET_ACCESS_KEY"),
            endpoint=os.environ.get("FORGE_S3_ENDPOINT"),
        )

    root = os.environ.get("FORGE_LOCAL_STORAGE") or str(REPO_ROOT / ".storage")
    return LocalStorage(Path(root))


def storage_summary() -> dict:
    s = get_storage()
    if isinstance(s, S3Storage):
        return {"backend": "s3", "detail": f"s3://{s.bucket}/{s.prefix}"}
    return {"backend": "local", "detail": str(s.root)}

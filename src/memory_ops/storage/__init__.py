"""Private encrypted object-storage boundary."""

from dataclasses import dataclass
from typing import Any, Protocol


class StorageEncryptionError(RuntimeError):
    """The object store did not confirm encryption for an upload."""


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    etag: str | None
    encryption: str


class ObjectStorage(Protocol):
    def put(
        self,
        key: str,
        content: bytes,
        media_type: str,
        content_hash: str,
    ) -> StoredObject: ...

    def delete(self, key: str) -> None: ...


class S3ObjectStorage:
    """S3-compatible private bucket adapter with mandatory SSE-S3."""

    def __init__(self, client: Any, bucket: str) -> None:
        if not bucket.strip():
            raise ValueError("storage bucket is required")
        self.client = client
        self.bucket = bucket

    def put(
        self,
        key: str,
        content: bytes,
        media_type: str,
        content_hash: str,
    ) -> StoredObject:
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=media_type,
            Metadata={"sha256": content_hash},
            ServerSideEncryption="AES256",
        )
        encryption = response.get("ServerSideEncryption")
        if encryption != "AES256":
            try:
                self.delete(key)
            except Exception as error:
                raise StorageEncryptionError(
                    "storage did not confirm encryption and cleanup failed"
                ) from error
            raise StorageEncryptionError("storage did not confirm encryption")
        return StoredObject(
            bucket=self.bucket,
            key=key,
            etag=response.get("ETag"),
            encryption=encryption,
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

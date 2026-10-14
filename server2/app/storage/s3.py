"""S3 / MinIO compatible object storage backend."""

from __future__ import annotations

from typing import BinaryIO, Iterator


class S3ObjectStore:
    """Stores objects in any S3-compatible object store (AWS S3, MinIO, ...)."""

    name = "s3"

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        use_ssl: bool = False,
    ) -> None:
        import boto3  # imported lazily so local mode has no boto dependency

        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=region,
            use_ssl=use_ssl,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            self._client.create_bucket(Bucket=self.bucket)

    def save(self, key: str, stream: BinaryIO, size: int) -> None:
        self._client.upload_fileobj(stream, self.bucket, key)

    def open_stream(self, key: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            body.close()

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def health(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False

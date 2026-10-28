"""
ALL S3 code lives here (the assessment asks for one module). Async via aioboto3.

Credentials are NOT configured here: boto's default chain finds them (IAM role on AWS,
AWS_ACCESS_KEY_ID/SECRET env vars for local MinIO/LocalStack). S3_ENDPOINT_URL is only set
for MinIO/LocalStack/moto; leave it empty for real AWS.
"""
import os
from typing import Annotated

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from fastapi import Depends

load_dotenv()

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
UPLOAD_URL_EXPIRES = 600  # 10 min to start the upload
DOWNLOAD_URL_EXPIRES = 900  # 15 min, as required


class Storage:
    def __init__(self, bucket: str, region: str, endpoint_url: str | None = None):
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url or None
        self._session = aioboto3.Session()
        # Path-style URLs for MinIO/LocalStack/moto (no virtual-host DNS there).
        self._config = AioConfig(s3={"addressing_style": "path"}) if self.endpoint_url else None

    def _client(self):
        return self._session.client(
            "s3", region_name=self.region, endpoint_url=self.endpoint_url, config=self._config
        )

    async def presigned_upload(self, key: str, content_type: str) -> dict:
        """Pre-signed POST: unlike a pre-signed PUT, S3 itself enforces the size limit and content type."""
        async with self._client() as s3:
            return await s3.generate_presigned_post(
                Bucket=self.bucket,
                Key=key,
                Fields={"Content-Type": content_type},
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, MAX_RECEIPT_BYTES],
                ],
                ExpiresIn=UPLOAD_URL_EXPIRES,
            )

    async def object_size(self, key: str) -> int | None:
        """Size in bytes, or None if the object doesn't exist."""
        async with self._client() as s3:
            try:
                head = await s3.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise
            return head["ContentLength"]

    async def first_bytes(self, key: str, count: int = 16) -> bytes:
        """Reads only the first bytes (HTTP Range), never the whole file."""
        async with self._client() as s3:
            obj = await s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{count - 1}")
            async with obj["Body"] as body:
                return await body.read()

    async def presigned_download(self, key: str) -> str:
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=DOWNLOAD_URL_EXPIRES
            )

    async def delete(self, key: str) -> None:
        """Idempotent: deleting a missing key is not an error in S3."""
        async with self._client() as s3:
            await s3.delete_object(Bucket=self.bucket, Key=key)


storage = Storage(
    bucket=os.environ["S3_BUCKET"],
    region=os.environ["AWS_REGION"],
    endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
)


async def get_storage() -> Storage:
    return storage


StorageDep = Annotated[Storage, Depends(get_storage)]

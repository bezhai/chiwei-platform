from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass
from io import BytesIO
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from PIL import Image

from app.service.results import error_row


class ObjectReader(Protocol):
    def read_object(self, key: str) -> bytes:
        ...


class MinioObjectReader:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        region: str = "us-east-1",
        timeout_seconds: float = 60.0,
    ) -> None:
        if "://" not in endpoint:
            scheme = "https" if secure else "http"
            endpoint = f"{scheme}://{endpoint}"
        parsed = urlsplit(endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"invalid MinIO endpoint: {endpoint!r}")
        self._base_url = f"{parsed.scheme}://{parsed.netloc}"
        self._host = parsed.netloc
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._region = region
        self._timeout_seconds = timeout_seconds

    def read_object(self, key: str) -> bytes:
        encoded_bucket = quote(self._bucket, safe="")
        encoded_key = quote(key, safe="/")
        canonical_uri = f"/{encoded_bucket}/{encoded_key}"
        url = f"{self._base_url}{canonical_uri}"
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = "UNSIGNED-PAYLOAD"
        canonical_headers = (
            f"host:{self._host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                "GET",
                canonical_uri,
                "",
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _s3_signing_key(self._secret_key, date_stamp, self._region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request = Request(
            url,
            headers={
                "Authorization": authorization,
                "Host": self._host,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            body = exc.read(256).decode("utf-8", errors="replace")
            raise RuntimeError(f"MinIO GET failed for {key}: HTTP {exc.code}: {body}") from exc


def _s3_signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret_key}".encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode("utf-8"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


@dataclass(frozen=True)
class LoadedImages:
    items: list[tuple[str, Image.Image]]
    error_rows: list[dict]


def load_images_from_paths(paths: list[str], reader: ObjectReader) -> LoadedImages:
    items: list[tuple[str, Image.Image]] = []
    errors: list[dict] = []
    for path in paths:
        try:
            raw = reader.read_object(path)
            image = Image.open(BytesIO(raw))
            image.load()
        except Exception as exc:
            errors.append(error_row(path, "input", f"{type(exc).__name__}: {exc}"))
            continue
        items.append((path, image))
    return LoadedImages(items=items, error_rows=errors)

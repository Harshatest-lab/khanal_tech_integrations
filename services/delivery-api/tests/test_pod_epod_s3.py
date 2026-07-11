"""Unit tests for EPOD → S3 upload helpers (no real AWS calls)."""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock

import pytest

from src.pod_epod_s3 import (
    decode_epod_bytes,
    fetch_pod_url_bytes,
    is_our_s3_pod_url,
    maybe_mirror_external_pod_url,
    maybe_upload_epod_clear_b64,
    s3_object_https_url,
    sniff_content_type_and_ext,
    upload_epod_to_s3,
    upload_pod_bytes_to_s3,
)


def test_decode_epod_bytes_padding() -> None:
    raw = base64.b64encode(b"hello").decode("ascii").rstrip("=")
    assert decode_epod_bytes(raw) == b"hello"


def test_sniff_pdf_jpeg_unknown() -> None:
    assert sniff_content_type_and_ext(b"%PDF-1.4\n") == ("application/pdf", "pdf")
    assert sniff_content_type_and_ext(b"\xff\xd8\xff\xe0\x00") == ("image/jpeg", "jpg")
    assert sniff_content_type_and_ext(b"\x89PNG\r\n\x1a\n\x00") == ("image/png", "png")
    assert sniff_content_type_and_ext(b"unknown") == ("application/octet-stream", "bin")


def test_s3_object_https_url_encodes_segments() -> None:
    u = s3_object_https_url(bucket="b", region="us-east-1", key="a/b c/d.pdf")
    assert u == "https://b.s3.us-east-1.amazonaws.com/a/b%20c/d.pdf"


def test_maybe_upload_no_bucket_noop() -> None:
    os.environ.pop("POD_S3_BUCKET", None)
    fields = {
        "lrnum": "LR1",
        "waybill": "",
        "epod_b64": base64.b64encode(b"%PDF-x").decode(),
        "pod_url": None,
        "pod_format": "base64",
    }
    orig = dict(fields)
    maybe_upload_epod_clear_b64(fields)
    assert fields == orig


def test_maybe_upload_calls_put_and_clears_b64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("POD_S3_PREFIX", "pods")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    calls: list[dict] = []

    def fake_upload(**kwargs: object) -> str:
        calls.append(kwargs)
        return "https://test-bucket.s3.us-east-1.amazonaws.com/pods/LR-99/u.pdf"

    monkeypatch.setattr("src.pod_epod_s3.upload_epod_to_s3", fake_upload)

    pdf = b"%PDF-1.1\n%\xe2\xe3\xcf\x0b\n"
    fields = {
        "lrnum": "LR-99",
        "waybill": "",
        "epod_b64": base64.b64encode(pdf).decode("ascii"),
        "pod_url": None,
        "pod_format": "base64",
    }
    maybe_upload_epod_clear_b64(fields)

    assert len(calls) == 1
    assert calls[0]["bucket"] == "test-bucket"
    assert calls[0]["key_prefix"] == "pods"
    assert calls[0]["lr_or_waybill"] == "LR-99"
    assert fields["epod_b64"] is None
    assert fields["pod_format"] == "url"
    assert fields["pod_url"] == "https://test-bucket.s3.us-east-1.amazonaws.com/pods/LR-99/u.pdf"


def test_upload_epod_injects_client() -> None:
    pdf = b"%PDF-1.1\n"
    mock = MagicMock()
    url = upload_epod_to_s3(
        bucket="b",
        key_prefix="p",
        lr_or_waybill="x",
        epod_b64=base64.b64encode(pdf).decode("ascii"),
        s3_client=mock,
    )
    mock.put_object.assert_called_once()
    assert "b.s3." in url


def test_is_our_s3_pod_url() -> None:
    bucket = "delivery-api-dev-delhivery-epod"
    ours = "https://delivery-api-dev-delhivery-epod.s3.us-east-1.amazonaws.com/delhivery-pod/LR1/x.pdf"
    theirs = "https://fe-app-hq-dev.s3.amazonaws.com/dev/SIGNATURE_URL?X-Amz-Signature=abc"
    assert is_our_s3_pod_url(ours, bucket) is True
    assert is_our_s3_pod_url(theirs, bucket) is False


def test_fetch_pod_url_bytes_via_opener() -> None:
    pdf = b"%PDF-1.4\n"

    class FakeResp:
        def __init__(self) -> None:
            self._data = pdf

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def getcode(self) -> int:
            return 200

        def read(self, n: int = -1) -> bytes:
            if self._data:
                out, self._data = self._data, b""
                return out
            return b""

    def fake_open(req: object, timeout: float) -> FakeResp:
        return FakeResp()

    assert fetch_pod_url_bytes("https://example.com/pod.pdf", opener=fake_open) == pdf


def test_maybe_mirror_external_pod_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_S3_BUCKET", "test-bucket")
    monkeypatch.setenv("POD_S3_PREFIX", "pods")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    external = "https://fe-app-hq-dev.s3.amazonaws.com/dev/pod.pdf?sig=1"
    pdf = b"%PDF-1.1\n"

    monkeypatch.setattr("src.pod_epod_s3.fetch_pod_url_bytes", lambda url: pdf)

    uploads: list[dict] = []

    def fake_upload(**kwargs: object) -> str:
        uploads.append(kwargs)
        return "https://test-bucket.s3.us-east-1.amazonaws.com/pods/WB-1/u.pdf"

    monkeypatch.setattr("src.pod_epod_s3.upload_pod_bytes_to_s3", fake_upload)

    fields = {
        "lrnum": None,
        "waybill": "WB-1",
        "epod_b64": None,
        "pod_url": external,
        "pod_format": "url",
    }
    maybe_mirror_external_pod_url(fields)

    assert len(uploads) == 1
    assert uploads[0]["data"] == pdf
    assert fields["pod_url"] == "https://test-bucket.s3.us-east-1.amazonaws.com/pods/WB-1/u.pdf"
    assert fields["pod_format"] == "url"


def test_maybe_mirror_skips_when_already_our_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_S3_BUCKET", "test-bucket")
    ours = "https://test-bucket.s3.us-east-1.amazonaws.com/pods/LR1/x.pdf"
    fields = {"lrnum": "LR1", "waybill": "", "epod_b64": None, "pod_url": ours, "pod_format": "url"}
    maybe_mirror_external_pod_url(fields)
    assert fields["pod_url"] == ours


def test_upload_pod_bytes_injects_client() -> None:
    pdf = b"%PDF-1.1\n"
    mock = MagicMock()
    url = upload_pod_bytes_to_s3(
        bucket="b",
        key_prefix="p",
        lr_or_waybill="x",
        data=pdf,
        s3_client=mock,
    )
    mock.put_object.assert_called_once()
    assert "b.s3." in url

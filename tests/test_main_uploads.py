from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import HTTPException, UploadFile

from app.main import parse_manifest, read_uploads
from app.models import ExpectedFields, UploadedImage


def upload(name: str, content: bytes = b"x", content_type: str = "application/octet-stream") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(content), headers=None)


def test_read_uploads_recovers_manifest_sent_as_image_field():
    manifest = json.dumps({"products": []}).encode()
    payloads, recovered = asyncio.run(
        read_uploads(
            [
                upload("labels/front.jpg", b"image", "image/jpeg"),
                upload("manifest.json", manifest, "application/json"),
            ]
        )
    )
    assert len(payloads) == 1
    assert payloads[0].filename == "labels/front.jpg"
    assert recovered is not None
    assert recovered.filename == "manifest.json"


def test_read_uploads_rejects_non_image_with_clear_400():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_uploads([upload("notes.txt", b"not an image", "text/plain")]))
    assert exc.value.status_code == 400
    assert "unsupported upload" in exc.value.detail


def test_single_manifest_folder_relative_path_matches_uploaded_basename():
    images = [UploadedImage(image_id="1", filename="folder/front.jpg", content=b"image")]
    manifest = upload(
        "manifest.json",
        json.dumps(
            {
                "products": [
                    {
                        "product_id": "cola-1",
                        "brand_name": "Drop of Sunshine",
                        "images": ["images/front.jpg"],
                    }
                ]
            }
        ).encode(),
        "application/json",
    )
    products = asyncio.run(parse_manifest(manifest, images, ExpectedFields(class_type="Wine")))
    assert len(products) == 1
    assert products[0]["product_id"] == "cola-1"
    assert products[0]["expected"].brand_name == "Drop of Sunshine"
    assert products[0]["expected"].class_type == "Wine"
    assert products[0]["images"][0].filename == "folder/front.jpg"


def test_manifest_ambiguous_basename_returns_clear_error():
    images = [
        UploadedImage(image_id="1", filename="a/front.jpg", content=b"a"),
        UploadedImage(image_id="2", filename="b/front.jpg", content=b"b"),
    ]
    manifest = upload(
        "manifest.json",
        json.dumps({"products": [{"product_id": "x", "images": ["front.jpg"]}]}).encode(),
        "application/json",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(parse_manifest(manifest, images, ExpectedFields()))
    assert exc.value.status_code == 400
    assert "ambiguous image filename" in exc.value.detail

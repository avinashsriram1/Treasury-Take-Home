from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models import ExpectedFields, ExtractionResult, UploadedImage
from app.services import azure_foundry_extractor
from app.services.azure_foundry_extractor import (
    AzureFoundryExtractionError,
    extract_with_azure_foundry,
    foundry_base_url,
    response_output_text,
)


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "model": "gpt-4.1-mini",
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": """
                            {
                              "fields": {
                                "brand_name": {"value": "North Orchard", "confidence": 0.93}
                              },
                              "government_warning": {
                                "present": true,
                                "heading_text": "GOVERNMENT WARNING",
                                "heading_all_caps": true,
                                "confidence": 0.92
                              },
                              "confidence": 0.91,
                              "notes": []
                            }
                            """,
                        }
                    ]
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }


class FakeClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse()


def test_foundry_base_url_normalizes_project_endpoint_and_query(monkeypatch):
    monkeypatch.setattr(
        azure_foundry_extractor,
        "settings",
        SimpleNamespace(
            azure_foundry_endpoint="https://example.services.ai.azure.com/api/projects/demo?api-version=2024-10-21",
        ),
    )
    assert foundry_base_url() == "https://example.services.ai.azure.com/api/projects/demo/openai/v1"


def test_response_output_text_reads_responses_api_payload():
    payload = {"output": [{"content": [{"type": "output_text", "text": '{"ok": true}'}]}]}
    assert response_output_text(payload) == '{"ok": true}'


def test_extract_with_azure_foundry_posts_to_responses_endpoint(monkeypatch):
    fake_client = FakeClient()

    async def fake_foundry_client():
        return fake_client

    monkeypatch.setattr(
        azure_foundry_extractor,
        "settings",
        SimpleNamespace(
            azure_foundry_endpoint="https://example.services.ai.azure.com/api/projects/demo/openai/v1/",
            azure_foundry_api_key="test-key",
            azure_foundry_deployment="gpt-4.1-mini",
            azure_foundry_max_output_tokens=50,
        ),
    )
    monkeypatch.setattr(azure_foundry_extractor, "foundry_client", fake_foundry_client)
    monkeypatch.setattr(azure_foundry_extractor, "contact_sheet_base64_jpeg", lambda _images: "abc123")

    result = asyncio.run(
        extract_with_azure_foundry(
            [UploadedImage(image_id="1", filename="label.jpg", content=b"image")], ExpectedFields()
        )
    )

    assert isinstance(result, ExtractionResult)
    assert result.provider == "azure_foundry"
    assert result.model_used == "gpt-4.1-mini"
    assert result.fields["brand_name"].value == "North Orchard"
    assert fake_client.calls[0]["url"] == "https://example.services.ai.azure.com/api/projects/demo/openai/v1/responses"
    assert fake_client.calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert fake_client.calls[0]["json"]["model"] == "gpt-4.1-mini"
    assert fake_client.calls[0]["json"]["max_output_tokens"] == 50


class InvalidJsonResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"output_text": "The label appears to say North Orchard, but this is not JSON."}


class InvalidJsonClient:
    async def post(self, url, headers, json):
        return InvalidJsonResponse()


def test_azure_foundry_parse_failure_logs_and_keeps_raw_output(monkeypatch, caplog):
    async def fake_foundry_client():
        return InvalidJsonClient()

    monkeypatch.setattr(
        azure_foundry_extractor,
        "settings",
        SimpleNamespace(
            azure_foundry_endpoint="https://example.services.ai.azure.com/api/projects/demo/openai/v1/",
            azure_foundry_api_key="test-key",
            azure_foundry_deployment="gpt-4.1-mini",
            azure_foundry_max_output_tokens=50,
        ),
    )
    monkeypatch.setattr(azure_foundry_extractor, "foundry_client", fake_foundry_client)
    monkeypatch.setattr(azure_foundry_extractor, "contact_sheet_base64_jpeg", lambda _images: "abc123")

    with caplog.at_level("WARNING"), pytest.raises(AzureFoundryExtractionError) as exc_info:
        asyncio.run(
            extract_with_azure_foundry(
                [UploadedImage(image_id="1", filename="label.jpg", content=b"image")], ExpectedFields()
            )
        )

    assert "not JSON" in exc_info.value.raw_output
    assert "Azure Foundry response parse failed" in caplog.text



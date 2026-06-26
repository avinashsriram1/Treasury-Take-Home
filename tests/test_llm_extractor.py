import asyncio
from types import SimpleNamespace

import pytest

from app.models import ExpectedFields, ExtractionResult, UploadedImage
from app.services import llm_extractor
from app.services.llm_extractor import (
    OpenRouterExtractionError,
    configured_free_models,
    extract_with_openrouter,
    parse_or_repair_extraction,
)


def test_parse_model_json_response():
    extraction = parse_or_repair_extraction(
        """
        {
          "fields": {
            "brand_name": {"value": "North Orchard", "confidence": 0.95, "evidence": "North Orchard"}
          },
          "government_warning": {
            "present": true,
            "heading_text": "GOVERNMENT WARNING",
            "heading_all_caps": true,
            "confidence": 0.9
          },
          "confidence": 0.9,
          "notes": []
        }
        """
    )
    assert isinstance(extraction, ExtractionResult)
    assert extraction.fields["brand_name"].value == "North Orchard"
    assert extraction.raw_text == ""


def test_free_mode_accepts_free_gemma(monkeypatch):
    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_models=("google/gemma-4-26b-a4b-it:free",),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
        ),
    )
    assert configured_free_models() == ("google/gemma-4-26b-a4b-it:free",)


def test_free_mode_accepts_openrouter_free(monkeypatch):
    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_models=("openrouter/free",),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
        ),
    )
    assert configured_free_models() == ("openrouter/free",)


def test_free_mode_rejects_paid_model(monkeypatch):
    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_models=("paid/model",),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
        ),
    )
    with pytest.raises(OpenRouterExtractionError):
        configured_free_models()


def test_fallback_disabled_stops_after_primary(monkeypatch):
    calls = []

    async def fake_request_model(model, _images, _expected):
        calls.append(model)
        raise OpenRouterExtractionError("timeout")

    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_api_key="test",
            openrouter_models=("google/gemma-4-26b-a4b-it:free", "openrouter/free"),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
            openrouter_enable_fallbacks=False,
        ),
    )
    monkeypatch.setattr(llm_extractor, "request_model", fake_request_model)
    with pytest.raises(OpenRouterExtractionError, match="free_llm_unavailable"):
        asyncio.run(
            extract_with_openrouter([UploadedImage(image_id="1", filename="x.png", content=b"x")], ExpectedFields())
        )
    assert calls == ["google/gemma-4-26b-a4b-it:free"]


def test_free_model_fallback_when_enabled(monkeypatch):
    calls = []

    async def fake_request_model(model, _images, _expected):
        calls.append(model)
        if model == "google/gemma-4-26b-a4b-it:free":
            raise OpenRouterExtractionError("timeout")
        return ExtractionResult(confidence=0.9, model_used=model)

    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_api_key="test",
            openrouter_models=("google/gemma-4-26b-a4b-it:free", "openrouter/free"),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
            openrouter_enable_fallbacks=True,
        ),
    )
    monkeypatch.setattr(llm_extractor, "request_model", fake_request_model)
    result = asyncio.run(
        extract_with_openrouter([UploadedImage(image_id="1", filename="x.png", content=b"x")], ExpectedFields())
    )
    assert result.model_used == "openrouter/free"
    assert result.fallback_used is True
    assert calls == ["google/gemma-4-26b-a4b-it:free", "openrouter/free"]


def test_all_free_routes_fail_as_reviewable_error(monkeypatch):
    async def fake_request_model(model, _images, _expected):
        raise OpenRouterExtractionError(f"{model} unavailable")

    monkeypatch.setattr(
        llm_extractor,
        "settings",
        SimpleNamespace(
            openrouter_api_key="test",
            openrouter_models=("google/gemma-4-26b-a4b-it:free", "openrouter/free"),
            openrouter_require_free=True,
            openrouter_allow_paid_fallback=False,
            openrouter_enable_fallbacks=True,
        ),
    )
    monkeypatch.setattr(llm_extractor, "request_model", fake_request_model)
    with pytest.raises(OpenRouterExtractionError, match="free_llm_unavailable"):
        asyncio.run(
            extract_with_openrouter([UploadedImage(image_id="1", filename="x.png", content=b"x")], ExpectedFields())
        )


def test_parse_markdown_fenced_json_response():
    extraction = parse_or_repair_extraction(
        """
        ```json
        {
          "fields": {
            "brand_name": {"value": "Drop of Sunshine", "confidence": 0.92}
          },
          "government_warning": {
            "present": true,
            "heading_text": "GOVERNMENT WARNING",
            "heading_all_caps": true,
            "confidence": 0.9
          },
          "confidence": 0.88,
          "notes": []
        }
        ```
        """
    )
    assert extraction.fields["brand_name"].value == "Drop of Sunshine"
    assert extraction.government_warning.present is True


def test_parse_flat_alias_json_response():
    extraction = parse_or_repair_extraction(
        """
        {
          "brand": "Alvin Langston",
          "class": "Whiskey",
          "abv": "142.8 proof",
          "net": "750 ml",
          "producer": "Hilton Head Distillery LLC, Hilton Head Island, SC",
          "countryOfOrigin": "United States",
          "warning": "GOVERNMENT WARNING: according to the surgeon general",
          "overall_confidence": 0.77
        }
        """
    )
    assert extraction.fields["brand_name"].value == "Alvin Langston"
    assert extraction.fields["class_type"].value == "Whiskey"
    assert extraction.fields["alcohol_content"].value == "142.8 proof"
    assert extraction.fields["net_contents"].value == "750 ml"
    assert extraction.fields["bottler"].value.startswith("Hilton Head Distillery")
    assert extraction.fields["country"].value == "United States"
    assert extraction.government_warning.present is True


def test_parse_azure_flat_dotted_extraction_json():
    extraction = parse_or_repair_extraction(
        """
        {
          "fields.brand_name": {"value": "Alvin Langston", "confidence": 0.99},
          "fields.class_type": {"value": "American Light Whiskey", "confidence": 0.95},
          "fields.alcohol_content": {"value": "71.4%", "confidence": 0.98},
          "fields.net_contents": {"value": "750ML", "confidence": 0.98},
          "fields.bottler": {
            "value": "Hilton Head Distillery, LLC, 14 Cardinal Rd. Hilton Head Island, SC 29926",
            "confidence": 0.96
          },
          "fields.country": {"value": "United States", "confidence": 0.90},
          "government_warning.present": {"value": true, "confidence": 0.99},
          "government_warning.heading_text": {"value": "GOVERNMENT WARNING", "confidence": 0.99},
          "government_warning.heading_all_caps": {"value": true, "confidence": 0.99},
          "confidence": 0.95,
          "notes": []
        }
        """
    )
    assert extraction.fields["brand_name"].value == "Alvin Langston"
    assert extraction.fields["class_type"].value == "American Light Whiskey"
    assert extraction.fields["alcohol_content"].value == "71.4%"
    assert extraction.fields["net_contents"].value == "750ML"
    assert extraction.fields["bottler"].value.startswith("Hilton Head Distillery")
    assert extraction.fields["country"].value == "United States"
    assert extraction.government_warning.present is True
    assert extraction.government_warning.heading_text == "GOVERNMENT WARNING"
    assert extraction.government_warning.heading_all_caps is True




def test_recover_dotted_fields_from_truncated_azure_json_tail():
    extraction = parse_or_repair_extraction(
        """
        {
          "fields.brand_name": {"value": "Alvin Langston", "confidence": 0.99},
          "fields.class_type": {"value": "American Light Whiskey", "confidence": 0.95},
          "fields.alcohol_content": {"value": "71.4% ALC/VOL / 142.8 PROOF", "confidence": 0.98},
          "fields.net_contents": {"value": "750 ml", "confidence": 0.99},
          "fields.bottler": {
            "value": "Hilton Head Distillery, LLC, 14 Cardinal Rd. Hilton Head Island, SC 29926",
            "confidence": 0.98
          },
          "fields.country": {"value": "United States", "confidence": 0.90},
          "government_warning.present": {"value": true, "confidence": 1.0},
          "government_warning.heading_text": {"value": "GOVERNMENT WARNING:", "confidence": 1.0},
          "government_warning.heading_all_caps": {"value": true, "confidence": 1.0},
          "confidence": 0.97,
          "notes": "Brand name has high confidence but this string is intentionally truncated
        """
    )
    assert extraction.fields["brand_name"].value == "Alvin Langston"
    assert extraction.fields["alcohol_content"].value == "71.4% ALC/VOL / 142.8 PROOF"
    assert extraction.government_warning.present is True
    assert extraction.government_warning.heading_text == "GOVERNMENT WARNING:"


from __future__ import annotations

import base64
import json
import time
from io import BytesIO
from typing import Any

import httpx
from PIL import Image
from pydantic import ValidationError

from app.config import settings
from app.models import ExpectedFields, ExtractedField, ExtractionResult, UploadedImage

SYSTEM_PROMPT = """You extract visible alcohol label information for TTB review.
Return only JSON that matches this shape:
{
  "fields": {
    "brand_name": {"value": string|null, "confidence": number, "evidence": string|null},
    "class_type": {"value": string|null, "confidence": number, "evidence": string|null},
    "alcohol_content": {"value": string|null, "confidence": number, "evidence": string|null},
    "net_contents": {"value": string|null, "confidence": number, "evidence": string|null},
    "bottler": {"value": string|null, "confidence": number, "evidence": string|null},
    "country": {"value": string|null, "confidence": number, "evidence": string|null}
  },
  "government_warning": {
    "present": boolean,
    "heading_text": string|null,
    "heading_all_caps": boolean|null,
    "body_text": string|null,
    "confidence": number,
    "evidence": string|null
  },
  "raw_text": string,
  "confidence": number,
  "notes": [string]
}
Only extract text visible in the image. Do not invent missing fields. The final verdict is not your job."""


class OpenRouterExtractionError(RuntimeError):
    pass


async def extract_with_openrouter(
    images: list[UploadedImage], expected: ExpectedFields
) -> ExtractionResult:
    if not settings.openrouter_api_key:
        raise OpenRouterExtractionError("OPENROUTER_API_KEY is not configured")

    started = time.perf_counter()
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Extract label fields from all images as one product. "
                f"Application data for comparison: {expected.model_dump(exclude_none=True)}"
            ),
        }
    ]
    for image in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_to_base64_jpeg(image.content)}"
                },
            }
        )

    request = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=settings.llm_request_timeout_seconds) as client:
        response = await client.post(
            f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.app_url
                or "https://github.com/avinashsriram1/Treasury-Take-Home",
                "X-Title": "Treasury Take Home V3",
            },
            json=request,
        )
    if response.status_code >= 400:
        raise OpenRouterExtractionError(
            f"OpenRouter request failed: {response.status_code} {response.text[:300]}"
        )

    payload = response.json()
    content_text = payload["choices"][0]["message"]["content"]
    extraction = parse_or_repair_extraction(content_text)
    extraction.model_used = payload.get("model") or settings.openrouter_model
    extraction.provider = "openrouter"
    extraction.token_usage = payload.get("usage")
    extraction.latency_ms = int((time.perf_counter() - started) * 1000)
    return extraction


def parse_or_repair_extraction(content: str) -> ExtractionResult:
    try:
        return ExtractionResult.model_validate_json(content)
    except ValidationError:
        try:
            repaired = extract_json_object(content)
            return ExtractionResult.model_validate(repaired)
        except Exception as exc:  # noqa: BLE001
            raise OpenRouterExtractionError("Model response was not valid extraction JSON") from exc


def extract_json_object(content: str) -> dict[str, Any]:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    parsed = json.loads(content[start : end + 1])
    fields = parsed.get("fields") or {}
    parsed["fields"] = {
        key: value if isinstance(value, dict) else {"value": value, "confidence": 0.5}
        for key, value in fields.items()
    }
    for key in [
        "brand_name",
        "class_type",
        "alcohol_content",
        "net_contents",
        "bottler",
        "country",
    ]:
        parsed["fields"].setdefault(key, ExtractedField().model_dump())
    parsed.setdefault("government_warning", {})
    parsed.setdefault("raw_text", "")
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("notes", [])
    return parsed


def image_to_base64_jpeg(bytes_: bytes) -> str:
    image = Image.open(BytesIO(bytes_)).convert("RGB")
    image.thumbnail((1600, 1600))
    output = BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")

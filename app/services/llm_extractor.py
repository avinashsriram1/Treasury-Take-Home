from __future__ import annotations

import base64
import json
import re
import time
from io import BytesIO
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageOps
from pydantic import ValidationError

from app.config import is_free_openrouter_model, settings
from app.models import ExpectedFields, ExtractedField, ExtractionResult, StageTiming, UploadedImage

SYSTEM_PROMPT = (
    "Extract visible alcohol label fields for TTB review. Return JSON only with keys: "
    "fields.brand_name, fields.class_type, fields.alcohol_content, fields.net_contents, "
    "fields.bottler, fields.country, government_warning.present, government_warning.heading_text, "
    "government_warning.heading_all_caps, confidence, notes. Each field value is an object with "
    "value, confidence, evidence. Use null when unknown. Do not decide the final verdict."
)


class OpenRouterExtractionError(RuntimeError):
    pass


_client: httpx.AsyncClient | None = None


def configured_models() -> tuple[str, ...]:
    models = settings.openrouter_models
    if settings.openrouter_require_free or not settings.openrouter_allow_paid_fallback:
        invalid = [model for model in models if not is_free_openrouter_model(model)]
        if invalid:
            raise OpenRouterExtractionError(
                "Paid OpenRouter models are disabled; invalid configured model(s): " + ", ".join(invalid)
            )
    return models


# Backwards-compatible name used by existing tests.
def configured_free_models() -> tuple[str, ...]:
    return configured_models()


async def openrouter_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(
            settings.openrouter_request_timeout_seconds,
            connect=settings.openrouter_connect_timeout_seconds,
        )
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


async def extract_with_openrouter(images: list[UploadedImage], expected: ExpectedFields) -> ExtractionResult:
    if not settings.openrouter_api_key:
        raise OpenRouterExtractionError("OPENROUTER_API_KEY is not configured")

    failures: list[str] = []
    for index, model in enumerate(configured_models()):
        total_started = time.perf_counter()
        try:
            extraction = await request_model(model, images, expected)
            extraction.fallback_used = index > 0
            extraction.latency_ms = int((time.perf_counter() - total_started) * 1000)
            extraction.stage_timings.append(StageTiming(stage="total", elapsed_ms=extraction.latency_ms))
            return extraction
        except (httpx.TimeoutException, httpx.HTTPError, OpenRouterExtractionError) as exc:
            failures.append(f"{model}: {exc}")
            if not settings.openrouter_enable_fallbacks:
                break
    reason = "free_llm_unavailable" if settings.openrouter_require_free else "llm_unavailable"
    raise OpenRouterExtractionError(reason + "; " + " | ".join(failures))


async def request_model(model: str, images: list[UploadedImage], expected: ExpectedFields) -> ExtractionResult:
    prep_started = time.perf_counter()
    image_payload = contact_sheet_base64_jpeg(images)
    prep_ms = int((time.perf_counter() - prep_started) * 1000)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Extract fields from the image(s) as one alcohol label product. "
                "Use null for unknown values. Application context: "
                f"{expected.model_dump(exclude_none=True)}"
            ),
        },
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_payload}"}},
    ]
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": settings.openrouter_max_tokens,
        "response_format": {"type": "json_object"},
    }
    client = await openrouter_client()
    request_started = time.perf_counter()
    response = await client.post(
        f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": settings.app_url or "https://github.com/avinashsriram1/Treasury-Take-Home",
            "X-Title": "Treasury Take Home V3",
        },
        json=request,
    )
    request_ms = int((time.perf_counter() - request_started) * 1000)
    if response.status_code >= 400:
        detail = response.text[:300].replace("\n", " ")
        raise OpenRouterExtractionError(f"OpenRouter request failed: {response.status_code} {detail}")

    parse_started = time.perf_counter()
    payload = response.json()
    content_text = payload["choices"][0]["message"]["content"]
    extraction = parse_or_repair_extraction(content_text)
    parse_ms = int((time.perf_counter() - parse_started) * 1000)
    extraction.model_used = payload.get("model") or model
    extraction.provider = "openrouter"
    extraction.token_usage = payload.get("usage")
    extraction.stage_timings = [
        StageTiming(stage="image_preparation", elapsed_ms=prep_ms),
        StageTiming(stage="openrouter_request", elapsed_ms=request_ms),
        StageTiming(stage="parse_validation", elapsed_ms=parse_ms),
    ]
    return extraction


def parse_or_repair_extraction(content: str) -> ExtractionResult:
    try:
        repaired = extract_json_object(content)
        return ExtractionResult.model_validate(repaired)
    except Exception:
        fallback = extract_dotted_fields_fallback(content)
        if fallback:
            return ExtractionResult.model_validate(fallback)
        try:
            return ExtractionResult.model_validate_json(content)
        except ValidationError as exc:
            raise OpenRouterExtractionError("Model response was not valid extraction JSON") from exc


def extract_json_object(content: str) -> dict[str, Any]:
    parsed = first_json_object(content)
    if isinstance(parsed, list):
        parsed = next((item for item in parsed if isinstance(item, dict)), {})
    if not isinstance(parsed, dict):
        raise ValueError("JSON payload was not an object")
    return normalize_extraction_payload(parsed)


def first_json_object(content: str) -> Any:
    cleaned = strip_code_fence(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(cleaned[start : end + 1])


def strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def normalize_extraction_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    canonical_fields = ["brand_name", "class_type", "alcohol_content", "net_contents", "bottler", "country"]
    aliases = {
        "brand": "brand_name",
        "brandName": "brand_name",
        "class": "class_type",
        "type": "class_type",
        "classType": "class_type",
        "alcohol": "alcohol_content",
        "abv": "alcohol_content",
        "proof": "alcohol_content",
        "alcoholContent": "alcohol_content",
        "net": "net_contents",
        "netContents": "net_contents",
        "contents": "net_contents",
        "producer": "bottler",
        "bottler_producer": "bottler",
        "bottlerProducer": "bottler",
        "country_of_origin": "country",
        "countryOfOrigin": "country",
    }
    raw_fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    fields: dict[str, Any] = dict(raw_fields)
    for source, target in aliases.items():
        if source in parsed and target not in fields:
            fields[target] = parsed[source]
    for key in canonical_fields:
        dotted_key = f"fields.{key}"
        if dotted_key in parsed and key not in fields:
            fields[key] = parsed[dotted_key]
        if key in parsed and key not in fields:
            fields[key] = parsed[key]
    parsed["fields"] = {key: normalize_field_value(fields.get(key)) for key in canonical_fields}

    warning = parsed.get("government_warning") or parsed.get("governmentWarning") or parsed.get("warning") or {}
    dotted_warning = {
        "present": parsed.get("government_warning.present"),
        "heading_text": parsed.get("government_warning.heading_text"),
        "heading_all_caps": parsed.get("government_warning.heading_all_caps"),
        "body_text": parsed.get("government_warning.body_text"),
    }
    dotted_warning = {
        key: normalize_extracted_scalar(value)
        for key, value in dotted_warning.items()
        if value is not None
    }
    if dotted_warning:
        warning = {**(warning if isinstance(warning, dict) else {}), **dotted_warning}
    if isinstance(warning, str):
        warning = {
            "present": "GOVERNMENT WARNING" in warning,
            "heading_text": "GOVERNMENT WARNING" if "GOVERNMENT WARNING" in warning else warning[:80],
            "heading_all_caps": "GOVERNMENT WARNING" in warning,
            "confidence": 0.5,
            "evidence": warning,
        }
    elif not isinstance(warning, dict):
        warning = {}
    if "heading" in warning and "heading_text" not in warning:
        warning["heading_text"] = warning["heading"]
    if "all_caps" in warning and "heading_all_caps" not in warning:
        warning["heading_all_caps"] = warning["all_caps"]
    parsed["government_warning"] = warning
    parsed.setdefault("raw_text", parsed.get("raw_extraction") or parsed.get("text") or "")
    parsed.setdefault("confidence", parsed.get("overall_confidence") or 0.0)
    parsed.setdefault("notes", [])
    if isinstance(parsed["notes"], str):
        parsed["notes"] = [parsed["notes"]]
    return parsed


def normalize_field_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        normalized = dict(value)
        if "text" in normalized and "value" not in normalized:
            normalized["value"] = normalized["text"]
        normalized.setdefault("confidence", 0.5 if normalized.get("value") else 0.0)
        return normalized
    if value is None:
        return ExtractedField().model_dump()
    return {"value": str(value), "confidence": 0.5}



def normalize_extracted_scalar(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value
def extract_dotted_fields_fallback(content: str) -> dict[str, Any] | None:
    canonical_fields = ["brand_name", "class_type", "alcohol_content", "net_contents", "bottler", "country"]
    fields: dict[str, Any] = {}
    for field in canonical_fields:
        value = extract_wrapped_value(content, f"fields.{field}")
        if value is not None:
            fields[field] = value

    warning: dict[str, Any] = {}
    warning_keys = {
        "government_warning.present": "present",
        "government_warning.heading_text": "heading_text",
        "government_warning.heading_all_caps": "heading_all_caps",
        "government_warning.body_text": "body_text",
    }
    for source, target in warning_keys.items():
        value = extract_wrapped_value(content, source)
        if value is not None:
            warning[target] = value

    if not fields and not warning:
        return None
    return normalize_extraction_payload(
        {
            "fields": fields,
            "government_warning": warning,
            "confidence": extract_top_level_number(content, "confidence") or 0.0,
            "notes": [],
        }
    )


def extract_wrapped_value(content: str, key: str) -> Any:
    pattern = rf'"{re.escape(key)}"\s*:\s*\{{(?P<body>.*?)\}}'
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return None
    body = match.group("body")
    value_pattern = r'"value"\s*:\s*(?P<value>"(?:\\.|[^"\\])*"|true|false|null|-?\d+(?:\.\d+)?)'
    value_match = re.search(value_pattern, body)
    if not value_match:
        return None
    try:
        return json.loads(value_match.group("value"))
    except json.JSONDecodeError:
        return None


def extract_top_level_number(content: str, key: str) -> float | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(-?\d+(?:\.\d+)?)', content)
    return float(match.group(1)) if match else None


def contact_sheet_base64_jpeg(images: list[UploadedImage]) -> str:
    if len(images) == 1:
        return image_to_base64_jpeg(
            images[0].content,
            long_edge=settings.openrouter_max_image_long_edge,
            quality=settings.openrouter_jpeg_quality,
        )

    columns = 2
    max_edge = settings.openrouter_max_image_long_edge
    tile_w = max(320, max_edge // columns)
    tile_h = max(420, int(tile_w * 1.3))
    label_h = 24
    thumbs: list[tuple[UploadedImage, Image.Image]] = []
    for uploaded in images:
        image = Image.open(BytesIO(uploaded.content)).convert("RGB")
        image = ImageOps.exif_transpose(image)
        image.thumbnail((tile_w, tile_h - label_h), Image.Resampling.LANCZOS)
        thumbs.append((uploaded, image))

    rows = (len(thumbs) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile_w, rows * tile_h), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (uploaded, image) in enumerate(thumbs):
        x = (index % columns) * tile_w
        y = (index // columns) * tile_h
        draw.rectangle((x, y, x + tile_w - 1, y + tile_h - 1), outline="#d0d7de", width=1)
        draw.text((x + 6, y + 5), f"Image {index + 1}: {uploaded.filename[:42]}", fill="#111827")
        paste_x = x + (tile_w - image.width) // 2
        paste_y = y + label_h + ((tile_h - label_h) - image.height) // 2
        sheet.paste(image, (paste_x, paste_y))

    output = BytesIO()
    sheet.save(output, format="JPEG", quality=settings.openrouter_jpeg_quality, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")


def image_to_base64_jpeg(bytes_: bytes, *, long_edge: int, quality: int) -> str:
    image = Image.open(BytesIO(bytes_)).convert("RGB")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return base64.b64encode(output.getvalue()).decode("ascii")


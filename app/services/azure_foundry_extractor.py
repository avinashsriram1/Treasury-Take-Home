from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings
from app.models import ExpectedFields, ExtractionResult, StageTiming, UploadedImage
from app.services.llm_extractor import SYSTEM_PROMPT, contact_sheet_base64_jpeg, parse_or_repair_extraction

logger = logging.getLogger(__name__)


class AzureFoundryExtractionError(RuntimeError):
    def __init__(self, message: str, raw_output: str = "") -> None:
        super().__init__(message)
        self.raw_output = raw_output


_client: httpx.AsyncClient | None = None


def foundry_base_url() -> str:
    endpoint = (settings.azure_foundry_endpoint or "").strip().split("?", 1)[0].rstrip("/")
    if not endpoint:
        raise AzureFoundryExtractionError("AZURE_FOUNDRY_ENDPOINT is not configured")
    if endpoint.endswith("/responses"):
        endpoint = endpoint[: -len("/responses")]
    if not endpoint.endswith("/openai/v1"):
        endpoint = endpoint.rstrip("/") + "/openai/v1"
    return endpoint


async def foundry_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = httpx.Timeout(
            settings.azure_foundry_request_timeout_seconds,
            connect=settings.azure_foundry_connect_timeout_seconds,
        )
        _client = httpx.AsyncClient(timeout=timeout)
    return _client


async def extract_with_azure_foundry(images: list[UploadedImage], expected: ExpectedFields) -> ExtractionResult:
    if not settings.azure_foundry_api_key:
        raise AzureFoundryExtractionError("AZURE_FOUNDRY_API_KEY is not configured")
    if not settings.azure_foundry_deployment:
        raise AzureFoundryExtractionError("AZURE_FOUNDRY_DEPLOYMENT is not configured")

    total_started = time.perf_counter()
    extraction = await request_foundry_model(settings.azure_foundry_deployment, images, expected)
    extraction.latency_ms = int((time.perf_counter() - total_started) * 1000)
    extraction.stage_timings.append(StageTiming(stage="total", elapsed_ms=extraction.latency_ms))
    return extraction


async def request_foundry_model(
    deployment: str,
    images: list[UploadedImage],
    expected: ExpectedFields,
) -> ExtractionResult:
    prep_started = time.perf_counter()
    image_payload = contact_sheet_base64_jpeg(images)
    prep_ms = int((time.perf_counter() - prep_started) * 1000)
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        "Extract fields from the image(s) as one alcohol label product. "
        "Use null for unknown values. Return compact JSON only. "
        f"Application context: {expected.model_dump(exclude_none=True)}"
    )
    request = {
        "model": deployment,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{image_payload}",
                    },
                ],
            }
        ],
        "max_output_tokens": settings.azure_foundry_max_output_tokens,
    }
    client = await foundry_client()
    request_started = time.perf_counter()
    response = await client.post(
        f"{foundry_base_url()}/responses",
        headers={
            "Authorization": f"Bearer {settings.azure_foundry_api_key}",
            "api-key": settings.azure_foundry_api_key,
            "Content-Type": "application/json",
        },
        json=request,
    )
    request_ms = int((time.perf_counter() - request_started) * 1000)
    if response.status_code >= 400:
        detail = response.text[:500].replace("\n", " ")
        raise AzureFoundryExtractionError(f"Azure Foundry request failed: {response.status_code} {detail}")

    parse_started = time.perf_counter()
    payload = response.json()
    output_text = response_output_text(payload)
    try:
        extraction = parse_or_repair_extraction(output_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Azure Foundry response parse failed: %s; raw_output=%r",
            exc,
            output_text[:4000],
        )
        raise AzureFoundryExtractionError(
            "Azure Foundry response was not valid extraction JSON",
            raw_output=output_text,
        ) from exc
    parse_ms = int((time.perf_counter() - parse_started) * 1000)
    extraction.model_used = payload.get("model") or deployment
    extraction.provider = "azure_foundry"
    extraction.token_usage = payload.get("usage")
    extraction.stage_timings = [
        StageTiming(stage="image_preparation", elapsed_ms=prep_ms),
        StageTiming(stage="azure_foundry_request", elapsed_ms=request_ms),
        StageTiming(stage="parse_validation", elapsed_ms=parse_ms),
    ]
    return extraction


def response_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    chunks: list[str] = []
    for output in payload.get("output", []):
        if not isinstance(output, dict):
            continue
        for part in output.get("content", []):
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text)
    if chunks:
        return "\n".join(chunks)

    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
    raise AzureFoundryExtractionError("Azure Foundry response did not contain output text")


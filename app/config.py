from __future__ import annotations

import os
from dataclasses import dataclass


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def is_free_openrouter_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized == "openrouter/free" or normalized.endswith(":free")


@dataclass(frozen=True)
class Settings:
    processing_mode: str = os.getenv("PROCESSING_MODE", "llm")
    llm_provider: str = os.getenv("LLM_PROVIDER", "openrouter").strip().lower()
    azure_foundry_endpoint: str | None = os.getenv("AZURE_FOUNDRY_ENDPOINT")
    azure_foundry_api_key: str | None = os.getenv("AZURE_FOUNDRY_API_KEY")
    azure_foundry_deployment: str = os.getenv("AZURE_FOUNDRY_DEPLOYMENT", "gpt-4.1-mini")
    azure_foundry_request_timeout_seconds: int = _int_env("AZURE_FOUNDRY_REQUEST_TIMEOUT_SECONDS", 10)
    azure_foundry_connect_timeout_seconds: int = _int_env("AZURE_FOUNDRY_CONNECT_TIMEOUT_SECONDS", 2)
    azure_foundry_max_output_tokens: int = _int_env("AZURE_FOUNDRY_MAX_OUTPUT_TOKENS", 500)
    openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    openrouter_fallback_models: tuple[str, ...] = _csv_env("OPENROUTER_FALLBACK_MODELS", "openrouter/free")
    openrouter_enable_fallbacks: bool = _bool_env("OPENROUTER_ENABLE_FALLBACKS", False)
    openrouter_require_free: bool = _bool_env("OPENROUTER_REQUIRE_FREE", True)
    openrouter_allow_paid_fallback: bool = _bool_env("OPENROUTER_ALLOW_PAID_FALLBACK", False)
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_request_timeout_seconds: int = _int_env("OPENROUTER_REQUEST_TIMEOUT_SECONDS", 5)
    openrouter_connect_timeout_seconds: int = _int_env("OPENROUTER_CONNECT_TIMEOUT_SECONDS", 2)
    openrouter_max_image_long_edge: int = _int_env("OPENROUTER_MAX_IMAGE_LONG_EDGE", 896)
    openrouter_jpeg_quality: int = min(95, _int_env("OPENROUTER_JPEG_QUALITY", 55))
    openrouter_max_tokens: int = _int_env("OPENROUTER_MAX_TOKENS", 500)
    allow_local_ocr: bool = _bool_env("ALLOW_LOCAL_OCR", True)
    show_raw_extraction: bool = _bool_env("SHOW_RAW_EXTRACTION", False)
    batch_parallelism: int = _int_env("BATCH_PARALLELISM", 2)
    llm_batch_parallelism: int = _int_env("LLM_BATCH_PARALLELISM", 4)
    local_ocr_batch_parallelism: int = _int_env("LOCAL_OCR_BATCH_PARALLELISM", 2)
    llm_request_timeout_seconds: int = _int_env("LLM_REQUEST_TIMEOUT_SECONDS", 5)
    max_images_per_product: int = _int_env("MAX_IMAGES_PER_PRODUCT", 4)
    app_url: str | None = os.getenv("APP_URL")

    @property
    def openrouter_models(self) -> tuple[str, ...]:
        models: list[str] = []
        candidates = (
            (self.openrouter_model, *self.openrouter_fallback_models)
            if self.openrouter_enable_fallbacks
            else (self.openrouter_model,)
        )
        for model in candidates:
            if model and model not in models:
                models.append(model)
        return tuple(models)

    @property
    def free_route_config_valid(self) -> bool:
        if not self.openrouter_require_free and self.openrouter_allow_paid_fallback:
            return True
        return all(is_free_openrouter_model(model) for model in self.openrouter_models)

    @property
    def free_route_config_error(self) -> str | None:
        if self.free_route_config_valid:
            return None
        invalid = [model for model in self.openrouter_models if not is_free_openrouter_model(model)]
        return "Paid OpenRouter models are disabled; invalid configured model(s): " + ", ".join(invalid)


settings = Settings()

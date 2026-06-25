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


@dataclass(frozen=True)
class Settings:
    processing_mode: str = os.getenv("PROCESSING_MODE", "llm")
    openrouter_api_key: str | None = os.getenv("OPENROUTER_API_KEY")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "nex-agi/nex-n2-pro")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    allow_local_ocr: bool = _bool_env("ALLOW_LOCAL_OCR", True)
    show_raw_extraction: bool = _bool_env("SHOW_RAW_EXTRACTION", False)
    batch_parallelism: int = _int_env("BATCH_PARALLELISM", 2)
    llm_request_timeout_seconds: int = _int_env("LLM_REQUEST_TIMEOUT_SECONDS", 30)
    max_images_per_product: int = _int_env("MAX_IMAGES_PER_PRODUCT", 4)
    app_url: str | None = os.getenv("APP_URL")


settings = Settings()

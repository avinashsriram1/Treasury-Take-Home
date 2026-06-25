from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    pass_ = "pass"
    review = "review"
    fail = "fail"


class CheckStatus(str, Enum):
    pass_ = "pass"
    review = "review"
    fail = "fail"
    missing = "missing"


class ProcessingMode(str, Enum):
    llm = "llm"
    local_ocr = "local_ocr"


class ExpectedFields(BaseModel):
    brand_name: str | None = None
    class_type: str | None = None
    alcohol_content: str | None = None
    net_contents: str | None = None
    bottler: str | None = None
    country: str | None = None


class UploadedImage(BaseModel):
    image_id: str
    filename: str
    content_type: str | None = None
    content: bytes = Field(repr=False)

    model_config = {"arbitrary_types_allowed": True}


class ExtractedField(BaseModel):
    value: str | None = None
    confidence: float = 0.0
    evidence: str | None = None


class GovernmentWarningExtraction(BaseModel):
    present: bool = False
    heading_text: str | None = None
    heading_all_caps: bool | None = None
    body_text: str | None = None
    confidence: float = 0.0
    evidence: str | None = None


class ExtractionResult(BaseModel):
    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    government_warning: GovernmentWarningExtraction = Field(
        default_factory=GovernmentWarningExtraction
    )
    raw_text: str = ""
    confidence: float = 0.0
    notes: list[str] = Field(default_factory=list)
    model_used: str | None = None
    provider: str | None = None
    token_usage: dict[str, Any] | None = None
    latency_ms: int = 0


class FieldCheck(BaseModel):
    field: str
    expected: str | None = None
    observed: str | None = None
    status: CheckStatus
    confidence: float
    detail: str
    evidence: str | None = None


class WarningCheck(BaseModel):
    present: bool
    status: CheckStatus
    found_text: str | None = None
    heading_all_caps: bool | None = None
    detail: str
    issues: list[str] = Field(default_factory=list)
    evidence: str | None = None


class VerificationResult(BaseModel):
    product_id: str
    label: str | None = None
    verdict: Verdict
    fields: dict[str, FieldCheck]
    government_warning: WarningCheck
    processing_mode: ProcessingMode
    model_used: str | None = None
    provider: str | None = None
    llm_latency_ms: int | None = None
    token_usage: dict[str, Any] | None = None
    extraction_confidence: float = 0.0
    raw_extraction_available: bool = False
    raw_extraction: str = ""
    image_count: int
    latency_ms: int
    notes: list[str] = Field(default_factory=list)


class BatchJob(BaseModel):
    job_id: str
    status: str
    total: int
    completed: int
    counts: dict[str, int]
    results: list[VerificationResult]
    errors: list[str] = Field(default_factory=list)


class CorrectionRecord(BaseModel):
    product_id: str
    label: str | None = None
    field: str
    expected: str | None = None
    corrected_value: str
    verdict: str | None = None
    verifier_note: str | None = None

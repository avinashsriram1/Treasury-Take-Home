from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from app.models import ExtractedField, ExtractionResult, GovernmentWarningExtraction, UploadedImage


async def extract_with_tesseract(images: list[UploadedImage]) -> ExtractionResult:
    started = time.perf_counter()
    raw_parts: list[str] = []
    notes: list[str] = []
    for image in images:
        try:
            raw_parts.append(run_tesseract(image.content, image.filename))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Local OCR failed for {image.filename}: {exc}")

    raw_text = " ".join(raw_parts)
    extraction = ExtractionResult(
        fields=regex_fields(raw_text),
        government_warning=warning_from_text(raw_text),
        raw_text=raw_text,
        confidence=0.55 if raw_text.strip() else 0.0,
        notes=notes or ["Local OCR test mode used Tesseract only."],
        model_used="tesseract-local",
        provider="local",
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    return extraction


def run_tesseract(bytes_: bytes, filename: str) -> str:
    suffix = Path(filename).suffix or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(bytes_)
        tmp_path = tmp.name
    try:
        cmd = os.getenv("TESSERACT_CMD", "tesseract")
        output = subprocess.run(
            [cmd, tmp_path, "stdout", "--psm", "6"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return output.stdout
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def regex_fields(raw_text: str) -> dict[str, ExtractedField]:
    fields: dict[str, ExtractedField] = {}
    normalized = re.sub(r"\s+", " ", raw_text).strip()
    abv = re.search(r"\d+(?:\.\d+)?\s*%\s*(?:alc\.?/vol\.?|abv)?|\d+(?:\.\d+)?\s*proof", normalized, re.I)
    net = re.search(r"\d+(?:\.\d+)?\s*(?:ml|l|cl|oz)\b", normalized, re.I)
    country = re.search(r"product of ([A-Za-z ]+)|imported from ([A-Za-z ]+)|,\s*([A-Z]{2})\b", normalized, re.I)
    class_type = re.search(
        r"\b(wine|beer|lager|ale|cider|whiskey|whisky|bourbon|vodka|gin|rum|tequila|liqueur)\b",
        normalized,
        re.I,
    )
    brand = likely_brand(raw_text)
    bottler = re.search(r"(?:produced and bottled by|bottled by|imported by)\s+([^\n.]+)", raw_text, re.I)
    if brand:
        fields["brand_name"] = ExtractedField(value=brand, confidence=0.45, evidence=brand)
    if abv:
        fields["alcohol_content"] = ExtractedField(value=abv.group(0), confidence=0.75, evidence=abv.group(0))
    if net:
        fields["net_contents"] = ExtractedField(value=net.group(0), confidence=0.75, evidence=net.group(0))
    if country:
        value = next(group for group in country.groups() if group)
        fields["country"] = ExtractedField(value=value.strip(), confidence=0.65, evidence=country.group(0))
    if class_type:
        fields["class_type"] = ExtractedField(value=class_type.group(0), confidence=0.65, evidence=class_type.group(0))
    if bottler:
        fields["bottler"] = ExtractedField(value=bottler.group(1).strip(), confidence=0.55, evidence=bottler.group(0))
    return fields


def likely_brand(raw_text: str) -> str | None:
    stop_words = {
        "government warning",
        "contains sulfites",
        "imported by",
        "produced by",
        "bottled by",
        "alcohol",
        "alc vol",
    }
    for line in raw_text.splitlines():
        cleaned = re.sub(r"[^A-Za-z0-9 '&.-]+", " ", line).strip()
        if len(cleaned) < 3 or len(cleaned) > 48:
            continue
        if any(word in cleaned.lower() for word in stop_words):
            continue
        if re.search(r"\d", cleaned):
            continue
        return cleaned.title() if cleaned.isupper() else cleaned
    return None


def warning_from_text(raw_text: str) -> GovernmentWarningExtraction:
    if "GOVERNMENT WARNING" in raw_text:
        return GovernmentWarningExtraction(
            present=True,
            heading_text="GOVERNMENT WARNING",
            heading_all_caps=True,
            body_text=None,
            confidence=0.9,
            evidence="GOVERNMENT WARNING",
        )
    if "Government Warning" in raw_text:
        return GovernmentWarningExtraction(
            present=True,
            heading_text="Government Warning",
            heading_all_caps=False,
            confidence=0.8,
            evidence="Government Warning",
        )
    return GovernmentWarningExtraction(present=False)

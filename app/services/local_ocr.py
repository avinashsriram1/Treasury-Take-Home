from __future__ import annotations

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
        output = subprocess.run(
            ["tesseract", tmp_path, "stdout", "--psm", "6"],
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
    abv = re.search(
        r"\d+(?:\.\d+)?\s*%\s*(?:alc\.?/vol\.?|abv)?|\d+(?:\.\d+)?\s*proof", raw_text, re.I
    )
    net = re.search(r"\d+(?:\.\d+)?\s*(?:ml|l|cl|oz)\b", raw_text, re.I)
    country = re.search(
        r"product of ([A-Za-z ]+)|imported from ([A-Za-z ]+)|,\s*([A-Z]{2})\b", raw_text, re.I
    )
    class_type = re.search(
        r"\b(wine|beer|lager|cider|whiskey|whisky|bourbon|vodka|gin|rum|tequila|liqueur)\b",
        raw_text,
        re.I,
    )
    if abv:
        fields["alcohol_content"] = ExtractedField(
            value=abv.group(0), confidence=0.75, evidence=abv.group(0)
        )
    if net:
        fields["net_contents"] = ExtractedField(
            value=net.group(0), confidence=0.75, evidence=net.group(0)
        )
    if country:
        value = next(group for group in country.groups() if group)
        fields["country"] = ExtractedField(value=value, confidence=0.65, evidence=country.group(0))
    if class_type:
        fields["class_type"] = ExtractedField(
            value=class_type.group(0), confidence=0.65, evidence=class_type.group(0)
        )
    return fields


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

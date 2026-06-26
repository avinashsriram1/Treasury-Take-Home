from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.models import (
    CheckStatus,
    ExpectedFields,
    ExtractionResult,
    FieldCheck,
    GovernmentWarningExtraction,
    ProcessingMode,
    Verdict,
    VerificationResult,
    WarningCheck,
)

CLASS_ALIASES = {
    "wine": {"wine", "white wine", "red wine", "rose wine", "ros? wine", "still wine"},
    "beer": {"beer", "lager", "ale", "stout", "porter", "malt beverage"},
    "cider": {"cider", "hard cider"},
    "spirits": {
        "spirits",
        "distilled spirits",
        "whiskey",
        "whisky",
        "bourbon",
        "rye whiskey",
        "vodka",
        "gin",
        "rum",
        "tequila",
        "liqueur",
    },
}

COUNTRY_ALIASES = {
    "united states": {
        "united states",
        "united states of america",
        "usa",
        "u.s.a.",
        "us",
        "u.s.",
        "america",
    },
    "austria": {"austria", "osterreich", "?sterreich"},
    "germany": {"germany", "deutschland"},
    "france": {"france"},
    "italy": {"italy"},
    "spain": {"spain"},
    "mexico": {"mexico"},
    "canada": {"canada"},
}

US_STATE_CODES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "IA",
    "ID",
    "IL",
    "IN",
    "KS",
    "KY",
    "LA",
    "MA",
    "MD",
    "ME",
    "MI",
    "MN",
    "MO",
    "MS",
    "MT",
    "NC",
    "ND",
    "NE",
    "NH",
    "NJ",
    "NM",
    "NV",
    "NY",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VA",
    "VT",
    "WA",
    "WI",
    "WV",
    "WY",
    "DC",
}


def verify_extraction(
    *,
    product_id: str,
    label: str | None,
    expected: ExpectedFields,
    extraction: ExtractionResult,
    processing_mode: ProcessingMode,
    image_count: int,
    latency_ms: int,
    show_raw: bool,
) -> VerificationResult:
    fields = {
        "brand_name": check_text("brand_name", "Brand name", expected.brand_name, extraction),
        "class_type": check_class_type(expected.class_type, extraction),
        "alcohol_content": check_abv(expected.alcohol_content, extraction),
        "net_contents": check_net_contents(expected.net_contents, extraction),
        "bottler": check_text("bottler", "Name and address of bottler/producer", expected.bottler, extraction),
        "country": check_country(expected.country, extraction),
    }

    warning = check_government_warning(extraction.government_warning)
    verdict = aggregate_verdict(fields, warning.status)

    return VerificationResult(
        product_id=product_id,
        label=label,
        verdict=verdict,
        fields=fields,
        government_warning=warning,
        processing_mode=processing_mode,
        model_used=extraction.model_used,
        provider=extraction.provider,
        llm_latency_ms=extraction.latency_ms if processing_mode == ProcessingMode.llm else None,
        token_usage=extraction.token_usage,
        extraction_confidence=extraction.confidence,
        raw_extraction_available=show_raw and bool(extraction.raw_text),
        raw_extraction=extraction.raw_text if show_raw else "",
        image_count=image_count,
        latency_ms=latency_ms,
        notes=extraction.notes,
        stage_timings=extraction.stage_timings,
        fallback_used=extraction.fallback_used,
    )


def not_checked_field(field: str, label: str, observed: str | None, confidence: float = 0.0) -> FieldCheck:
    return FieldCheck(
        field=field,
        expected=None,
        observed=observed,
        status=CheckStatus.not_checked,
        confidence=confidence,
        detail=f"{label} was not supplied in the application data, so this field was not compared.",
    )


def check_text(field: str, label: str, expected: str | None, extraction: ExtractionResult) -> FieldCheck:
    expected = clean(expected)
    extracted = extraction.fields.get(field)
    observed = clean(extracted.value if extracted else None)
    confidence = extracted.confidence if extracted else 0.0
    if not expected:
        return not_checked_field(field, label, observed, confidence)
    if not observed:
        return missing_field(field, label, expected)

    score = similarity(expected, observed)
    if normalize(expected) in normalize(observed) or normalize(observed) in normalize(expected):
        score = max(score, 0.95)

    if score >= 0.86:
        return FieldCheck(
            field=field,
            expected=expected,
            observed=expected if score < 0.98 else observed,
            status=CheckStatus.pass_,
            confidence=max(confidence, score),
            detail=f"{label} matches the extracted label evidence.",
            evidence=extracted.evidence,
        )
    if score >= 0.62:
        return FieldCheck(
            field=field,
            expected=expected,
            observed="Possible partial match",
            status=CheckStatus.review,
            confidence=max(confidence, score),
            detail=f"{label} may match, but an agent should confirm it.",
            evidence=extracted.evidence,
        )
    return FieldCheck(
        field=field,
        expected=expected,
        observed=observed,
        status=CheckStatus.fail,
        confidence=max(confidence, score),
        detail=f"{label} conflicts with the application data.",
        evidence=extracted.evidence,
    )


def check_class_type(expected: str | None, extraction: ExtractionResult) -> FieldCheck:
    expected = clean(expected)
    extracted = extraction.fields.get("class_type")
    observed = clean(extracted.value if extracted else None)
    if not expected:
        return not_checked_field("class_type", "Class/type", observed, extracted.confidence if extracted else 0.0)
    if not observed:
        return missing_field("class_type", "Class/type", expected)

    expected_family = class_family(expected)
    observed_family = class_family(observed)
    if expected_family and observed_family and expected_family == observed_family:
        return FieldCheck(
            field="class_type",
            expected=expected,
            observed=canonical_class(observed_family, observed),
            status=CheckStatus.pass_,
            confidence=max(extracted.confidence, 0.9),
            detail="Class/type is compatible with the expected alcohol family.",
            evidence=extracted.evidence,
        )
    if similarity(expected, observed) >= 0.82:
        return FieldCheck(
            field="class_type",
            expected=expected,
            observed=observed,
            status=CheckStatus.pass_,
            confidence=max(extracted.confidence, 0.86),
            detail="Class/type matches the extracted label evidence.",
            evidence=extracted.evidence,
        )
    return FieldCheck(
        field="class_type",
        expected=expected,
        observed=observed,
        status=CheckStatus.fail,
        confidence=0.1,
        detail="Class/type conflicts with the application data.",
        evidence=extracted.evidence,
    )


def check_abv(expected: str | None, extraction: ExtractionResult) -> FieldCheck:
    expected = clean(expected)
    extracted = extraction.fields.get("alcohol_content")
    observed = clean(extracted.value if extracted else None)
    if not expected:
        return not_checked_field(
            "alcohol_content", "Alcohol content", observed, extracted.confidence if extracted else 0.0
        )
    expected_abv = parse_abv(expected)
    if expected_abv is None:
        return FieldCheck(
            field="alcohol_content",
            expected=expected,
            observed=None,
            status=CheckStatus.review,
            confidence=0.0,
            detail="Expected alcohol content could not be parsed.",
        )
    observed_abv = parse_abv(observed)
    if observed_abv is None:
        return missing_field("alcohol_content", "Alcohol content", expected)
    delta = abs(expected_abv - observed_abv)
    status = CheckStatus.pass_ if delta <= 0.15 else CheckStatus.review if delta <= 0.5 else CheckStatus.fail
    detail = (
        "Alcohol content matches."
        if status == CheckStatus.pass_
        else "Alcohol content is close but should be confirmed."
        if status == CheckStatus.review
        else "Alcohol content conflicts with the application data."
    )
    return FieldCheck(
        field="alcohol_content",
        expected=expected,
        observed=format_abv(observed_abv, observed),
        status=status,
        confidence=0.99 if status == CheckStatus.pass_ else 0.5,
        detail=detail,
        evidence=extracted.evidence if extracted else None,
    )


def check_net_contents(expected: str | None, extraction: ExtractionResult) -> FieldCheck:
    expected = clean(expected)
    extracted = extraction.fields.get("net_contents")
    observed = clean(extracted.value if extracted else None)
    if not expected:
        return not_checked_field("net_contents", "Net contents", observed, extracted.confidence if extracted else 0.0)
    expected_ml = parse_net_ml(expected)
    observed_ml = parse_net_ml(observed)
    if expected_ml is None:
        return FieldCheck(
            field="net_contents",
            expected=expected,
            observed=None,
            status=CheckStatus.review,
            confidence=0.0,
            detail="Expected net contents could not be parsed.",
        )
    if observed_ml is None:
        return missing_field("net_contents", "Net contents", expected)
    delta = abs(expected_ml - observed_ml)
    tolerance = max(2.0, expected_ml * 0.01)
    status = CheckStatus.pass_ if delta <= tolerance else CheckStatus.fail
    return FieldCheck(
        field="net_contents",
        expected=expected,
        observed=f"{observed_ml:g} mL",
        status=status,
        confidence=0.99 if status == CheckStatus.pass_ else 0.0,
        detail=(
            "Net contents match after unit normalization."
            if status == CheckStatus.pass_
            else "Net contents conflict with the application data."
        ),
        evidence=extracted.evidence if extracted else None,
    )


def check_country(expected: str | None, extraction: ExtractionResult) -> FieldCheck:
    expected = clean(expected)
    extracted = extraction.fields.get("country")
    observed = clean(extracted.value if extracted else None)
    if not expected:
        return not_checked_field("country", "Country of origin", observed, extracted.confidence if extracted else 0.0)
    if not observed:
        return missing_field("country", "Country", expected)
    expected_norm = normalize_country(expected)
    observed_norm = normalize_country(observed) or infer_us_from_location(observed)
    if expected_norm and observed_norm and expected_norm == observed_norm:
        return FieldCheck(
            field="country",
            expected=expected,
            observed=title_country(observed_norm),
            status=CheckStatus.pass_,
            confidence=max(extracted.confidence, 0.9),
            detail="Country of origin matches normalized country evidence.",
            evidence=extracted.evidence,
        )
    return FieldCheck(
        field="country",
        expected=expected,
        observed=observed,
        status=CheckStatus.fail,
        confidence=0.1,
        detail="Country of origin conflicts with the application data.",
        evidence=extracted.evidence,
    )


def check_government_warning(warning: GovernmentWarningExtraction) -> WarningCheck:
    heading = clean(warning.heading_text)
    if not warning.present or not heading:
        return WarningCheck(
            present=False,
            status=CheckStatus.fail,
            found_text=None,
            heading_all_caps=None,
            detail="Government warning statement is missing.",
            issues=["Government warning statement is mandatory on alcohol labels."],
            evidence=warning.evidence,
        )
    if "GOVERNMENT WARNING" not in heading:
        return WarningCheck(
            present=True,
            status=CheckStatus.fail,
            found_text=heading,
            heading_all_caps=False,
            detail="Government warning heading is present but not in required all caps.",
            issues=["Heading must appear as GOVERNMENT WARNING."],
            evidence=warning.evidence,
        )
    return WarningCheck(
        present=True,
        status=CheckStatus.pass_,
        found_text=heading,
        heading_all_caps=True,
        detail="Government warning heading is present in all capital letters.",
        evidence=warning.evidence,
    )


def aggregate_verdict(fields: dict[str, FieldCheck], warning_status: CheckStatus) -> Verdict:
    if warning_status in {CheckStatus.fail, CheckStatus.missing}:
        return Verdict.fail
    checked_fields = [field for field in fields.values() if field.status != CheckStatus.not_checked]
    missing_observed = [
        field
        for field in checked_fields
        if field.expected and field.observed is None and field.status in {CheckStatus.fail, CheckStatus.missing}
    ]
    if len(missing_observed) >= 2:
        return Verdict.fail
    if any(field.status == CheckStatus.fail and field.observed is not None for field in checked_fields):
        return Verdict.fail
    if len(missing_observed) == 1:
        return Verdict.review
    if warning_status == CheckStatus.review or any(
        field.status in {CheckStatus.review, CheckStatus.missing} for field in checked_fields
    ):
        return Verdict.review
    return Verdict.pass_


def missing_field(field: str, label: str, expected: str) -> FieldCheck:
    return FieldCheck(
        field=field,
        expected=expected,
        observed=None,
        status=CheckStatus.fail,
        confidence=0.0,
        detail=f"{label} was not found with enough confidence.",
    )


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def class_family(value: str) -> str | None:
    norm = normalize(value)
    for family, aliases in CLASS_ALIASES.items():
        if any(alias in norm for alias in aliases):
            return family
    return None


def canonical_class(family: str, observed: str) -> str:
    if family == "spirits":
        return observed.title()
    return family.title()


def parse_abv(value: str | None) -> float | None:
    if not value:
        return None
    text = value.lower()
    proof = re.search(r"(\d+(?:\.\d+)?)\s*proof", text)
    if proof:
        return float(proof.group(1)) / 2.0
    percent = re.search(r"(\d+(?:\.\d+)?)\s*%|\b(\d+(?:\.\d+)?)\s*(?:alc|abv)", text)
    if percent:
        return float(percent.group(1) or percent.group(2))
    return None


def format_abv(abv: float, original: str | None) -> str:
    if original and "proof" in original.lower():
        return f"{abv:.2f}% / {abv * 2:.0f} Proof"
    return f"{abv:.2f}%"


def parse_net_ml(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(ml|milliliter|millilitre|l|liter|litre|cl|oz)", value.lower())
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit in {"ml", "milliliter", "millilitre"}:
        return amount
    if unit in {"l", "liter", "litre"}:
        return amount * 1000
    if unit == "cl":
        return amount * 10
    if unit == "oz":
        return amount * 29.5735
    return None


def normalize_country(value: str | None) -> str | None:
    if not value:
        return None
    norm = normalize(value)
    for canonical, aliases in COUNTRY_ALIASES.items():
        if any(alias.replace(".", "") in norm.replace(".", "") for alias in aliases):
            return canonical
    return infer_us_from_location(value)


def infer_us_from_location(value: str) -> str | None:
    state = re.search(r",\s*([A-Z]{2})(?:\b|$)", value)
    if state and state.group(1) in US_STATE_CODES:
        return "united states"
    return None


def title_country(value: str) -> str:
    return "United States" if value == "united states" else value.title()

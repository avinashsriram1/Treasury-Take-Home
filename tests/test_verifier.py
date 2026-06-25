from app.models import (
    CheckStatus,
    ExpectedFields,
    ExtractedField,
    ExtractionResult,
    ProcessingMode,
    Verdict,
)
from app.services.verifier import parse_abv, parse_net_ml, verify_extraction


def result(expected: ExpectedFields, extraction: ExtractionResult):
    return verify_extraction(
        product_id="p",
        label=None,
        expected=expected,
        extraction=extraction,
        processing_mode=ProcessingMode.llm,
        image_count=1,
        latency_ms=1,
        show_raw=False,
    )


def warning_ok():
    return {
        "present": True,
        "heading_text": "GOVERNMENT WARNING",
        "heading_all_caps": True,
        "confidence": 0.9,
    }


def test_proof_to_abv():
    assert parse_abv("80 Proof") == 40
    assert parse_abv("40% Alc./Vol.") == 40


def test_net_contents_units():
    assert parse_net_ml("1.0L") == 1000
    assert parse_net_ml("75 cl") == 750


def test_one_missing_observed_field_is_review():
    extraction = ExtractionResult(
        fields={
            "class_type": ExtractedField(value="Wine", confidence=0.9),
            "alcohol_content": ExtractedField(value="12%", confidence=0.9),
            "net_contents": ExtractedField(value="750 mL", confidence=0.9),
        },
        government_warning=warning_ok(),
    )
    verified = result(
        ExpectedFields(
            brand_name="Lenz Moser",
            class_type="Wine",
            alcohol_content="12%",
            net_contents="750 mL",
        ),
        extraction,
    )
    assert verified.fields["brand_name"].status == CheckStatus.fail
    assert verified.verdict == Verdict.review


def test_two_missing_observed_fields_fail():
    extraction = ExtractionResult(
        fields={
            "alcohol_content": ExtractedField(value="12%", confidence=0.9),
            "net_contents": ExtractedField(value="750 mL", confidence=0.9),
        },
        government_warning=warning_ok(),
    )
    verified = result(
        ExpectedFields(
            brand_name="Lenz Moser",
            class_type="Wine",
            alcohol_content="12%",
            net_contents="750 mL",
        ),
        extraction,
    )
    assert verified.verdict == Verdict.fail


def test_government_warning_missing_hard_fails():
    extraction = ExtractionResult(
        fields={
            "brand_name": ExtractedField(value="Lenz Moser", confidence=0.9),
            "class_type": ExtractedField(value="Wine", confidence=0.9),
            "alcohol_content": ExtractedField(value="12%", confidence=0.9),
            "net_contents": ExtractedField(value="750 mL", confidence=0.9),
        }
    )
    verified = result(
        ExpectedFields(
            brand_name="Lenz Moser",
            class_type="Wine",
            alcohol_content="12%",
            net_contents="750 mL",
        ),
        extraction,
    )
    assert verified.verdict == Verdict.fail

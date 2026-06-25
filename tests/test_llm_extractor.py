from app.models import ExtractionResult
from app.services.llm_extractor import parse_or_repair_extraction


def test_parse_model_json_response():
    extraction = parse_or_repair_extraction(
        """
        {
          "fields": {
            "brand_name": {"value": "North Orchard", "confidence": 0.95, "evidence": "North Orchard"}
          },
          "government_warning": {
            "present": true,
            "heading_text": "GOVERNMENT WARNING",
            "heading_all_caps": true,
            "confidence": 0.9
          },
          "raw_text": "North Orchard GOVERNMENT WARNING",
          "confidence": 0.9,
          "notes": []
        }
        """
    )
    assert isinstance(extraction, ExtractionResult)
    assert extraction.fields["brand_name"].value == "North Orchard"

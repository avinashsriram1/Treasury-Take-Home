import json
from pathlib import Path

from scripts.scrape_cola_dataset import (
    ColaApplication,
    LabelImageRecord,
    parse_listing,
    parse_printable,
    write_fixture_outputs,
)


def test_parse_listing_deduplicates_ttb_ids():
    html = """
    <tr><td><a href="viewColaDetails.do?ttbid=12345678901234">one</a></td><td>06/01/2026</td></tr>
    <tr><td><a href="viewColaDetails.do?ttbid=12345678901234">one again</a></td><td>06/01/2026</td></tr>
    <tr><td><a href="viewColaDetails.do?ttbid=99999999999999">two</a></td><td>06/02/2026</td></tr>
    """
    assert parse_listing(html) == [
        ("12345678901234", "06/01/2026"),
        ("99999999999999", "06/02/2026"),
    ]


def test_parse_printable_extracts_application_fields():
    page = """
    <div class="label">BRAND NAME</div><div class="data">North Orchard</div>
    <div class="label">NET CONTENTS</div><div class="data">355 mL</div>
    <div class="label">ALCOHOL CONTENT</div><div class="data">6.2%</div>
    <div class="label">CLASS/TYPE DESCRIPTION</div><div class="data">Hard Cider</div>
    <div class="label">NAME AND ADDRESS</div><div class="data">North Orchard Cidery, Burlington, VT</div>
    <input alt="Type of Product: Wine">
    <input alt="Type of Product: Malt Beverage" checked>
    """
    application = parse_printable(page, "12345678901234", "06/01/2026")
    assert application.brand_name == "North Orchard"
    assert application.class_type == "Hard Cider"
    assert application.alcohol_content == "6.2%"
    assert application.net_contents == "355 mL"
    assert application.bottler == "North Orchard Cidery, Burlington, VT"
    assert application.product_type == "Malt Beverage"


def test_write_fixture_outputs_creates_batch_and_single_layout(tmp_path: Path):
    out_dir = tmp_path / "cola-scale"
    downloaded = out_dir / "_downloaded_images"
    downloaded.mkdir(parents=True)
    (downloaded / "12345678901234_01.jpg").write_bytes(b"image-one")
    (downloaded / "99999999999999_01.jpg").write_bytes(b"image-two")

    app_one = ColaApplication(
        ttb_id="12345678901234",
        completed_date="06/01/2026",
        label="North Orchard",
        brand_name="North Orchard",
        class_type_description="Hard Cider",
        alcohol_content="6.2%",
        net_contents="355 mL",
        applicant_name_address="North Orchard Cidery, Burlington, VT",
        source_url="https://example.test/one",
    )
    app_two = ColaApplication(
        ttb_id="99999999999999",
        completed_date="06/02/2026",
        label="Drop of Sunshine",
        brand_name="Drop of Sunshine",
        class_type_description="Wine",
        alcohol_content="12%",
        net_contents="750 mL",
        applicant_name_address="Example Winery, Lodi, CA",
        source_url="https://example.test/two",
    )
    records = [
        LabelImageRecord(app_one, 1, "remote-one", "12345678901234_01.jpg"),
        LabelImageRecord(app_two, 1, "remote-two", "99999999999999_01.jpg"),
    ]

    write_fixture_outputs(records=records, raw_rows=[], out_dir=out_dir, single_count=1, zip_output=True)

    batch_dir = out_dir / "batch-002"
    assert (batch_dir / "images" / "12345678901234_01.jpg").exists()
    assert (batch_dir / "images" / "99999999999999_01.jpg").exists()
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["products"]) == 2
    assert manifest["products"][0]["images"] == ["images/12345678901234_01.jpg"]
    assert (batch_dir / "batch-002.zip").exists()

    single_root = out_dir / "single-verification"
    single_dir = next(path for path in single_root.iterdir() if path.is_dir())
    assert (single_dir / "images" / "12345678901234_01.jpg").exists()
    assert (single_dir / "application.json").exists()
    assert (single_dir / "manifest.json").exists()
